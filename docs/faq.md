# FAQ

## Will LibrAIry delete my files?

No. There is no automatic delete path. Duplicate handling uses reversible quarantine proposals.

## Will it reorganize my existing library?

No. The existing library is indexed and searched, but not renamed, moved, or modified. New committed files land under the v1 categories.

## What about legacy RAM/ROM folders?

Fresh installs need nothing. If your library still uses the legacy `RAM/`/`ROM/` zones: LibrAIry will never restructure an existing library. Option A (recommended): manually move top-level content to the plain categories (`Music/`, `Movies/`, `Shows/`, `Photos/`, `Documents/`, `Books/`, `Projects/`, `Misc/`) before first indexing. Option B: leave it; the indexer is structure-agnostic, old paths remain searchable/browsable, and newly committed files land in the plain categories at the library root, so the zones fade into legacy corners over time.

## Does it need cloud AI?

No. Ollama/local AI is the default path, and deterministic heuristics/catalog lookups still work when AI is unavailable. Cloud providers are opt-in.

## Does LibrAIry serve SMB, FTP, or WebDAV?

No. Use your NAS or operating system to share `HOST_LIBRARY_DIR`.
