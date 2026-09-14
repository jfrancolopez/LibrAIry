from __future__ import annotations

from pathlib import Path

from librairy.ai.redact import SAFE_TAGS, RedactedItemView

ROOT = Path(__file__).resolve().parents[1]
DOC_PATHS = [ROOT / "README.md", ROOT / "Instructions.md", *sorted((ROOT / "docs").glob("*.md"))]
WEB_SETTINGS = {
    "runtime.confidence_threshold",
    "runtime.batch_size",
    "templates.<category>.style",
    "dedup.use_fingerprints",
    "dedup.use_rmlint",
    "dedup.use_czkawka",
    "ai.provider_order",
    "ai.ollama.endpoints",
    "ai.openai.enabled",
    "ai.anthropic.enabled",
    "ai.gemini.enabled",
    "content_search.enabled",
    "backup.enabled",
    "backup.remote",
    "backup.bandwidth_limit",
    "backup.schedule",
    "backup.daily_at",
    "backup.categories",
    "backup.include_db_snapshot",
}


def test_configuration_docs_cover_env_example_and_web_settings() -> None:
    config = (ROOT / "docs/configuration.md").read_text(encoding="utf-8")
    env_keys = {
        line.split("=", 1)[0]
        for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#") and "=" in line
    }

    for key in env_keys:
        assert f"`{key}`" in config
    for key in WEB_SETTINGS:
        assert f"`{key}`" in config


def test_security_docs_match_redaction_allowlist() -> None:
    security = (ROOT / "docs/security.md").read_text(encoding="utf-8")

    for field in RedactedItemView.model_fields:
        assert f"`{field}`" in security
    for tag in SAFE_TAGS:
        assert f"`{tag}`" in security


def test_docs_do_not_reference_deleted_artifacts_as_current() -> None:
    text = "\n".join(path.read_text(encoding="utf-8") for path in DOC_PATHS)

    assert "setup.sh" not in text
    assert "/data/reports" not in text
    assert "inbox-processor" not in text


def test_readme_links_to_new_documentation_set() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    for name in (
        "install-docker.md",
        "install-unraid.md",
        "configuration.md",
        "using-librairy.md",
        "troubleshooting.md",
        "security.md",
        "backup-restore.md",
        "performance.md",
        "faq.md",
    ):
        assert name in readme


def test_content_search_privacy_assertions_match_code() -> None:
    content_docs = (ROOT / "docs/content-search.md").read_text(encoding="utf-8")
    ai_imports = "\n".join(
        path.read_text(encoding="utf-8") for path in (ROOT / "src/librairy/ai").glob("*.py")
    )

    assert "never sent to local or cloud AI providers" in content_docs
    assert "content.extract" not in ai_imports
    assert "content_fts" not in ai_imports
    assert "content" not in RedactedItemView.model_fields


def test_docker_docs_include_macos_test_folder_walkthrough() -> None:
    docker_docs = (ROOT / "docs/install-docker.md").read_text(encoding="utf-8")

    assert "Using Test Folders On macOS" in docker_docs
    assert "HOST_INBOX_DIR=/Users/<you>/Desktop/librairy-test-inbox" in docker_docs
    assert "docker compose up -d --build" in docker_docs


def test_the_tool_list_names_every_binary_the_image_installs() -> None:
    """The troubleshooting page lists the helper tools, and it had drifted.

    It named ffprobe, exiftool, fpcalc, rmlint and czkawka_cli — and omitted
    poppler, which is how a document is read at all rather than guessed at from
    its name, and rclone, which is the entire backup system. A tool list that is
    missing the two most consequential entries is worse than no list: somebody
    debugging a document that would not identify had no reason to look for
    `pdftotext`.

    Read from the Dockerfile, so the page cannot fall behind the image again.
    """
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    page = (ROOT / "docs/troubleshooting.md").read_text(encoding="utf-8")
    #  What the runtime stage installs, under the names a person would search
    #  for rather than the package names apt uses.
    expected = {
        "ffmpeg": "ffprobe",
        "libchromaprint-tools": "fpcalc",
        "libimage-exiftool-perl": "exiftool",
        "poppler-utils": "poppler",
        "rmlint": "rmlint",
    }
    for package, tool in expected.items():
        assert package in dockerfile, f"{package} is no longer installed"
        assert tool in page, f"the image installs {package} and the docs never mention {tool}"
    for copied in ("czkawka_cli", "rclone"):
        assert copied in dockerfile
        assert copied in page, f"{copied} is in the image and not in the docs"


def test_ocr_is_documented_as_absent_rather_than_silently_missing() -> None:
    """`librairy/ocr.py` says the image ships without tesseract, deliberately.

    Nothing user-facing said so. Somebody with a drawer of scans saw *no text
    layer — this is a scan* on every one of them and had no documented way to
    change that, for a decision the program made on their behalf and had good
    reasons for.
    """
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    page = (ROOT / "docs/troubleshooting.md").read_text(encoding="utf-8")

    assert "tesseract" not in dockerfile, (
        "tesseract is in the image now — the docs say it deliberately is not"
    )
    assert "tesseract" in page
    assert "no text layer" in page
