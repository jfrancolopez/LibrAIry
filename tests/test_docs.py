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
        "faq.md",
    ):
        assert name in readme
