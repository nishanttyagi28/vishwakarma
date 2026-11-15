"""Tests for the Forge model adapters, tool executor and agent runtime.

All agent tests run against FakeModel with temporary workspaces: fast,
deterministic, no network, no Ollama required. OllamaBackend is tested
against an httpx MockTransport.
"""

import json

import httpx
import pytest

from forge.model.base import ModelBackendError
from forge.model.fake import FakeModel
from forge.model.ollama_backend import OllamaBackend
from forge.permissions import FsGrants, PermissionChecker, RoleGrant
from forge.runtime.agent import (
    COMPLETED,
    FAILED_BUDGET,
    FAILED_MODEL,
    FAILED_MODEL_ERROR,
    FAILED_PERMISSION,
    AgentRuntime,
)
from forge.runtime.tools import ToolExecutor

WS = "w"


def grant(role="worker", write=("src/**",), shell=("git status",)) -> RoleGrant:
    return RoleGrant(
        role=role,
        mission="test mission",
        fs=FsGrants(read=[f"{WS}:"], write=[f"{WS}:{p}" for p in write]),
        shell_allow=list(shell),
        tools=["fs", "shell"],
    )


def runtime(tmp_path, script, role_grant, max_steps=8):
    checker = PermissionChecker({role_grant.role: role_grant}, {WS: str(tmp_path)})
    tools = ToolExecutor(checker, role_grant.role)
    model = FakeModel(script)
    agent = AgentRuntime(
        model=model,
        tools=tools,
        role=role_grant.role,
        mission=role_grant.mission,
        max_steps=max_steps,
        transcript_dir=tmp_path / "runs",
    )
    return agent, model


def turn(action=None, final=None, thought="t"):
    if final is not None:
        return json.dumps({"thought": thought, "final": final})
    return json.dumps({"thought": thought, "action": action})


def report(summary="done", files=None, tests=None):
    return {
        "summary": summary,
        "files_changed": files or [],
        "tests_run": tests or [],
        "self_assessment": "ok",
        "open_issues": [],
    }


# --- happy path ----------------------------------------------------------------


def test_agent_writes_file_and_finalises(tmp_path):
    script = [
        turn(action={"tool": "fs.write", "args": {"path": f"{WS}:src/hello.py",
                                                "content": "print('hi')"}}),
        turn(final=report(files=[f"{WS}:src/hello.py"])),
    ]
    agent, _ = runtime(tmp_path, script, grant())
    result = agent.run("run-1", "write a hello module")
    assert result.status == COMPLETED
    assert result.report is not None
    assert result.report.files_changed == [f"{WS}:src/hello.py"]
    assert (tmp_path / "src" / "hello.py").read_text() == "print('hi')"


def test_agent_can_read_files(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("VALUE = 1", encoding="utf-8")
    script = [
        turn(action={"tool": "fs.read", "args": {"path": f"{WS}:src/a.py"}}),
        turn(final=report()),
    ]
    agent, _ = runtime(tmp_path, script, grant())
    result = agent.run("run-1", "inspect module")
    assert result.status == COMPLETED


def test_agent_can_run_allowlisted_shell(tmp_path):
    script = [
        turn(action={"tool": "shell.run", "args": {"command": "git status"}}),
        turn(final=report()),
    ]
    agent, _ = runtime(tmp_path, script, grant())
    result = agent.run("run-1", "check repo")
    assert result.status == COMPLETED


def test_unknown_tool_returns_observation(tmp_path):
    script = [
        turn(action={"tool": "fs.format_everything", "args": {}}),
        turn(final=report()),
    ]
    agent, _ = runtime(tmp_path, script, grant())
    result = agent.run("run-1", "x")
    assert result.status == COMPLETED  # observed, not fatal; no file harm possible


def test_transcript_records_every_turn(tmp_path):
    script = [
        turn(action={"tool": "fs.write", "args": {"path": f"{WS}:src/x.py", "content": "1"}}),
        turn(final=report()),
    ]
    agent, _ = runtime(tmp_path, script, grant())
    agent.run("run-9", "ctx")
    lines = (tmp_path / "runs" / "run-9.jsonl").read_text(encoding="utf-8").splitlines()
    kinds = [json.loads(line)["type"] for line in lines]
    assert kinds[0] == "run_start"
    assert "model_raw" in kinds and "tool" in kinds and "final" in kinds


# --- protocol robustness ---------------------------------------------------------


def test_malformed_json_is_corrected_once(tmp_path):
    script = [
        "not json at all",
        turn(action={"tool": "fs.write", "args": {"path": f"{WS}:src/ok.py", "content": "1"}}),
        turn(final=report()),
    ]
    agent, model = runtime(tmp_path, script, grant())
    result = agent.run("run-1", "ctx")
    assert result.status == COMPLETED
    assert result.malformed_turns == 1
    # the correction turn must have been appended (message count grows)
    assert model.calls[-1][0] > model.calls[0][0]


def test_persistent_malformed_json_fails_run(tmp_path):
    script = ["oops", "oops again", "still oops", "and oops"]
    agent, _ = runtime(tmp_path, script, grant())
    result = agent.run("run-1", "ctx")
    assert result.status == FAILED_MODEL
    assert "malformed" in result.failure_reason


def test_final_report_validation_failure_fails_run(tmp_path):
    script = [turn(final={"summary": 12345})]  # summary must be a string
    agent, _ = runtime(tmp_path, script, grant())
    result = agent.run("run-1", "ctx")
    assert result.status == FAILED_MODEL
    assert "validation" in result.failure_reason


def test_step_budget_exhaustion_fails_run(tmp_path):
    filler = turn(action={"tool": "fs.read", "args": {"path": f"{WS}:src/a.py"}})
    script = [filler, filler, filler, filler]
    agent, _ = runtime(tmp_path, script, grant(), max_steps=3)
    result = agent.run("run-1", "ctx")
    assert result.status == FAILED_BUDGET
    assert result.steps_used == 3


def test_backend_failure_fails_run_cleanly(tmp_path):
    agent, _ = runtime(tmp_path, [], grant())  # empty script -> RuntimeError
    result = agent.run("run-1", "ctx")
    assert result.status == FAILED_MODEL_ERROR


# --- permission enforcement inside the loop ---------------------------------------


def test_permission_denials_are_observed_then_abort(tmp_path):
    """Write outside the grant: observed twice, aborts on the fourth attempt."""
    attempts = [
        turn(action={"tool": "fs.write", "args": {"path": f"{WS}:forbidden/x.py",
                                                  "content": "1"}})
    ] * 4 + [turn(final=report())]
    agent, _ = runtime(tmp_path, attempts, grant(write=("src/**",)))
    result = agent.run("run-1", "ctx")
    assert result.status == FAILED_PERMISSION
    assert result.denials == 4
    assert not (tmp_path / "forbidden").exists()


def test_denial_resets_on_correction(tmp_path):
    """One denied attempt followed by a legal write completes normally."""
    script = [
        turn(action={"tool": "fs.write", "args": {"path": f"{WS}:forbidden/x.py",
                                                  "content": "1"}}),
        turn(action={"tool": "fs.write", "args": {"path": f"{WS}:src/ok.py", "content": "1"}}),
        turn(final=report()),
    ]
    agent, _ = runtime(tmp_path, script, grant(write=("src/**",)))
    result = agent.run("run-1", "ctx")
    assert result.status == COMPLETED
    assert result.denials == 1
    assert (tmp_path / "src" / "ok.py").exists()


# --- Ollama adapter (mock transport, no live server) -------------------------------


def test_ollama_backend_sends_json_mode_and_parses():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200, json={"message": {"content": "{\"a\": 1}"}, "done": True}
        )

    backend = OllamaBackend(model="m", transport=httpx.MockTransport(handler))
    out = backend.chat([{"role": "user", "content": "hi"}], json_mode=True)
    assert out == '{"a": 1}'
    assert captured["payload"]["format"] == "json"
    assert captured["payload"]["stream"] is False
    assert captured["payload"]["model"] == "m"


def test_ollama_backend_without_json_mode_omits_format():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, json={"message": {"content": "plain"}})

    backend = OllamaBackend(model="m", transport=httpx.MockTransport(handler))
    assert backend.chat([{"role": "user", "content": "hi"}]) == "plain"
    assert "format" not in captured["payload"]


def test_ollama_backend_http_error_raises_backend_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    backend = OllamaBackend(model="m", transport=httpx.MockTransport(handler))
    with pytest.raises(ModelBackendError):
        backend.chat([{"role": "user", "content": "hi"}])


def test_ollama_backend_bad_shape_raises_backend_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": True})

    backend = OllamaBackend(model="m", transport=httpx.MockTransport(handler))
    with pytest.raises(ModelBackendError):
        backend.chat([{"role": "user", "content": "hi"}])


# --- default workspace normalisation (F-007 smoke finding) --------------------


def test_prefixless_path_uses_default_workspace(tmp_path):
    grant_ = grant()
    checker = PermissionChecker({grant_.role: grant_}, {WS: str(tmp_path)})
    tools = ToolExecutor(checker, grant_.role, default_workspace=WS)
    result = tools.execute("fs.write", {"path": "src/hello.py", "content": "x=1"})
    assert result.ok
    assert "w:src/hello.py" in result.output  # observation states the resolved spec
    assert (tmp_path / "src" / "hello.py").read_text() == "x=1"


def test_prefixless_path_without_default_gives_teachable_error(tmp_path):
    grant_ = grant()
    checker = PermissionChecker({grant_.role: grant_}, {WS: str(tmp_path)})
    tools = ToolExecutor(checker, grant_.role)
    result = tools.execute("fs.write", {"path": "src/hello.py", "content": "x=1"})
    assert not result.ok
    assert "workspace:relative/path" in result.output
    assert "src/main.py" in result.output  # example included in the error
    assert not (tmp_path / "src" / "hello.py").exists()


def test_explicit_spec_still_wins(tmp_path):
    grant_ = grant()
    checker = PermissionChecker({grant_.role: grant_}, {WS: str(tmp_path)})
    tools = ToolExecutor(checker, grant_.role, default_workspace=WS)
    result = tools.execute("fs.write", {"path": f"{WS}:src/a.py", "content": "ok"})
    assert result.ok
