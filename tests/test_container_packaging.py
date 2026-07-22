from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_dockerfile_is_multistage_runtime_with_healthcheck_and_entrypoint() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "AS builder" in dockerfile
    assert "AS runtime" in dockerfile
    assert "ARG CZKAWKA_CLI_VERSION=" in dockerfile
    assert "COPY --from=builder" in dockerfile
    assert "HEALTHCHECK" in dockerfile
    assert "ENTRYPOINT [\"docker-entrypoint.sh\"]" in dockerfile
    assert "CMD [\"librairy\", \"run\"]" in dockerfile


def test_entrypoint_supports_puid_pgid_and_drops_privileges() -> None:
    entrypoint = (ROOT / "docker-entrypoint.sh").read_text(encoding="utf-8")

    assert "PUID=\"${PUID:-99}\"" in entrypoint
    assert "PGID=\"${PGID:-100}\"" in entrypoint
    assert "usermod" in entrypoint
    assert "groupmod" in entrypoint
    assert "exec gosu \"${PUID}:${PGID}\" \"$@\"" in entrypoint


def test_dockerignore_excludes_non_runtime_context() -> None:
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")

    for pattern in (".git", "tests", "docs", "data", ".venv"):
        assert pattern in dockerignore


def test_compose_exposes_puid_pgid_defaults() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert "PUID: ${PUID:-99}" in compose
    assert "PGID: ${PGID:-100}" in compose
    assert "healthcheck:" in compose
    assert "http://127.0.0.1:8080/healthz" in compose
