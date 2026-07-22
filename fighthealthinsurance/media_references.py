"""Single source of truth for press / media coverage of Fight Health Insurance.

``MEDIA_REFERENCES`` powers the public "Media References" page
(``MediaReferencesView``) and ``SOCIAL_MEDIA_REFERENCES`` powers its social
section. Keeping the coverage here as data (rather than hard-coded markup) lets
the page, its tests, and any future consumers share one list instead of
duplicating the same outlets and URLs across templates.

Every entry is a plain dict so it can be rendered directly in a Django template:

    outlet:            Human-readable name of the outlet / creator (e.g. "Forbes").
    kind:              Short category label ("Article", "Podcast", "TV", "TikTok").
    cta:               Verb-first call to action for the link ("Read the article").
    title:             Headline / episode / segment title.
    url:               External URL to the coverage.
    date:              Human-readable date string (kept as a string because some
                       sources only give us a month or year).
    description:       One- or two-sentence summary of the coverage.
    logo:              Optional static path to the outlet's logo image.
    internal_url_name: Optional Django URL name for an on-site page we maintain
                       about this coverage (e.g. the PBS NewsHour page).

Only coverage that specifically references Fight Health Insurance / Fight
Paperwork / Holden Karau's tool belongs here (not general "AI vs. insurance"
stories). Entries are ordered newest-first so the page reads as a
reverse-chronological timeline of coverage.
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
        "outlet": "GOTO — The Brightest Minds in Tech",
        "kind": "Podcast",
        "cta": "Listen to the episode",
        "title": "This AI Fights Health Insurance Denials — Holden Karau & Julian Wood",
        "url": "https://open.spotify.com/episode/0KwGwhijL6IfxxgncORqg8",
        "date": "January 2026",
        "description": "Holden Karau joins GOTO's tech podcast to talk about how Fight Health Insurance uses AI to take on insurance denials.",
    },
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
        "outlet": "The San Francisco Standard",
        "kind": "Article",
        "cta": "Read the article",
        "title": "Health insurance is hellish. Doctors are fighting back with AI",
        "url": "https://sfstandard.com/2025/06/30/fight-paperwork-health-insurance-ai-tool/",
        "date": "June 30, 2025",
        "description": "The San Francisco Standard reports on doctors using Fight Paperwork — the professional version of Fight Health Insurance — to appeal denials more efficiently.",
        "logo": "images/sflogo.png",
    },
    {
        "outlet": "Newsweek",
        "kind": "Article",
        "cta": "Read the article",
        "title": "New Website Helps Americans 'Fight Health Insurance' Over Claims",
        "url": "https://www.newsweek.com/health-insurance-claims-ai-website-2012389",
        "date": "January 9, 2025",
        "description": "Newsweek covers how Fight Health Insurance lets people upload a denial and generate a customized appeal draft.",
        "logo": "images/newsweeklogo.png",
    },
    {
        "outlet": "The Joe Reis Show",
        "kind": "Podcast",
        "cta": "Listen to the episode",
        "title": "Holden Karau — Fight Health Insurance",
        "url": "https://open.spotify.com/episode/6BUN3ACn8fL4rj8fkXV42o",
        "date": "December 16, 2024",
        "description": "Holden Karau talks with Joe Reis about building Fight Health Insurance and using AI to appeal claim denials.",
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
        "outlet": "KJZZ — The Show",
        "kind": "Radio",
        "cta": "Listen to the interview",
        "title": "How one software developer is using AI to help people fight health insurance denials",
        "url": "https://www.kjzz.org/the-show/2024-10-03/how-one-software-developer-is-using-ai-to-help-people-fight-health-insurance-denials",
        "date": "October 3, 2024",
        "description": "Phoenix public radio station KJZZ interviews Holden Karau about building FightHealthInsurance.com to help people write appeals.",
        "logo": "images/kjzzlogo.png",
    },
    {
        "outlet": "Slashdot",
        "kind": "Article",
        "cta": "Read the story",
        "title": "Tech Worker Builds Free AI-Powered Tool For Fighting US Health Insurance Denials",
        "url": "https://science.slashdot.org/story/24/08/31/2131240/tech-worker-builds-free-ai-powered-tool-for-fighting-us-health-insurance-denials",
        "date": "August 31, 2024",
        "description": "Slashdot highlights the free, open-source tool Holden Karau built so patients can scan denials and generate appeal letters.",
        "logo": "images/slashdotlogo.png",
    },
    {
        "outlet": "BGR",
        "kind": "Article",
        "cta": "Read the article",
        "title": "This Free Site Uses AI To Help You Fight Health Insurance Claim Denials",
        "url": "https://www.bgr.com/tech/this-free-site-uses-ai-to-help-you-fight-health-insurance-claim-denials/",
        "date": "August 27, 2024",
        "description": "BGR walks through how the free Fight Health Insurance site uses AI to help people appeal denied claims.",
        "logo": "images/bgrlogo.png",
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
        "outlet": "Quartz",
        "kind": "Article",
        "cta": "Read the article",
        "title": "This free tool uses AI to appeal insurance denials — which may have been denied by AI",
        "url": "https://qz.com/fight-health-insurance-denials-appeals-ai-1851733712",
        "date": "2024",
        "description": "Quartz spotlights Fight Health Insurance as a free tool that uses AI to appeal denials — some of which were themselves issued by AI.",
        "logo": "images/quartzlogo.png",
    },
]


SOCIAL_MEDIA_REFERENCES: list[MediaReference] = [
    {
        "outlet": "Your Rich BFF (Vivian Tu)",
        "kind": "YouTube",
        "cta": "Watch on YouTube",
        "title": "Using AI to fight a health insurance denial",
        "url": "https://www.youtube.com/watch?v=rEudKDcGZ2g",
        "date": "2024",
        "description": "Personal-finance creator Your Rich BFF (Vivian Tu) shows her audience how to use Fight Health Insurance to appeal a denied claim.",
    },
    {
        "outlet": "The San Francisco Standard",
        "kind": "TikTok",
        "cta": "Watch on TikTok",
        "title": "Holden Karau is using AI to fight health insurance denials",
        "url": "https://www.tiktok.com/@sfstandard/video/7411675257297177886",
        "date": "August 2024",
        "description": "The SF Standard's TikTok on how Holden Karau built Fight Health Insurance after facing roughly 40 denials herself.",
        "logo": "images/sflogo.png",
    },
    {
        "outlet": "NewsNation Prime",
        "kind": "YouTube",
        "cta": "Watch on YouTube",
        "title": "Fighting health insurance companies with AI",
        "url": "https://www.youtube.com/watch?v=lI26LrDU2dg",
        "date": "2024",
        "description": "The NewsNation Prime segment on fighting health insurance companies with AI, available on YouTube.",
        "logo": "images/newsnationlogo.png",
    },
]
