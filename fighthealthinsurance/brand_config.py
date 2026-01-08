"""
Brand configuration for multi-brand support.

Defines branding for Fight Health Insurance (FHI) and Appeal My Claims (AMC).
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class BrandConfig:
    """Configuration for a single brand."""

    slug: str  # 'fhi' or 'amc'
    name: str  # Display name
    domain: str  # Primary domain
    logo_url: Optional[str]  # Path to logo image (or None for text)
    logo_text: Optional[str]  # Text-based logo if no image
    primary_color: str  # Hex color
    tagline: str
    footer_text: str
    social_links: dict  # Dict of social media links
    show_full_nav: bool  # True for FHI, False for AMC
    canonical_domain: str  # For SEO canonical URLs


# Brand configurations
BRANDS = {
    "fhi": BrandConfig(
        slug="fhi",
        name="Fight Health Insurance",
        domain="fighthealthinsurance.com",
        logo_url="/images/better-logo-optimized.png",
        logo_text=None,
        primary_color="#a5c422",  # Lime green
        tagline="Make your health insurance company cry too",
        footer_text="",
        social_links={
            "instagram": "https://www.instagram.com/fighthealthinsurance/",
            "linkedin": "https://www.linkedin.com/company/fight-health-insurance",
            "youtube": "https://www.youtube.com/@fighthealthinsuranceyt",
            "substack": "https://fighthealthinsurance.substack.com/",
            "reddit": "https://www.reddit.com/r/fightpaperwork/",
            "tiktok": "https://www.tiktok.com/@fighthealthinsurancett",
        },
        show_full_nav=True,
        canonical_domain="https://www.fighthealthinsurance.com",
    ),
    "amc": BrandConfig(
        slug="amc",
        name="Appeal My Claims",
        domain="appealmyclaims.com",
        logo_url=None,
        logo_text="Appeal My Claims",
        primary_color="#2563eb",  # Blue
        tagline="Generate your appeal in minutes",
        footer_text='Powered by <a href="https://www.fighthealthinsurance.com" target="_blank" rel="noopener noreferrer">Fight Health Insurance</a>',
        social_links={},  # No social links for AMC
        show_full_nav=False,
        canonical_domain="https://www.appealmyclaims.com",
    ),
}


def get_brand_from_domain(domain: str) -> str:
    """
    Determine brand from request domain.

    Returns 'fhi', 'amc', or default 'fhi'.

    Args:
        domain: The request domain (e.g., 'appealmyclaims.com')

    Returns:
        Brand slug ('fhi' or 'amc')
    """
    domain_lower = domain.lower()
    if "appealmyclaims.com" in domain_lower:
        return "amc"
    # Default to FHI for all other domains
    return "fhi"


def get_brand_from_path(path: str) -> Optional[str]:
    """
    Determine brand from URL path prefix.

    Returns 'amc' if path starts with /appealmyclaims/, else None.

    Args:
        path: The request path (e.g., '/appealmyclaims/scan')

    Returns:
        Brand slug ('amc') or None if no path-based brand detected
    """
    if path.startswith("/appealmyclaims/"):
        return "amc"
    return None


def get_brand_config(brand_slug: str) -> BrandConfig:
    """
    Get brand configuration by slug.

    Args:
        brand_slug: Brand identifier ('fhi' or 'amc')

    Returns:
        BrandConfig for the specified brand, defaulting to FHI if not found
    """
    return BRANDS.get(brand_slug, BRANDS["fhi"])
