from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def instructions(relpath: str) -> str:
    """File contents minus comment lines, so prose can name what it replaced."""
    text = (ROOT / relpath).read_text(encoding="utf-8")
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def test_dockerfile_is_multistage_runtime_with_healthcheck_and_entrypoint() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "AS builder" in dockerfile
    assert "AS runtime" in dockerfile
    assert "ARG CZKAWKA_CLI_VERSION=" in dockerfile
    assert "COPY --from=builder" in dockerfile
    assert "HEALTHCHECK" in dockerfile
    assert "ENTRYPOINT [\"docker-entrypoint.sh\"]" in dockerfile
    assert "CMD [\"librairy\", \"run\"]" in dockerfile
    assert "poppler-utils" in dockerfile
    assert "rclone" in dockerfile


def test_dockerfile_runs_as_non_root_by_default() -> None:
    """Docker Scout fails an image whose config leaves USER unset (i.e. root)."""
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "USER librairy" in dockerfile
    # USER has to come after the privileged build steps, or pip/apt cannot write.
    assert dockerfile.index("USER librairy") > dockerfile.index("pip install")


def test_dockerfile_avoids_debian_packages_with_unfixable_cves() -> None:
    """Debian's rclone and gosu are Go 1.19 builds: 4 critical + 29 high CVEs.

    rclone comes from upstream's static build instead, and gosu is replaced by
    setpriv from util-linux, which has no Go runtime at all.
    """
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "ARG RCLONE_VERSION=" in dockerfile
    assert "downloads.rclone.org" in dockerfile
    assert "ARG RCLONE_SHA256_AMD64=" in dockerfile
    assert "ARG RCLONE_SHA256_ARM64=" in dockerfile
    assert "gosu" not in instructions("Dockerfile")
    assert "util-linux" in dockerfile
    # Bookworm has no fix for its perl/nss/mbedtls/tiff criticals; trixie does.
    assert "slim-trixie" in dockerfile
    assert "slim-bookworm" not in dockerfile
    # Pick up point-release security updates published after the base was cut.
    assert dockerfile.count("apt-get upgrade -y") == 2


def test_entrypoint_supports_puid_pgid_and_drops_privileges() -> None:
    entrypoint = (ROOT / "docker-entrypoint.sh").read_text(encoding="utf-8")

    assert "PUID=\"${PUID:-99}\"" in entrypoint
    assert "PGID=\"${PGID:-100}\"" in entrypoint
    assert "usermod" in entrypoint
    assert "groupmod" in entrypoint
    drop = 'exec setpriv --reuid="${PUID}" --regid="${PGID}" --init-groups -- "$@"'
    assert drop in entrypoint
    assert "gosu" not in instructions("docker-entrypoint.sh")


def test_entrypoint_reports_unwritable_mounts_when_started_non_root() -> None:
    """A non-root start cannot chown, so an unwritable bind mount must say so."""
    entrypoint = (ROOT / "docker-entrypoint.sh").read_text(encoding="utf-8")

    assert "is not writable by uid" in entrypoint
    assert "user: \\\"0:0\\\"" in entrypoint


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
    # PUID/PGID remapping needs root, which the image no longer defaults to.
    assert 'user: "${LIBRAIRY_USER:-0:0}"' in compose


def test_release_workflow_publishes_supply_chain_attestations() -> None:
    """Without provenance + SBOM, Scout cannot identify the base image."""
    release = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")

    assert "provenance: mode=max" in release
    assert "sbom: true" in release
    assert "id-token: write" in release
    assert "attestations: write" in release
