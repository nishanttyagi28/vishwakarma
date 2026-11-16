"""Deterministic parallel roadmap execution over a dependency DAG (A-008)."""

from forge.chief import Roadmap, TaskSpec
from forge.ledger import APPROVED
from forge.model.fake import FakeModel
from tests.test_chief import (  # noqa: F401  (fixtures and scripts)
    env,
    pa_script,
    reviewer_script,
    specialist_script,
)


def _factory(_task_id: str):
    """Fresh scripted backends per task, so concurrent scripts never interleave."""
    return {
        "prompt_architect": FakeModel(pa_script()),
        "backend": FakeModel(specialist_script()),
        "qa": FakeModel(reviewer_script("CLEAN")),
    }


def _spec(task_id: str, depends=None) -> TaskSpec:
    return TaskSpec(id=task_id, title=f"task {task_id}", role="backend",
                    depends=depends or [], scope="src/", exclusions="",
                    acceptance="gates pass")


def test_independent_tasks_run_with_two_workers(env):
    chief, ledger, registry, journal, checker, config, models, root, tmp_path = env
    config.limits.max_parallel_workers = 2
    outcomes = chief.run_roadmap(
        Roadmap(tasks=[_spec("P-1"), _spec("P-2")]),
        models_for_task=_factory,
    )
    assert [o.task_id for o in outcomes] == ["P-1", "P-2"]
    assert all(o.status == APPROVED for o in outcomes)
    assert ledger.status_of("P-1") == APPROVED
    assert ledger.status_of("P-2") == APPROVED
    # both workers wrote real transcripts, not a shared fake stream
    assert list((tmp_path / "runs").glob("sp-P-1-*.jsonl"))
    assert list((tmp_path / "runs").glob("sp-P-2-*.jsonl"))


def test_dependent_task_runs_in_the_next_batch(env):
    chief, ledger, registry, journal, checker, config, models, root, tmp_path = env
    config.limits.max_parallel_workers = 2
    outcomes = chief.run_roadmap(
        Roadmap(tasks=[_spec("D-1"), _spec("D-2", depends=["D-1"])]),
        models_for_task=_factory,
    )
    assert [o.task_id for o in outcomes] == ["D-1", "D-2"]
    assert all(o.status == APPROVED for o in outcomes)
    assert ledger.status_of("D-2") == APPROVED


def test_shared_model_backend_falls_back_to_sequential(env):
    chief, ledger, registry, journal, checker, config, models, root, tmp_path = env
    config.limits.max_parallel_workers = 4
    models["prompt_architect"] = FakeModel(pa_script() * 2)
    models["backend"] = FakeModel(specialist_script() * 2)
    models["qa"] = FakeModel(reviewer_script("CLEAN") * 2)
    outcomes = chief.run_roadmap(
        Roadmap(tasks=[_spec("S-1"), _spec("S-2")]),
        models_for_task=None,
    )
    assert [o.task_id for o in outcomes] == ["S-1", "S-2"]
    assert all(o.status == APPROVED for o in outcomes)


def test_worker_count_is_configurable_and_bounded(env):
    chief, ledger, registry, journal, checker, config, models, root, tmp_path = env
    config.limits.max_parallel_workers = 2
    # max_tasks=1 must execute exactly one task even with two workers.
    outcomes = chief.run_roadmap(
        Roadmap(tasks=[_spec("W-1"), _spec("W-2")]),
        max_tasks=1,
        models_for_task=_factory,
    )
    assert [o.task_id for o in outcomes] == ["W-1"]
    assert ledger.status_of("W-2") == "BACKLOG"
