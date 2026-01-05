#!/usr/bin/env python3
"""
Generate a complete Microsoft Ads bulk upload CSV with campaigns, ad groups, keywords, and ads.

Microsoft Ads Bulk Upload format includes:
- Campaigns
- Ad Groups
- Keywords (within ad groups)
- Responsive Search Ads (within ad groups)
"""

import csv
import json
import sys
from pathlib import Path
from typing import Set, List, Dict, Any


def truncate_text(text: str, max_length: int) -> str:
    """Truncate text to max_length, adding ellipsis if needed."""
    if len(text) <= max_length:
        return text
    return text[: max_length - 1].rstrip() + "…"


def generate_keyword_variations(
    procedure: str, condition: str = None, title: str = None
) -> Set[str]:
    """Generate keyword variations for a microsite."""
    keywords = set()
    procedure = procedure.lower().strip()
    condition_lower = condition.lower().strip() if condition else None

    # Core procedure keywords
    keywords.add(procedure)
    keywords.add(f"{procedure} appeal")
    keywords.add(f"{procedure} denial")
    keywords.add(f"{procedure} insurance denial")
    keywords.add(f"{procedure} denied by insurance")
    keywords.add(f"insurance denied {procedure}")
    keywords.add(f"appeal {procedure} denial")
    keywords.add(f"fight {procedure} denial")
    keywords.add(f"{procedure} coverage denied")
    keywords.add(f"{procedure} not covered")
    keywords.add(f"{procedure} not medically necessary")
    keywords.add(f"{procedure} pre authorization denied")
    keywords.add(f"{procedure} preauth denial")
    keywords.add(f"how to appeal {procedure} denial")
    keywords.add(f"insurance won't cover {procedure}")
    keywords.add(f"insurance refusing {procedure}")

    # Add condition-based keywords if condition is provided
    if condition_lower:
        keywords.add(f"{condition_lower} treatment denial")
        keywords.add(f"{condition_lower} {procedure}")
        keywords.add(f"{condition_lower} {procedure} denied")
        keywords.add(f"{procedure} for {condition_lower}")
        keywords.add(f"{condition_lower} insurance denial")
        keywords.add(f"{condition_lower} appeal")
        keywords.add(f"insurance denied {condition_lower} treatment")
        keywords.add(f"{condition_lower} medication denied")
        keywords.add(f"{condition_lower} coverage denial")

    # Generic appeal keywords with procedure
    keywords.add(f"write appeal letter {procedure}")
    keywords.add(f"appeal letter for {procedure}")
    keywords.add(f"{procedure} appeal letter template")

    # Insurance company specific (limited set)
    for company in ["blue cross", "aetna", "united healthcare"]:
        keywords.add(f"{company} denied {procedure}")

    return keywords


def generate_microsoft_ads_bulk_csv(
    microsites_json_path: Path, output_csv_path: Path, base_domain: str
):
    """
    Generate comprehensive Microsoft Ads bulk upload CSV.

    Includes campaign, ad groups, keywords, and responsive search ads.
    """
    # Load microsites
    with open(microsites_json_path, "r") as f:
        microsites = json.load(f)

    HEADLINE_MAX = 30
    DESCRIPTION_MAX = 90

    rows = []
    campaign_name = "Fight Health Insurance - Microsites"

    # 1. Create Campaign row
    rows.append({
        "Type": "Campaign",
        "Campaign": campaign_name,
        "Ad Group": "",
        "Keyword": "",
        "Match Type": "",
        "Final URL": "",
        "Headline 1": "",
        "Headline 2": "",
        "Headline 3": "",
        "Headline 4": "",
        "Headline 5": "",
        "Headline 6": "",
        "Headline 7": "",
        "Headline 8": "",
        "Headline 9": "",
        "Headline 10": "",
        "Headline 11": "",
        "Headline 12": "",
        "Headline 13": "",
        "Headline 14": "",
        "Headline 15": "",
        "Description 1": "",
        "Description 2": "",
        "Description 3": "",
        "Description 4": "",
        "Path 1": "",
        "Path 2": "",
        "Status": "Active",
    })

    # 2. Process each microsite
    for slug, microsite in microsites.items():
        ad_group_name = microsite.get("title", slug)
        procedure = microsite.get("default_procedure", "")
        condition = microsite.get("default_condition")
        final_url = f"https://{base_domain}/microsite/{slug}/"

        # 2a. Create Ad Group row
        rows.append({
            "Type": "Ad Group",
            "Campaign": campaign_name,
            "Ad Group": ad_group_name,
            "Keyword": "",
            "Match Type": "",
            "Final URL": "",
            "Headline 1": "",
            "Headline 2": "",
            "Headline 3": "",
            "Headline 4": "",
            "Headline 5": "",
            "Headline 6": "",
            "Headline 7": "",
            "Headline 8": "",
            "Headline 9": "",
            "Headline 10": "",
            "Headline 11": "",
            "Headline 12": "",
            "Headline 13": "",
            "Headline 14": "",
            "Headline 15": "",
            "Description 1": "",
            "Description 2": "",
            "Description 3": "",
            "Description 4": "",
            "Path 1": "",
            "Path 2": "",
            "Status": "Active",
        })

        # 2b. Add keywords for this ad group
        keywords = generate_keyword_variations(procedure, condition, ad_group_name)

        for keyword in sorted(keywords):
            # Exact match
            rows.append({
                "Type": "Keyword",
                "Campaign": campaign_name,
                "Ad Group": ad_group_name,
                "Keyword": f"[{keyword}]",
                "Match Type": "Exact",
                "Final URL": "",
                "Headline 1": "",
                "Headline 2": "",
                "Headline 3": "",
                "Headline 4": "",
                "Headline 5": "",
                "Headline 6": "",
                "Headline 7": "",
                "Headline 8": "",
                "Headline 9": "",
                "Headline 10": "",
                "Headline 11": "",
                "Headline 12": "",
                "Headline 13": "",
                "Headline 14": "",
                "Headline 15": "",
                "Description 1": "",
                "Description 2": "",
                "Description 3": "",
                "Description 4": "",
                "Path 1": "",
                "Path 2": "",
                "Status": "Active",
            })

            # Phrase match
            rows.append({
                "Type": "Keyword",
                "Campaign": campaign_name,
                "Ad Group": ad_group_name,
                "Keyword": f'"{keyword}"',
                "Match Type": "Phrase",
                "Final URL": "",
                "Headline 1": "",
                "Headline 2": "",
                "Headline 3": "",
                "Headline 4": "",
                "Headline 5": "",
                "Headline 6": "",
                "Headline 7": "",
                "Headline 8": "",
                "Headline 9": "",
                "Headline 10": "",
                "Headline 11": "",
                "Headline 12": "",
                "Headline 13": "",
                "Headline 14": "",
                "Headline 15": "",
                "Description 1": "",
                "Description 2": "",
                "Description 3": "",
                "Description 4": "",
                "Path 1": "",
                "Path 2": "",
                "Status": "Active",
            })

        # 2c. Create Responsive Search Ad for this ad group
        # Build headlines
        headlines = []
        if microsite.get("title"):
            headlines.append(truncate_text(microsite["title"], HEADLINE_MAX))
        if microsite.get("hero_h1"):
            headlines.append(truncate_text(microsite["hero_h1"], HEADLINE_MAX))
        if microsite.get("cta"):
            headlines.append(truncate_text(microsite["cta"], HEADLINE_MAX))
        if microsite.get("default_procedure"):
            proc = microsite["default_procedure"]
            headlines.append(truncate_text(f"Appeal {proc} Denial", HEADLINE_MAX))

        headlines.append("Fight Insurance Denials")
        headlines.append("Free Appeal Help")
        headlines.append("AI-Powered Appeal Letters")

        if microsite.get("tagline"):
            headlines.append(truncate_text(microsite["tagline"], HEADLINE_MAX))

        headlines.append("Get Your Appeal Started")
        headlines.append("Free Appeal Generator")

        # Pad to at least 3
        while len(headlines) < 3:
            headlines.append("Appeal Your Denial Today")

        # Build descriptions
        descriptions = []
        if microsite.get("hero_subhead"):
            descriptions.append(truncate_text(microsite["hero_subhead"], DESCRIPTION_MAX))
        if microsite.get("how_we_help"):
            descriptions.append(truncate_text(microsite["how_we_help"], DESCRIPTION_MAX))
        if microsite.get("intro"):
            descriptions.append(truncate_text(microsite["intro"], DESCRIPTION_MAX))

        descriptions.append("Free AI-powered tool to help you write persuasive insurance appeal letters.")

        # Pad to at least 2
        while len(descriptions) < 2:
            descriptions.append("Fight health insurance denials with our free appeal letter generator.")

        # Path fields
        path1 = truncate_text(slug.split("-")[0], 15)
        path2 = truncate_text(slug.split("-")[1] if len(slug.split("-")) > 1 else "appeal", 15)

        # Create ad row
        ad_row = {
            "Type": "Responsive Search Ad",
            "Campaign": campaign_name,
            "Ad Group": ad_group_name,
            "Keyword": "",
            "Match Type": "",
            "Final URL": final_url,
            "Path 1": path1,
            "Path 2": path2,
            "Status": "Active",
        }

        # Add up to 15 headlines
        for i in range(15):
            ad_row[f"Headline {i+1}"] = headlines[i] if i < len(headlines) else ""

        # Add up to 4 descriptions
        for i in range(4):
            ad_row[f"Description {i+1}"] = descriptions[i] if i < len(descriptions) else ""

        rows.append(ad_row)

    # Write CSV
    if rows:
        fieldnames = [
            "Type",
            "Campaign",
            "Ad Group",
            "Keyword",
            "Match Type",
            "Final URL",
        ]
        fieldnames += [f"Headline {i+1}" for i in range(15)]
        fieldnames += [f"Description {i+1}" for i in range(4)]
        fieldnames += ["Path 1", "Path 2", "Status"]

        with open(output_csv_path, "w", newline="", encoding="utf-8") as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

        # Count stats
        campaigns = sum(1 for r in rows if r["Type"] == "Campaign")
        ad_groups = sum(1 for r in rows if r["Type"] == "Ad Group")
        keywords = sum(1 for r in rows if r["Type"] == "Keyword")
        ads = sum(1 for r in rows if r["Type"] == "Responsive Search Ad")

        print(f"✓ Generated complete Microsoft Ads bulk upload CSV: {output_csv_path}")
        print(f"  • {campaigns} campaign")
        print(f"  • {ad_groups} ad groups")
        print(f"  • {keywords:,} keywords")
        print(f"  • {ads} responsive search ads")
        print(f"  • Total rows: {len(rows):,}")
    else:
        print("✗ No data generated!")
        sys.exit(1)


if __name__ == "__main__":
    script_dir = Path(__file__).parent
    microsites_json = script_dir / "fighthealthinsurance" / "static" / "microsites.json"
    output_csv = script_dir / "microsoft_ads_complete.csv"
    base_domain = "fighthealthinsurance.com"

    if not microsites_json.exists():
        print(f"✗ Error: {microsites_json} not found!")
        sys.exit(1)

    generate_microsoft_ads_bulk_csv(microsites_json, output_csv, base_domain)
