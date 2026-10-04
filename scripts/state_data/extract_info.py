import os
import json
import time
import asyncio
import requests
from typing import List, Optional, Literal, Dict, Any, cast, TypedDict
from pathlib import Path
import httpx
import re
import urllib.parse
from urllib.parse import urljoin
import urllib3
from bs4 import BeautifulSoup, Tag
from pydantic import BaseModel, Field, field_validator
from openai import AsyncOpenAI
import instructor
# Ignoring the following type warning simnply because duckduckgo_search doesn't provide type hints
from ddgs import DDGS # type: ignore
from dotenv import load_dotenv

# Suppress insecure request warnings for government sites with self-signed SSL certs
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

search_tool: Any = DDGS()

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
    # Commenting out LSC in favor of using the extract__lsc_grantee_urls method
    # "legal_services_corporation": "{state_name} LSC legal services corporation grantees legal aid",
    "protection_and_advocacy": "{state_name} protection and advocacy organization disability rights",
    "fair_hearing_request": "{state_name} Medicaid fair hearing request appeal portal",
    "managed_care_and_independent_review_orgs": "{state_name} department managed care independent medical review",
    # Commenting out LCD reports; most of the scripting was returning the generic national page
    # and while I have an idea for how to handle this an alternative way, it will need
    # to be developed further and potentially put into a completel different script/module
    # "lcd_reports": "site:cms.gov medicare coverage database local coverage final lcds state report {state_abbr}",
    "ndc_cdl_drugs": "{state_name} Medicaid preferred drug list PDL contract drug list"
}

# Pydantic Schema
class AppealsResource(BaseModel):
    state_code: str = Field(..., description="2-letter US state code.")
    agency_name: str = Field(..., description="Official name of the agency or organization.")
    category: Literal[
        "Medicaid Ombudsman", 
        "SHIP", 
        "Senior Medicare Patrol (SMP)",
        "State Insurance Department", 
        "Consumer Assistance Program",
        "Legal Aid / Protection & Advocacy",
        "Medicaid Fair Hearing",
        "Managed Care / Independent Review",
        "Preferred Drug List / Prescription Appeals",
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


def generate_state_resource_index(
    force_rebuild: bool = False,
) -> List[Dict[str, Any]]:
    """Generates, loads, or backfills state_medicaid_medicare_resources.json for all 50 states + DC.

    Combines DuckDuckGo searches for remaining categories with scraped LSC
    grantee profile URLs.

    Args:
        force_rebuild: If True, ignores existing cache and regenerates all fields from scratch.
    """
    # Quick override toggle: set to True to force a complete rebuild without changing call sites
    # force_rebuild = True

    existing_data: List[Dict[str, Any]] = []

    # Load existing cache if available and rebuild is not forced
    if os.path.exists(INDEX_FILE) and not force_rebuild:
        print(f"Found existing '{INDEX_FILE}'. Checking for missing fields...")
        try:
            with open(INDEX_FILE, "r", encoding="utf-8") as f:
                existing_data = json.load(f)
        except Exception as e:
            print(
                f"Error reading '{INDEX_FILE}': {e}. Rebuilding index from"
                " scratch."
            )

    # Index existing records by state_abbr for quick lookup
    existing_by_abbr = {
        item.get("state_abbr"): item
        for item in existing_data
    }

    lsc_grantees_by_state: Optional[dict[str, list[dict[str, str]]]] = None  # Lazy-loaded on demand
    all_state_resources: List[Dict[str, Any]] = []
    has_updates = False

    for idx, state in enumerate(STATES, start=1):
        state_name = state["name"]
        state_abbr = state["abbr"]

        # Retrieve cached state entry or initialize a fresh record
        state_entry = existing_by_abbr.get(
            state_abbr, {"state_name": state_name, "state_abbr": state_abbr}
        )

        # Backfill standard search categories if value is missing or empty
        for category_key, query_template in SEARCH_CATEGORIES.items():
            current_url = state_entry.get(category_key)

            if not current_url or str(current_url).strip().upper() in ["NOT FOUND"]:
                print(
                    f"[{idx}/{len(STATES)}] Backfilling missing '{category_key}'"
                    f" for {state_name} ({state_abbr})..."
                )
                query = query_template.format(
                    state_name=state_name, state_abbr=state_abbr
                )
                found_url = search_duckduckgo(query)
                state_entry[category_key] = found_url or ""
                print(f"  • {category_key}: {found_url or 'NOT FOUND'}")
                time.sleep(1.0)
                has_updates = True

        # Backfill LSC Grantee profiles if missing or empty
        if not state_entry.get("lsc_grantee_profiles"):
            if lsc_grantees_by_state is None:
                print("Scraping LSC grantee profile URLs...")
                lsc_grantees_by_state = extract_lsc_grantee_urls(
                    resolve_redirects=True
                )

            state_grantees = lsc_grantees_by_state.get(state_name, [])
            state_entry["lsc_grantee_profiles"] = [
                g["profile_url"] for g in state_grantees
            ]
            print(
                f"[{idx}/{len(STATES)}] Added"
                f" {len(state_grantees)} lsc_grantee_profiles for {state_name}"
            )
            has_updates = True

        all_state_resources.append(state_entry)

    # Save file if new entries were backfilled, or if forcing a full rebuild
    if has_updates or force_rebuild or not os.path.exists(INDEX_FILE):
        with open(INDEX_FILE, "w", encoding="utf-8") as f:
            json.dump(all_state_resources, f, indent=4)
        print(f"Successfully saved updated resource index to '{INDEX_FILE}'.")
    else:
        print(f"All categories intact in '{INDEX_FILE}'. No updates required.")

    return all_state_resources


# HTML Scraping & LLM Parsing
DEFAULT_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

#Regex patterns for whitespace cleaning
MULTIPLE_NEWLINES_REGEX: re.Pattern[str] = re.compile(r"\n{3,}")
HORIZONTAL_SPACES_REGEX: re.Pattern[str] = re.compile(r"[ \t]+")

async def fetch_and_clean_text(url: str) -> str:
    """Fetches web content using browser headers and extracts text strictly using
    a high-quality BeautifulSoup pipeline, converting <a> tags to Markdown links.
    """
    if not url or url.upper() in ["NOT FOUND", "NONE", "NULL"]:
        return ""

    try:
        async with httpx.AsyncClient(
            headers=DEFAULT_HEADERS, 
            follow_redirects=True, 
            timeout=15.0,
            verify=False
        ) as client:
            response: httpx.Response = await client.get(url)
            response.raise_for_status()
            html_content: str = response.text

            # Parse HTML with BeautifulSoup
            soup: BeautifulSoup = BeautifulSoup(html_content, "html.parser")

            # Decompose strictly non-content, metadata, and interactive elements
            unwanted_tags: list[str] = [
                "script", "style", "noscript", "svg", "iframe",
                "head", "template", "input", "button", "select", "textarea",
            ]
            tag: Tag
            for tag in soup.find_all(unwanted_tags):
                tag.decompose()

            # Target Main Content Container safely
            # Avoids matching generic header/navigation classes like 'main-nav'
            main_content: Optional[Tag] = cast(
                Optional[Tag],
                soup.find("main") or 
                soup.find("article") or 
                soup.find(id=re.compile(r"^(main-content|main|content|primary)$", re.I)) or
                soup.find(class_=re.compile(r"^(main-content|main-body|site-content|entry-content)$", re.I))
            )

            # Ensure the extracted container has actual body text; fallback to full soup if it's too short
            if main_content is not None and len(main_content.get_text(strip=True)) < 300:
                main_content = None

            # Use main content container if valid, otherwise default to full page soup
            target_soup: Tag | BeautifulSoup = main_content if main_content is not None else soup

            # Convert <a> tags into clean text with URLs / Phone numbers
            a: Tag
            for a in target_soup.find_all("a", href=True):
                raw_href: str | list[str] = a.get("href", "")
                href: str = raw_href[0].strip() if isinstance(raw_href, list) else str(raw_href).strip()
                anchor_text: str = a.get_text(strip=True)

                if href.startswith("tel:"):
                    phone_digits: str = href.replace("tel:", "").strip()
                    if anchor_text and phone_digits not in anchor_text:
                        a.replace_with(f" {anchor_text} ({phone_digits}) ")
                    else:
                        a.replace_with(f" {anchor_text or phone_digits} ")

                elif href.startswith("mailto:"):
                    email: str = href.replace("mailto:", "").strip()
                    a.replace_with(f" {anchor_text} ({email}) ")

                elif not href.startswith("javascript:"):
                    full_url: str = urljoin(url, href)
                    if anchor_text:
                        a.replace_with(f" [{anchor_text}]({full_url}) ")
                    else:
                        a.replace_with(f" {full_url} ")

            # Extract text while preserving structural line breaks
            raw_text: str = target_soup.get_text(separator="\n", strip=True)
            
            cleaned_lines: List[str] = [
                HORIZONTAL_SPACES_REGEX.sub(" ", line).strip() 
                for line in raw_text.splitlines()
            ]
            cleaned_text: str = "\n".join(cleaned_lines)
            cleaned_text = MULTIPLE_NEWLINES_REGEX.sub("\n\n", cleaned_text)

            return cleaned_text.strip()[:25000]

    except httpx.HTTPStatusError as exc:
        print(
            f"  [HTTP Error {exc.response.status_code}] Could not access {url}"
        )
        return ""
    except Exception as exc:
        print(f"  [Fetch Error] Failed to fetch {url}: {exc}")
        return ""


def get_local_instructor_client():
    """Initializes Instructor connected to a local OpenAI-compatible server."""
    openai_client = AsyncOpenAI(
        base_url=LOCAL_SERVER_URL,
        api_key="local_api_key"  # A value is required here even if no key is needed
    )

    # Using MD_JSON mode for improved Gemma compatibility.  Other models may perform better with
    # Mode.JSON_SCHEMA or simply Mode.JSON
    return instructor.from_openai(openai_client, mode=instructor.Mode.MD_JSON)


async def extract_resources_local(
    text_content: str, 
    model_name: str, 
    target_state_name: str,
    search_category: str  # Add this parameter
) -> List[AppealsResource]:
    client = get_local_instructor_client()
    
    system_prompt = f"""
    You are an expert data extraction agent for an open-source medical appeals project.
    Analyze the provided text and extract ONLY primary entities, organizations, or programs that help citizens of {target_state_name} with medical appeals.
    Your goal is to create resource objects that contain contact information, deadlines (optional), and brief notes.
    
    Target Search Context: This webpage was fetched while searching for '{search_category}'.
    
    STRICT EXTRACTION RULES:
    1. RELEVANCE: ONLY extract resources located in or actively serving {target_state_name}. Ignore national headquarters (e.g., Washington DC) or out-of-state agencies.
    2. VALID CONTACT METHODS: Extract entities if they provide direct contact information OR if the webpage itself is an official state resource or policy page (where the page URL serves as the primary contact mechanism).
    3. EXCLUSIONS: Do NOT extract parent funding bodies, grantors, or federal oversight agencies (e.g., ACL, CMS) mentioned solely in funding disclaimers or footers.
    4. CONCISE NOTES: Keep the `notes` field strictly under 2 sentences.
    5. GROUPING: If multiple programs share the exact same contact phone number and address under one parent organization, group them into ONE primary resource. Use the parent organization as the `agency_name`, and list the sub-organizations in the `notes` field.
    """
    
    try:
        response = await client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Website Text:\n\n{text_content}"}
            ],
            response_model=PageExtraction,
            max_tokens=2048,
            temperature=0.1,  # Low temperature prevents structural wandering
            extra_body={
                # Disables reasoning output channels across common local backends (llama.cpp, Ollama, vLLM)
                "reasoning_effort": "none",
                "chat_template_kwargs": {"thinking": False},
            },
            max_retries=2
        )
        return response.resources
    except Exception as e:
        print(f"Extraction LLM Error: {e}")
        return []


# Part of Pipeline execution
async def process_single_url(
    target_url: str, 
    state_code: str,
    state_name: str,
    category_label: str, 
    model_name: str,
    timeout_seconds: float = 300.0  # 5 minutes per URL, important for preventing local execution loops.
) -> List[AppealsResource]:
    """Fetches a URL, runs extraction, applies state & URL fallbacks, and deduplicates.

    Enforces a strict per-URL timeout.
    """
    if not target_url or not target_url.startswith("http"):
        return []

    if target_url.lower().endswith(".pdf"):
        print(f"  [Skipping] PDF parsing not supported in this pipeline: {target_url}")
        return []

    # Inner execution logic wrapped for asyncio.wait_for
    async def _extract_task() -> List[AppealsResource]:
        print(f"Fetching ({category_label}): {target_url}")
        cleaned_text = await fetch_and_clean_text(target_url)
        print(f"Processing '{target_url}' (Extracted {len(cleaned_text)} characters of text)")
        
        if not cleaned_text or len(cleaned_text.strip()) < 50:
            print(f"  [Skipping] Insufficient or empty text retrieved from: {target_url}")
            return []

        max_chars = 5000
        if len(cleaned_text) > max_chars:
            cleaned_text = cleaned_text[:max_chars]

        extracted = await extract_resources_local(
            text_content=cleaned_text, 
            model_name=model_name, 
            target_state_name=state_name,
            search_category=category_label
        )
        
        unique_resources: List[AppealsResource] = []
        seen: set[tuple[str, Optional[str]]] = set()

        for item in extracted:
            if item.url:
                item.url = sanitize_url(item.url)
            # Filter out obvious out-of-state resources
            if item.state_code and item.state_code.upper() != state_code.upper():
                print(f"  [Filtering] Discarded out-of-state resource: {item.agency_name} ({item.state_code})")
                continue
                
            # Fallbacks
            if not item.state_code:
                item.state_code = state_code
            if not item.url:
                item.url = target_url

            # Deduplicate by Agency Name and Phone Number
            dedup_key = (item.agency_name.lower().strip(), item.phone_number)
            if dedup_key not in seen:
                seen.add(dedup_key)
                unique_resources.append(item)
            else:
                print(f"  [Deduplicating] Discarded exact duplicate: {item.agency_name}")
                
        return unique_resources

    # Run the extraction task with a hard 5-minute timeout
    try:
        return await asyncio.wait_for(_extract_task(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        print(f"  [URL Timeout] Exceeded 5-minute ({timeout_seconds}s) limit for: {target_url}. Skipping.")
        return []
    except Exception as e:
        print(f"Scrape/Extract failed for {target_url}: {e}")
        return []

def sanitize_url(raw_url: str) -> str:
    if not raw_url:
        return ""
    # Replace Fraction Slash (\u2044), Division Slash (\u2215), and Backslash with standard ASCII '/'
    clean_url = (
        raw_url.replace("\u2044", "/")
        .replace("\u2215", "/")
        .replace("⁄", "/")
        .replace("\\", "/")
    )
    return clean_url.strip()

class GranteeEntry(TypedDict):
    grantee_name: str
    node_url: str
    profile_url: str

def extract_lsc_grantee_urls(
    target_url: str = "https://www.lsc.gov/about-lsc/our-grantees",
    resolve_redirects: bool = True,
) -> Dict[str, Any]:
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

    results: Dict[str, List[GranteeEntry]] = {}

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
        state_grantees: List[GranteeEntry] = []

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

        if not isinstance(state_name, str):
            print(f"Skipping {state_abbr}: state_name is missing or not a string.")
            continue

        print(f"\n==================================================")
        print(f" Processing State: {state_name} ({state_abbr})")
        print(f"==================================================")

        # List to track unique resource entries for this state
        state_resources: List[Dict[str, Any]] = []
        seen_resource_keys: set[tuple[str, Optional[str]]] = set()

        # Iterate over all category keys in the state entry
        for key, value in state_entry.items():
            if key in ["state_name", "state_abbr"] or not value:
                continue

            # Normalize value into a list to handle both single string URLs and URL lists
            if isinstance(value, list):
                urls_to_process = [str(url) for url in cast(List[Any], value)]
            elif isinstance(value, str):
                urls_to_process = [value]
            else:
                # This handles cases where the value might be a number, bool, or None
                urls_to_process = [str(value)] if value else []

            for target_url in urls_to_process:
                # Skip invalid entries, blank strings, or "NOT FOUND" placeholders
                if target_url.strip().upper() in ["NOT FOUND", "NONE", "NULL", ""] or not target_url.startswith("http"):
                    continue

                extracted_items = await process_single_url(
                    target_url=target_url, 
                    state_code=state_abbr, 
                    state_name=state_name,
                    category_label=key, 
                    model_name=HEALTH_BACKEND_MODEL
                )

                for item in extracted_items:
                    # Deduplicate by agency name + phone/category, NOT strictly by URL
                    agency_key = item.agency_name.lower().strip()
                    dedup_key = (agency_key, item.phone_number or item.category)
                    
                    if dedup_key not in seen_resource_keys:
                        seen_resource_keys.add(dedup_key)
                        state_resources.append(item.model_dump())
                        print(f"  Found: {item.agency_name} [{item.category}]")
                    else:
                        print(f"  [Deduplicating] Discarded duplicate entity: {item.agency_name}")

        # Record results for this state
        all_extracted_results.append({
            "state_name": state_name,
            "state_abbr": state_abbr,
            "resource_count": len(state_resources),
            "resources": state_resources
        })

        # Save incremental progress after each state completes
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            json.dump(all_extracted_results, f, indent=2, ensure_ascii=False)

    print(f"\n Pipeline complete! Extracted resources saved to '{OUTPUT_FILE}'.")


if __name__ == "__main__":
    asyncio.run(run_extraction_pipeline())