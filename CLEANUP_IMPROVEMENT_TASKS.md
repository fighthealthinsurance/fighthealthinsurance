# Fight Health Insurance - Cleanup & Improvement Tasks

This document tracks technical debt, cleanup tasks, and improvement opportunities for the Fight Health Insurance codebase.

**How to use this document:**
- Check off items as you complete them using `[x]`
- Add notes in the "Notes" column as you work
- Move completed sections to the bottom
- Add new items as you discover them

---

## Table of Contents

1. [Quick Wins](#1-quick-wins)
2. [CSS Consolidation](#2-css-consolidation)
3. [Chat Interface Refactoring](#3-chat-interface-refactoring)
4. [Code Deduplication](#4-code-deduplication)
5. [Large File Decomposition](#5-large-file-decomposition)
6. [Security Fixes](#6-security-fixes)
7. [Accessibility Improvements](#7-accessibility-improvements)
8. [Performance Optimizations](#8-performance-optimizations)
9. [Testing Gaps](#9-testing-gaps)
10. [Frontend Improvements](#10-frontend-improvements)
11. [API & Documentation](#11-api--documentation)
12. [UX Improvements](#12-ux-improvements)
13. [GitHub Issues Reference](#13-github-issues-reference)

---

## 1. Quick Wins

**Priority: Do First - Low effort, high impact**

| Done | Task | File(s) | Notes |
|------|------|---------|-------|
| [x] | Remove duplicate `request` context processor | `settings.py` | Removed unused TEMPLATE_CONTEXT_PROCESSORS |
| [~] | Remove 101 console.log statements | `static/js/*.ts` | User decided to keep them for now |
| [x] | Add `.env.example` with all env vars | Project root | Documents all env vars with comments |
| [x] | Fix wildcard imports | `fhi_users/auth/rest_serializers.py` | Now uses explicit imports |
| [x] | Remove hardcoded test Stripe key from settings | `settings.py:383` | Now requires env var |

---

## 2. CSS Consolidation

**Priority: High | Estimated: 4-6 hours**

### Current State
- **239 inline `style=` attributes** across HTML templates
- **18 embedded `<style>` tags** in templates
- 6 CSS files exist but aren't fully utilized

### 2.1 Extract Inline Styles

| Done | Template | Issue | Target CSS |
|------|----------|-------|------------|
| [x] | `base.html` | 2 `<style>` blocks (ribbon, spinner) extracted | `custom.css` |
| [x] | `appeal.html` | Alert boxes now use .alert-box classes | `custom.css` |
| [x] | `scrub.html` | Form styling, box containers now use CSS classes | `custom.css` |
| [x] | `privacy_policy.html` | Uses Bootstrap utility classes (mt-3, mb-3) | N/A |
| [x] | `chat_consent.html` | Uses Bootstrap d-none class | N/A |
| [x] | `microsite.html` | Uses icon-external-sm class, removed duplicate btn-green | `custom.css` |
| [x] | `categorize.html` | Uses alert-box, btn-green, container-narrow classes | `custom.css` |
| [~] | `403_csrf.html` | Self-contained error page (intentionally standalone) | N/A |

### 2.2 Create Reusable CSS Classes

Add to `custom.css`:

```css
/* Alert variants - extract from inline styles */
.alert-success-custom {
  background-color: #e8f5e9;
  border: 1px solid #c8e6c9;
  padding: 1rem;
  border-radius: 4px;
}
.alert-danger-custom {
  background-color: #ffebee;
  border: 1px solid #ffcdd2;
  padding: 1rem;
  border-radius: 4px;
}
.alert-warning-custom {
  background-color: #fff3cd;
  border: 1px solid #ffc107;
  padding: 1rem;
  border-radius: 4px;
}

/* Form inputs - extract from widget attrs */
.form-input-wide { width: 50em; height: 5em; }
.form-input-medium { width: 30em; height: 3em; }

/* Preloader/Loading - move from base.html <style> */
.preloader { /* ... */ }
.ribbon { /* ... */ }
```

### 2.3 Form Widget Cleanup

| Done | Form File | Current | Change To |
|------|-----------|---------|-----------|
| [x] | `forms/__init__.py` | Inline styles removed | Uses form-textarea-wide, form-textarea-medium, form-input-state classes |
| [ ] | `forms/questions.py` | Various inline styles | CSS classes |

---

## 3. Chat Interface Refactoring

**Priority: High | File: `chat_interface.py` (~1500 lines)**

The chat interface module is large and handles multiple responsibilities. Break it into focused modules.

### 3.1 Extract Tool Handlers

| Done | Handler | Current Location | New File | Notes |
|------|---------|------------------|----------|-------|
| [x] | Tool regex patterns | `chat_interface.py` | `chat/tools/patterns.py` | All tool detection patterns extracted |
| [ ] | PubMed tool handler | `chat_interface.py` | `chat/tools/pubmed_tool.py` | |
| [ ] | Medicaid info tool handler | `chat_interface.py` | `chat/tools/medicaid_info_tool.py` | |
| [ ] | Medicaid eligibility handler | `chat_interface.py` | `chat/tools/medicaid_eligibility_tool.py` | |
| [ ] | Appeal tool handler | `chat_interface.py` | `chat/tools/appeal_tool.py` | |
| [ ] | Prior auth tool handler | `chat_interface.py` | `chat/tools/prior_auth_tool.py` | |

### 3.2 Extract Core Logic

| Done | Component | Description | New File |
|------|-----------|-------------|----------|
| [ ] | LLM calling logic | `_build_llm_calls`, `_score_response` | `chat/llm_client.py` |
| [ ] | Retry logic | Retry with backoff patterns | `chat/retry_handler.py` |
| [ ] | Context accumulation | Message history management | `chat/context_manager.py` |
| [x] | Crisis detection | `detect_crisis_keywords`, `detect_false_promises` | `chat/safety_filters.py` |

### 3.3 Extract Microsite PubMed Search

| Done | Task | Notes |
|------|------|-------|
| [ ] | Move microsite-specific PubMed search out of `handle_chat_message` | Currently tightly coupled |
| [ ] | Create `MicrositePubMedHandler` class | Separate concern |

### 3.4 Proposed Structure

```
fighthealthinsurance/chat/
├── __init__.py
├── interface.py          # Main ChatInterface class (slimmed down)
├── context_manager.py    # Message history, context accumulation
├── llm_client.py         # LLM API calls, response scoring
├── retry_handler.py      # Retry logic with backoff
├── safety_filters.py     # Crisis detection, false promise detection
└── tools/
    ├── __init__.py
    ├── base_tool.py      # Abstract base for tools
    ├── pubmed_tool.py
    ├── medicaid_info_tool.py
    ├── medicaid_eligibility_tool.py
    ├── appeal_tool.py
    └── prior_auth_tool.py
```

---

## 4. Code Deduplication

**Priority: High | Estimated: 8-12 hours**

### Current State
- `views.py` - ~1,700 lines
- `rest_views.py` - ~2,000 lines
- `common_view_logic.py` - ~2,100 lines
- **Significant overlap between these files**

### 4.1 Create Shared Mixins

| Done | Mixin | Purpose | Source Files |
|------|-------|---------|--------------|
| [ ] | `DenialProcessorMixin` | Denial creation/processing | `views.py`, `rest_views.py` |
| [ ] | `AppealGeneratorMixin` | Appeal generation logic | `common_view_logic.py` |
| [ ] | `FaxHandlerMixin` | Fax sending/status | `views.py`, `rest_views.py` |
| [ ] | `ValidationMixin` | Form/data validation | Multiple |

### 4.2 Proposed Mixins Structure

```
fighthealthinsurance/mixins/
├── __init__.py
├── denial_mixins.py
├── appeal_mixins.py
├── fax_mixins.py
└── validation_mixins.py
```

### 4.3 Form Consolidation

| Done | Task | Notes |
|------|------|-------|
| [ ] | Extract `BaseDenialFormMixin` | `DenialForm` and `ProDenialForm` share structure |
| [ ] | Review `DenialRefForm` inheritance | Multiple forms extend it |

---

## 5. Large File Decomposition

**Priority: Medium | Ongoing**

### Files to Split

| Done | File | Size | Strategy | New Structure |
|------|------|------|----------|---------------|
| [ ] | `models.py` | 57KB | Split by domain | `models/` package |
| [ ] | `chat_interface.py` | ~1500 lines | See Section 3 | `chat/` package |
| [ ] | `views.py` | ~1700 lines | Group by feature | `views/` package |
| [ ] | `common_view_logic.py` | ~2100 lines | Extract helpers | Multiple modules |
| [ ] | `urls.py` | 75 routes | Split by area | `urls/` package |
| [ ] | `rest_serializers.py` | 33KB | Split by model | `serializers/` package |
| [ ] | `admin.py` | 16KB | Split by model | `admin/` package |

### 5.1 Models Package Structure

```
fighthealthinsurance/models/
├── __init__.py          # Re-exports for backwards compatibility
├── denial.py            # Denial, DenialTypes, DenialQualityTags, DenialQA
├── appeal.py            # Appeal, ProposedAppeal, AppealAttachment
├── insurance.py         # PlanType, Regulator, PlanSource
├── fax.py               # FaxesToSend and related
├── followup.py          # FollowUp, FollowUpSched, FollowUpType
├── chat.py              # OngoingChat, ChatLeads
├── stripe.py            # Stripe-related models
└── ml.py                # GenericQuestionGeneration, GenericContextGeneration
```

---

## 6. Security Fixes

**Priority: CRITICAL - Do before production**

### 6.1 Critical Issues

| Done | Issue | Location | Fix |
|------|-------|----------|-----|
| [x] | Hardcoded SECRET_KEY | `settings.py:81` | Prod class uses env var |
| [x] | Hardcoded Stripe test key | `settings.py:383` | Removed default, requires env var |
| [~] | `SESSION_COOKIE_HTTPONLY = False` | `settings.py:66` | Intentional for cross-site JS access |
| [~] | `CSRF_COOKIE_HTTPONLY = False` | `settings.py:67` | Intentional for cross-site functionality |
| [x] | `ALLOWED_HOSTS = ["*"]` | `settings.py:95` | Prod class restricts to actual domains |
| [x] | `CORS_ALLOW_ALL_ORIGINS = True` | `settings.py:329` | Prod class uses whitelist |
| [x] | `DEBUG = True` in Base config | `settings.py:84` | Prod class sets to False |

### 6.2 High Priority Issues

| Done | Issue | Location | Fix |
|------|-------|----------|-----|
| [x] | No rate limiting on auth endpoints | `rest_auth_views.py` | Added DRF throttling (100/hr anon, 1000/hr user) |
| [ ] | Weak password validation | `auth_utils.py:67-77` | Enforce Django validators |
| [x] | Missing HSTS header | `settings.py` | Added to Prod class (1 year, include subdomains, preload) |
| [ ] | Missing CSP header | `settings.py` | Add Content-Security-Policy |
| [ ] | Open redirect in Stripe cancel_url | `rest_auth_views.py:785-790` | Validate against whitelist |
| [ ] | Audit logging disabled by default | `settings.py:48` | Enable by default |

### 6.3 Medium Priority

| Done | Issue | Location | Fix |
|------|-------|----------|-----|
| [ ] | No file upload validation | `forms/__init__.py` | Add type/size validators |
| [ ] | Audit `mark_safe()` usage | `views.py:26` | Ensure no user input |
| [ ] | Verify encryption algorithm | `models.py` | Confirm AES-256 |

---

## 7. Accessibility Improvements

**Priority: High | WCAG 2.1 AA Compliance**

### 7.1 Critical Issues

| Done | Issue | Location | Fix |
|------|-------|----------|-----|
| [x] | Missing skip link | `base.html` | Added skip link with CSS in custom.css |
| [x] | No `<main>` landmark | `base.html` | Wrapped content in `<main id="main-content" role="main">` |
| [ ] | Inline onclick without keyboard support | `login.html:47` | Use event listeners |
| [ ] | Link used as button | `403_csrf.html:78` | Change to `<button>` element |

### 7.2 Color Contrast

| Done | Issue | Location | Fix |
|------|-------|----------|-----|
| [ ] | `#a5c422` green on white may fail WCAG | `custom.css:8,22` | Test and darken if needed |
| [ ] | Reduced opacity text | `custom.css:310` | Ensure 4.5:1 ratio |

### 7.3 Form Accessibility

| Done | Issue | Location | Fix |
|------|-------|----------|-----|
| [ ] | Error messages lack `aria-live` | `scrub.html:169-180` | Add `aria-live="polite"` |
| [ ] | Some inline onclick handlers | Various templates | Add keyboard events |

---

## 8. Performance Optimizations

**Priority: Medium-High**

### 8.1 N+1 Query Fixes (Critical)

| Done | Issue | Location | Fix |
|------|-------|----------|-----|
| [ ] | ChatViewSet.list missing prefetch | `rest_views.py:79-92` | Add `select_related('user', 'professional_user')` |
| [ ] | DenialQA queries in loop | `rest_views.py:457,467` | Use `prefetch_related('denial_qa')` |
| [ ] | filter_to_allowed_denials N+1 | `rest_views.py:718` | Use `.values_list('denial_id', flat=True)` |
| [ ] | filter_to_allowed_appeals N+1 | `rest_views.py:914-918` | Same fix as above |
| [ ] | AppealViewSet.list missing prefetch | `rest_views.py:613-617` | Add `select_related('for_denial', 'patient_user')` |

### 8.2 Missing Database Indexes

| Done | Field | Model | Notes |
|------|-------|-------|-------|
| [ ] | `hashed_email` | Denial | High-traffic lookup |
| [ ] | `hashed_email` | FaxesToSend | High-traffic lookup |
| [ ] | `hashed_email` | OngoingChat | High-traffic lookup |
| [ ] | `created` | Multiple models | Time-range queries |
| [ ] | `(professional_user_id, updated_at)` | OngoingChat | Chat listing |

### 8.3 JavaScript Bundle Optimization

| Done | Issue | Fix |
|------|-------|-----|
| [ ] | TensorFlow.js always loaded (~2-3MB) | Dynamic import when needed |
| [ ] | Tesseract.js always loaded (~3MB) | Dynamic import when needed |
| [ ] | pdfjs-dist always loaded (~2.5MB) | Dynamic import when needed |
| [ ] | jQuery loaded (84KB unminified) | Remove or replace with vanilla JS |
| [ ] | Source maps in production | Disable or use cheap-source-map |

### 8.4 Caching

| Done | Issue | Location | Fix |
|------|-------|----------|-----|
| [ ] | LocMemCache not suitable for multi-process | `settings.py:290-298` | Switch to Redis |
| [ ] | Only 1000 cache entries | `settings.py:295` | Increase or use Redis |
| [ ] | `COMPRESS_OFFLINE = False` | `settings.py:164` | Set to `True` for production |

### 8.5 Large Text Field Loading

| Done | Issue | Fix |
|------|-------|-----|
| [ ] | `Appeal.appeal_text` loaded in lists | Use `defer('appeal_text')` |
| [ ] | `Denial.denial_text` loaded in filters | Use `defer('denial_text')` |
| [ ] | `OngoingChat.chat_history` loaded in lists | Use `defer('chat_history')` |

---

## 9. Testing Gaps

**Priority: High - Required for safe refactoring**

### 9.1 Critical Gaps (Zero Coverage)

| Done | Category | Count | Notes |
|------|----------|-------|-------|
| [ ] | Model tests | 44+ models | No dedicated model tests |
| [ ] | Form validation tests | 10+ forms | Only utility tests exist |
| [ ] | Admin tests | 20+ admin classes | Zero coverage |
| [ ] | Staff views tests | `staff_views.py` | 152 lines untested |
| [ ] | Auth utilities | `auth_utils.py` | 280 lines untested |

### 9.2 Partial Coverage

| Done | Category | Current State | Needs |
|------|----------|---------------|-------|
| [ ] | Chat interface | Minimal | Unit tests for helper functions |
| [ ] | ML modules | Some coverage | Complete ml_router, ml_plan_doc_helper |
| [ ] | Serializers | Indirect only | Validation tests |
| [ ] | WebSocket handlers | Basic | Error path testing |

### 9.3 Test Infrastructure

| Done | Task | Notes |
|------|------|-------|
| [ ] | Create `conftest.py` | Centralize fixtures |
| [ ] | Add factory_boy factories | Better test data generation |
| [ ] | Add parametrized tests | Edge case coverage |
| [ ] | Create Cypress e2e tests | GitHub #521 |

---

## 10. Frontend Improvements

**Priority: Medium**

### 10.1 Console.log Removal (101 occurrences)

| Done | File | Count | Notes |
|------|------|-------|-------|
| [ ] | `chat_interface.tsx` | 26 | Most critical |
| [ ] | `appeal_fetcher.ts` | 14 | |
| [ ] | `entity_fetcher.ts` | 11 | |
| [ ] | `blog_post.tsx` | 3 | |
| [ ] | `blog.tsx` | 3 | |
| [ ] | Other files | ~44 | Various |

### 10.2 Error Boundaries (Missing)

| Done | Task | Notes |
|------|------|-------|
| [ ] | Create `ErrorBoundary` component | Catch React errors |
| [ ] | Wrap chat interface | Critical for production |
| [ ] | Wrap blog components | Has `dangerouslySetInnerHTML` |
| [ ] | Wrap chooser component | API calls may fail |

### 10.3 State Management Cleanup

| Done | Issue | Location | Fix |
|------|-------|----------|-----|
| [ ] | Global module variables | `appeal_fetcher.ts:9-16` | Convert to React hook |
| [ ] | Complex state object | `chat_interface.tsx:195-206` | Split into atomic states |
| [ ] | Global module variables | `entity_fetcher.ts:115-167` | Convert to React hook |

### 10.4 Memory Leaks

| Done | Issue | Location | Fix |
|------|-------|----------|-----|
| [ ] | setInterval without cleanup | `appeal_fetcher.ts:141` | Add useEffect cleanup |

### 10.5 Webpack Optimizations

| Done | Task | Notes |
|------|------|-------|
| [ ] | Add production minification | Currently missing |
| [ ] | Enable tree-shaking | Reduce bundle size |
| [ ] | Fix source map strategy | Only for dev |
| [ ] | Add MiniCssExtractPlugin | Extract CSS in production |
| [ ] | Add webpack-bundle-analyzer | Monitor sizes |

---

## 11. API & Documentation

**Priority: Medium**

### 11.1 OpenAPI Coverage

| Done | Task | Notes |
|------|------|-------|
| [ ] | Add `@extend_schema` to all REST endpoints | Currently ~18 of ~78 |
| [ ] | Document request parameters | Many missing `@OpenApiParameter` |
| [ ] | Add response examples | Improve API docs |

### 11.2 Code Documentation

| Done | Task | Current | Target |
|------|------|---------|--------|
| [ ] | Add model docstrings | ~1.7 per model | All models |
| [ ] | Add view method docstrings | Sparse | All public methods |
| [ ] | Document helper classes | Minimal | Full docstrings |

### 11.3 Environment Documentation

| Done | Task | Notes |
|------|------|-------|
| [ ] | Create `.env.example` | List all env vars |
| [ ] | Document required vs optional | In README or separate doc |
| [ ] | Document encryption keys | DEFF_SALT, DEFF_PASSWORD |

---

## 12. UX Improvements

**Priority: Medium | From GitHub Issues**

### 12.1 High Priority Features

| Done | Issue | Title | Notes |
|------|-------|-------|-------|
| [ ] | #569 | Logged-in patient view | Dashboard for tracking appeals |
| [ ] | #564 | Improve appeal tracking | Better follow-up workflow |
| [ ] | #625 | OCR for explain denial | Match appeal flow OCR |

### 12.2 Medium Priority Features

| Done | Issue | Title | Notes |
|------|-------|-------|-------|
| [ ] | #568 | Call script generator | "What to Say on the Phone" |
| [ ] | #567 | State-by-state help index | Directory of resources |
| [ ] | #566 | Insurer playbook pages | Anti-dark-pattern guidance |
| [ ] | #588 | Canonical URLs | SEO improvement |

### 12.3 UI Consistency

| Done | Task | Notes |
|------|------|-------|
| [ ] | Standardize alert styling | Use CSS classes from Section 2 |
| [ ] | Use `flow_progress.html` consistently | Partial already exists |
| [ ] | Improve loading states | Current preloader basic |
| [ ] | Mobile responsiveness audit | Breakpoints: 768px, 992px, 1200px |

---

## 13. PRs to Review/Incorporate

**These PRs have good ideas but may need cleanup before merging:**

| Done | PR | Title | Status | Notes |
|------|-----|-------|--------|-------|
| [ ] | #626 | Client-side OCR for Explain Denial | Ready | Adds Tesseract.js OCR to explain_denial page, feature parity with appeal flow |
| [ ] | #614 | Insurance Company & Plan Extraction | Ready | Adds InsuranceCompany/InsurancePlan models, regional brand support, specificity scoring |
| [ ] | #600 | State-by-State Help System | WIP | Has sitemap bug (national entry lacks state fields), needs fix before merge |

### PR #626 Details (OCR for Explain Denial)
- New `explain_denial.ts` module using existing `scrub_ocr.ts`
- Client-side processing preserves privacy
- Template updates for file upload

### PR #614 Details (Insurance Extraction)
- `InsuranceCompany` and `InsurancePlan` models
- Regional brand support (Anthem variants, Empire BlueCross, etc.)
- Dynamic company loading from DB instead of hardcoded lists
- 13 test cases, backward compatible

### PR #600 Details (State Help)
- `state_help.py` with data classes and cached JSON loader
- `state_help.json` with all 50 states + DC
- New views: `StateHelpIndexView`, `StateHelpView`
- **Bug to fix:** Sitemap includes "national" entry that lacks required fields → 404s

---

## 14. GitHub Issues Reference

### Open Issues (88 total)

**High Priority:**
- #625 - Add scan/OCR of denial on explain my denial
- #613 - Improve extracting insurance company & plan
- #569 - Logged-in patient view
- #564 - Improve appeal tracking
- #521 - Create Cypress e2e tests

**Medium Priority:**
- #604 - Explore using Ty instead of mypy
- #588 - Canonical URLs for all pages
- #570 - Plan documents Q&A landing page
- #568 - Call script generator
- #567 - State-by-state help index
- #566 - Insurer playbook pages
- #554 - Add Google Scholar scraping

**Lower Priority:**
- #274 - Separate API domain
- #266 - Preventive care category
- #250 - Ask about fax/phone on denial extraction
- #243 - Hash requirements.txt for venv reinstall
- #235 - Drop http(s) & www from domains
- #229 - Cross-practice collaboration
- #222 - Explore Daphne vs uvicorn
- #218 - Appeal listing filters
- #206 - REST endpoint for user pre-filled options
- #203 - Refactor actor checking logic
- #185 - DOI checker
- #159 - Aggressive reference scrubbing

---

## Recommended Work Order

1. **Security Fixes (Section 6.1)** - Critical, do first
2. **Quick Wins (Section 1)** - Fast impact
3. **Test Infrastructure (Section 9.3)** - Safety net for refactoring
4. **CSS Consolidation (Section 2)** - Visual cleanup, low risk
5. **Chat Interface Refactoring (Section 3)** - Major maintainability win
6. **N+1 Query Fixes (Section 8.1)** - Performance critical
7. **Code Deduplication (Section 4)** - Maintainability
8. **Accessibility (Section 7.1)** - Critical for compliance
9. **Frontend Cleanup (Section 10)** - Console logs, error boundaries
10. **Large File Decomposition (Section 5)** - Ongoing, incremental

---

## Progress Log

### Completed
*(Move completed items here with date)*

### In Progress
*(Track what you're working on)*

### Blocked
*(Note any blockers)*

---

*Last updated: December 25, 2025*
