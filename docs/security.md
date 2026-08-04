# Security

LibrAIry is designed for a trusted LAN. Do not expose it directly to the public internet. If remote access is needed, put it behind your own VPN or reverse proxy with TLS and additional authentication.

## Authentication

**The portal password is optional.** By default (`AUTH_REQUIRED=false`) LibrAIry assumes the trusted LAN it is designed for: there is no first-run password step, `/` goes straight to the dashboard, and anyone who can reach the host on your network can use the portal. Set a password whenever you want one — Settings → Portal Security, or the first-run screen — and the portal locks immediately: unauthenticated requests are redirected to `/login`.

When a password is set it is stored as a scrypt hash in SQLite, login is rate-limited, and it can be changed or removed again from Settings (both require the current password). This is intentionally not a multi-user system — there is one admin password, not accounts.

Set `AUTH_REQUIRED=true` if the host is reachable beyond a trusted LAN. That restores the mandatory first-run setup screen and refuses password removal from the UI.

Sessions are server-side with CSRF protection in both modes. In open mode a session is minted on first page load purely to carry the CSRF token, so cross-site form posts are still rejected.

## Container Hardening

Scanned with `docker scout quickview`. As of v1.2.0 the image meets 5 of 7 Scout
policies (health score B), up from 2 of 7 (health score E) in v1.1.0.

What changed:

- **Debian trixie, not bookworm.** Bookworm ships `perl`, `nss`, `mbedtls`, `jpeg-xl`,
  `libssh2`, and `tiff` at versions whose critical and high CVEs Debian never fixed in
  that release. The base moved to `python:3.12-slim-trixie`; Python stays on 3.12 to
  match the CI matrix.
- **No `gosu`.** Debian's `gosu` is compiled against Go 1.19, which alone accounted for
  4 critical and 29 high CVEs in the Go standard library. Privileges are dropped with
  `setpriv` from `util-linux` instead — same exec-and-drop behaviour, written in C.
- **Upstream `rclone`.** Debian's package is the other Go 1.19 binary. The image now
  installs rclone's own static build, pinned by version and SHA256 like `czkawka_cli`.
- **Non-root by default.** The image sets `USER librairy` (uid 1000). Hosts that need
  `PUID`/`PGID` remapping start the container as root (`--user 0:0`); the entrypoint
  chowns the mounts and drops privileges itself. Started non-root, it checks the mounts
  are writable and stops with a message naming the directory rather than failing later
  with an opaque permission error.
- **Supply-chain attestations.** Release builds publish max-mode SLSA provenance and an
  SBOM, so scanners identify the base image exactly instead of guessing. BuildKit's own
  provenance statement is SLSA v0.2 and Scout's policy only accepts v1, so the release
  workflow attaches a v1 statement with `actions/attest-build-provenance`. An image built
  by hand with `docker buildx --provenance=mode=max` therefore still reports one
  attestation deviation; only the CI-built image clears the policy.
- **`apt-get upgrade` in both stages**, picking up security updates published after the
  base image was cut.

Two deviations remain, both deliberate:

- **`perl` (2 critical, 2 high) and `cjson` (2 high)** are marked *not fixed* by Debian —
  no patched version exists in trixie. `perl` is required by `libimage-exiftool-perl`
  and `cjson` by `ffmpeg`; dropping either would remove metadata extraction. These are
  tracked, not ignored: they clear as soon as Debian ships fixes.
- **Copyleft licensed packages.** LibrAIry orchestrates GPL tools on purpose — `rmlint`
  is GPLv3, `ffmpeg` and `czkawka` are GPL. They are invoked as separate processes, never
  linked into LibrAIry (MIT). This policy will always report a violation for this image.

## Cloud AI Redaction

Cloud providers are disabled unless you set an API key and explicitly enable the provider in Settings with a `CLOUD` confirmation. Prompts never include absolute host paths, GPS coordinates, session tokens, or API keys.

`RedactedItemView` fields sent to AI:

- `display_path`
- `file_name`
- `extension`
- `size_bucket`
- `media_kind`
- `duration_seconds`
- `resolution`
- `codec`
- `embedded_title`
- `embedded_artist`
- `embedded_album`
- `embedded_genre`
- `track_number`
- `year`
- `sibling_file_names`
- `folder_chain`
- `hashtag_hints`
- `evidence_summaries`

Safe embedded tag keys:

- `album`
- `album_artist`
- `albumartist`
- `artist`
- `genre`
- `title`
- `track`
- `tracknumber`

Any tag value containing path markers, slashes, backslashes, or coordinate-looking decimals is dropped.
