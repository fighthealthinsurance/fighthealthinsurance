# Algorithmic-review detector notes

`algorithmic_review_detector.py` uses two pattern maps:

- `TERM_PATTERNS` for generic automation and criteria language.
- `VENDOR_PATTERNS` for utilization-management vendors/tools.

To add a future vendor, add a new key + regex to `VENDOR_PATTERNS`, and (optionally) add a matching template block in `TEMPLATE_BLOCKS` with a unique block id.

The detector returns `suggested_template_blocks`, and `common_view_logic.py` appends the rendered block text into `non_ai_appeals` so the generated appeal asks for individualized review, criteria disclosure, and human clinician review without asserting legal violations.
