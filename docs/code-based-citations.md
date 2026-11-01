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
| CPT 99281-99285, an emergency service as the procedure, or a sentence tying emergency care to the person's claim | No Surprises Act emergency services, PHS Act 2799A-1(a), 45 CFR 149.110 (for employer plans also 29 CFR 2590.716-4 and 26 U.S.C. 9816(a)): emergency services without prior authorization, in any network, with cost sharing no higher than in network, judged by the prudent layperson standard and not from diagnosis codes alone | Employer (including self-funded), individual including marketplace, non-federal government, FEHB; not short-term limited-duration insurance, excepted benefits or account-based plans such as HRAs |
| Same | 42 CFR 422.113(b) | Medicare Advantage |
| Same | 42 CFR 438.114 | Medicaid managed care |
| HCPCS A0430, A0431, A0435, A0436, an air ambulance as the procedure, or a sentence tying one to the claim | No Surprises Act air ambulance, PHS Act 2799A-2, 45 CFR 149.130 (also 29 CFR 2590.717-1 and 26 U.S.C. 9817): the same cost sharing as an in-network air ambulance | Same as the emergency row |
| An out-of-network phrase and a participating facility phrase, each in a sentence about the claim | PHS Act 2799A-1(b), 45 CFR 149.120 (also 29 CFR 2590.716-5 and 26 U.S.C. 9816(b)): cost sharing no higher than in network at a participating hospital, hospital outpatient department, critical access hospital or ambulatory surgical center, unless the clinician met the notice-and-consent criteria of 45 CFR 149.420(c)-(i), which never apply to ancillary services, care no participating provider could furnish, or unforeseen urgent needs (149.420(b)) | Same as the emergency row |

**What a letter can use, and what is for the model only.** Each rule has a
summary, written so a letter can quote it without handing the plan an exit
(the No Surprises Act summaries say a lack of out-of-network benefits is not
an exclusion), and a "Cite only if" line the model reads to decide whether the
rule fits: the plan covers emergency department services, air ambulance or the
service itself at all; ground ambulances are out; 149.110(b)(5) keeps only
exclusions or coordination of benefits not inconsistent with emergency
coverage, waiting periods and normal cost sharing; and 149.420 binds the
clinician, so a letter uses it only for what makes consent valid, never as a
rule the plan broke.

**Which plans see which rules.** Each rule is rendered with the coverage it
reaches. The Labor regulation and the tax code section are added for an
employer, TPA or unknown plan. With known public coverage and no private plan source, only the
program's own rule appears (a TPA flag beside Medicaid is not enough); with
public and private sources together, a caveat says the No Surprises Act rules
apply only if the private plan denied it; with no plan information, a caveat
names the plans the act reaches. A known Original Medicare or VA patient gets
none of these.

**Triggers.** Negated mentions ("no ER visit occurred") and general policy
sentences ("emergency services do not require prior authorization", "to find
a participating hospital, visit our website") are ignored, and line breaks
inside a term still match. The pre-2022 emergency rule (45 CFR 147.138(b))
only governs plan years that began before January 1, 2022 and is not cited.

**Verification:** each rule's section was checked against eCFR's current
text, including its applicability date. Recheck when eCFR shows an amendment
to 45 CFR part 149, 29 CFR part 2590, or 42 CFR parts 422 and 438.

**Why the tax code is cited by statute.** Treasury's versions of these rules
(26 CFR 54.9816-4T, 54.9816-5T and 54.9817-1T) were issued in July 2021 as
temporary regulations and never made permanent. Whether the three-year limit
on temporary regulations in 26 U.S.C. 7805(e)(2) has ended them is unsettled,
so the citation is the statute itself: 26 U.S.C. 9816(a) for emergency
services, 9816(b) for non-emergency services at participating facilities, and
9817 for air ambulances.

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
