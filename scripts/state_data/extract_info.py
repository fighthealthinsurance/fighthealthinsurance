import os
import json
import time
import asyncio
from typing import List, Optional, Literal, Dict, Any
from urllib.parse import urljoin
from pathlib import Path
import httpx
from bs4 import BeautifulSoup, Tag
from pydantic import BaseModel, Field, field_validator
from openai import AsyncOpenAI
import instructor
from ddgs import DDGS
from dotenv import load_dotenv

# Constants for i/o files and state names
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
INDEX_FILE = DATA_DIR / "state_medicaid_medicare_resources.json"
OUTPUT_FILE = DATA_DIR / "extracted_state_resources.json"

load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.dirname(__file__)), '.env'))

# Adjust URL to match your local server port (Lemonade: 13305, Ollama: 11434, etc)
HEALTH_BACKEND_HOST = os.environ.get("HEALTH_BACKEND_HOST", "localhost")
HEALTH_BACKEND_PORT = os.environ.get("HEALTH_BACKEND_PORT", "13305")
LOCAL_SERVER_URL = "http://" + HEALTH_BACKEND_HOST + ":" + HEALTH_BACKEND_PORT + "/v1"
HEALTH_BACKEND_MODEL = os.environ.get("HEALTH_BACKEND_MODEL", "totallylegitco/fighthealthinsurance_model_v0.5")

STATES = [
    {"name": "Alabama", "abbr": "AL"}, {"name": "Alaska", "abbr": "AK"},
    {"name": "Arizona", "abbr": "AZ"}, {"name": "Arkansas", "abbr": "AR"},
    {"name": "California", "abbr": "CA"}, {"name": "Colorado", "abbr": "CO"},
    {"name": "Connecticut", "abbr": "CT"}, {"name": "Delaware", "abbr": "DE"},
    {"name": "District of Columbia", "abbr": "DC"}, {"name": "Florida", "abbr": "FL"},
    {"name": "Georgia", "abbr": "GA"}, {"name": "Hawaii", "abbr": "HI"},
    {"name": "Idaho", "abbr": "ID"}, {"name": "Illinois", "abbr": "IL"},
    {"name": "Indiana", "abbr": "IN"}, {"name": "Iowa", "abbr": "IA"},
    {"name": "Kansas", "abbr": "KS"}, {"name": "Kentucky", "abbr": "KY"},
    {"name": "Louisiana", "abbr": "LA"}, {"name": "Maine", "abbr": "ME"},
    {"name": "Maryland", "abbr": "MD"}, {"name": "Massachusetts", "abbr": "MA"},
    {"name": "Michigan", "abbr": "MI"}, {"name": "Minnesota", "abbr": "MN"},
    {"name": "Mississippi", "abbr": "MS"}, {"name": "Missouri", "abbr": "MO"},
    {"name": "Montana", "abbr": "MT"}, {"name": "Nebraska", "abbr": "NE"},
    {"name": "Nevada", "abbr": "NV"}, {"name": "New Hampshire", "abbr": "NH"},
    {"name": "New Jersey", "abbr": "NJ"}, {"name": "New Mexico", "abbr": "NM"},
    {"name": "New York", "abbr": "NY"}, {"name": "North Carolina", "abbr": "NC"},
    {"name": "North Dakota", "abbr": "ND"}, {"name": "Ohio", "abbr": "OH"},
    {"name": "Oklahoma", "abbr": "OK"}, {"name": "Oregon", "abbr": "OR"},
    {"name": "Pennsylvania", "abbr": "PA"}, {"name": "Rhode Island", "abbr": "RI"},
    {"name": "South Carolina", "abbr": "SC"}, {"name": "South Dakota", "abbr": "SD"},
    {"name": "Tennessee", "abbr": "TN"}, {"name": "Texas", "abbr": "TX"},
    {"name": "Utah", "abbr": "UT"}, {"name": "Vermont", "abbr": "VT"},
    {"name": "Virginia", "abbr": "VA"}, {"name": "Washington", "abbr": "WA"},
    {"name": "West Virginia", "abbr": "WV"}, {"name": "Wisconsin", "abbr": "WI"},
    {"name": "Wyoming", "abbr": "WY"}
]

SEARCH_CATEGORIES = {
    "state_health_ins_assist_programs": "{state_name} SHIP state health insurance assistance program official site",
    "senior_medicare_patrol": "{state_name} Senior Medicare Patrol SMP official site",
    # Commentint out LSC in favor of using the extract__lsc_grantee_urls method
    # "legal_services_corporation": "{state_name} LSC legal services corporation grantees legal aid",
    "protection_and_advocacy": "{state_name} protection and advocacy organization disability rights",
    "fair_hearing_request": "{state_name} Medicaid fair hearing request appeal portal",
    "managed_care_and_independent_review_orgs": "{state_name} department managed care independent medical review",
    "lcd_reports": "site:cms.gov medicare coverage database local coverage final lcds state report {state_abbr}",
    "ndc_cdl_drugs": "{state_name} Medicaid preferred drug list PDL contract drug list"
}


# Pydantic Schema
class AppealsResource(BaseModel):
    state_code: str = Field(..., description="2-letter US state code (e.g., 'CA', 'NY').")
    agency_name: str = Field(..., description="Official name of the agency or organization.")
    category: Literal[
        "Medicaid Ombudsman", 
        "SHIP", 
        "State Insurance Department", 
        "Consumer Assistance Program",
        "Legal Aid / Protection & Advocacy",
        "Medicaid Fair Hearing",
        "Other"
    ] = Field(..., description="The type of assistance or resource provided.")
    phone_number: Optional[str] = Field(
        None, 
        description="Primary contact phone number, formatted as +1XXXXXXXXXX if possible."
    )
    url: Optional[str] = Field(
        None, 
        description="Direct URL starting with http:// or https://. Return null if not directly mentioned in text."
    )
    appeal_deadline_days: Optional[int] = Field(
        None, 
        description="Filing deadline in days, if mentioned (e.g., 30, 60, 90)."
    )
    notes: Optional[str] = Field(
        None, 
        description="Critical context, eligibility restrictions, or filing rules."
    )

    @field_validator("url")
    @classmethod
    def clean_and_validate_url(cls, v: Optional[str]) -> Optional[str]:
        """Strip conversational LLM artifacts like '(Link not provided)'."""
        if not v:
            return None
        v = v.strip()
        if not (v.startswith("http://") or v.startswith("https://")):
            return None
        return v


class PageExtraction(BaseModel):
    resources: list[AppealsResource] = Field(
        default_factory=list[AppealsResource], 
        description="List of all appeal resources extracted from the page."
    )


# Index searches from DuckDuckGo
def search_duckduckgo(query: str, max_retries: int = 3) -> Optional[str]:
    """Queries DuckDuckGo via the new `ddgs` package and returns top result URL."""
    for attempt in range(max_retries):
        try:
            with DDGS() as ddgs:
                # `text()` returns a list of result dicts
                results = list(ddgs.text(query, max_results=1))
                if results and len(results) > 0:
                    return results[0].get("href")
        except Exception as e:
            if "429" in str(e) or "Ratelimit" in str(e):
                wait_time = (attempt + 1) * 3
                print(f"Rate limited on '{query}'. Waiting {wait_time}s...")
                time.sleep(wait_time)
            else:
                print(f"Search error for query '{query}': {e}")
                break
    return None


def generate_state_resource_index() -> List[Dict[str, Any]]:
    """Generates or loads state_medicaid_medicare_resources.json for all 50 states + DC.

    Combines DuckDuckGo searches for remaining categories with scraped LSC
    grantee profile URLs.
    """
    # Check for existing index file
    if os.path.exists(INDEX_FILE):
        print(f"Found existing '{INDEX_FILE}'. Loading index...")
        with open(INDEX_FILE, "r", encoding="utf-8") as f:
            return json.load(f)

    print(
        f"Generating '{INDEX_FILE}' using DuckDuckGo search & LSC scraping..."
    )

    # Scrape LSC grantee profile URLs once upfront
    print("Scraping LSC grantee profile URLs...")
    lsc_grantees_by_state = extract_grantee_urls(resolve_redirects=True)

    all_state_resources: List[Dict[str, Any]] = []

    for idx, state in enumerate(STATES, start=1):
        state_name = state["name"]
        state_abbr = state["abbr"]
        print(
            f"[{idx}/{len(STATES)}] Finding resources for {state_name}"
            f" ({state_abbr})..."
        )

        state_entry: Dict[str, Any] = {
            "state_name": state_name,
            "state_abbr": state_abbr,
        }

        # DuckDuckGo searches for remaining SEARCH_CATEGORIES
        for category_key, query_template in SEARCH_CATEGORIES.items():
            query = query_template.format(
                state_name=state_name, state_abbr=state_abbr
            )
            found_url = search_duckduckgo(query)
            state_entry[category_key] = found_url or ""
            print(f"  • {category_key}: {found_url or 'NOT FOUND'}")

            # Rate-limiting pause to prevent DDG block
            time.sleep(1.0)

        # Attach scraped LSC grantee profile URLs for this state to be loaded later
        state_grantees = lsc_grantees_by_state.get(state_name, [])
        state_entry["lsc_grantee_profiles"] = [
            g["profile_url"] for g in state_grantees
        ]
        print(f"  • lsc_grantee_profiles: {len(state_grantees)} profiles found")

        all_state_resources.append(state_entry)

    # Save initial index
    with open(INDEX_FILE, "w", encoding="utf-8") as f:
        json.dump(all_state_resources, f, indent=4)

    print(f"Successfully wrote resource index to '{INDEX_FILE}'.")
    return all_state_resources


# HTML Scraping & LLM Parsing

def fetch_and_clean_text(url: str) -> str:
    """Fetches static webpage and converts HTML <a> tags to Markdown [Text](URL)."""
    headers = {"User-Agent": "Mozilla/5.0 (FHI Resource Gatherer)"}
    
    with httpx.Client(headers=headers, follow_redirects=True, timeout=15.0) as client:
        response = client.get(url)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, "html.parser")
        
        # Remove noisy elements
        for element in soup(["script", "style", "nav", "footer", "header", "aside"]):
            if isinstance(element, Tag):
                element.decompose()
            
        # Transform <a> tags into Markdown links to preserve URLs for LLM context
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            full_url = urljoin(url, href)
            text = a.get_text(strip=True)
            if text and not href.startswith("javascript:"):
                a.replace_with(f" [{text}]({full_url}) ")

        text = soup.get_text(separator="\n", strip=True)
        return text[:20000]


def get_local_instructor_client():
    """Initializes Instructor connected to a local OpenAI-compatible server."""
    openai_client = AsyncOpenAI(
        base_url=LOCAL_SERVER_URL,
        api_key="local_api_key"  # A value is required here even if no key is needed
    )
    
    return instructor.from_openai(openai_client, mode=instructor.Mode.JSON_SCHEMA)


async def extract_resources_local(text_content: str, model_name: str) -> List[AppealsResource]:
    client = get_local_instructor_client()
    
    system_prompt = """
    You are an expert data extraction agent for an open-source medical appeals project.
    Analyze the provided text from a healthcare/government webpage and extract any organizations, 
    agencies, legal aid bodies, or programs that help citizens with Medicare or Medicaid appeals.
    
    Extract all relevant entities into the requested JSON schema.
    """
    
    try:
        response = await client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Website Text:\n\n{text_content}"}
            ],
            response_model=PageExtraction,
            max_retries=3
        )
        return response.resources
    except Exception as e:
        print(f"Extraction LLM Error: {e}")
        return []


# Pipeline execution
async def process_single_url(
    target_url: str, 
    state_code: str, 
    category_label: str, 
    model_name: str
) -> List[AppealsResource]:
    """Fetches a URL, runs extraction, applies state & URL fallbacks."""
    if not target_url or not target_url.startswith("http"):
        return []

    print(f"Fetching ({category_label}): {target_url}")
    try:
        page_text = fetch_and_clean_text(target_url)
        extracted = await extract_resources_local(page_text, model_name=model_name)
        
        # Post-processing cleanups & fallbacks
        for item in extracted:
            # Fall back state code if model missed it
            if not item.state_code:
                item.state_code = state_code
            # Fall back URL to target_url if model returned None or invalid string
            if not item.url:
                item.url = target_url
                
        return extracted
    except Exception as e:
        print(f"Scrape/Extract failed for {target_url}: {e}")
        return []

def extract_lsc_grantee_urls(
    target_url: str = "https://www.lsc.gov/about-lsc/our-grantees",
    resolve_redirects: bool = True,
) -> dict:
    """Fetches the LSC 'Our Grantees' page, parses state accordion sections,

    and extracts grantee names along with their full redirected profile URLs.

    Returns:
        dict: { state_name: [ {"name": grantee_name, "node_url": initial_url,
        "profile_url": final_url}, ... ] }
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            " (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
    }

    session = requests.Session()
    session.headers.update(headers)

    # Fetch raw HTML from LSC grantees page
    response = session.get(target_url)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")

    results = {}

    # Locate all state accordion items
    accordion_items = soup.find_all(
        "div", class_="paragraph--type--accordion-item"
    )

    for item in accordion_items:
        # Extract state name
        title_elem = item.find("div", class_="field--name-field-plain-title")
        if not title_elem:
            continue
        state_name = title_elem.get_text(strip=True)

        # Locate grantee links inside the state's list items (excluding 'State Totals')
        content_elem = item.find(
            "div", class_="field--name-field-long-description"
        )
        if not content_elem:
            continue

        grantee_links = content_elem.find_all("a")
        state_grantees = []

        for link in grantee_links:
            href = link.get("href")
            link_text = link.get_text(strip=True)

            # Skip 'State Totals' links
            if not href or link_text.lower() == "state totals":
                continue

            full_node_url = urllib.parse.urljoin(target_url, href)
            final_profile_url = full_node_url

            # 3. Follow HTTP redirects to retrieve the expanded URL path
            if resolve_redirects:
                try:
                    # Request headers only or stream GET to resolve redirect path efficiently
                    res = session.get(full_node_url, allow_redirects=True)
                    final_profile_url = res.url
                except requests.RequestException as err:
                    print(
                        f"Error resolving redirect for {full_node_url}: {err}"
                    )

            state_grantees.append(
                {
                    "grantee_name": link_text,
                    "node_url": full_node_url,
                    "profile_url": final_profile_url,
                }
            )

        if state_grantees:
            results[state_name] = state_grantees

    return results


async def run_extraction_pipeline():
    # Ensure state index JSON exists
    state_index = generate_state_resource_index()

    all_extracted_results: List[Dict[str, Any]] = []

    print(f"\nBeginning extraction across {len(state_index)} states using '{HEALTH_BACKEND_MODEL}'...\n")

    for state_entry in state_index:
        state_name = state_entry.get("state_name")
        state_abbr = state_entry.get("state_abbr")

        if not isinstance(state_abbr, str):
            print(f"Skipping {state_name}: state_abbr is missing or not a string.")
            continue

        print(f"\n==================================================")
        print(f" Processing State: {state_name} ({state_abbr})")
        print(f"==================================================")

        state_extracted_resources: List[Dict[str, Any]] = []

        # Iterate over all category keys in the state entry
        for key, value in state_entry.items():
            if key in ["state_name", "state_abbr"] or not value:
                continue

            # Normalize value into a list to seamlessly handle both single string URLs and URL lists
            urls_to_process = value if isinstance(value, list) else [value]

            for target_url in urls_to_process:
                extracted_items = await process_single_url(
                    target_url=target_url, 
                    state_code=state_abbr, 
                    category_label=key, 
                    model_name=HEALTH_BACKEND_MODEL
                )

                for item in extracted_items:
                    item_dict = item.model_dump()
                    state_extracted_resources.append(item_dict)
                    print(f"Found: {item.agency_name} [{item.category}]")

        # Record results for this state
        all_extracted_results.append({
            "state_name": state_name,
            "state_abbr": state_abbr,
            "resource_count": len(state_extracted_resources),
            "resources": state_extracted_resources
        })

        # Save incremental progress after each state completes
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            json.dump(all_extracted_results, f, indent=2)

    print(f"\n Pipeline complete! Extracted resources saved to '{OUTPUT_FILE}'.")


if __name__ == "__main__":
    asyncio.run(run_extraction_pipeline())