import os
import asyncio
from typing import List, Optional, Literal
from pydantic import BaseModel, HttpUrl, Field, ValidationError
from bs4 import BeautifulSoup
import httpx
from openai import AsyncOpenAI
import instructor
from dotenv import load_dotenv
from urllib.parse import urljoin

load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.dirname(__file__)), '.env'))

# Adjust URL to match your local server port (Lemonade: 13305, Ollama: 11434, etc)
HEALTH_BACKEND_HOST = os.environ.get("HEALTH_BACKEND_HOST", "localhost")
HEALTH_BACKEND_PORT = os.environ.get("HEALTH_BACKEND_PORT", "13305")
LOCAL_SERVER_URL = "http://" + HEALTH_BACKEND_HOST + ":" + HEALTH_BACKEND_PORT + "/v1"
HEALTH_BACKEND_MODEL = os.environ.get("HEALTH_BACKEND_MODEL", "totallylegitco/fighthealthinsurance_model_v0.5")

# Pydantic schema definition
class AppealsResource(BaseModel):
    state_code: str = Field(
        ..., 
        description="2-letter US state code (e.g., 'CA', 'NY')."
    )
    agency_name: str = Field(
        ..., 
        description="Official name of the agency or organization."
    )
    category: Literal[
        "Medicaid Ombudsman", 
        "SHIP", 
        "State Insurance Department", 
        "Consumer Assistance Program",
        "Legal Aid / Protection & Advocacy",
        "Medicaid Fair Hearing",
        "Other"
    ] = Field(
        ..., 
        description="The type of assistance or resource provided."
    )
    phone_number: Optional[str] = Field(
        None, 
        description="Primary contact phone number, formatted as +1XXXXXXXXXX if possible."
    )
    url: Optional[str] = Field( # Using str instead of HttpUrl for looser local LLM formatting tolerances
        None, 
        description="Direct URL to the agency's appeals, contact, or homepage."
    )
    appeal_deadline_days: Optional[int] = Field(
        None, 
        description="The deadline to file an appeal in days, if mentioned (e.g., 30, 60, 90)."
    )
    notes: Optional[str] = Field(
        None, 
        description="Any critical context, such as 'Only handles Managed Care' or specific eligibility rules."
    )

class PageExtraction(BaseModel):
    resources: List[AppealsResource] = Field(
        default_factory=list, 
        description="List of all relevant appeal resources found on the page."
    )


# LLM Setup
def get_local_instructor_client():
    """
    Initializes an Instructor client pointed at a local OpenAI-compatible server.
    Configured with JSON_SCHEMA mode for maximum local model compatibility.
    Can be reconfigured for a cloud instance instead if desired
    """
       
    openai_client = AsyncOpenAI(
        base_url=LOCAL_SERVER_URL,
        # Keys usually aren't used locally, but the string cannot be empty regardless
        api_key="local_api_key" 
    )
    
    # instructor.from_openai wraps the client and adds Pydantic validation & retries
    # Mode.JSON_SCHEMA tells Instructor to inject JSON schemas directly into prompts
    return instructor.from_openai(openai_client, mode=instructor.Mode.JSON_SCHEMA)


# Extract text, retry on fail.

async def extract_resources_local(text_content: str, model_name: str = HEALTH_BACKEND_MODEL) -> List[AppealsResource]:
    client = get_local_instructor_client()
    
    system_prompt = """
    You are an expert data extraction agent.
    Analyze the provided text from a healthcare/government website and extract any state agencies, 
    ombudsman services, legal aid organizations, managed care, independent review boards,
    or advocacy programs that assist citizens with Medicare or Medicaid appeals.  You also
    extract information related to National Drug Codes or Contracted Drug Lists when relevant.
    
    Extract all matching entities into the specified JSON structure.
    """
    
    try:
        response = await client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Website Text:\n\n{text_content}"}
            ],
            response_model=PageExtraction,
            max_retries=3 # If the model outputs invalid JSON, Instructor feeds the error back to the model up to 3 times
        )
        return response.resources
    except ValidationError as e:
        print(f"Extraction failed after retries: {e}")
        return []


# Scraper execution.  My update in the future to swap httpx for Playwright if a signicant amount
# of blocking occurs.  

def fetch_and_clean_text(url: str) -> str:
    headers = {"User-Agent": "Mozilla/5.0 (FHI Resource Gatherer)"}
    with httpx.Client(headers=headers, follow_redirects=True, timeout=15.0) as client:
        response = client.get(url)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, "html.parser")
        
        # Remove unwanted layout blocks
        for element in soup(["script", "style", "nav", "footer", "header", "aside"]):
            element.decompose()
            
        # Format <a> tags as markdown-style links so the LLM sees the href.
        # Otherwise, URL outputs can be hallucinated.
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            # Resolve relative URLs like "/help/contact" against the page URL
            full_url = urljoin(url, href)
            text = a.get_text(strip=True)
            if text and not href.startswith("javascript:"):
                a.replace_with(f" [{text}]({full_url}) ")

        return soup.get_text(separator="\n", strip=True)[:15000]

async def main():
    target_url = "https://www.shiphelp.org/ships/california/"
    
    print(f"Fetching {target_url}...")
    page_text = fetch_and_clean_text(target_url)
    
    print(f"Extracting with local model '{HEALTH_BACKEND_MODEL}' via Instructor...")
    resources = await extract_resources_local(page_text, model_name=HEALTH_BACKEND_MODEL)
    
    print(f"\nSuccessfully extracted {len(resources)} resources:")
    for res in resources:
        print(res.model_dump_json(indent=2))

if __name__ == "__main__":
    asyncio.run(main())
