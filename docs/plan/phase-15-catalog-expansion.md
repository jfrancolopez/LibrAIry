# Phase 15 — Catalog Expansion + Web API Key Entry (v1.2)

**Status:** IN PROGRESS — P15-01, P15-02 done (2026-07-23)
**Depends on:** Phase 14 (settings/cards design language in place)
**Size:** M–L (catalog tasks are independent; key-entry task needs its own security care)

---

## Product Context

<!-- context-boilerplate v1 -->
<!-- CANONICAL COPY lives in docs/plan/README.md. Do not edit here; if decisions change, the canonical copy is updated and propagated. -->

**LibrAIry** is a self-hosted, privacy-first, AI-assisted file organizer and library manager. It ships as a single Docker container for NAS systems (UNRAID is the primary target) and desktop workstations. The user drops messy files into an **inbox** folder; LibrAIry analyzes them continuously in the background and proposes clean names and destinations inside an organized **library**; the user reviews proposals in batches from a lightweight LAN web portal (approve / edit / reject / postpone); only then does LibrAIry move files. It is an *orchestrator*, leaning on proven external tools (ffprobe, exiftool, fpcalc/Chromaprint, rmlint, czkawka) and free catalog APIs (MusicBrainz, AcoustID, TMDB), using AI only when deterministic evidence is insufficient — local AI (Ollama) by default, cloud AI strictly opt-in.

**Safety invariants (non-negotiable; enforced in code and by tests):** LibrAIry NEVER deletes user files; NEVER overwrites (deterministic collision renames); the existing library is READ-ONLY; analysis never mutates the filesystem — only the commit engine moves files, executing exactly an approved, immutable, hash-verified plan; every destination is containment-validated (traversal/absolute/symlink escapes fail closed); quarantine is reversible; v1 renames/moves only; every operation is journaled and undoable; privacy is local-first with structural redaction and per-provider cloud opt-in.

**Locked product decisions (do not reopen):** one container, web + worker under a Python supervisor; single-admin LAN portal (scrypt, SQLite sessions, CSRF, rate limiting); taxonomy `Music/ Movies/ Shows/ Photos/ Documents/ Books/ Projects/ Misc/`; SQLite WAL + FTS5 (no Postgres, no Elasticsearch); Python 3.11+, FastAPI + uvicorn + Jinja2 + HTMX, vanilla CSS/JS (no Node build), raw stdlib sqlite3 (no ORM), Pydantic, pytest, ruff, GitHub Actions; Ollama default with per-provider cloud opt-in; duplicates → reversible quarantine only; portal is "a lightweight dashboard and review tool, NOT a file manager"; no microservices/queues/plugin system/Kubernetes.

<!-- end context-boilerplate -->

---

## Phase goal

Owner intent (2026-07-23): "find all the free catalogs — if I configure multiple, the system uses all of them to organize the library," with the settings page explaining each one (what it's for, where to sign up, cost) — plus web-based API key entry so keys don't require container restarts. Evidence from every enabled catalog merges into the existing classification cascade **before** AI, exactly like TMDB/MusicBrainz today.

## Design constraints binding this phase

- **Adapter pattern stands:** new catalogs follow `classify/music.py` (MusicBrainz+AcoustID) and `classify/video.py` (TMDB): stdlib `urllib` HTTP, results recorded as `EvidenceEntry` on proposals, response caching via the existing metadata-cache helpers (`tools/common.py`), and a politeness rate-limiter modeled on the MusicBrainz one (`MB_RATE_LIMIT`). **No new Python dependencies.**
- **All catalogs optional and individually toggleable** in settings; keyless ones default ON, keyed ones activate when a key/token is present. A failing/unreachable catalog degrades silently to the next evidence source (existing cascade behavior).
- **Privacy:** catalog queries send the same redacted material AI gets (titles/fingerprints/years — never absolute paths). State per catalog in the info card what is sent.
- **Precedence:** environment variables always win over DB-stored keys; document it wherever keys appear.

## The catalog roster (research done 2026-07-23 — re-verify signup URLs at implementation time)

| Catalog | Identifies | Key? | Cost | Notes for the info card |
|---|---|---|---|---|
| MusicBrainz *(existing)* | music releases/artists | none | free | rate-limited 1 req/s; already integrated |
| AcoustID *(existing)* | audio by fingerprint | free key | free | pairs with fpcalc/Chromaprint; acoustid.org |
| TMDB *(existing)* | movies & TV | free key | free | themoviedb.org/settings/api |
| **Open Library** | books by title/author/ISBN | **none** | free | openlibrary.org/developers/api; easiest win for `Books/` |
| **TVmaze** | TV shows/episodes | **none** | free | tvmaze.com/api; complements TMDB for episode naming |
| **Cover Art Archive** | album art via MusicBrainz IDs | **none** | free | coverartarchive.org; art is *evidence/preview* only — v1 never writes files, so store/reference art in appdata thumbnails, never into the library |
| **Discogs** | music releases (esp. vinyl/rare) | free personal token | free | discogs.com/settings/developers |
| **Last.fm** | genre tags for music | free key | free | last.fm/api; feeds the genre-first templates |

## Backlog items

### P15-01 Catalog info cards in Settings
**Depends on:** — | **Size:** S
**Description:** Replace the bare key-status rows with one card per catalog (existing three first): what it identifies, what data leaves the machine, cost (all free), signup URL as a link, a 3-step "how to get your key" mini-tutorial, and live status (key set / not set / not needed / disabled). Reuse the Phase-14 card design language. Keys still show only set/not-set — never values.
**Acceptance criteria:**
- [x] Cards render for MusicBrainz, AcoustID, TMDB (plus Open Library) with accurate copy, cost, signup URL, key steps, and what-leaves-the-machine.

### P15-02 Open Library adapter (books, keyless)
**Depends on:** P15-01 | **Size:** M
**Description:** New `classify/books.py` adapter: query Open Library search API by cleaned title/author/ISBN for document/book-classified items; merge results as evidence (title/author/year → clean name + `Books/` destination via existing taxonomy templates). Toggle `catalog.openlibrary.enabled` default ON. Mocked-HTTP unit tests (no network in CI), politeness delay, cache.
**Acceptance criteria:**
- [x] A book with an opaque filename gets an Open Library evidence entry, a better clean name/author/year, and higher confidence; toggle-off and no-lookup paths stay heuristic-only (tests). Verified against the live API.

### P15-03 TVmaze adapter (TV, keyless)
**Depends on:** P15-01 | **Size:** M
**Description:** `classify/video.py` gains a TVmaze lookup for show/episode items (singlesearch/shows + episodebynumber), used when TMDB lacks a key or returns nothing; evidence merged with the same shape TMDB produces. Toggle `catalog.tvmaze.enabled` default ON. Mocked tests as P15-02.
**Acceptance criteria:**
- [ ] Episode fixture resolves via TVmaze when TMDB is disabled (test); both-enabled ordering documented and tested.

### P15-04 Discogs + Last.fm adapters (music, token/key)
**Depends on:** P15-01 | **Size:** M
**Description:** Two small adapters in/beside `classify/music.py`: Discogs release search (personal token) as fallback evidence when MusicBrainz confidence is low; Last.fm `tag.getTopTags`/track tags (API key) feeding genre evidence for the genre-first templates. Both default OFF until a key exists. Mocked tests; per-catalog toggles.
**Acceptance criteria:**
- [ ] With keys set, adapters contribute evidence (tests); without keys, zero requests and no errors.

### P15-05 Cover Art Archive (art evidence, keyless)
**Depends on:** P15-04 (uses MusicBrainz release IDs already in evidence) | **Size:** S
**Description:** When music evidence contains a MusicBrainz release ID, fetch the cover thumbnail into the appdata thumbnail cache and show it on review cards/browse detail. **Never write art into the library** (v1 invariant: renames/moves only). Toggle default ON (keyless).
**Acceptance criteria:**
- [ ] Review card for a MusicBrainz-matched album shows cover art from cache; library tree untouched (test asserts no new files under library).

### P15-06 Web-based API key entry (security-scoped)
**Depends on:** P15-01 | **Size:** M
**Description:** The v1-deferred item, now with its security review baked in: masked `type="password"` inputs on the catalog/provider cards for TMDB, AcoustID, Discogs, Last.fm, OpenAI, Anthropic, Gemini; POST over the existing CSRF-protected settings flow; stored in the settings DB; **never rendered back** (placeholder shows "•••• set — [replace] [clear]"); `logging.RedactionFilter` covers the new values (test); `effective_settings` merge gives **env-var precedence over DB** (test + docs); per-provider/catalog TEST button reusing the `ai test` / catalog-probe machinery, returning ok/fail inline. Threat-model note in the doc: single-admin LAN app, CSRF + session auth guard the endpoint; DB file permissions are the at-rest boundary (same as session tokens today) — no new crypto invented.
**Acceptance criteria:**
- [ ] Keys settable/replaceable/clearable from the web; never appear in any response body or log (tests); env precedence test; TEST buttons work against mocks.

### P15-07 OpenAI browser sign-in — honest scoping
**Depends on:** P15-06 | **Size:** XS
**Description:** Owner asked for "authenticate with OpenAI via browser." As of 2026-07, OpenAI offers **no public OAuth flow that issues API keys to third-party self-hosted apps** — do NOT fake one or embed anyone else's client credentials. Ship instead: the OpenAI card deep-links to platform.openai.com/api-keys with a 3-step tutorial, web key entry (P15-06), and TEST button. Record in this doc's open-questions log: "browser OAuth revisited if OpenAI opens a public program." If at implementation time OpenAI HAS opened such a program, stop and ask the owner before adding an OAuth dependency.
**Acceptance criteria:**
- [ ] OpenAI card ships link+tutorial+key-entry+test; no OAuth code exists; decision logged.

## Verification steps

1. Per adapter: mocked-HTTP unit tests only in CI; one manual live drill per catalog in the container (real network) recorded in the log.
2. Full-cascade drill: an inbox with one book, one TV episode, one album track — with all keyless catalogs on and AI off — produces catalog-evidenced proposals for all three.
3. `ruff check src tests scripts && pytest` green per task; ship as v1.2.0.

## Exit gate checklist

- [ ] All enabled catalogs contribute merged evidence before AI; every one individually toggleable; keyless ones work with zero configuration.
- [ ] Web key entry secure per P15-06 criteria; no secret ever rendered or logged.
- [ ] No new Python dependencies; privacy notes accurate per catalog.

## Open questions log

*(Executing agent: record ambiguities and the safest-default decision taken, then continue.)*
