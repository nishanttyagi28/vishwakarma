"""Forge Trace (P-001) tests: agent-to-agent handoff observability.

Covers: successful chains, failed handoffs, retries, invalid responses,
secret redaction, interrupted execution + recovery, append-only enforcement,
and full-chain reconstruction from task creation to final approval.
"""
from __future__ import annotations

import sqlite3

import pytest

from forge.chief import Roadmap, TaskSpec
from forge.ledger import KIND_BLOCK, KIND_FAILURE, KIND_RETRY, LedgerError
from forge.model.fake import FakeModel
from tests.test_chief import (  # noqa: F401  (fixture import)
    env,
    pa_script,
    reviewer_script,
    specialist_script,
)


def _kinds(ledger, task_id):
    return [(h.sender, h.receiver, h.kind) for h in ledger.handoffs_for(task_id)]


def test_successful_mission_full_chain_reconstruction(env):
    chief, ledger, *_rest, models, _root, _tmp_path = (
        env[0], env[1], env[2], env[3], env[4], env[5], env[6], env[7], env[8])
    models["prompt_architect"] = FakeModel(pa_script())
    models["backend"] = FakeModel(specialist_script())
    models["qa"] = FakeModel(reviewer_script("CLEAN"))

    outcomes = chief.run_roadmap(Roadmap(tasks=[TaskSpec(
        id="TR-1", title="implement add feature", role="backend",
        scope="src/", exclusions="", acceptance="gates pass")]))
    assert outcomes[0].status == "APPROVED"

    handoffs = ledger.handoffs_for("TR-1")
    kinds = [h.kind for h in handoffs]
    # PA -> specialist dispatch -> report -> gates -> review dispatch -> verdict
    assert kinds[:6] == ["dispatch", "dispatch", "report", "gates",
                         "dispatch", "verdict"]
    # parent-child chain: each handoff links to its actual predecessor
    for prev, nxt in zip(handoffs, handoffs[1:], strict=False):
        assert nxt.parent_id == prev.id

    # "WAS the final result actually verified?": the report handoff must NOT
    # claim verification on text alone; only the gates/verdict records do.
    by_kind = {h.kind: h for h in handoffs}
    assert by_kind["report"].verified is False
    assert by_kind["gates"].verified is True
    assert by_kind["verdict"].verified is True
    assert by_kind["verdict"].detail["verdict"] == "CLEAN"

    # WHO / WHAT context / model / prompt version all present
    dispatch = handoffs[1]
    assert dispatch.receiver == "backend"
    assert dispatch.objective == "implement add feature"
    assert dispatch.context_refs == ["proj:docs/SPEC.md"]
    assert dispatch.prompt_version == 1
    assert dispatch.model == "FakeModel"

    # tool usage captured from the specialist transcript
    assert by_kind["report"].detail["tools"] == {"fs.write": 1}


def test_report_is_never_verified_on_text_alone(env):
    """Gates fail -> report handoff and verdict stay unverified."""
    chief, ledger, registry, journal, checker, config, models, root, tmp_path = env
    models["prompt_architect"] = FakeModel(pa_script())
    # specialist writes to a path no gate exercises? make gate fail instead:
    # qa says ISSUES so the chain never reaches approval.
    models["backend"] = FakeModel(specialist_script())
    models["qa"] = FakeModel(reviewer_script("ISSUES", ["bad docs"]) * 2)

    outcomes = chief.run_roadmap(Roadmap(tasks=[TaskSpec(
        id="TR-2", title="implement add feature", role="backend",
        scope="src/", exclusions="", acceptance="x")]))
    assert outcomes[0].status == "BLOCKED_HUMAN"

    handoffs = ledger.handoffs_for("TR-2")
    verdict = [h for h in handoffs if h.kind == "verdict"][-1]
    assert verdict.verified is False
    assert verdict.detail["verdict"] == "ISSUES"
    failed = ledger.failed_handoffs()
    assert any(h.task_id == "TR-2" and h.kind == KIND_BLOCK for h in failed)


def test_invalid_responses_record_failed_handoff(env):
    chief, ledger, registry, journal, checker, config, models, root, tmp_path = env
    models["prompt_architect"] = FakeModel(["garbage", "garbage", "garbage",
                                            "garbage"])
    models["backend"] = FakeModel(specialist_script())
    models["qa"] = FakeModel(reviewer_script("CLEAN"))

    outcomes = chief.run_roadmap(Roadmap(tasks=[TaskSpec(
        id="TR-3", title="implement add feature", role="backend",
        scope="src/", exclusions="", acceptance="x")]))
    assert outcomes[0].status == "BLOCKED_HUMAN"

    failed = ledger.failed_handoffs()
    pa_failure = [h for h in failed if h.task_id == "TR-3"
                  and h.kind == KIND_FAILURE]
    assert pa_failure, "PA failure must be recorded as a failed handoff"
    assert pa_failure[0].receiver == "prompt_architect"
    assert pa_failure[0].detail["error"], "failure detail must carry the error"


def test_retry_chain_and_parent_linkage(env):
    chief, ledger, registry, journal, checker, config, models, root, tmp_path = env
    models["prompt_architect"] = FakeModel(pa_script())
    models["backend"] = FakeModel(specialist_script() + specialist_script())
    models["qa"] = FakeModel(reviewer_script("ISSUES", ["docstring"])
                             + reviewer_script("CLEAN"))

    outcomes = chief.run_roadmap(Roadmap(tasks=[TaskSpec(
        id="TR-4", title="implement add feature", role="backend",
        scope="src/", exclusions="", acceptance="gates pass")]))
    assert outcomes[0].status == "APPROVED"

    handoffs = ledger.handoffs_for("TR-4")
    retries = [h for h in handoffs if h.kind == KIND_RETRY]
    assert len(retries) == 1
    verdicts = [h for h in handoffs if h.kind == "verdict"]
    assert retries[0].parent_id == verdicts[0].id  # retry follows the verdict
    assert "docstring" in str(retries[0].detail["feedback"])
    # full parent chain stays intact across the revision
    for prev, nxt in zip(handoffs, handoffs[1:], strict=False):
        assert nxt.parent_id == prev.id


def test_secret_redaction_in_handoff_records(env):
    chief, ledger, registry, journal, checker, config, models, root, tmp_path = env
    secret_title = "implement add with token=supersecretvalue123"
    leaky_summary = "done; note password=hunter2hunter2 in config"
    models["prompt_architect"] = FakeModel(pa_script())
    backend_script = specialist_script()
    backend_script[-1] = backend_script[-1].replace(
        '"summary": "implemented add"', f'"summary": "{leaky_summary}"')
    models["backend"] = FakeModel(backend_script)
    models["qa"] = FakeModel(reviewer_script("CLEAN"))

    outcomes = chief.run_roadmap(Roadmap(tasks=[TaskSpec(
        id="TR-5", title=secret_title, role="backend",
        scope="src/", exclusions="", acceptance="x")]))
    assert outcomes[0].status == "APPROVED"

    for h in ledger.handoffs_for("TR-5"):
        blob = str(h.detail) + h.objective
        assert "supersecretvalue123" not in blob
        assert "hunter2hunter2" not in blob
    assert any("[REDACTED]" in str(h.detail) for h in ledger.handoffs_for("TR-5"))


def test_interrupted_execution_recovery_extends_the_chain(env):
    """Budget exhaustion -> BLOCKED_HUMAN -> unblock -> recovery -> success:
    the second mission run's handoffs continue the chain (parent linkage) and
    the task still ends APPROVED."""
    chief, ledger, registry, journal, checker, config, models, root, tmp_path = env
    models["prompt_architect"] = FakeModel(pa_script())
    # first attempt: specialist produces only malformed protocol turns -> the
    # run aborts and the mission escalates. (Revision-budget exhaustion is NOT
    # recoverable by design; a run failure is the recoverable case.)
    models["backend"] = FakeModel(["garbage"] * 5)
    models["qa"] = FakeModel(reviewer_script("CLEAN"))

    outcomes = chief.run_roadmap(Roadmap(tasks=[TaskSpec(
        id="TR-6", title="implement add feature", role="backend",
        scope="src/", exclusions="", acceptance="x")]))
    assert outcomes[0].status == "BLOCKED_HUMAN"
    first_chain_ids = [h.id for h in ledger.handoffs_for("TR-6")]
    assert ledger.failed_handoffs(), "run failure must be a failed handoff"

    ledger.unblock("TR-6", "chief", "bounded retry")
    # fresh scripts for the retry (the first run consumed the old ones)
    models["prompt_architect"] = FakeModel(pa_script())
    models["backend"] = FakeModel(specialist_script())
    models["qa"] = FakeModel(reviewer_script("CLEAN"))
    outcomes = chief.run_roadmap(Roadmap(tasks=[TaskSpec(
        id="TR-6", title="implement add feature", role="backend",
        scope="src/", exclusions="", acceptance="x")]))
    assert outcomes[0].status == "APPROVED"

    handoffs = ledger.handoffs_for("TR-6")
    assert [h.id for h in handoffs][:len(first_chain_ids)] == first_chain_ids
    # the recovered run links back onto the first chain
    recovered = handoffs[len(first_chain_ids):]
    assert recovered[0].parent_id == first_chain_ids[-1]
    verdicts = [h for h in handoffs if h.kind == "verdict"]
    assert verdicts[-1].verified is True


def test_handoffs_are_append_only(env):
    chief, ledger, registry, journal, checker, config, models, root, tmp_path = env
    hid = ledger.record_handoff(
        "TR-X", sender="chief", receiver="backend", kind="dispatch",
        objective="x", status="pending",
    )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        with ledger._conn:
            ledger._conn.execute(
                "UPDATE handoffs SET status='completed' WHERE id=?", (hid,))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        with ledger._conn:
            ledger._conn.execute("DELETE FROM handoffs WHERE id=?", (hid,))
    with pytest.raises(LedgerError, match="parent"):
        ledger.record_handoff(
            "TR-X", sender="a", receiver="b", kind="report", parent_id="h-nope")


def test_trace_cli_reports_chain_and_failures(env, tmp_path):
    chief, ledger, registry, journal, checker, config, models, root, tmp_path = env
    models["prompt_architect"] = FakeModel(pa_script())
    models["backend"] = FakeModel(specialist_script())
    models["qa"] = FakeModel(reviewer_script("CLEAN"))
    chief.run_roadmap(Roadmap(tasks=[TaskSpec(
        id="TR-7", title="implement add feature", role="backend",
        scope="src/", exclusions="", acceptance="x")]))

    # the same ledger file backs the CLI: use an independent TaskLedger view
    from forge.ledger import TaskLedger as TL
    viewer = TL(tmp_path / "forge.sqlite3")
    assert [h.kind for h in viewer.handoffs_for("TR-7")][0] == "dispatch"
    assert viewer.failed_handoffs() == []
    viewer.close()
