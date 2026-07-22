# Security

LibrAIry is designed for a trusted LAN. Do not expose it directly to the public internet. If remote access is needed, put it behind your own VPN or reverse proxy with TLS and additional authentication.

## Authentication

The portal has one admin password, stored as a scrypt hash in SQLite. Sessions are server-side with CSRF protection. This is intentionally not a multi-user system.

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
