"""rmlint's output has to actually arrive.

Every dedup test injects `rmlint_check`, so the subprocess call itself was
never exercised, and it was wrong: `-o json:-` made rmlint write its report to
a file literally named "-" in the working directory while stdout stayed empty.
The parse failed, the agreed-pairs set came back empty, and every fingerprint
match was recorded as "rmlint disagrees" -- which is the check that has to pass
before a duplicate can be staged. Nothing was ever staged.

Two guards, because the machine running the tests may not have rmlint: the
argv, which is where the bug lived, and a real run wherever the binary exists.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from librairy.config import Settings
from librairy.tools.rmlint import duplicate_path_pairs, duplicates


def settings_for(tmp_path: Path) -> Settings:
    return Settings(APPDATA_DIR=tmp_path / "appdata", _env_file=None)


def test_output_goes_to_stdout_not_to_a_file_called_dash(tmp_path: Path) -> None:
    seen: list[list[str]] = []

    def capture(command, settings):  # noqa: ANN001, ARG001
        seen.append(command)
        from librairy.tools.common import ToolResult

        return ToolResult(True, data=[])

    import librairy.tools.rmlint as module

    original = module.run_json_tool
    module.run_json_tool = capture
    try:
        duplicates([tmp_path / "a"], settings_for(tmp_path))
    finally:
        module.run_json_tool = original

    assert "json:stdout" in seen[0]
    assert "json:-" not in seen[0]


@pytest.mark.skipif(shutil.which("rmlint") is None, reason="rmlint is not installed here")
def test_rmlint_really_pairs_two_identical_files(tmp_path: Path) -> None:
    """The end-to-end check the injected fakes could never make."""
    left = tmp_path / "left.bin"
    right = tmp_path / "nested" / "right.bin"
    right.parent.mkdir()
    payload = b"the same bytes, twice" * 500
    left.write_bytes(payload)
    right.write_bytes(payload)

    result = duplicates([left, right], settings_for(tmp_path))

    assert result.ok, result.error
    pairs = duplicate_path_pairs(result.data or [])
    assert frozenset((left.as_posix(), right.as_posix())) in pairs
    # And the working directory is left as it was found.
    assert not (Path.cwd() / "-").exists()
