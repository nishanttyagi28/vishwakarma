"""Cost/token accounting and the OpenAI-compatible adapter (mocked HTTP only)."""

import json
import sqlite3

import httpx
import pytest

from forge.chief import Roadmap, TaskSpec
from forge.ledger import BLOCKED_HUMAN, LedgerError, TaskLedger
from forge.model.base import ModelBackendError, ModelUsage
from forge.model.fake import FakeModel
from forge.model.openai_backend import OpenAICompatibleBackend
from forge.webui import build_dashboard_html
from tests.test_chief import (  # noqa: F401  (fixtures and scripts)
    env,
    pa_script,
    reviewer_script,
    specialist_script,
)


def test_usage_is_recorded_per_task_and_mission(tmp_path):
    ledger = TaskLedger(tmp_path / "l.db")
    try:
        ledger.create("U-1", "usage task", "backend")
        ledger.record_usage("U-1", role="backend", model="m", mission_id="m-1",
                            prompt_tokens=100, completion_tokens=50,
                            cost_usd=0.002)
        ledger.record_usage("U-1", role="qa", model="m", mission_id="m-1",
                            prompt_tokens=10, completion_tokens=5,
                            cost_usd=0.0001)
        task_usage = ledger.usage_for_task("U-1")
        mission_usage = ledger.usage_for_mission("m-1")
        assert len(task_usage) == 2 and len(mission_usage) == 2
        summary = ledger.summarize_usage(mission_usage)
        assert summary.calls == 2
        assert summary.prompt_tokens == 110
        assert summary.completion_tokens == 55
        assert summary.total_tokens == 165
        assert summary.cost_usd == pytest.approx(0.0021)
        assert ledger.usage_tasks() == ["U-1"]
        assert ledger.usage_missions() == ["m-1"]
    finally:
        ledger.close()


def test_usage_events_are_append_only(tmp_path):
    ledger = TaskLedger(tmp_path / "l.db")
    try:
        ledger.create("U-1", "usage task", "backend")
        ledger.record_usage("U-1", role="backend", prompt_tokens=1)
        conn = sqlite3.connect(ledger.db_path)
        try:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute("UPDATE usage_events SET prompt_tokens = 999")
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute("DELETE FROM usage_events")
        finally:
            conn.close()
    finally:
        ledger.close()


def test_negative_usage_is_refused(tmp_path):
    ledger = TaskLedger(tmp_path / "l.db")
    try:
        ledger.create("U-1", "usage task", "backend")
        with pytest.raises(LedgerError):
            ledger.record_usage("U-1", role="backend", prompt_tokens=-1)
    finally:
        ledger.close()


def _mock_handler(captured: dict[str, object]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("Authorization")
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": '{"ok": true}'}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        })

    return httpx.MockTransport(handler)


def test_openai_backend_uses_env_config_and_records_usage(monkeypatch):
    monkeypatch.setenv("FORGE_OPENAI_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("FORGE_OPENAI_MODEL", "example-chat")
    monkeypatch.setenv("FORGE_OPENAI_API_KEY", "test-key")
    captured: dict[str, object] = {}
    backend = OpenAICompatibleBackend.from_env(
        transport=_mock_handler(captured)
    )
    reply = backend.chat([{"role": "user", "content": "hi"}], json_mode=True)
    assert reply == '{"ok": true}'
    assert captured["url"] == "https://example.test/v1/chat/completions"
    assert captured["auth"] == "Bearer test-key"
    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["model"] == "example-chat"
    assert payload["response_format"] == {"type": "json_object"}
    assert backend.last_usage.prompt_tokens == 100
    assert backend.last_usage.completion_tokens == 20
    assert backend.last_usage.total_tokens == 120


def test_openai_backend_computes_cost_from_rate_env(monkeypatch):
    monkeypatch.setenv("FORGE_OPENAI_MODEL", "m")
    monkeypatch.setenv("FORGE_INPUT_COST_PER_MTOK", "1.0")
    monkeypatch.setenv("FORGE_OUTPUT_COST_PER_MTOK", "2.0")
    captured: dict[str, object] = {}
    backend = OpenAICompatibleBackend.from_env(transport=_mock_handler(captured))
    backend.chat([{"role": "user", "content": "hi"}])
    assert backend.last_usage.cost_usd == pytest.approx((100 * 1.0 + 20 * 2.0) / 1e6)
    assert captured["auth"] is None  # no key configured -> no Authorization header


def test_openai_backend_http_error_is_model_backend_error(monkeypatch):
    monkeypatch.setenv("FORGE_OPENAI_MODEL", "m")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "rate limited"})

    backend = OpenAICompatibleBackend.from_env(transport=httpx.MockTransport(handler))
    with pytest.raises(ModelBackendError):
        backend.chat([{"role": "user", "content": "hi"}])


def test_openai_backend_bad_shape_is_model_backend_error(monkeypatch):
    monkeypatch.setenv("FORGE_OPENAI_MODEL", "m")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": True})

    backend = OpenAICompatibleBackend.from_env(transport=httpx.MockTransport(handler))
    with pytest.raises(ModelBackendError):
        backend.chat([{"role": "user", "content": "hi"}])


def test_budget_exhaustion_blocks_task_and_records_usage(env):
    chief, ledger, registry, journal, checker, config, models, root, tmp_path = env
    config.limits.max_tokens_per_task = 5
    models["prompt_architect"] = FakeModel(pa_script())
    models["backend"] = FakeModel(
        specialist_script(),
        usages=[ModelUsage(10, 5), ModelUsage(10, 5)],
    )
    models["qa"] = FakeModel(reviewer_script("CLEAN"))
    outcomes = chief.run_roadmap(Roadmap(tasks=[TaskSpec(
        id="B-1", title="budgeted task", role="backend",
        scope="src/", exclusions="", acceptance="x",
    )]))
    assert outcomes[0].status == BLOCKED_HUMAN
    usage = ledger.usage_for_task("B-1")
    assert usage, "budget breach must still record the usage that caused it"
    assert sum(u.total_tokens for u in usage) == 30
    assert usage[0].mission_id.startswith("mission-")
    assert any("budget exceeded" in h.detail.get("reason", "")
               for h in ledger.handoffs_for("B-1") if h.kind == "block")


def test_dashboard_shows_task_and_mission_usage(tmp_path):
    ledger = TaskLedger(tmp_path / "l.db")
    try:
        ledger.create("U-1", "usage task", "backend")
        ledger.record_usage("U-1", role="backend", model="m", mission_id="m-1",
                            prompt_tokens=1234, completion_tokens=56,
                            cost_usd=0.000123)
        page = build_dashboard_html(ledger)
        assert "Mission usage" in page
        assert "m-1" in page
        assert "Token/cost usage" in page
        assert "1234" in page and "0.000123" in page
    finally:
        ledger.close()
