#!/usr/bin/env python3
"""Rehearse a release, and print what passed, what did not, and what could not.

Not a test runner. `pytest tests/test_release_acceptance.py` proves the gates
that fit in a test; this walks the ones an operator actually performs — build
inputs, packaging, the container runtime — and produces one table with a status
for every required gate.

Three rules it works under:

* **Every gate gets a status.** `PASS`, `FAIL`, `BLOCKED` or `NOT TESTED`.
  A gate that could not be run is never quietly omitted, and never softened
  into a pass.
* **Nothing is published.** No tag is created or moved, no image is pushed, no
  release is drafted. The point is to find out whether a release *would* be
  safe.
* **Nothing touches production.** Every path it writes to is a temporary
  directory it made itself.

    python scripts/release_acceptance.py
    python scripts/release_acceptance.py --json
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

PASS = "PASS"
FAIL = "FAIL"
BLOCKED = "BLOCKED"
NOT_TESTED = "NOT TESTED"


@dataclass
class Gate:
    """One thing that has to be true before a release is safe."""

    area: str
    name: str
    status: str
    detail: str = ""


@dataclass
class Report:
    gates: list[Gate] = field(default_factory=list)

    def add(self, area: str, name: str, status: str, detail: str = "") -> None:
        self.gates.append(Gate(area, name, status, detail))

    @property
    def blocking(self) -> list[Gate]:
        return [gate for gate in self.gates if gate.status in (FAIL, BLOCKED)]

    @property
    def verdict(self) -> str:
        """READY only when every required gate passed. Never rounded up."""
        if any(gate.status == FAIL for gate in self.gates):
            return "BLOCKED — one or more gates failed"
        if any(gate.status == BLOCKED for gate in self.gates):
            return "BLOCKED — one or more gates could not be run"
        if any(gate.status == NOT_TESTED for gate in self.gates):
            return "BLOCKED — one or more gates were not tested"
        return "READY"


def run(command: list[str], **kwargs) -> subprocess.CompletedProcess:  # noqa: ANN003
    return subprocess.run(  # noqa: S603
        command, capture_output=True, text=True, cwd=ROOT, check=False, **kwargs
    )


# --- A: release identity -------------------------------------------------------


def identity(report: Report) -> dict[str, str]:
    """What this build calls itself, and how it relates to what was released."""
    from librairy import __version__
    from librairy.db import SCHEMA_VERSION

    head = run(["git", "rev-parse", "--short", "HEAD"]).stdout.strip()
    tags = [t for t in run(["git", "tag", "-l"]).stdout.split() if t]
    exact = run(["git", "describe", "--tags", "--exact-match"]).stdout.strip()
    nearest = run(["git", "describe", "--tags", "--abbrev=0"]).stdout.strip()
    ahead = run(["git", "rev-list", f"{nearest}..HEAD", "--count"]).stdout.strip()

    found = {
        "version": __version__,
        "schema": str(SCHEMA_VERSION),
        "head": head,
        "tags": ", ".join(tags) or "(none)",
        "nearest_tag": nearest or "(none)",
        "commits_since_tag": ahead or "?",
        "head_is_released": "yes" if exact else "no",
        "rc_name": f"librairy:rc-{head}" if head else "librairy:rc-unknown",
    }
    #  A version already carried by a tag belongs to the commit that tag names.
    #  Reusing it for a different tree is how two different builds come to
    #  answer `--version` with the same number.
    collision = f"v{__version__}" in tags and not exact
    report.add(
        "BUILD",
        "source version resolved",
        FAIL if collision else PASS,
        f"{__version__} at {head}; nearest tag {found['nearest_tag']}"
        + (
            f"; v{__version__} already names another commit"
            if collision
            else ""
        ),
    )
    report.add(
        "BUILD",
        "no existing tag reused",
        PASS,
        "this pass creates and moves no tag",
    )
    return found


def packaging(report: Report) -> None:
    version = (ROOT / "src/librairy/__init__.py").read_text(encoding="utf-8")
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    single = 'dynamic = ["version"]' in pyproject and "__version__" in version
    report.add(
        "BUILD",
        "one authoritative version source",
        PASS if single else FAIL,
        "pyproject reads librairy.__version__",
    )

    labelled = all(
        key in dockerfile
        for key in (
            "org.opencontainers.image.version",
            "org.opencontainers.image.revision",
            "org.opencontainers.image.source",
        )
    )
    report.add(
        "BUILD",
        "image provenance labels",
        PASS if labelled else FAIL,
        "version, revision and source declared",
    )

    #  Every external artefact the image pulls in, and whether it is pinned to
    #  something that cannot change under it.
    pins = {
        "czkawka": re.search(r"CZKAWKA_CLI_VERSION=([^\s]+)", dockerfile),
        "rclone": re.search(r"RCLONE_VERSION=([^\s]+)", dockerfile),
        "base image": re.search(r"FROM (python:[^\s]+)", dockerfile),
    }
    checksums = dockerfile.count("sha256sum -c -")
    unpinned = [name for name, found in pins.items() if not found]
    report.add(
        "BUILD",
        "external build inputs pinned",
        PASS if not unpinned and checksums >= 2 else FAIL,
        ", ".join(f"{k}={v.group(1)}" for k, v in pins.items() if v)
        + f"; {checksums} checksum verifications",
    )
    #  Debian package versions are not pinned, which is deliberate: the base is
    #  moved forward precisely to pick up security updates. Reported so the
    #  claim being made is the accurate one.
    report.add(
        "BUILD",
        "apt package versions pinned",
        NOT_TESTED,
        "deliberately unpinned — `apt-get upgrade -y` is how the image picks up "
        "security fixes; the base image tag is the pin",
    )


def secrets(report: Report) -> None:
    """Targeted, and it never prints what it finds."""
    suspicious = re.compile(
        r"(sk-[A-Za-z0-9]{32,}|ghp_[A-Za-z0-9]{30,}|AKIA[0-9A-Z]{16}"
        r"|-----BEGIN (?:RSA |OPENSSH )?PRIVATE KEY-----)"
    )
    tracked = run(["git", "ls-files"]).stdout.split()
    offenders = []
    for name in tracked:
        path = ROOT / name
        if not path.is_file() or path.stat().st_size > 2_000_000:
            continue
        try:
            body = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if suspicious.search(body):
            offenders.append(name)
    report.add(
        "BUILD",
        "no credentials in the repository",
        PASS if not offenders else FAIL,
        "scanned every tracked file"
        + (f"; found credential-shaped strings in {len(offenders)} file(s)" if offenders else ""),
    )

    env = (ROOT / ".env.example").read_text(encoding="utf-8")
    live = [
        line.split("=", 1)[0]
        for line in env.splitlines()
        if "=" in line
        and not line.startswith("#")
        and any(word in line.upper() for word in ("KEY", "TOKEN", "SECRET", "PASSWORD"))
        and line.split("=", 1)[1].strip() not in ("", '""')
    ]
    report.add(
        "BUILD",
        "sample configuration ships no secret",
        PASS if not live else FAIL,
        "every credential field in .env.example is empty"
        if not live
        else f"{len(live)} populated credential field(s)",
    )


def changelog(report: Report) -> None:
    """Would the release announcement describe this program?

    `release.yml` publishes `CHANGELOG.md` verbatim as the release body, so
    whatever is in that file is what a reader of the release sees. The check is
    a date rather than a word list: matching against phrases somebody chose
    turns into a game of writing the phrase, while "the notes are older than
    the code they describe" is the thing that is actually wrong.

    An uncommitted change to the file counts as current — somebody is writing
    it right now, which is the state this drill is usually run in.
    """
    dirty = "CHANGELOG.md" in run(["git", "status", "--porcelain"]).stdout
    notes = run(["git", "log", "-1", "--format=%ct", "--", "CHANGELOG.md"]).stdout.strip()
    code = run(["git", "log", "-1", "--format=%ct", "--", "src"]).stdout.strip()
    if not notes or not code:
        report.add(
            "BUILD",
            "changelog describes what would ship",
            NOT_TESTED,
            "no repository history to compare against",
        )
        return
    current = dirty or int(notes) >= int(code)
    report.add(
        "BUILD",
        "changelog describes what would ship",
        PASS if current else FAIL,
        "release body is the whole of CHANGELOG.md"
        + (
            "; being edited now" if dirty and int(notes) < int(code)
            else "; last written after the last source change" if current
            else "; source has moved on since it was last written"
        ),
    )


def compose(report: Report) -> None:
    if shutil.which("docker") is None:
        report.add("BUILD", "compose valid", BLOCKED, "docker not installed")
        return
    result = run(["docker", "compose", "config", "-q"])
    report.add(
        "BUILD",
        "compose valid",
        PASS if result.returncode == 0 else FAIL,
        (result.stderr.strip()[:160] or "docker compose config -q exits 0"),
    )
    body = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    wanted = {
        "restart policy": "restart:" in body,
        "healthcheck": "healthcheck:" in body,
        "four volumes": body.count("/data/") >= 4,
        "port mapping": "ports:" in body,
    }
    missing = [name for name, ok in wanted.items() if not ok]
    report.add(
        "BUILD",
        "compose declares an operable service",
        PASS if not missing else FAIL,
        ", ".join(wanted) if not missing else f"missing: {', '.join(missing)}",
    )


# --- B/C/D: the gates that live in the suite ------------------------------------

SUITE_GATES = {
    "FRESH INSTALL": [
        ("empty database migrates",
         "test_an_empty_database_migrates_to_the_current_head"),
        ("every page renders empty",
         "test_every_page_renders_on_an_empty_installation"),
        ("providers optional",
         "test_a_fresh_install_needs_no_provider_credentials"),
        ("first file end to end",
         "test_the_first_file_goes_all_the_way_through"),
        ("restart persistence",
         "test_restarting_changes_nothing"),
    ],
    "MIGRATION": [
        ("historical databases upgrade",
         "test_a_historical_database_upgrades_without_losing_what_was_decided"),
        ("historical plans untouched",
         "test_an_approved_historical_plan_is_not_touched_by_upgrading"),
        ("search correct after upgrade",
         "test_search_is_correct_after_an_upgrade"),
        ("no gap in the chain",
         "test_the_supported_upgrade_range_is_what_the_migrations_cover"),
        ("newer schema refused",
         "test_a_database_from_the_future_is_refused_rather_than_downgraded"),
        ("failed migration recoverable",
         "test_a_failed_migration_leaves_the_pre_upgrade_copy_usable"),
    ],
    "RECOVERY": [
        ("snapshot is usable",
         "test_a_snapshot_taken_before_an_upgrade_is_a_working_database"),
        ("mismatch explained, never repaired",
         "test_an_imperfect_restore_is_explained_and_never_repaired"),
        ("decisions never discarded",
         "test_recovery_never_discards_what_a_person_decided"),
        ("stale measurement not trusted",
         "test_a_stale_measurement_is_never_attached_to_the_new_bytes"),
    ],
    "ROLLBACK": [
        ("previous build plus its snapshot",
         "test_rolling_back_means_the_previous_build_and_its_own_snapshot"),
        ("docs never advertise the unsafe path",
         "test_the_documented_rollback_never_tells_anybody_to_reuse_the_new_database"),
        ("four words kept apart",
         "test_the_documentation_keeps_the_four_words_apart"),
    ],
}


def suite(report: Report) -> None:
    """Run the acceptance tests and map each gate onto its result."""
    result = run(
        [sys.executable, "-m", "pytest", "tests/test_release_acceptance.py", "-q",
         "--no-header", "-p", "no:cacheprovider", "-rf"]
    )
    failed = set(re.findall(r"FAILED [^:]+::(\w+)", result.stdout))
    ran = result.returncode in (0, 1)
    for area, gates in SUITE_GATES.items():
        for name, test in gates:
            if not ran:
                report.add(area, name, BLOCKED, "the acceptance suite did not run")
            else:
                report.add(area, name, FAIL if test in failed else PASS, test)


# --- E: the container runtime ---------------------------------------------------

RUNTIME_GATES = (
    "production image build",
    "image contains no developer tooling",
    "container start",
    "required binaries present",
    "restart and clean shutdown",
)


def runtime(report: Report) -> None:
    """Everything that needs a container runtime, or an honest BLOCKED.

    Deliberately does not start a daemon. A release gate that turns itself on
    is a release gate that can be satisfied by accident.
    """
    if shutil.which("docker") is None:
        for gate in RUNTIME_GATES:
            report.add("RUNTIME", gate, BLOCKED, "docker not installed")
        return
    available = run(["docker", "ps"]).returncode == 0
    if not available:
        for gate in RUNTIME_GATES:
            report.add("RUNTIME", gate, BLOCKED, "docker daemon unavailable")
        return
    for gate in RUNTIME_GATES:
        report.add("RUNTIME", gate, NOT_TESTED, "run the drill on a host with the daemon up")


# --- quality and documentation ---------------------------------------------------


def quality(report: Report) -> None:
    lint = run([sys.executable, "-m", "ruff", "check", "src", "tests", "scripts"])
    report.add(
        "QUALITY",
        "ruff",
        PASS if lint.returncode == 0 else FAIL,
        lint.stdout.strip().splitlines()[-1] if lint.stdout.strip() else "clean",
    )
    report.add(
        "QUALITY",
        "full test suite",
        NOT_TESTED,
        "run `pytest` separately; this drill runs only the acceptance file",
    )


DOCS = {
    "install": ("docs/operations.md", "## Install"),
    "upgrade": ("docs/operations.md", "## Upgrade"),
    "backup": ("docs/operations.md", "## Backup"),
    "restore": ("docs/operations.md", "## Restore"),
    "reconcile": ("docs/operations.md", "## Reconcile"),
    "rollback": ("docs/operations.md", "## Roll back"),
    "version": ("docs/operations.md", "librairy version"),
}


def documentation(report: Report) -> None:
    for name, (relpath, marker) in DOCS.items():
        path = ROOT / relpath
        present = path.is_file() and marker in path.read_text(encoding="utf-8")
        report.add(
            "DOCUMENTATION",
            name,
            PASS if present else FAIL,
            f"{relpath} — {marker}",
        )


# --- output ----------------------------------------------------------------------


def render(report: Report, found: dict[str, str]) -> None:
    print("RELEASE ACCEPTANCE\n")
    for key, value in found.items():
        print(f"  {key:20} {value}")
    print()
    width = max(len(gate.name) for gate in report.gates) + 2
    area = ""
    for gate in report.gates:
        if gate.area != area:
            area = gate.area
            print(f"\n{area}")
        print(f"  {gate.status:<11} {gate.name:<{width}} {gate.detail}")
    print(f"\nVERDICT: {report.verdict}")
    if report.blocking:
        print("\nNot ready because:")
        for gate in report.blocking:
            print(f"  - [{gate.status}] {gate.area}: {gate.name} — {gate.detail}")
    print("\nNothing was tagged, pushed or published by this drill.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Emit the matrix as JSON")
    args = parser.parse_args(argv)

    report = Report()
    found = identity(report)
    packaging(report)
    secrets(report)
    changelog(report)
    compose(report)
    suite(report)
    runtime(report)
    quality(report)
    documentation(report)

    if args.json:
        print(json.dumps(
            {"identity": found, "verdict": report.verdict,
             "gates": [asdict(gate) for gate in report.gates]},
            indent=2,
        ))
    else:
        render(report, found)
    return 0 if report.verdict == "READY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
