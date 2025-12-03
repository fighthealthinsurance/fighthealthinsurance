# UX Improvements Summary

This document summarizes the UX improvements made in the `feature/ux-flow-improvements` branch.

## Overview

These changes focus on improving the user experience for people going through the health insurance denial appeal process - a stressful situation where clear guidance and feedback are essential.

## Changes Made

### 1. Flow Progress Indicator

**File:** `templates/partials/flow_progress.html`

Added an 8-step visual progress stepper that shows users where they are in the appeal process:

1. Upload - Submit denial letter
2. History - Provide health history (optional)
3. Documents - Upload plan documents (optional)
4. Processing - System extracts information
5. Review - Verify extracted information
6. Questions - Answer clarifying questions
7. Generate - Create appeal letter
8. Send - Fax or download appeal

The stepper uses accessible markup with `aria-current` for screen readers and visual indicators for completed/current/upcoming steps.

### 2. Back Button Support

**Files:** Various templates and `views.py`

Added `back_url` context variable to flow views, enabling users to navigate backwards through the multi-step process. This is critical for users who need to correct information or made a mistake.

### 3. Custom Error Pages

**Files:**
- `templates/404.html` - Page not found
- `templates/500.html` - Server error
- `templates/403_csrf.html` - CSRF validation failed

All error pages now:
- Use friendly, non-technical language
- Provide clear next steps
- Include support contact: support42@fighthealthinsurance.com
- Reassure users their data is safe

### 4. Form UX Improvements

**File:** `fighthealthinsurance/forms/__init__.py`

Enhanced `BasePostInferedForm` and `FaxForm` with:
- Clear, descriptive labels
- Helpful placeholder text with examples
- `help_text` explaining what each field is for
- Consistent styling

Example improvements:
```python
plan_id = forms.CharField(
    label="Plan ID / Member ID",
    help_text="Usually found on your insurance card.",
    widget=forms.TextInput(attrs={"placeholder": "e.g., ABC123456789"}),
)
```

### 5. PWYW (Pay What You Want) in Chat Interface

**File:** `fighthealthinsurance/static/js/chat_interface.tsx`

Added a non-intrusive donation banner that:
- Appears after 3 assistant messages (not immediately)
- Can be dismissed and stays dismissed (localStorage)
- Offers preset amounts ($0, $10, $25) plus custom
- Links to Stripe for payment
- Emphasizes the service is free regardless of payment

### 6. Plan Document Fax Number Extraction

**File:** `fighthealthinsurance/common_view_logic.py`

Enhanced `extract_set_fax_number()` to also search uploaded plan documents (PDFs) for fax numbers, not just the denial letter. This helps users who have the appeals fax number in their plan documents but not their denial letter.

### 7. Plan Document Summary Generation

**Files:**
- `fighthealthinsurance/ml/ml_plan_doc_helper.py` (new)
- `fighthealthinsurance/models.py` - Added `plan_documents_summary` field
- `fighthealthinsurance/generate_appeal.py` - Integrated summary into appeal generation

New ML-powered feature that:
1. Generates search terms based on the denial letter and procedure
2. Extracts relevant pages from uploaded plan documents
3. Summarizes those sections for use in appeal generation

This helps because plan documents are often 100+ pages and can't fit in model context windows. By extracting and summarizing only relevant sections, we can include important plan language in appeals.

## Database Migration

Migration `0135_add_plan_documents_summary.py` adds the new `plan_documents_summary` field to the Denial model.

## Testing Recommendations

1. **Flow navigation:** Walk through the entire appeal flow, testing back buttons at each step
2. **Error pages:** Trigger 404, 500, and CSRF errors to verify custom pages display
3. **Form validation:** Test form submissions with various input combinations
4. **PWYW banner:** Verify it appears after 3 messages and dismissal persists
5. **Plan document processing:** Upload PDFs and verify fax extraction and summarization

### 8. Navigation Improvements (Phase 3)

**Files:** `categorize.html`, `outside_help.html`, `appeals.html`, `appeal.html`

Added "Start over with a new denial" links throughout the flow. Because the appeal generation flow is POST-based (each step submits form data), true browser back-button navigation would lose state. Instead, we provide:

- Clear "Start over" links on every page for users who want to begin fresh
- Consistent button styling with arrows (`Continue →`)
- Back buttons on GET-based pages (health history, plan documents)

## Known Limitations / Future Work

- True back-button navigation would require storing flow state in sessions and converting POST-based steps to GET with stored state
- PWYW integration with existing Stripe setup may need verification
- Plan document summarization timeout (60s) may need adjustment based on document sizes

## Related Files Modified

- `fighthealthinsurance/views.py` - Added `current_step` and `back_url` context
- `fighthealthinsurance/forms/__init__.py` - Enhanced form fields
- `fighthealthinsurance/static/js/chat_interface.tsx` - PWYW component
- `fighthealthinsurance/common_view_logic.py` - Fax extraction, plan doc helper integration
- `fighthealthinsurance/generate_appeal.py` - Plan document summary in appeals
- `fighthealthinsurance/ml/ml_plan_doc_helper.py` - New helper class
- Various templates for progress indicator and back buttons
