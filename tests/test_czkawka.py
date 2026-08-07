from __future__ import annotations

import json
import subprocess
from pathlib import Path

from librairy.config import Settings
from librairy.tools.czkawka import parse_similar_media, similar_media


def settings_for(tmp_path: Path) -> Settings:
    return Settings(APPDATA_DIR=tmp_path / "appdata", CZKAWKA_EXTENSIONS="jpg,png", _env_file=None)


def test_parse_czkawka_similarity_groups() -> None:
    groups = parse_similar_media(
        {
            "groups": [
                {
                    "files": [
                        {"path": "/data/inbox/a.jpg", "similarity": 0.92},
                        {"path": "/data/inbox/b.jpg", "similarity": 0.91},
                    ]
                },
                {"files": [{"path": "/data/inbox/single.jpg"}]},
            ]
        }
    )

    assert len(groups) == 1
    assert [file.path for file in groups[0].files] == ["/data/inbox/a.jpg", "/data/inbox/b.jpg"]
    assert groups[0].files[0].score == 0.92


def test_czkawka_extensions_change_invocation(tmp_path: Path, monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:  # noqa: ANN003
        calls.append(command)
        output_path = Path(command[command.index("-C") + 1])
        output_path.write_text(json.dumps([]), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("librairy.tools.czkawka.shutil.which", lambda binary: f"/bin/{binary}")
    monkeypatch.setattr("librairy.tools.czkawka.subprocess.run", fake_run)

    result = similar_media([tmp_path / "inbox"], "image", settings_for(tmp_path))

    assert result.ok is True
    assert "-C" in calls[0]
    # czkawka exits non-zero when it finds matches; without -W every successful
    # detection would surface as a tool failure.
    assert "-W" in calls[0]
    # One -x per extension: czkawka reads a comma-joined list as a single unknown
    # extension, excludes every supported type, and silently reports no groups.
    assert calls[0][-4:] == ["-x", "jpg", "-x", "png"]
    assert "jpg,png" not in calls[0]


def test_every_root_gets_its_own_directory_flag(tmp_path: Path, monkeypatch) -> None:
    """czkawka 11 reads a second path after one -d as a stray positional and
    refuses the command, so the scan never ran once. Same shape as the -x bug
    above; the argv is the only place this can be caught."""
    seen: list[list[str]] = []

    class _Done:
        returncode = 0
        stderr = ""

    def fake_run(command, **kwargs):  # noqa: ANN001, ANN202, ARG001
        seen.append(command)
        Path(command[command.index("-C") + 1]).write_text("[]", encoding="utf-8")
        return _Done()

    monkeypatch.setattr("librairy.tools.czkawka.shutil.which", lambda _name: "/usr/bin/czkawka_cli")
    monkeypatch.setattr("librairy.tools.czkawka.subprocess.run", fake_run)

    similar_media([tmp_path / "inbox", tmp_path / "library"], "image", settings_for(tmp_path))

    command = seen[0]
    assert command.count("-d") == 2
    directories = [command[i + 1] for i, arg in enumerate(command) if arg == "-d"]
    assert directories == [(tmp_path / "inbox").as_posix(), (tmp_path / "library").as_posix()]


def test_the_cache_goes_somewhere_writable_and_survives_a_restart(
    tmp_path: Path, monkeypatch
) -> None:
    """The container drops to PUID:PGID with HOME still pointing at root's home.

    czkawka then panics on a cache it cannot create -- exit 101, empty stderr,
    every cycle. Pointing it at appdata fixes that and keeps the perceptual
    hashes, which are the entire cost of a scan, across `docker compose up`.
    """
    seen: dict[str, str] = {}

    class _Done:
        returncode = 0
        stderr = ""

    def fake_run(command, **kwargs):  # noqa: ANN001, ANN202
        seen.update(kwargs["env"])
        Path(command[command.index("-C") + 1]).write_text("[]", encoding="utf-8")
        return _Done()

    monkeypatch.setattr("librairy.tools.czkawka.shutil.which", lambda _name: "/usr/bin/czkawka_cli")
    monkeypatch.setattr("librairy.tools.czkawka.subprocess.run", fake_run)
    settings = settings_for(tmp_path)

    similar_media([tmp_path / "inbox"], "image", settings)

    cache = settings.appdata_dir / "cache" / "czkawka"
    assert seen["HOME"] == str(cache)
    assert seen["XDG_CACHE_HOME"] == str(cache)
    assert cache.is_dir()


def run_and_capture(tmp_path: Path, monkeypatch, mode: str, **overrides) -> list[str]:
    captured: list[list[str]] = []

    class _Done:
        returncode = 0
        stderr = ""

    def fake_run(command, **kwargs):  # noqa: ANN001, ANN202, ARG001
        captured.append(command)
        Path(command[command.index("-C") + 1]).write_text("[]", encoding="utf-8")
        return _Done()

    monkeypatch.setattr("librairy.tools.czkawka.shutil.which", lambda _name: "/usr/bin/czkawka_cli")
    monkeypatch.setattr("librairy.tools.czkawka.subprocess.run", fake_run)
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata", CZKAWKA_EXTENSIONS="jpg", _env_file=None, **overrides
    )

    similar_media([tmp_path / "inbox"], mode, settings)

    return captured[0]


def test_sensitivity_is_a_word_and_reaches_the_right_flag(tmp_path: Path, monkeypatch) -> None:
    """czkawka scores images 0-40 and videos 0-20, on scales nobody can
    calibrate without running it twice — and the two modes spell the same idea
    with different flags, where the wrong one kills the scan outright."""
    image = run_and_capture(tmp_path, monkeypatch, "image", CZKAWKA_SIMILARITY="balanced")
    video = run_and_capture(tmp_path, monkeypatch, "video", CZKAWKA_SIMILARITY="balanced")

    assert image[image.index("--max-difference") + 1] == "12"
    assert "--tolerance" not in image
    assert video[video.index("--tolerance") + 1] == "10"
    assert "--max-difference" not in video


def test_strict_is_the_default_and_finds_only_identical_looking_files(
    tmp_path: Path, monkeypatch
) -> None:
    """Measured against a real library: at 5 only visually identical files
    group; at 20 eleven unrelated photographs arrive as one pile."""
    command = run_and_capture(tmp_path, monkeypatch, "image")

    assert command[command.index("--max-difference") + 1] == "5"


def test_a_typo_in_the_setting_does_not_stop_the_scan(tmp_path: Path, monkeypatch) -> None:
    """An unknown word is a mistake in a config file, not a reason to give up
    looking for duplicates. czkawka's own default stands in."""
    command = run_and_capture(tmp_path, monkeypatch, "image", CZKAWKA_SIMILARITY="very-fuzzy")

    assert "--max-difference" not in command


def test_exact_duplicate_mode_takes_no_sensitivity(tmp_path: Path, monkeypatch) -> None:
    """"dup" compares bytes. There is nothing to be more or less sure about."""
    command = run_and_capture(tmp_path, monkeypatch, "dup", CZKAWKA_SIMILARITY="loose")

    assert "--max-difference" not in command
    assert "--tolerance" not in command
