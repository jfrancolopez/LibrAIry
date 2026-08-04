from __future__ import annotations

import json

from librairy.tools import acoustid


class _Fake:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _opener(payload, calls):
    def opener(request, timeout=None):  # noqa: ANN001, ARG001
        calls.append(request.full_url)
        return _Fake(payload)

    return opener


def setup_function() -> None:
    acoustid.reset_cache()


def _lookup(payload, calls, **kw):
    return acoustid.lookup(
        kw.pop("fingerprint", "AQADtEmi"),
        kw.pop("duration", 355),
        api_key=kw.pop("api_key", "k"),
        opener=_opener(payload, calls),
        sleeper=lambda s: None,
    )


def test_returns_score_and_recording_id_and_caches() -> None:
    calls: list[str] = []
    payload = {
        "status": "ok",
        "results": [{"score": 0.97, "id": "acoustid-uuid", "recordings": [{"id": "mb-uuid"}]}],
    }

    first = _lookup(payload, calls)
    _lookup(payload, calls)

    assert first == {"score": 0.97, "recording_id": "mb-uuid"}
    assert len(calls) == 1, "identical fingerprint should hit the process cache"


def test_only_the_fingerprint_and_duration_leave_the_machine() -> None:
    """Privacy: no filename or path may appear in the request URL."""
    calls: list[str] = []
    payload = {"results": [{"score": 0.9, "recordings": [{"id": "mb-uuid"}]}]}

    _lookup(payload, calls)

    url = calls[0]
    assert "fingerprint=AQADtEmi" in url
    assert "duration=355" in url
    assert "meta=recordings" in url


def test_skips_results_that_carry_no_musicbrainz_recording() -> None:
    """A score with nothing to resolve cannot name the file, so keep looking."""
    calls: list[str] = []
    payload = {
        "results": [
            {"score": 0.99, "id": "no-recordings"},
            {"score": 0.71, "recordings": [{"id": "mb-uuid"}]},
        ]
    }

    assert _lookup(payload, calls) == {"score": 0.71, "recording_id": "mb-uuid"}


def test_unidentified_and_unconfigured_return_none() -> None:
    calls: list[str] = []

    assert _lookup({"status": "ok", "results": []}, calls) is None
    acoustid.reset_cache()
    assert _lookup({}, calls, fingerprint="") is None
    assert _lookup({}, calls, api_key="") is None
    assert _lookup({}, calls, duration=0) is None
    assert calls == [""] or len(calls) == 1, "unconfigured lookups must not hit the network"


def test_network_failure_degrades_to_none() -> None:
    def broken(request, timeout=None):  # noqa: ANN001, ARG001
        raise OSError("acoustid unreachable")

    result = acoustid.lookup(
        "AQADtEmi", 355, api_key="k", opener=broken, sleeper=lambda s: None
    )

    assert result is None


def test_lookup_for_settings_is_none_without_a_key(tmp_path) -> None:
    from librairy.config import Settings

    settings = Settings(APPDATA_DIR=tmp_path / "a", LIBRARY_DIR=tmp_path / "l", _env_file=None)

    assert acoustid.lookup_for_settings(settings) is None


def test_lookup_for_settings_fingerprints_relative_to_the_inbox(tmp_path, monkeypatch) -> None:
    from librairy.config import Settings
    from librairy.tools.common import ToolResult

    seen: list = []

    def fake_fpcalc(path, settings):  # noqa: ANN001, ARG001
        seen.append(path)
        return ToolResult(True, data={"duration": 12, "fingerprint": "FP"})

    monkeypatch.setattr("librairy.tools.fpcalc.fingerprint", fake_fpcalc)
    monkeypatch.setattr(
        "librairy.tools.acoustid.lookup",
        lambda fp, dur, **kw: {"score": 1.0, "recording_id": f"{fp}-{dur}"},  # noqa: ARG005
    )
    settings = Settings(
        APPDATA_DIR=tmp_path / "a",
        LIBRARY_DIR=tmp_path / "l",
        INBOX_DIR=tmp_path / "in",
        ACOUSTID_KEY="k",
        _env_file=None,
    )

    lookup = acoustid.lookup_for_settings(settings)
    result = lookup("sub/track.flac", settings)

    assert seen == [tmp_path / "in" / "sub" / "track.flac"]
    assert result == {"score": 1.0, "recording_id": "FP-12"}


def test_unfingerprintable_file_returns_none(tmp_path, monkeypatch) -> None:
    from librairy.config import Settings
    from librairy.tools.common import ToolResult

    monkeypatch.setattr(
        "librairy.tools.fpcalc.fingerprint",
        lambda path, settings: ToolResult(False, error="missing binary: fpcalc"),  # noqa: ARG005
    )
    settings = Settings(
        APPDATA_DIR=tmp_path / "a",
        LIBRARY_DIR=tmp_path / "l",
        INBOX_DIR=tmp_path / "in",
        ACOUSTID_KEY="k",
        _env_file=None,
    )

    assert acoustid.lookup_for_settings(settings)("track.flac", settings) is None
