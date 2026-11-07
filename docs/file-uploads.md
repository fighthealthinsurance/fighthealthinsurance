# File uploads

Every page that takes files, what each takes, and how the files are kept
and served. The numbers live in `fighthealthinsurance/upload_limits.py`;
the per-visitor counts in `fighthealthinsurance/upload_rate_limit.py`. The
checklist this follows is the OWASP File Upload Cheat Sheet and OWASP ASVS
5.0 chapter V5 (File Handling).

## What each page takes

| Page | Types | Size | Other checks |
| --- | --- | --- | --- |
| Explain My Policy (`/understand-policy`) | .pdf, .docx, .txt | 256 bytes to 20MB | first bytes of PDF and Word files |
| Appeal step 3, plan documents (`dvc`) | .pdf, .docx, .txt | 256 bytes to 20MB each, 10 files, 60MB in all | first bytes of PDF and Word files |
| Share page (`share-plan-documents`) and the "share for research" copies | .pdf, .docx, .txt | 256 bytes to 20MB each, 10 files, 60MB in all | PDF opened in a separate process, no password, at most 3000 pages; Word file a real zip with no macros, at most 2000 parts and 64MB unpacked; text file has no NUL bytes |
| Follow-up documents (`followup`) | documents (.pdf, .docx, .doc, .rtf, .txt) and photos (.png, .jpg, .jpeg, .gif, .tif, .tiff, .heic, .webp) | up to 20MB each, 10 files, 60MB in all | first bytes of PDF, Word, PNG, JPEG and GIF files |
| Appeal attachments (REST, signed-in appeal owners) | same as follow-up documents | up to 25MB | same as follow-up documents |
| Server-side OCR (`server_side_ocr`) | images | up to 20MB each, 30 files | at most 50 million pixels before decoding, 60 seconds of OCR each |

## How many files one visitor may upload

Across all of the pages above, one address (an IPv6 address by its /64)
and one signed-in account may each upload 20 files an hour and 60 a day (`FHI_UPLOAD_FILES_PER_HOUR`, `FHI_UPLOAD_FILES_PER_DAY`). Files are
counted when they arrive, before any check, so files that fail the checks
count too; an upload over the limit is refused whole and counts nothing.
Logged-in staff are not counted. A typed email is not counted: anyone can
type anyone's. The counts are kept in the database
(`UploadCount`), since each web pod has its own cache, as keyed digests that
change every period, never the address itself. The share page
keeps its own daily budgets on top of these.

## Keeping and serving files

- Files are encrypted at rest (`EncryptedFileField`) and stored under a name
  the app makes: the visitor's name (made safe) plus a random part, or for
  shared files `shared-<id>`. Nothing uploaded is served from a public
  folder or run.
- File names kept or shown are made safe by `upload_limits.safe_filename`:
  no folders, only letters, digits, spaces, dots, hyphens and underscores,
  at most 200 characters.
- Staff read shared files as plain text that can't run anything, or download
  the original as an attachment (`staff_views.py`). Appeal attachments
  download as attachments with a type taken from their name, not from the
  uploader's browser, and `X-Content-Type-Options: nosniff`.
- Every upload form is a POST protected by Django's CSRF check.

## Not done yet

- Files are not scanned with an antivirus. That needs an antivirus service in
  the cluster.
- Documents are not rebuilt (content disarm and reconstruction). Shared
  files are read as text in a separate process, and staff see that text
  first.
