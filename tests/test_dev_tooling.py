"""The browser is a tool for building LibrAIry, not a part of it.

`scripts/ui_check.py` drives headless Chrome to look at pages, because DOM
assertions kept passing while the page was visibly wrong. That is worth
keeping. What is not acceptable is any of it reaching the appliance: a
Chromium layer in the image, a browser service in Compose, a runtime import,
or a process still alive after a run.

Every one of those is asserted here rather than promised in a comment. The
tests read the production artifacts with comments stripped, so the prose in
this repo — which talks about Chrome at length — cannot satisfy or break them.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BROWSERS = ("chromium", "chrome", "google-chrome", "playwright", "selenium", "puppeteer")


def uncommented(relpath: str, comment: str = "#") -> str:
    """File contents minus comment lines, lowercased.

    The Dockerfile is allowed to *say* "no browser here". It is not allowed to
    install one, and this is the difference.
    """
    text = (ROOT / relpath).read_text(encoding="utf-8")
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith(comment)
    ).lower()


# --- nothing browser-shaped is in the shipped appliance ------------------------


def test_the_production_image_installs_no_browser() -> None:
    dockerfile = uncommented("Dockerfile")

    for name in BROWSERS:
        assert name not in dockerfile, f"{name} is being installed into the image"


def test_compose_declares_no_browser_service() -> None:
    compose = uncommented("docker-compose.yml")

    for name in BROWSERS:
        assert name not in compose, f"{name} appears as a Compose service"
    # One service. A browser sidecar would be a second.
    assert compose.count("container_name:") == 1


def test_the_runtime_dependencies_contain_no_browser_automation() -> None:
    """A dev extra may carry whatever it likes. `dependencies` may not."""
    import tomllib

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]

    runtime = " ".join(project["dependencies"]).lower()
    for name in (*BROWSERS, "pyppeteer", "splinter"):
        assert name not in runtime, f"{name} is a runtime dependency"


def test_the_wheel_ships_the_package_and_nothing_else() -> None:
    """The harness lives in `scripts/` and `tests/dev/`, neither of which is
    packaged. If that ever changes, a browser tool starts shipping to UNRAID."""
    import tomllib

    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert config["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == ["src/librairy"]


def test_no_application_module_imports_the_harness() -> None:
    offenders = []
    for path in (ROOT / "src" / "librairy").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for needle in ("ui_check", "tests.dev", "from tests", "import tests"):
            if needle in text:
                offenders.append(f"{path.relative_to(ROOT)}: {needle}")

    assert not offenders, offenders


def test_the_application_starts_with_no_browser_on_the_path(tmp_path: Path) -> None:
    """PATH emptied, LIBRAIRY_CHROME unset: the app must not notice."""
    script = textwrap.dedent(
        """
        from librairy.config import Settings
        from librairy.db import connect
        from librairy.web.app import create_app
        import sys
        from pathlib import Path
        root = Path(sys.argv[1])
        settings = Settings(APPDATA_DIR=root / "appdata", INBOX_DIR=root / "inbox",
                            LIBRARY_DIR=root / "library", QUARANTINE_DIR=root / "quarantine",
                            AUTH_REQUIRED=False, _env_file=None)
        for d in (settings.inbox_dir, settings.library_dir, settings.quarantine_dir):
            d.mkdir(parents=True, exist_ok=True)
        create_app(settings, connect(settings))
        print("started")
        """
    )
    env = {
        "PATH": "",
        "HOME": str(tmp_path),
        "PYTHONPATH": str(ROOT / "src"),
        # Settings reads the environment; keep it from finding a real install.
        "APPDATA_DIR": str(tmp_path / "appdata"),
    }
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script, str(tmp_path)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "started" in result.stdout


def test_the_install_docs_do_not_ask_anyone_to_install_a_browser() -> None:
    """Someone following the UNRAID guide must never be told to add Chrome."""
    for relpath in ("docs/install-unraid.md", "docs/install-docker.md", "docs/configuration.md"):
        text = (ROOT / relpath).read_text(encoding="utf-8").lower()
        for name in BROWSERS:
            assert name not in text, f"{relpath} mentions {name}"


def test_the_readme_says_where_the_boundary_is() -> None:
    # Whitespace-normalised: the sentence is allowed to wrap.
    readme = " ".join((ROOT / "README.md").read_text(encoding="utf-8").split())

    assert "development validation tool only" in readme
    assert "not part of LibrAIry production runtime" in readme


def test_no_scheduled_or_background_browser_anywhere() -> None:
    """No cron, no timer, no supervisor entry that starts a browser."""
    for relpath in ("docker-compose.yml", "Dockerfile", "docker-entrypoint.sh"):
        text = uncommented(relpath)
        assert "screenshot" not in text
        assert "ui_check" not in text
    supervisor = (ROOT / "src" / "librairy" / "supervisor.py").read_text(encoding="utf-8")
    assert "chrome" not in supervisor.lower()


# --- and the harness cleans up after itself -----------------------------------


@pytest.fixture
def harness():
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import ui_check

        yield ui_check
    finally:
        sys.path.remove(str(ROOT / "scripts"))


def fake_browser(tmp_path: Path, body: str) -> str:
    """A stand-in that behaves like the real thing: does its job, never exits.

    Chrome on macOS writes the PNG, prints the DOM, and then sits there. The
    harness has to be the one that ends it, so the stand-in has to sit there
    too or the test proves nothing.
    """
    path = tmp_path / "fake-chrome"
    path.write_text(
        "#!/bin/sh\n"
        f"{body}\n"
        "while true; do sleep 1; done\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return str(path)


def test_the_harness_terminates_a_browser_that_will_not_exit(tmp_path: Path, harness) -> None:
    target = tmp_path / "shot.png"
    binary = fake_browser(tmp_path, f'printf pngdata > "{target}"')

    with harness.chrome(binary) as browser:
        browser.timeout = 10
        browser.screenshot("file:///dev/null", target, 100, 100)
        profile = browser.profile

    assert target.read_text() == "pngdata"
    assert not profile.exists(), "the temporary profile survived the run"
    assert not still_running(str(profile)), "a browser process outlived the harness"


def test_the_harness_removes_its_profile_even_when_the_run_fails(tmp_path: Path, harness) -> None:
    # A browser that produces nothing: the harness must still tidy up.
    binary = fake_browser(tmp_path, "true")

    browser = harness.Chrome(binary, timeout=2)
    profile = browser.profile
    try:
        with pytest.raises(RuntimeError):
            browser.screenshot("file:///dev/null", tmp_path / "never.png", 100, 100)
    finally:
        browser.close()

    assert not profile.exists()
    assert not still_running(str(profile))


def test_the_harness_uses_a_temporary_profile_not_the_real_one(tmp_path: Path, harness) -> None:
    binary = fake_browser(tmp_path, "true")

    with harness.chrome(binary) as browser:
        command = browser._command([])
        profile = browser.profile

    assert any(argument.startswith("--user-data-dir=") for argument in command)
    assert str(profile).startswith(_tempdir())
    assert "Chrome" not in str(profile) or str(profile).startswith(_tempdir())


def test_the_harness_fails_cleanly_when_no_browser_is_installed(monkeypatch, harness) -> None:
    monkeypatch.setenv("LIBRAIRY_CHROME", "/nonexistent/chrome")
    monkeypatch.setattr(harness.shutil, "which", lambda _name: None)
    monkeypatch.setattr(harness, "CHROME_CANDIDATES", ())

    with pytest.raises(harness.MissingBrowser) as excinfo:
        harness.find_chrome()

    message = str(excinfo.value)
    assert "development-only" in message
    assert "traceback" not in message.lower()


def test_the_command_exits_two_rather_than_crashing_without_a_browser(monkeypatch, harness) -> None:
    monkeypatch.setattr(harness, "find_chrome", _raise_missing(harness))

    assert harness.main(["review"]) == 2


def test_output_goes_to_a_gitignored_directory(harness) -> None:
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").split()

    assert harness.OUT.is_relative_to(ROOT / ".dev")
    assert ".dev/" in ignored


def test_the_harness_lists_its_pages_without_starting_anything(harness, capsys) -> None:
    assert harness.main(["--list"]) == 0

    assert "review" in capsys.readouterr().out


# --- helpers ------------------------------------------------------------------


def _tempdir() -> str:
    import tempfile

    return tempfile.gettempdir()


def _raise_missing(harness):  # noqa: ANN202
    def raiser(_binary=None):  # noqa: ANN202
        raise harness.MissingBrowser("no browser; this is a development-only tool")

    return raiser


def still_running(marker: str) -> bool:
    """Any process whose command line still mentions this run's profile."""
    if not shutil.which("pgrep"):  # pragma: no cover - unix only
        return False
    found = subprocess.run(  # noqa: S603
        ["pgrep", "-f", marker], capture_output=True, text=True, check=False
    )
    mine = str(os.getpid())
    return any(line.strip() and line.strip() != mine for line in found.stdout.splitlines())
