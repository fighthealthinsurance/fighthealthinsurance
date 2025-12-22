# Insurance Company & Plan Extraction - Feature Summary

## Problem Statement

Previously, the system stored insurance company names as simple text fields. This made it difficult to:
- Differentiate between state-specific plans (e.g., Anthem Medicaid California vs Anthem Medicaid New York)
- Analyze denial patterns by specific insurance plans
- Track which companies and plans have higher denial rates
- Generate company-specific appeal strategies

## Solution

Added structured models for insurance companies and their specific plans, with automatic extraction and matching.

## What Was Added

### New Database Models

1. **InsuranceCompany** - Represents insurance carriers
   - Stores company name, alternative names, website
   - Includes regex patterns for automatic matching
   - Examples: Anthem, UnitedHealthcare, Aetna, Cigna, etc.

2. **InsurancePlan** - Represents specific plans under each company
   - Links to InsuranceCompany via foreign key
   - Includes plan name, state, plan type, and plan source
   - Regex patterns for matching state-specific plans
   - Examples: "Anthem Medicaid (CA)", "Anthem Medicaid (NY)"

### Updated Denial Model

- Added `insurance_company_obj` - Optional FK to InsuranceCompany
- Added `insurance_plan_obj` - Optional FK to InsurancePlan
- Kept `insurance_company` text field for backward compatibility

### Enhanced Extraction Logic

1. **`extract_set_insurance_company()`** - Now also matches to structured models
   - ML extracts company name from denial text
   - Matches against InsuranceCompany records (exact and fuzzy matching)
   - Checks alternative names
   - Sets both text field and structured FK

2. **`match_insurance_plan_from_regex()`** - New method for plan matching
   - Iterates through InsurancePlan records
   - Uses regex patterns to identify state-specific plans
   - Sets both company and plan FKs when matched

### User Interface Improvements

- Forms now include dropdowns for selecting insurance companies and plans
- Text field retained as fallback for companies not in the database
- Admin interface for managing companies and plans
- Autocomplete and filtering in admin

### Initial Data

Fixture file with:
- 8 major insurance companies
- 20 state-specific plans covering:
  - Anthem Medicaid plans (CA, NY, VA, IN, KY, OH, WI, GA)
  - UnitedHealthcare Medicaid plans (CA, TX, FL, NY)
  - Medicare Advantage plans
  - Molina Healthcare state plans
  - Centene/Ambetter marketplace plans

## Files Changed

- `fighthealthinsurance/models.py` - Added InsuranceCompany and InsurancePlan models
- `fighthealthinsurance/admin.py` - Admin interfaces
- `fighthealthinsurance/forms/__init__.py` - Form fields for selection
- `fighthealthinsurance/common_view_logic.py` - Enhanced extraction
- `fighthealthinsurance/migrations/0144_add_insurance_company_and_plan_models.py` - Migration
- `fighthealthinsurance/fixtures/insurance_companies.yaml` - Initial data
- `tests/sync/test_insurance_company_plan.py` - Tests

## Usage

### Loading Initial Data

```bash
python manage.py migrate
python manage.py loaddata insurance_companies
```

### In Django Admin

1. Navigate to Insurance Companies or Insurance Plans
2. Add new companies/plans with regex patterns
3. View denials filtered by company or plan

### In Code

```python
# Query denials by company
anthem_denials = Denial.objects.filter(
    insurance_company_obj__name="Anthem Blue Cross Blue Shield"
)

# Query state-specific plans
ca_medicaid_denials = Denial.objects.filter(
    insurance_plan_obj__state="CA",
    insurance_plan_obj__plan_source__name="Medicaid"
)

# Get all plans for a company
anthem = InsuranceCompany.objects.get(name__icontains="Anthem")
anthem_plans = anthem.plans.all()
```

## Testing

Run the test suite:
```bash
python manage.py test tests.sync.test_insurance_company_plan
```

Tests cover:
- Model creation and relationships
- Unique constraints
- Cascade deletes and SET_NULL behavior
- Denial linking to companies and plans

## Documentation

- `docs/INSURANCE_COMPANY_PLAN_EXTRACTION.md` - Feature documentation
- `docs/INSURANCE_MODEL_DIAGRAM.md` - Visual diagrams and examples

## Benefits

1. **Better Data Analysis** - Track denials by specific insurance plans and states
2. **State-Specific Plans** - Differentiate between state Medicaid programs
3. **Automatic Matching** - ML + regex patterns identify companies and plans
4. **Backward Compatible** - Text fields preserved, structured data optional
5. **Scalable** - Easy to add new companies and plans via admin
6. **Future-Ready** - Foundation for company-specific appeal strategies

## Next Steps (Future Enhancements)

- Company-specific appeal templates
- Automatic lookup of state insurance regulators by plan
- Plan-specific medical policy references
- Denial rate analytics by company and plan
- Appeal success rate tracking by insurer
