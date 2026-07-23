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
    assert calls[0][-2:] == ["-x", "jpg,png"]
