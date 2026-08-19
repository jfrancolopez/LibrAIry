from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from librairy import __version__
from librairy.config import Settings
from librairy.db import connect
from librairy.web.app import create_app

ROOT = Path(__file__).resolve().parents[1]


def test_the_version_has_exactly_one_source() -> None:
    """Two numbers, and the one you saw depended on how stale your install was.

    `pyproject.toml` declared 1.2.0 and this checkout's editable `dist-info`
    said 1.0.0, because an editable install writes its metadata once and never
    reads the project file again. `__version__` asked `importlib.metadata`, so
    the footer and `--version` reported 1.0.0 from a source tree that said
    1.2.0 — while the container, built from a wheel out of the same tree,
    reported 1.2.0.

    Now the number is a literal in `librairy/__init__.py` and `pyproject.toml`
    reads it from there. Nothing at runtime consults metadata, so nothing can
    disagree with it.
    """
    import tomllib

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert "version" not in project["project"], "a second number to keep in step"
    assert project["project"]["dynamic"] == ["version"]
    assert project["tool"]["hatch"]["version"]["path"] == "src/librairy/__init__.py"


def test_every_surface_that_shows_the_version_shows_the_same_one(tmp_path: Path) -> None:
    """The web footer and the CLI, against the one literal."""
    import subprocess
    import sys

    settings = Settings(APPDATA_DIR=tmp_path / "appdata", _env_file=None)
    client = TestClient(create_app(settings, connect(settings)))

    footer = client.get("/setup").text
    cli = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "librairy", "--version"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )

    assert f"LibrAIry v{__version__}" in footer
    assert cli.stdout.strip() == f"librairy {__version__}"


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
