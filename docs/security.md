# Security

LibrAIry is designed for a trusted LAN. Do not expose it directly to the public internet. If remote access is needed, put it behind your own VPN or reverse proxy with TLS and additional authentication.

## Authentication

**The portal password is optional.** By default (`AUTH_REQUIRED=false`) LibrAIry assumes the trusted LAN it is designed for: there is no first-run password step, `/` goes straight to the dashboard, and anyone who can reach the host on your network can use the portal. Set a password whenever you want one — Settings → Portal Security, or the first-run screen — and the portal locks immediately: unauthenticated requests are redirected to `/login`.

When a password is set it is stored as a scrypt hash in SQLite, login is rate-limited, and it can be changed or removed again from Settings (both require the current password). This is intentionally not a multi-user system — there is one admin password, not accounts.

Set `AUTH_REQUIRED=true` if the host is reachable beyond a trusted LAN. That restores the mandatory first-run setup screen and refuses password removal from the UI.

Sessions are server-side with CSRF protection in both modes. In open mode a session is minted on first page load purely to carry the CSRF token, so cross-site form posts are still rejected.

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
