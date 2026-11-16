"""Forge UI (P-003) tests: read-only dashboard rendering and serving."""
from __future__ import annotations

import urllib.request

from forge.chief import Roadmap, TaskSpec
from forge.model.fake import FakeModel
from forge.webui import DashboardServer, build_dashboard_html
from tests.test_chief import (  # noqa: F401  (fixture import)
    env,
    pa_script,
    reviewer_script,
    specialist_script,
)


def _seeded_ledger(env):
    chief, ledger, registry, journal, checker, config, models, root, tmp_path = env
    models["prompt_architect"] = FakeModel(pa_script())
    models["backend"] = FakeModel(specialist_script())
    models["qa"] = FakeModel(reviewer_script("CLEAN"))
    chief.run_roadmap(Roadmap(tasks=[TaskSpec(
        id="U-1", title="implement add feature", role="backend",
        scope="src/", exclusions="", acceptance="gates pass")]))
    return ledger


def test_dashboard_shows_tasks_trace_and_gates(env):
    ledger = _seeded_ledger(env)
    page = build_dashboard_html(ledger)
    assert "U-1" in page and "APPROVED" in page
    # forge trace timeline with verification honesty visible
    assert "dispatch" in page and "verdict" in page
    assert "gate_runner" in page and "verified" in page
    # gate result payload + hash stamp present
    assert "{python} -c" in page and len(page) > 2000


def test_dashboard_escapes_hostile_task_titles(env):
    ledger = _seeded_ledger(env)
    ledger.create("U-<script>alert(1)</script>", "t", "backend")
    page = build_dashboard_html(ledger)
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


def test_dashboard_reflects_failed_handoffs(env):
    ledger = _seeded_ledger(env)
    ledger.record_handoff(
        "U-1", sender="backend", receiver="chief", kind="failure",
        status="failed", detail={"error": "run aborted"},
    )
    page = build_dashboard_html(ledger)
    assert "failure" in page and "run aborted" in page


def test_dashboard_served_over_local_http(env):
    ledger = _seeded_ledger(env)
    server = DashboardServer(ledger, port=0)
    try:
        server.start_background()
        with urllib.request.urlopen(
            f"http://127.0.0.1:{server.port}/", timeout=5
        ) as resp:
            body = resp.read().decode("utf-8")
        assert resp.status == 200
        assert "Forge Dashboard" in body and "U-1" in body
    finally:
        server.close()


def test_dashboard_task_view_filters_one_task(env):
    ledger = _seeded_ledger(env)
    ledger.create("OTHER-9", "unrelated task", "backend")
    page = build_dashboard_html(ledger, "U-1")
    assert "U-1" in page
    assert "OTHER-9" not in page


def test_dashboard_task_view_handles_unknown_and_hostile_ids(env):
    ledger = _seeded_ledger(env)
    page = build_dashboard_html(ledger, "<script>alert(1)</script>")
    assert "no such task" in page
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


def test_dashboard_serves_per_task_query(env):
    ledger = _seeded_ledger(env)
    ledger.create("OTHER-9", "unrelated task", "backend")
    server = DashboardServer(ledger, port=0)
    try:
        server.start_background()
        with urllib.request.urlopen(
            f"http://127.0.0.1:{server.port}/?task=U-1", timeout=5
        ) as resp:
            body = resp.read().decode("utf-8")
        assert resp.status == 200
        assert "U-1" in body and "OTHER-9" not in body
    finally:
        server.close()
