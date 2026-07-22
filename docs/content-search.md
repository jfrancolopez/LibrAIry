# Content Search

Content search is disabled by default. Enable it in Settings, then the next worker cycle extracts text from committed library files in Documents, Books, and Projects.

- Supported: TXT, Markdown, DOCX, EPUB, and PDF through `pdftotext` when available.
- Not supported: OCR for scanned images.
- Extracted text stays local in SQLite `content_fts`.
- Extracted text is never sent to local or cloud AI providers.
- Rebuild only the content index with `librairy index rebuild --content`.

Use Search and check “search inside documents” to include `[CONTENT]` matches and snippets.
