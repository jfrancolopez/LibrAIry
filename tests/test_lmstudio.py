from __future__ import annotations

import json
from pathlib import Path

from librairy.ai.base import ProviderConfig
from librairy.ai.lmstudio import LMStudioProvider, normalize_host
from librairy.ai.registry import configured_providers, set_lmstudio
from librairy.config import Settings
from librairy.db import connect


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _config(host="http://192.168.1.50:1234", enabled=True):
    return ProviderConfig(
        name="lmstudio", kind="lmstudio", endpoint=host,
        model="qwen2.5-7b-instruct", enabled=enabled, is_local=True,
    )


def test_bare_ip_gets_scheme_and_default_port() -> None:
    assert normalize_host("192.168.1.50") == "http://192.168.1.50:1234"
    assert normalize_host("192.168.1.50:5000") == "http://192.168.1.50:5000"
    assert normalize_host("http://box.local:1234/") == "http://box.local:1234"
    assert normalize_host("") == ""


def test_health_lists_loaded_models(monkeypatch) -> None:
    seen = {}

    def fake_urlopen(req, timeout=None):  # noqa: ANN001, ARG001
        seen["url"] = req.full_url
        return _Resp({"data": [{"id": "qwen2.5-7b-instruct"}, {"id": "llama-3.1-8b"}]})

    monkeypatch.setattr("librairy.ai.lmstudio.request.urlopen", fake_urlopen)
    result = LMStudioProvider(_config()).health(5)

    assert result.ok is True
    assert "qwen2.5-7b-instruct" in result.models
    assert seen["url"] == "http://192.168.1.50:1234/v1/models"


def test_health_reports_unreachable_host(monkeypatch) -> None:
    def boom(req, timeout=None):  # noqa: ANN001, ARG001
        raise OSError("Connection refused")

    monkeypatch.setattr("librairy.ai.lmstudio.request.urlopen", boom)
    result = LMStudioProvider(_config()).health(5)

    assert result.ok is False
    assert "refused" in (result.error or "").lower()


def test_classify_posts_to_chat_completions(monkeypatch) -> None:
    seen = {}
    answer = {
        "category": "documents",
        "confidence": 0.88,
        "name_fields": {"title": "Report"},
        "rationale": "text document",
    }

    def fake_urlopen(req, timeout=None):  # noqa: ANN001, ARG001
        seen["url"] = req.full_url
        seen["body"] = json.loads(req.data.decode())
        return _Resp({"choices": [{"message": {"content": json.dumps(answer)}}]})

    monkeypatch.setattr("librairy.ai.lmstudio.request.urlopen", fake_urlopen)
    result = LMStudioProvider(_config()).classify({"file_name": "x.txt"}, 5)

    assert seen["url"].endswith("/v1/chat/completions")
    assert seen["body"]["model"] == "qwen2.5-7b-instruct"
    assert result is not None
    assert result.category == "documents"


def test_registry_registers_lmstudio_as_local_without_a_key(tmp_path: Path) -> None:
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata", LMSTUDIO_HOST="192.168.1.50", _env_file=None
    )
    conn = connect(settings)

    providers = [p for p in configured_providers(conn, settings) if p.kind == "lmstudio"]

    assert len(providers) == 1
    assert providers[0].is_local is True
    assert providers[0].enabled is True


def test_settings_host_overrides_env_without_restart(tmp_path: Path) -> None:
    settings = Settings(APPDATA_DIR=tmp_path / "appdata", LMSTUDIO_HOST="", _env_file=None)
    conn = connect(settings)

    assert [p for p in configured_providers(conn, settings) if p.kind == "lmstudio"] == []

    set_lmstudio(conn, host="10.0.0.7", model="qwen3-8b")
    providers = [p for p in configured_providers(conn, settings) if p.kind == "lmstudio"]

    assert providers[0].endpoint == "http://10.0.0.7:1234"
    assert providers[0].model == "qwen3-8b"


def test_diagnose_maps_a_timeout_to_the_setting_that_causes_it() -> None:
    """LM Studio binds to 127.0.0.1 until told otherwise, so a running server
    on a pingable machine still times out. "timed out" alone sends people
    hunting through their firewall for a problem that is a checkbox."""
    from librairy.ai.lmstudio import diagnose

    assert "Serve on Local Network" in diagnose("timed out")
    assert "Start the server" in diagnose("[Errno 61] Connection refused")
    assert "does not resolve" in diagnose("[Errno 8] nodename nor servname provided")
    assert "No route" in diagnose("[Errno 101] Network is unreachable")
    assert diagnose("something novel")  # never silent


def test_probe_reports_models_for_an_unsaved_address(monkeypatch) -> None:
    from librairy.ai.lmstudio import probe

    calls: list[str] = []

    def fake_urlopen(req, timeout=None):  # noqa: ANN001, ARG001
        calls.append(req.full_url)
        return _Resp({"data": [{"id": "qwen2.5-7b-instruct"}, {"id": "gemma-3-4b"}]})

    monkeypatch.setattr("librairy.ai.lmstudio.request.urlopen", fake_urlopen)
    result = probe("192.168.145.36")

    assert result.ok is True
    assert result.models == ("qwen2.5-7b-instruct", "gemma-3-4b")
    assert calls == ["http://192.168.145.36:1234/v1/models"]


def test_probe_with_no_address_does_not_reach_the_network(monkeypatch) -> None:
    from librairy.ai.lmstudio import probe

    def forbidden(req, timeout=None):  # noqa: ANN001, ARG001
        raise AssertionError("probed with an empty address")

    monkeypatch.setattr("librairy.ai.lmstudio.request.urlopen", forbidden)
    result = probe("   ")

    assert result.ok is False
    assert "No address" in (result.error or "")


def test_embedding_models_are_not_offered_as_chat_models() -> None:
    """LM Studio serves embedding models from /v1/models alongside chat ones.

    They cannot answer /v1/chat/completions at all, so offering one as a choice
    is offering a broken configuration.
    """
    from librairy.ai.lmstudio import is_chat_model

    assert is_chat_model("google/gemma-4-e4b") is True
    assert is_chat_model("qwen/qwen3.5-9b") is True
    assert is_chat_model("text-embedding-nomic-embed-text-v1.5") is False
    assert is_chat_model("bge-reranker-v2-m3") is False
