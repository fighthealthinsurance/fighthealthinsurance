"""Outside links that a site check found answering 404 stay replaced.

Each URL below was checked twice, then swapped for the organization's
current page, a parent page, or a Wayback Machine snapshot. This keeps a
copy-paste from an old draft from bringing one back.
"""

import pathlib
import unittest

REPO = pathlib.Path(__file__).resolve().parents[2]

# Text files only: the printed privacy policy PDF is regenerated from the page.
SCANNED = [
    REPO / "fighthealthinsurance" / "templates",
    REPO / "fighthealthinsurance" / "static" / "blog",
    REPO / "fighthealthinsurance" / "static" / "js",
    REPO / "fighthealthinsurance" / "static" / "microsites.json",
    REPO / "fighthealthinsurance" / "static" / "state_help.json",
    REPO / "fighthealthinsurance" / "static" / "denial_watch.json",
    REPO / "fighthealthinsurance" / "financial_assistance_directory.py",
    REPO / "scripts" / "denial_watch",
]

DEAD = [
    "https://www.google.com/safetycenter/tools",
    "/posted-reports/aca-plan-transparency-reports/transparency-coverage-report-aca-health-plans-2025.pdf",
    "https://www.kff.org/health-reform/issue-brief/what-happens-if-the-enhanced-aca-marketplace-subsidies-expire/",
    "https://hrexecutive.com/return-of-short-term-health-insurance/",
    "https://www.kff.org/private-insurance/issue-brief/aca-marketplace-premiums-in-2026/",
    "https://www.ajmc.com/view/aetna-to-exit-aca-marketplace-in-2026",
    "https://www.ftc.gov/news-events/news/press-releases/2024/07/ftc-finds-top-pharmacy-benefit-managers-inflated-drug-costs-over-1000",
    "https://www.healthvermont.gov/immunizations-infectious-disease/immunization",
    "https://www.flhealthsource.gov/immunizations/",
    "https://www.dshs.texas.gov/immunize/",
    "https://health.hawaii.gov/docd/disease_investigation/vaccine-preventable-disease/",
    "https://cdphe.colorado.gov/prevention-and-wellness/immunizations",
    "https://www.nmhealth.org/about/erd/ideb/ip/",
    "https://msdh.ms.gov/msdhsite/_static/14,0,420.html",
    "https://dhhr.wv.gov/oeps/disease/ob/pages/default.aspx",
    "https://healthandwelfare.idaho.gov/health/immunization",
    "https://www.ahip.org/news/press-releases/ahip-announces-vaccine-coverage-commitment-2025-2026",
    "https://kffhealthnews.org/news/article/health-insurance-customers-appeals-claims-win/",
    "https://www.medicare.gov/pharmaceutical-assistance-program/state-programs.aspx",
    "https://www.gamunex-c.com/patient-resources",
    "https://www.wecu.org/",
    "https://www.hemophilia.org/educational-programs/financial-assistance-and-insurance",
    "https://www.merck.com/company-overview/responsibility/access-to-health/patient-assistance-programs/",
    "https://aafa.org/allergies/allergy-treatments/immunotherapy/",
    "https://www.spine.org/Portals/0/assets/downloads/ResearchClinicalCare/Guidelines/LumbarFusion.pdf",
    "https://www.breztri.com/savings-and-support.html",
    "https://chfs.ky.gov/agencies/dail/Pages/ship.aspx",
]


def _text_files():
    for path in SCANNED:
        files = [path] if path.is_file() else sorted(p for p in path.rglob("*") if p.is_file())
        for f in files:
            if f.suffix in {".html", ".md", ".js", ".json", ".py", ".csv", ".txt"}:
                yield f


class OutsideLinksNotKnownDeadTest(unittest.TestCase):
    def test_the_scan_reaches_the_files_that_held_them(self):
        names = {f.name for f in _text_files()}
        for name in ("microsites.json", "state_help.json", "privacy_policy.html", "state_reports.csv"):
            self.assertIn(name, names)

    def test_no_known_dead_link_is_back(self):
        found = []
        for f in _text_files():
            text = f.read_text(encoding="utf-8", errors="ignore")
            found += [f"{f.relative_to(REPO)}: {url}" for url in DEAD if url in text]
        self.assertEqual(found, [], "dead outside links are back:\n" + "\n".join(found))
