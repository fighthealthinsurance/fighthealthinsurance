from pathlib import Path
from typing import Dict, Any, Optional, List
import json, re
import pandas as pd

# Look for data/ next to the repo root
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DEFAULT_FILE = "medicaid_state_resources.csv"

def _explode_scraped_faq(df: pd.DataFrame) -> pd.DataFrame:
    if "scraped_faq" not in df.columns:
        return pd.DataFrame(columns=["faq_question","faq_answer","faq_source_url","faq_fetched"])
    rows: List[dict] = []
    for _, r in df.iterrows():
        blob = r.get("scraped_faq")
        if pd.isna(blob) or not str(blob).strip():
            continue
        for line in str(blob).splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                j = json.loads(line)
                rows.append({
                    "state": r.get("state"),
                    "faq_question": j.get("question"),
                    "faq_answer": j.get("answer"),
                    "faq_source_url": j.get("source_url"),
                    "faq_fetched": j.get("fetched"),
                })
            except json.JSONDecodeError:
                continue
    return pd.DataFrame(rows)

def get_medicaid_info(query: Dict[str, Any]) -> str:
    """
    query example: {"state":"Ohio","topic":"","limit":5}
    Returns a clean, professional format with key contact info.
    """
    state = (query.get("state") or "").strip()
    limit = int(query.get("limit") or 5)

    # Pick the CSV
    file_path = DATA_DIR / DEFAULT_FILE
    if not file_path.exists():
        matches = list(DATA_DIR.glob("*medicaid*.csv"))
        if not matches:
            return f"Could not find Medicaid data file."
        file_path = matches[0]

    # Read CSV
    try:
        df = pd.read_csv(file_path)
    except Exception as e:
        return f"Error reading data: {e}"

    # Filter by state
    if state and "state" in df.columns:
        df = df[df["state"].astype(str).str.lower() == state.lower()]
    
    if df.empty:
        return f"No Medicaid data found for {state}."

    # Focus on the most important contact info
    important_cols = ["agency", "agency_phone", "helpline", "helpline_contact", "agency_website"]
    available_cols = [col for col in important_cols if col in df.columns]
    
    if not available_cols:
        return f"Medicaid data found for {state}, but no contact information available."
    
    # Get the first few rows with contact info
    contact_info = df[available_cols].drop_duplicates().head(limit)
    
    # Format as clean markdown
    result = []
    
    for idx, row in contact_info.iterrows():
        # Agency name
        if "agency" in available_cols and pd.notna(row["agency"]) and str(row["agency"]).strip():
            result.append(f"**{row['agency']}:**")
        
        # Main phone
        if "agency_phone" in available_cols and pd.notna(row["agency_phone"]) and str(row["agency_phone"]).strip():
            phone = str(row["agency_phone"]).strip()
            result.append(f"**Main Phone:** {phone}")
        
        # Website (after main phone)
        if "agency_website" in available_cols and pd.notna(row["agency_website"]) and str(row["agency_website"]).strip():
            website = str(row["agency_website"]).strip()
            result.append(f"**Website:** {website}")
        
        # Add spacing before legal section
        result.append("")  # Empty line
        
        # Legal helpline section
        if "helpline" in available_cols and pd.notna(row["helpline"]) and str(row["helpline"]).strip():
            result.append(f"**Legal Resources:**")
            result.append(f"{row['helpline']}")
        
        # Helpline contact
        if "helpline_contact" in available_cols and pd.notna(row["helpline_contact"]) and str(row["helpline_contact"]).strip():
            phone = str(row["helpline_contact"]).strip()
            result.append(f"**Phone:** {phone}")

    return "\n".join(result)
