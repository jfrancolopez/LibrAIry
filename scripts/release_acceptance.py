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
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
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
    #  Debian package *versions* are deliberately not pinned, and a gate whose
    #  correct answer is "no, on purpose" can never be answered — it sat at
    #  NOT TESTED for ever and kept the verdict from reading anything. So this
    #  states the property that is actually true and actually protects anyone:
    #  the base is pinned to one tag, and the image takes the security updates
    #  published against it since that tag was cut.
    upgraded = dockerfile.count("apt-get upgrade -y")
    base_pinned = bool(pins["base image"])
    report.add(
        "BUILD",
        "operating system takes security updates",
        PASS if upgraded >= 2 and base_pinned else FAIL,
        f"base pinned to {pins['base image'].group(1) if base_pinned else '?'}; "
        f"`apt-get upgrade -y` in {upgraded} stage(s). Package versions are "
        "deliberately unpinned: moving the base forward is how the image clears CVEs",
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
        [sys.executable, "-m", "pytest", "tests/test_release_acceptance.py",
         "tests/test_release_runtime.py", "tests/test_container_packaging.py",
         "tests/test_release.py", "tests/test_undo_busy.py", "-q",
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
    "provenance labels on the image",
    "version reports the built revision",
    "image contains no developer tooling",
    "image contains no repository or fixture data",
    "required binaries present",
    "container start",
    "provider-free startup",
    "database created and sound",
    "core pages served",
    "clean restart preserves state",
    "abrupt restart leaves the database sound",
    "no pending decision runs on startup",
    "clean shutdown",
)

COMPOSE_GATES = (
    "compose up on temporary volumes",
    "healthcheck reports healthy",
    "published port is reachable",
    "every mounted root is writable",
    "compose down leaves the data behind",
)

#  Pages a person reaches without deciding anything. Every one of them has to
#  render on an installation with no credentials and, as it happens, no network.
PAGES = (
    "/healthz", "/dashboard", "/review", "/browse", "/search/results?q=test",
    "/health", "/reconcile", "/history", "/commit", "/quarantine",
    "/delete-queue", "/settings/format-policy",
)

BINARIES = ("ffmpeg", "ffprobe", "exiftool", "pdfinfo", "pdftotext",
            "rclone", "rmlint", "czkawka_cli", "fpcalc", "setpriv", "curl")

DEV_TOOLING = ("google-chrome", "chromium", "chromedriver", "playwright",
               "selenium", "geckodriver", "firefox", "node", "git")

HEALTH_TIMEOUT = 180
CONTAINER = "librairy-acceptance"


def docker(*args: str, timeout: int = 900) -> subprocess.CompletedProcess:
    return run(["docker", *args], timeout=timeout)


def _wait_healthy(name: str) -> str:
    """Poll until the container's own healthcheck settles, or time out."""
    deadline = time.monotonic() + HEALTH_TIMEOUT
    status = "unknown"
    while time.monotonic() < deadline:
        probe = docker("inspect", name, "--format", "{{.State.Health.Status}}", timeout=30)
        status = probe.stdout.strip() or "unknown"
        if status in ("healthy", "unhealthy"):
            return status
        time.sleep(3)
    return status


def _in_container(name: str, script: str) -> subprocess.CompletedProcess:
    return docker("exec", name, "python3", "-c", script, timeout=300)


#  One consistent snapshot: the worker is running while this reads, and two
#  separate counts taken a second apart disagree for reasons that are not a bug.
_DB_CHECK = """
import sqlite3
c = sqlite3.connect('file:/data/appdata/librairy.db?mode=ro', uri=True)
c.execute('BEGIN')
q = lambda sql: c.execute(sql).fetchone()[0]
print('schema', q('PRAGMA user_version'))
print('integrity', q('PRAGMA integrity_check'))
violations = c.execute('PRAGMA foreign_key_check').fetchall()
print('foreign_keys', 'violations' if violations else 'ok')
print('executed', q('SELECT COUNT(*) FROM plan_ops WHERE executed_at IS NOT NULL'))
print('approved', q("SELECT COUNT(*) FROM plans WHERE status='approved'"))
print('history', q('SELECT COUNT(*) FROM history'))
"""


def _fetch_pages(name: str) -> str:
    script = (
        "import urllib.request\n"
        f"for p in {PAGES!r}:\n"
        "    try:\n"
        "        print(p, urllib.request.urlopen('http://127.0.0.1:8080'+p, timeout=30).status)\n"
        "    except Exception as e:\n"
        "        print(p, 'ERR', e)\n"
    )
    return _in_container(name, script).stdout


def runtime(report: Report, found: dict[str, str]) -> None:
    """Build the image and actually run it, on paths this function creates.

    Deliberately does not start a daemon: a release gate that turns itself on
    is a release gate that can be satisfied by accident. If the daemon is down
    every gate here is BLOCKED, and the verdict is not rounded up.
    """
    if shutil.which("docker") is None:
        for gate in (*RUNTIME_GATES, *COMPOSE_GATES):
            report.add("RUNTIME", gate, BLOCKED, "docker not installed")
        return
    if run(["docker", "ps"]).returncode != 0:
        for gate in (*RUNTIME_GATES, *COMPOSE_GATES):
            report.add("RUNTIME", gate, BLOCKED, "docker daemon unavailable")
        return

    tag = found.get("rc_name", "librairy:rc-unknown")
    revision = run(["git", "rev-parse", "HEAD"]).stdout.strip()
    workspace = Path(tempfile.mkdtemp(prefix="librairy-acceptance-"))
    try:
        _runtime_gates(report, tag, revision, workspace)
    finally:
        docker("rm", "-f", CONTAINER, timeout=120)
        shutil.rmtree(workspace, ignore_errors=True)


def _runtime_gates(report: Report, tag: str, revision: str, workspace: Path) -> None:
    started = time.monotonic()
    build = docker("build", "--build-arg", f"LIBRAIRY_REVISION={revision}", "-t", tag, ".")
    if build.returncode != 0:
        tail = (build.stderr or build.stdout).strip().splitlines()[-1:] or ["see output"]
        report.add("RUNTIME", "production image build", FAIL, tail[0])
        for gate in RUNTIME_GATES[1:] + COMPOSE_GATES:
            report.add("RUNTIME", gate, BLOCKED, "no image to test")
        return
    size = docker("image", "inspect", tag, "--format", "{{.Size}}").stdout.strip()
    megabytes = int(size) // 1_000_000 if size.isdigit() else "?"
    report.add("RUNTIME", "production image build", PASS,
               f"{tag} · {megabytes} MB · {time.monotonic() - started:.0f}s")

    labels = docker("image", "inspect", tag, "--format", "{{json .Config.Labels}}").stdout
    stamped = json.loads(labels or "{}") or {}
    wanted = {
        "org.opencontainers.image.revision": revision,
        "org.opencontainers.image.source": "https://github.com/jfrancolopez/LibrAIry",
    }
    wrong = {k: stamped.get(k) for k, v in wanted.items() if stamped.get(k) != v}
    report.add("RUNTIME", "provenance labels on the image",
               PASS if not wrong else FAIL,
               f"revision={stamped.get('org.opencontainers.image.revision', '')[:12]} "
               f"version={stamped.get('org.opencontainers.image.version', '?')}"
               + (f"; wrong: {wrong}" if wrong else ""))

    said = docker("run", "--rm", "--network", "none", tag, "librairy", "version").stdout
    report.add("RUNTIME", "version reports the built revision",
               PASS if revision in said else FAIL,
               " · ".join(line.strip() for line in said.strip().splitlines()))

    sweep = docker("run", "--rm", "--entrypoint", "sh", tag, "-c",
                   "for b in " + " ".join(DEV_TOOLING) + "; do command -v $b; done; true")
    report.add("RUNTIME", "image contains no developer tooling",
               PASS if not sweep.stdout.strip() else FAIL,
               "none of " + ", ".join(DEV_TOOLING[:4]) + ", …"
               if not sweep.stdout.strip() else sweep.stdout.strip())

    leaks = docker("run", "--rm", "--entrypoint", "sh", tag, "-c",
                   "find / -xdev \\( -name .git -o -name .env -o -name 'id_rsa' "
                   "-o -name '*.sqlite3' -o -iname '*inbox-test*' -o -iname '*library-test*' "
                   "-o -path '/app/tests' -o -path '/app/scripts' \\) 2>/dev/null; true")
    report.add("RUNTIME", "image contains no repository or fixture data",
               PASS if not leaks.stdout.strip() else FAIL,
               "no .git, .env, keys, fixtures or developer database"
               if not leaks.stdout.strip() else leaks.stdout.strip()[:200])

    probe = docker("run", "--rm", "--entrypoint", "sh", tag, "-c",
                   "for b in " + " ".join(BINARIES) + "; do command -v $b >/dev/null "
                   "|| echo MISSING $b; done; true")
    report.add("RUNTIME", "required binaries present",
               PASS if not probe.stdout.strip() else FAIL,
               f"{len(BINARIES)} checked: " + ", ".join(BINARIES[:5]) + ", …"
               if not probe.stdout.strip() else probe.stdout.strip())

    for name in ("appdata", "inbox", "library", "quarantine"):
        (workspace / name).mkdir(parents=True, exist_ok=True)
    (workspace / "inbox" / "acceptance.txt").write_text("release acceptance\n", encoding="utf-8")

    docker("rm", "-f", CONTAINER, timeout=120)
    launch = docker(
        "run", "-d", "--name", CONTAINER, "--user", "0:0",
        "-e", f"PUID={os.getuid()}", "-e", f"PGID={os.getgid()}",
        *[a for name in ("appdata", "inbox", "library", "quarantine")
          for a in ("-v", f"{workspace / name}:/data/{name}")],
        tag,
    )
    if launch.returncode != 0:
        report.add("RUNTIME", "container start", FAIL, launch.stderr.strip()[:160])
        for gate in RUNTIME_GATES[7:] + COMPOSE_GATES:
            report.add("RUNTIME", gate, BLOCKED, "container did not start")
        return
    health = _wait_healthy(CONTAINER)
    report.add("RUNTIME", "container start", PASS if health == "healthy" else FAIL,
               f"healthcheck: {health}; roots under {workspace}")

    logs = docker("logs", CONTAINER, timeout=120)
    output = logs.stdout + logs.stderr
    report.add("RUNTIME", "provider-free startup",
               PASS if "Traceback" not in output else FAIL,
               "no credentials supplied; no traceback"
               if "Traceback" not in output else "traceback during startup")

    checked = _in_container(CONTAINER, _DB_CHECK).stdout
    facts = dict(line.split(" ", 1) for line in checked.strip().splitlines() if " " in line)
    sound = facts.get("integrity") == "ok" and facts.get("foreign_keys") == "ok"
    report.add("RUNTIME", "database created and sound", PASS if sound else FAIL,
               f"schema {facts.get('schema', '?')}, integrity {facts.get('integrity', '?')}, "
               f"foreign keys {facts.get('foreign_keys', '?')}")

    served = _fetch_pages(CONTAINER)
    bad = [line for line in served.splitlines() if not line.endswith(" 200")]
    report.add("RUNTIME", "core pages served", PASS if not bad else FAIL,
               f"{len(PAGES)} pages, all 200" if not bad else "; ".join(bad[:3]))

    before = _in_container(CONTAINER, _DB_CHECK).stdout
    docker("restart", CONTAINER, timeout=180)
    health = _wait_healthy(CONTAINER)
    after = _in_container(CONTAINER, _DB_CHECK).stdout
    report.add("RUNTIME", "clean restart preserves state",
               PASS if health == "healthy" and _same(before, after) else FAIL,
               "schema, history and pending decisions unchanged"
               if _same(before, after) else "state changed across a restart")

    docker("kill", CONTAINER, timeout=120)
    docker("start", CONTAINER, timeout=180)
    health = _wait_healthy(CONTAINER)
    killed = _in_container(CONTAINER, _DB_CHECK).stdout
    facts = dict(line.split(" ", 1) for line in killed.strip().splitlines() if " " in line)
    report.add("RUNTIME", "abrupt restart leaves the database sound",
               PASS if facts.get("integrity") == "ok" and health == "healthy" else FAIL,
               f"SIGKILL then start: integrity {facts.get('integrity', '?')}, "
               f"foreign keys {facts.get('foreign_keys', '?')}")
    report.add("RUNTIME", "no pending decision runs on startup",
               PASS if _same(before, killed) else FAIL,
               "nothing executed by restarting alone"
               if _same(before, killed) else "a restart changed what had been carried out")

    stopped = docker("stop", "-t", "30", CONTAINER, timeout=180)
    code = docker("inspect", CONTAINER, "--format", "{{.State.ExitCode}}").stdout.strip()
    report.add("RUNTIME", "clean shutdown",
               PASS if stopped.returncode == 0 and code in ("0", "143") else FAIL,
               f"stop returned {stopped.returncode}, container exited {code}")
    docker("rm", "-f", CONTAINER, timeout=120)

    _compose_gates(report, tag, workspace)


def _same(before: str, after: str) -> bool:
    """Everything the database check reports, minus nothing. Order is stable."""
    return before.strip() == after.strip()


def _compose_gates(report: Report, tag: str, workspace: Path) -> None:
    """The real topology, pointed at throwaway paths. Never production."""
    project = workspace / "compose"
    for name in ("appdata", "inbox", "library", "quarantine"):
        (project / name).mkdir(parents=True, exist_ok=True)
    port = "18099"
    #  A .env of its own, with no credentials in it: this proves wiring, and a
    #  test container has no business holding a provider key.
    (project / ".env").write_text(
        "\n".join([
            f"HOST_INBOX_DIR={project / 'inbox'}",
            f"HOST_LIBRARY_DIR={project / 'library'}",
            f"HOST_QUARANTINE_DIR={project / 'quarantine'}",
            f"HOST_APPDATA_DIR={project / 'appdata'}",
            f"DASHBOARD_PORT={port}",
            f"PUID={os.getuid()}",
            f"PGID={os.getgid()}",
            "",
        ]),
        encoding="utf-8",
    )
    (project / "docker-compose.override.yml").write_text(
        "services:\n"
        "  librairy:\n"
        f"    image: {tag}\n"
        "    container_name: librairy-acceptance-compose\n"
        "    pull_policy: never\n",
        encoding="utf-8",
    )
    base = [
        "compose", "-f", "docker-compose.yml",
        "-f", str(project / "docker-compose.override.yml"),
        "--project-directory", str(project), "--env-file", str(project / ".env"),
    ]
    up = docker(*base, "up", "-d", timeout=900)
    if up.returncode != 0:
        for gate in COMPOSE_GATES:
            report.add("COMPOSE", gate, FAIL, up.stderr.strip().splitlines()[-1:][0][:160]
                       if up.stderr.strip() else "compose up failed")
        return
    report.add("COMPOSE", "compose up on temporary volumes", PASS,
               f"roots under {project}")

    health = _wait_healthy("librairy-acceptance-compose")
    report.add("COMPOSE", "healthcheck reports healthy",
               PASS if health == "healthy" else FAIL, f"docker health: {health}")

    reached = run(["curl", "-fsS", "-m", "20", f"http://127.0.0.1:{port}/healthz"])
    report.add("COMPOSE", "published port is reachable",
               PASS if reached.returncode == 0 else FAIL,
               f"host {port} -> container 8080: {reached.stdout.strip() or 'no answer'}")

    writable = docker(
        "exec", "librairy-acceptance-compose", "sh", "-c",
        "for d in /data/inbox /data/library /data/quarantine /data/appdata; do "
        "touch $d/.probe 2>/dev/null && rm -f $d/.probe || echo UNWRITABLE $d; done; true",
        timeout=120,
    )
    report.add("COMPOSE", "every mounted root is writable",
               PASS if not writable.stdout.strip() else FAIL,
               "all four bind mounts read and write"
               if not writable.stdout.strip() else writable.stdout.strip())

    docker(*base, "down", timeout=300)
    survived = (project / "appdata" / "librairy.db").is_file()
    report.add("COMPOSE", "compose down leaves the data behind",
               PASS if survived else FAIL,
               "the database outlives the container"
               if survived else "appdata did not survive `compose down`")


# --- quality and documentation ---------------------------------------------------


def quality(report: Report) -> None:
    lint = run([sys.executable, "-m", "ruff", "check", "src", "tests", "scripts"])
    report.add(
        "QUALITY",
        "ruff",
        PASS if lint.returncode == 0 else FAIL,
        lint.stdout.strip().splitlines()[-1] if lint.stdout.strip() else "clean",
    )
    whole = run([sys.executable, "-m", "pytest", "-q", "--no-header",
                 "-p", "no:cacheprovider"], timeout=3600)
    counts = re.search(r"(\d+) passed(?:, (\d+) skipped)?", whole.stdout or "")
    report.add(
        "QUALITY",
        "full test suite",
        PASS if whole.returncode == 0 else FAIL,
        counts.group(0) if counts else (whole.stdout or "").strip().splitlines()[-1:][0]
        if (whole.stdout or "").strip() else "pytest produced no summary",
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
    runtime(report, found)
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
