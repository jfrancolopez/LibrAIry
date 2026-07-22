from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1]


def test_unraid_template_contains_required_fields() -> None:
    tree = ElementTree.parse(ROOT / "packaging/unraid/librairy.xml")
    root = tree.getroot()

    assert root.findtext("Name") == "LibrAIry"
    assert root.findtext("Repository") == "ghcr.io/jfrancolopez/librairy:latest"
    assert root.findtext("WebUI") == "http://[IP]:[PORT:8080]"
    assert root.findtext("Category") == "MediaApp:Other Tools:"
    assert root.findtext("Icon", "").endswith("/packaging/unraid/icon.svg")


def test_unraid_template_defines_ports_paths_and_env() -> None:
    tree = ElementTree.parse(ROOT / "packaging/unraid/librairy.xml")
    configs = tree.getroot().findall("Config")
    targets = {config.attrib["Target"] for config in configs}

    assert {"8080", "/data/inbox", "/data/library", "/data/quarantine", "/data/appdata"} <= targets
    assert {"OLLAMA_HOST", "OLLAMA_MODEL_PRIMARY", "TMDB_KEY", "ACOUSTID_KEY"} <= targets
    assert {"PUID", "PGID"} <= targets


def test_install_guides_cover_clean_start_and_webui() -> None:
    unraid = (ROOT / "docs/install-unraid.md").read_text(encoding="utf-8")
    docker = (ROOT / "docs/install-docker.md").read_text(encoding="utf-8")

    assert "PUID/PGID" in unraid
    assert "legacy `RAM/`/`ROM/` zones" in unraid
    assert "http://<unraid-ip>:8080" in unraid
    assert "docker compose up -d --build" in docker
    assert "ghcr.io/jfrancolopez/librairy:latest" in docker
