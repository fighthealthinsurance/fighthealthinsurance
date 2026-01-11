#!/usr/bin/env python3
"""
Generate a keywords CSV file for Microsoft Ads import with all microsites.

Microsoft Ads keyword CSV format:
- Campaign, Ad Group, Keyword, Match Type, Bid (optional)
"""

import csv
import json
import sys
from pathlib import Path
from typing import Set


def generate_keyword_variations(
    procedure: str, condition: str = None, title: str = None
) -> Set[str]:
    """
    Generate keyword variations for a microsite.

    Args:
        procedure: The procedure/treatment name
        condition: Optional condition name
        title: Optional title for additional context

    Returns:
        Set of keyword strings
    """
    keywords = set()

    # Normalize inputs
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
    keywords.add(f"sample appeal letter {procedure}")

    # Insurance company specific (generic)
    for company in ["blue cross", "aetna", "cigna", "united healthcare", "humana"]:
        keywords.add(f"{company} denied {procedure}")
        if condition_lower:
            keywords.add(f"{company} {condition_lower} denial")

    return keywords


def generate_microsoft_ads_keywords_csv(
    microsites_json_path: Path, output_csv_path: Path
):
    """
    Generate Microsoft Ads keywords CSV from microsites.json.

    Args:
        microsites_json_path: Path to microsites.json
        output_csv_path: Path to output CSV file
    """
    # Load microsites
    with open(microsites_json_path, "r") as f:
        microsites = json.load(f)

    rows = []
    total_keywords = 0

    for slug, microsite in microsites.items():
        campaign_name = "Fight Health Insurance - Microsites"
        ad_group_name = microsite.get("title", slug)

        procedure = microsite.get("default_procedure", "")
        condition = microsite.get("default_condition")
        title = microsite.get("title")

        # Generate keywords for this microsite
        keywords = generate_keyword_variations(procedure, condition, title)

        # Add each keyword with different match types
        for keyword in sorted(keywords):
            # Exact match [keyword]
            rows.append({
                "Campaign": campaign_name,
                "Ad Group": ad_group_name,
                "Keyword": f"[{keyword}]",
                "Match Type": "Exact",
                "Bid": "",  # Let Microsoft Ads auto-bid or set manually
            })

            # Phrase match "keyword"
            rows.append({
                "Campaign": campaign_name,
                "Ad Group": ad_group_name,
                "Keyword": f'"{keyword}"',
                "Match Type": "Phrase",
                "Bid": "",
            })

            # Broad match keyword (no quotes/brackets)
            rows.append({
                "Campaign": campaign_name,
                "Ad Group": ad_group_name,
                "Keyword": keyword,
                "Match Type": "Broad",
                "Bid": "",
            })

            total_keywords += 3

    # Write CSV
    if rows:
        fieldnames = ["Campaign", "Ad Group", "Keyword", "Match Type", "Bid"]

        with open(output_csv_path, "w", newline="", encoding="utf-8") as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

        print(f"✓ Generated {total_keywords:,} keywords ({total_keywords//3:,} unique) in {output_csv_path}")
        print(f"✓ Total microsites processed: {len(microsites)}")
        print(f"✓ Match types: Exact, Phrase, and Broad for each keyword")
    else:
        print("✗ No keywords generated!")
        sys.exit(1)


if __name__ == "__main__":
    # Paths
    script_dir = Path(__file__).parent
    microsites_json = (
        script_dir / "fighthealthinsurance" / "static" / "microsites.json"
    )
    output_csv = script_dir / "microsoft_ads_keywords.csv"

    if not microsites_json.exists():
        print(f"✗ Error: {microsites_json} not found!")
        sys.exit(1)

    generate_microsoft_ads_keywords_csv(microsites_json, output_csv)
