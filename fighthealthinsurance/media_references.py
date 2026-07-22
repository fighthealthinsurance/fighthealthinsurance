"""Single source of truth for press / media coverage of Fight Health Insurance.

The ``MEDIA_REFERENCES`` list powers the public "Media References" page
(``MediaReferencesView``). Keeping the coverage here as data (rather than
hard-coded markup) lets the page, its tests, and any future consumers share one
list instead of duplicating the same outlets and URLs across templates.

Each entry is a plain dict so it can be rendered directly in a Django template:

    outlet:            Human-readable name of the outlet (e.g. "Forbes").
    kind:              Short category label ("Article", "Podcast", "TV").
    cta:               Verb-first call to action for the link ("Read the article").
    title:             Headline / episode / segment title.
    url:               External URL to the coverage.
    date:              Human-readable date string (kept as a string because some
                       sources only give us a month or year).
    description:       One- or two-sentence summary of the coverage.
    logo:              Optional static path to the outlet's logo image.
    internal_url_name: Optional Django URL name for an on-site page we maintain
                       about this coverage (e.g. the PBS NewsHour page).

Entries are ordered newest-first so the page reads as a reverse-chronological
timeline of coverage.
"""

from typing import Optional, TypedDict


class MediaReference(TypedDict, total=False):
    outlet: str
    kind: str
    cta: str
    title: str
    url: str
    date: str
    description: str
    logo: Optional[str]
    internal_url_name: Optional[str]


MEDIA_REFERENCES: list[MediaReference] = [
    {
        "outlet": "PBS NewsHour",
        "kind": "TV",
        "cta": "Watch the segment",
        "title": "How patients are using AI to fight back against denied insurance claims",
        "url": "https://www.pbs.org/newshour/show/how-patients-are-using-ai-to-fight-back-against-denied-insurance-claims",
        "date": "2025",
        "description": "PBS NewsHour looks at how patients are turning to AI tools like Fight Health Insurance to appeal denied claims and level the playing field with insurers.",
        "internal_url_name": "pbs-newshour",
    },
    {
        "outlet": "STAT News",
        "kind": "Article",
        "cta": "Read the article",
        "title": "How artificial intelligence can help patients appeal health insurance denials",
        "url": "https://www.statnews.com/2024/12/12/artificial-intelligence-appealing-health-insurance-denials",
        "date": "December 12, 2024",
        "description": "STAT News examines how AI is being used to help patients push back on health insurance denials.",
        "logo": "images/statlogo.png",
    },
    {
        "outlet": "Forbes",
        "kind": "Article",
        "cta": "Read the article",
        "title": "These Entrepreneurs Are Using AI To Fight Health Insurance Claims Denials",
        "url": "https://www.forbes.com/sites/amyfeldman/2024/12/11/these-entrepreneurs-are-using-ai-to-fight-health-insurance-claims-denials",
        "date": "December 11, 2024",
        "description": "Forbes profiles the founders using artificial intelligence to help patients appeal denied health insurance claims.",
        "logo": "images/forbeslogo.png",
    },
    {
        "outlet": "Business Insider",
        "kind": "Article",
        "cta": "Read the article",
        "title": "This free project uses AI to help you appeal a health insurance claim denial",
        "url": "https://www.businessinsider.com/free-health-insurance-claim-denial-appeal-project-startup-2024-12",
        "date": "December 2024",
        "description": "Business Insider covers the free project helping patients generate appeals for denied health insurance claims.",
        "logo": "images/businessinsiderlogo.png",
    },
    {
        "outlet": "CBS News",
        "kind": "Article",
        "cta": "Read the article",
        "title": "Health insurance denials draw new scrutiny",
        "url": "https://www.cbsnews.com/news/health-insurance-costs-inflation-denials-luigi-mangione-united-healthcare",
        "date": "December 2024",
        "description": "CBS News reports on rising frustration over health insurance denials and the tools patients are using to respond.",
        "logo": "images/cbslogo.png",
    },
    {
        "outlet": "WBUR — Here & Now",
        "kind": "Radio",
        "cta": "Listen to the segment",
        "title": "Using AI to appeal health insurance denials",
        "url": "https://www.wbur.org/hereandnow/2024/11/11/ai-insurance-appeals",
        "date": "November 11, 2024",
        "description": "WBUR's Here & Now discusses how AI can help patients write appeals when their health insurance claims are denied.",
        "logo": "images/wbrlogo.png",
    },
    {
        "outlet": "Fast Company",
        "kind": "Article",
        "cta": "Read the article",
        "title": "If your health insurance denies a claim, this tool uses AI to help you fight back",
        "url": "https://www.fastcompany.com/91209422/if-your-health-insurance-denies-a-claim-this-tool-uses-ai-to-help-you-fight-back",
        "date": "2024",
        "description": "Fast Company highlights how Fight Health Insurance uses AI to help patients contest denied claims.",
        "logo": "images/fastcompanylogo.png",
    },
    {
        "outlet": "NewsNation",
        "kind": "TV",
        "cta": "Watch the segment",
        "title": "Fighting health insurance companies with AI (NewsNation Prime)",
        "url": "https://www.newsnationnow.com/video/fighting-health-insurance-companies-with-ai-newsnation-prime/10025608",
        "date": "2024",
        "description": "NewsNation Prime features how patients can use AI to fight back against health insurance companies.",
        "logo": "images/newsnationlogo.png",
    },
    {
        "outlet": "An Arm and a Leg",
        "kind": "Podcast",
        "cta": "Listen to the episode",
        "title": "Fight Health Insurance — with AI",
        "url": "https://armandalegshow.com/episode/fight-health-insurance-with-ai",
        "date": "2024",
        "description": "The An Arm and a Leg podcast talks with the team behind Fight Health Insurance about using AI to appeal denials.",
        "logo": "images/armleglogo.png",
    },
    {
        "outlet": "The San Francisco Standard",
        "kind": "Article",
        "cta": "Read the article",
        "title": "Holden Karau built a free tool to fight health insurance claim denials",
        "url": "https://sfstandard.com/2024/08/23/holden-karau-fight-health-insurance-appeal-claims-denials",
        "date": "August 23, 2024",
        "description": "The San Francisco Standard profiles founder Holden Karau and the free tool she built to help people appeal insurance denials.",
        "logo": "images/sflogo.png",
    },
]
