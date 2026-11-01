# Citations chosen by billing code and by the kind of care

Letters cite better when the citation is picked from what the denial is about
(a billing code, a kind of care, a plan type) rather than from words that
happen to appear in a document title. This note maps the free, public
government sources that can drive that, what triggers each one, and what is
built so far.

Only codes and public identifiers ever leave the app for these lookups. No
patient data, denial text or health history is sent to any outside service.

## Built

**Emergency care and surprise billing** (`regulatory_citations.py`,
`SURPRISE_BILLING_RULES`). Chosen by the facts of the denial, joined to the
regulatory block the appeal prompt already carries.

| Trigger | Rule | Plans it reaches |
|---|---|---|
| Emergency room, emergency department or emergency services wording, or CPT 99281-99285 | No Surprises Act emergency services, PHS Act 2799A-1(a), 45 CFR 149.110: no prior authorization, any network, in-network cost sharing, prudent layperson standard, not decided from diagnosis codes alone | Employer (including self-funded), marketplace, individual, non-federal government, FEHB |
| Same | 42 CFR 422.113(b) | Medicare Advantage |
| Same | 42 CFR 438.114 | Medicaid managed care |
| Air ambulance wording, or HCPCS A0430, A0431, A0435, A0436 | No Surprises Act air ambulance, PHS Act 2799A-2, 45 CFR 149.130: in-network cost sharing when the plan covers air ambulance; not ground ambulance, and not a coverage mandate | Employer, marketplace, individual, non-federal government, FEHB |
| An out-of-network phrase together with an in-network hospital or facility phrase | PHS Act 2799A-1(b), 45 CFR 149.120 and 149.420: in-network cost sharing unless valid notice and consent, which is not allowed for ancillary services | Same |

Wording that says the care was not an emergency is ignored. The pre-2022
emergency rule (45 CFR 147.138(b)) only governs plan years that began before
January 1, 2022 and is not cited. Original Medicare and VA get none of these.

**Verification:** each rule's section was checked against eCFR's current
text, including its applicability date. Recheck when eCFR shows an amendment
to parts 149, 422 or 438.

## Not built yet

**LCDs and coverage articles by CPT/HCPCS code (CMS Medicare Coverage
Database API).** The API lists the codes for each article and LCD
(`/v1/data/article/hcpc-code`, `/v1/data/lcd/hcpc-code`), but:

- Every code endpoint needs a license token from
  `/v1/metadata/license-agreement`, and requesting it means accepting the
  AMA (CPT), ADA and AHA license terms. That is a licensing decision for the
  organization, not something code should accept silently.
- There is no search by code. Finding the documents for a code means fetching
  the code list of every article (thousands, behind the token) and building
  our own index, refreshed weekly (the API publishes weekly update periods).
- LCDs bind only in their contractor's (MAC's) jurisdiction, so a citation
  needs the patient's state to pick the right one and must say it is regional.

Once the license question is settled, the shape is: a weekly job builds a
code-to-document index into a new table, letters look up the denial's
CPT/HCPCS codes there first, and the NCD title match stays the fallback.

**NCCI edits for coding and bundling denials.** CMS publishes the
procedure-to-procedure (PTP) and medically unlikely edit (MUE) files quarterly,
free. Trigger: a denial citing bundling, unbundling, a modifier, or units.
Verify by the file's quarter and version. Note the PTP files are CPT based, so
the same AMA license question applies to redistributing their content.

**Preventive services under ACA section 2713.** The app already holds USPSTF
data. Trigger: a preventive code or a preventive-care denial. Cite only A and
B grades, with the grade's date, for non-grandfathered private plans.

## Ways a citation can apply

Beyond codes, the full map of citation categories (evidence and criteria, the
plan's own documents, appeal rights, drugs, parity, precedent, state mandates,
prior-authorization reform, network rules) is tracked with the citation work;
this note covers the code-driven and federal-rule pieces.
