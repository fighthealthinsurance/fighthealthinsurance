# Insurance Company and Plan Extraction

## Overview

The Fight Health Insurance system now includes structured models for insurance companies and their specific plans. This allows differentiation between state-specific plans like "Anthem Medicaid California" and "Anthem Medicaid New York".

## Models

### InsuranceCompany

Represents an insurance carrier (e.g., Anthem, Blue Cross, Aetna).

**Fields:**
- `name` - Official company name
- `alt_names` - Alternative names/abbreviations (one per line)
- `regex` - Pattern to match this company in denial text
- `negative_regex` - Pattern to exclude false matches
- `website` - Company's official website
- `notes` - Additional information

**Example:**
```python
anthem = InsuranceCompany.objects.create(
    name="Anthem Blue Cross Blue Shield",
    alt_names="Anthem\nBCBS\nBlue Cross Blue Shield",
    regex=r"(anthem|blue\s*cross\s*blue\s*shield|bcbs)",
    website="https://www.anthem.com"
)
```

### InsurancePlan

Represents a specific plan offered by an insurance company.

**Fields:**
- `insurance_company` - ForeignKey to InsuranceCompany
- `plan_name` - Specific plan name (e.g., "Medicaid", "Gold PPO")
- `state` - Two-letter state code if state-specific
- `plan_type` - ForeignKey to PlanType (HMO, PPO, etc.)
- `plan_source` - ForeignKey to PlanSource (Medicaid, Medicare, Employer, etc.)
- `regex` - Pattern to match this specific plan
- `negative_regex` - Pattern to exclude false matches
- `plan_id_prefix` - Common prefix for plan IDs
- `notes` - Additional information

## Automatic Extraction

When a denial is submitted, the system:

1. **Extracts company name** - Uses ML to extract insurance company name from denial text
2. **Matches to structured data** - Attempts to match extracted name against InsuranceCompany records
3. **Matches state-specific plans** - Uses regex patterns to identify specific plans (e.g., Medicaid CA vs NY)
4. **Updates denial** - Sets both text field and structured foreign keys

## Fixtures

Common insurance companies and plans are provided in `fighthealthinsurance/fixtures/insurance_companies.yaml`.

To load fixtures:
```bash
python manage.py loaddata insurance_companies
```

## Use Cases

### State-Specific Medicaid
Anthem operates Medicaid plans in multiple states. The system can now differentiate:
- Anthem Medi-Cal (California Medicaid)
- Anthem Blue Cross Blue Shield Medicaid (New York)
- Anthem Hoosier Healthwise (Indiana Medicaid)

### Plan Analysis
With structured data, you can:
- Count denials by insurance company
- Identify state-specific patterns
- Track which plans have higher denial rates
- Generate company-specific appeal strategies

## Testing

Tests are in `tests/sync/test_insurance_company_plan.py`.

Run tests:
```bash
python manage.py test tests.sync.test_insurance_company_plan
```
