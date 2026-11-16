"""Forge UI (P-003) tests: read-only dashboard rendering and serving."""
from __future__ import annotations

import json
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


def test_task_detail_page_has_metadata_graph_and_gate_diffs(env):
    ledger = _seeded_ledger(env)
    ledger.create("G-1", "gate diff task", "backend", scope="src/",
                  acceptance="diff visible")
    ledger.promote("G-1")
    ledger.claim("G-1", "run-g")
    ledger.submit_for_review("G-1", "backend", "done")
    ledger.request_gates("G-1")
    ledger.record_gates("G-1", json.dumps({
        "gate": "pytest", "passed": False, "output_tail": "old failure"}))
    ledger.record_gates("G-1", json.dumps({
        "gate": "pytest", "passed": True, "output_tail": "new success"}))
    page = build_dashboard_html(ledger, "G-1")
    assert "gate diff task" in page
    assert "acceptance" in page and "diff visible" in page
    assert "Gate-result diffs" in page
    assert "old failure" in page and "new success" in page
    assert "--- pytest (FAIL)" in page and "+++ pytest (PASS)" in page
    graph_page = build_dashboard_html(ledger, "U-1")
    assert 'class="graph"' in graph_page


def test_mission_timeline_page_links_tasks(env):
    ledger = _seeded_ledger(env)
    ledger.record_usage("U-1", role="backend", model="m", mission_id="m-ui",
                        prompt_tokens=100, completion_tokens=25,
                        cost_usd=0.0001)
    page = build_dashboard_html(ledger, mission_id="m-ui")
    assert "Mission timeline: m-ui" in page
    assert 'href="/?task=U-1"' in page
    assert "125" in page


def test_mission_timeline_unknown_is_safe(env):
    ledger = _seeded_ledger(env)
    page = build_dashboard_html(ledger, mission_id="<script>x</script>")
    assert "no usage recorded" in page
    assert "<script>x</script>" not in page


def test_dashboard_auto_refreshes_and_is_loopback_only(env):
    ledger = _seeded_ledger(env)
    page = build_dashboard_html(ledger)
    assert 'http-equiv="refresh"' in page
    assert "auto-refresh 5s" in page
    server = DashboardServer(ledger, port=0)
    try:
        assert server.httpd.server_address[0] == "127.0.0.1"
    finally:
        server.close()
