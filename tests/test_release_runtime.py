"""Release invariants that only a running container could have found.

Everything here was written after the fact: the port defect made the documented
way to change the port produce an unreachable container, and no amount of
`docker compose config` would have said so. These pin the shape of the package
so the same class of mistake fails in CI instead of on somebody's NAS.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")


def test_the_port_an_operator_sets_is_the_port_they_can_reach() -> None:
    """DASHBOARD_PORT named two different things.

    Compose used it as the *host* half of the mapping; `env_file` also handed
    it to the application, which reads it as the port to *bind*. Setting it to
    anything but 8080 — which is what the sample configuration invites — made
    uvicorn listen on a port nothing was mapped to, the healthcheck curl 8080
    and find nothing, and the container sit `unhealthy` for ever behind a UI
    that could not be reached.
    """
    # The host half varies; the container half is fixed.
    assert '- "${DASHBOARD_PORT:-8080}:8080"' in COMPOSE
    # And the application is told the fixed one, overriding whatever .env says.
    assert re.search(r"^\s+DASHBOARD_PORT:\s*8080\s*$", COMPOSE, re.M)


def test_the_container_port_is_the_same_number_in_every_place_it_appears() -> None:
    """Three places have to agree or the container never reports healthy."""
    assert "EXPOSE 8080" in DOCKERFILE
    assert "http://127.0.0.1:8080/healthz" in COMPOSE  # compose healthcheck
    assert '- "${DASHBOARD_PORT:-8080}:8080"' in COMPOSE  # mapping target


def test_the_sample_configuration_says_which_side_of_the_mapping_it_means() -> None:
    """"Web dashboard port" was true of both ports and useful about neither."""
    sample = (ROOT / ".env.example").read_text(encoding="utf-8")
    port_block = sample.split("DASHBOARD_PORT=")[0].rsplit("\n\n", 1)[-1]

    assert "host" in port_block.lower()
    assert "8080" in port_block


def test_compose_hands_the_application_no_credential_it_did_not_ask_for() -> None:
    """`env_file` is whole-file: everything in .env reaches the container.

    That is the intended behaviour for provider keys, and it is exactly why
    anything compose sets *for its own purposes* has to be overridden back.
    """
    assert "env_file:" in COMPOSE
    environment = COMPOSE.split("environment:", 1)[1].split("volumes:", 1)[0]
    assert "DASHBOARD_PORT" in environment


@pytest.mark.parametrize(
    "binary",
    ["ffmpeg", "ffprobe", "exiftool", "pdftotext", "rclone", "rmlint", "fpcalc", "setpriv"],
)
def test_every_binary_the_product_shells_out_to_is_installed_or_fetched(binary: str) -> None:
    """The build's own smoke test runs these; this says which list it is.

    `exiftool` and `pdftotext` arrive under package names that do not contain
    the command, which is how a missing one would go unnoticed until a PDF or
    a photo reached a real installation.
    """
    packaged = {
        "exiftool": "libimage-exiftool-perl",
        "pdftotext": "poppler-utils",
        "fpcalc": "libchromaprint-tools",
        "setpriv": "util-linux",
    }
    assert packaged.get(binary, binary) in DOCKERFILE


def test_the_build_verifies_those_binaries_before_the_image_is_finished() -> None:
    """A binary that is installed but not on PATH fails at the first real use."""
    for check in ("ffprobe -version", "fpcalc -version", "pdftotext -v",
                  "rclone version", "rmlint --version", "czkawka_cli --version",
                  "setpriv --help", "librairy version"):
        assert check in DOCKERFILE


def test_the_image_never_learns_to_ask_git_what_commit_it_is() -> None:
    """A fallback that shelled out to git would report the *builder's* tree,
    and there is no repository in the image to ask anyway."""
    import ast

    from librairy import build_info

    source = Path(build_info.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    # Prose may discuss git all it likes; no *code* may reach for it.
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert "subprocess" not in imported
    assert not [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.strip().split(" ")[0] in {"git", "/usr/bin/git"}
    ]
    # And the image has no git to ask, so the fallback could not exist anyway.
    # Comments stripped: the Dockerfile explains this in prose right above it.
    runtime_stage = "\n".join(
        line
        for line in DOCKERFILE.split("AS runtime", 1)[1].splitlines()
        if not line.lstrip().startswith("#")
    )
    assert not re.search(r"\bgit\b(?!hub)", runtime_stage)


def test_the_healthcheck_needs_nothing_the_image_does_not_have() -> None:
    """`curl` is the healthcheck's only dependency, in both places it is defined."""
    assert "curl" in DOCKERFILE.split("AS runtime", 1)[1].split("WORKDIR", 1)[0]
    assert '"CMD", "curl"' in COMPOSE


# --- the matrix itself -----------------------------------------------------------


def _drill():
    """The acceptance drill, imported as a module rather than shelled out to."""
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location(
        "release_acceptance", ROOT / "scripts" / "release_acceptance.py"
    )
    module = importlib.util.module_from_spec(spec)
    # Registered before execution: @dataclass resolves string annotations
    # through sys.modules[cls.__module__], which does not exist otherwise.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_a_runtime_gate_is_never_silently_omitted(monkeypatch) -> None:
    """A gate that cannot be run has to appear as BLOCKED, not vanish.

    The failure this prevents is the quiet one: a matrix that lists ten gates
    on a machine with Docker and eight on a machine without, and reads as
    complete both times.
    """
    drill = _drill()
    monkeypatch.setattr(drill.shutil, "which", lambda _: None)
    report = drill.Report()

    drill.runtime(report, {"rc_name": "librairy:rc-test"})

    named = {gate.name for gate in report.gates}
    assert set(drill.RUNTIME_GATES) <= named
    assert set(drill.COMPOSE_GATES) <= named
    assert all(gate.status == drill.BLOCKED for gate in report.gates)


def test_the_drill_reports_the_same_gates_whether_or_not_docker_answers(monkeypatch) -> None:
    """No daemon and a broken daemon are the same size of matrix."""
    drill = _drill()
    monkeypatch.setattr(drill.shutil, "which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(drill, "run", lambda *a, **k: type("R", (), {"returncode": 1})())
    report = drill.Report()

    drill.runtime(report, {"rc_name": "librairy:rc-test"})

    assert {gate.name for gate in report.gates} == set(drill.RUNTIME_GATES) | set(
        drill.COMPOSE_GATES
    )
    assert {gate.detail for gate in report.gates} == {"docker daemon unavailable"}


def test_a_blocked_gate_can_never_be_rounded_up_to_ready() -> None:
    drill = _drill()

    for status in (drill.FAIL, drill.BLOCKED, drill.NOT_TESTED):
        report = drill.Report()
        report.add("RUNTIME", "container start", drill.PASS)
        report.add("RUNTIME", "clean shutdown", status)
        assert report.verdict != "READY"
        assert report.blocking or status == drill.NOT_TESTED

    clean = drill.Report()
    clean.add("RUNTIME", "container start", drill.PASS)
    assert clean.verdict == "READY"


def test_the_drill_never_publishes_anything() -> None:
    """Read as source, because the point is that these calls do not exist."""
    source = (ROOT / "scripts" / "release_acceptance.py").read_text(encoding="utf-8")

    # Reading what tags exist is the whole point of the identity section;
    # writing one is what must not be here.
    assert '"git", "tag", "-l"' in source
    for forbidden in ("docker push", '"docker", "push"', "gh release",
                      '"git", "push"', '"git", "tag", "v'):
        assert forbidden not in source
    assert "Nothing was tagged, pushed or published" in source
