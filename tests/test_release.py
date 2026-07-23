from __future__ import annotations

import importlib.metadata
from pathlib import Path

from fastapi.testclient import TestClient

from librairy import __version__
from librairy.config import Settings
from librairy.db import connect
from librairy.web.app import create_app

ROOT = Path(__file__).resolve().parents[1]


def test_version_is_sourced_from_package_metadata() -> None:
    assert __version__ == importlib.metadata.version("librairy")


def test_web_footer_shows_version(tmp_path: Path) -> None:
    settings = Settings(APPDATA_DIR=tmp_path / "appdata", _env_file=None)
    client = TestClient(create_app(settings, connect(settings)))

    response = client.get("/setup")

    assert f"LibrAIry v{__version__}" in response.text


def test_release_workflow_builds_multiarch_ghcr_image() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")

    assert "tags:" in workflow
    assert "v*" in workflow
    assert "docker/setup-buildx-action" in workflow
    assert "linux/amd64,linux/arm64" in workflow
    assert "ghcr.io/${{ github.repository_owner }}/librairy" in workflow
    assert "cache-from" in workflow
    assert 'value=latest,enable=${{ !contains(github.ref_name, \'-\') }}' in workflow


def test_dockerfile_uses_prebuilt_checksummed_czkawka() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "cargo install" not in dockerfile
    assert "releases/download" in dockerfile
    assert "sha256sum -c" in dockerfile
    assert "linux_czkawka_cli_x86_64" in dockerfile
    assert "linux_czkawka_cli_arm64" in dockerfile


def test_changelog_lists_v1_safety_never_list() -> None:
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    for phrase in ("Never deletes", "Never overwrites", "Never mutates", "approved immutable"):
        assert phrase in changelog
