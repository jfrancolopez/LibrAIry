"""The compose file a NAS actually deploys.

`docker-compose.yml` carries `build:` because a developer needs it. On a machine
holding a library that is the wrong file: `up --build` produces an image tagged
`librairy:latest` whose source commit nothing records, and the next one replaces
it with something else. `docker-compose.release.yml` exists so that a deployment
can only run something that was published.

Two files describing the same service will drift. These hold them together on
the parts where drift is dangerous — the ports, the mounts, and the promise that
the release file cannot build. Read as text, like every other compose assertion
in this suite, so the tests need nothing the repository does not already have.
"""

from __future__ import annotations

import re
from pathlib import Path

import librairy

ROOT = Path(__file__).resolve().parents[1]
DEV = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
RELEASE = (ROOT / "docker-compose.release.yml").read_text(encoding="utf-8")


def uncommented(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.strip().startswith("#"))


def directive(text: str, key: str) -> str:
    """The value of a single `key: value` line, whitespace-normalised."""
    match = re.search(rf"^\s*{re.escape(key)}:\s*(.+?)\s*$", uncommented(text), re.M)
    assert match, f"no `{key}:` line found"
    return match.group(1)


def volumes(text: str) -> list[str]:
    body = uncommented(text)
    block = re.search(r"^\s*volumes:\n((?:\s*-\s.*\n)+)", body, re.M)
    assert block, "no volumes block"
    return [line.strip().lstrip("- ").strip() for line in block.group(1).splitlines()]


def test_the_release_file_cannot_build_anything() -> None:
    """The whole reason it exists. A `build:` here would defeat it silently."""
    assert not re.search(r"^\s*build:", uncommented(RELEASE), re.M)
    assert re.search(r"^\s*build:", uncommented(DEV), re.M), "the developer file still builds"


def test_it_names_a_published_version_never_latest() -> None:
    image = directive(RELEASE, "image")
    assert image.startswith("ghcr.io/jfrancolopez/librairy:"), image
    tag = image.split(":", 1)[1]
    assert tag != "latest", "`latest` moves; a deployment must name what it deployed"
    assert re.fullmatch(r"v\d+\.\d+\.\d+", tag), tag


def test_the_pinned_version_is_this_source_tree() -> None:
    """A deploy file naming an older release than the code beside it is a trap."""
    assert directive(RELEASE, "image").split(":", 1)[1] == f"v{librairy.__version__}"


def test_it_replaces_the_running_container_rather_than_adding_one() -> None:
    assert directive(RELEASE, "container_name") == directive(DEV, "container_name")


def test_the_container_port_is_8080_everywhere() -> None:
    """The defect this repository already shipped once: DASHBOARD_PORT is the
    host side, and the application must never be handed it as a bind port."""
    assert directive(RELEASE, "DASHBOARD_PORT") == "8080"
    mapping = re.search(r'-\s*"\$\{DASHBOARD_PORT:-8080\}:(\d+)"', RELEASE)
    assert mapping and mapping.group(1) == "8080", "published mapping must end at 8080"
    assert "http://127.0.0.1:8080/healthz" in RELEASE


def test_it_mounts_exactly_what_the_developer_file_mounts() -> None:
    """Different mounts would mean the upgrade quietly moved someone's library."""
    assert volumes(RELEASE) == volumes(DEV)


def test_the_privilege_story_is_the_same_one() -> None:
    for key in ("user", "PUID", "PGID", "command", "restart"):
        assert directive(RELEASE, key) == directive(DEV, key), key


def test_the_deployment_guide_points_at_this_file_and_not_the_other_one() -> None:
    guide = (ROOT / "docs/deploying-a-release.md").read_text(encoding="utf-8")
    assert "docker-compose.release.yml" in guide
    assert "never `latest`" in guide
    #  The guide has to say the thing that makes an image-only rollback
    #  impossible, because someone will otherwise try it.
    assert "refuses to open a database whose schema is *newer*" in guide
