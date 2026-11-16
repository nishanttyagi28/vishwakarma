"""Forge UI (P-003/A-009): a read-only local dashboard.

Zero dependencies beyond the stdlib: one module renders the ledger (tasks,
task detail, mission timeline, Forge Trace handoff graph, gate-result diffs,
state transitions, token/cost usage) as static HTML. An ``http.server`` serves
it on 127.0.0.1 only. No writes, no auth surface beyond loopback, no model
calls, no external assets. The page auto-refreshes every few seconds.

CLI:  python -m forge.cli ui [--port N] [--print]
Views:
  /                     all tasks + mission usage
  /?task=TASK           task detail page
  /?mission=MISSION     mission timeline page
"""
from __future__ import annotations

import difflib
import html
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .ledger import HandoffRecord, TaskLedger, TaskRecord

AUTO_REFRESH_SECONDS = 5

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
{refresh_meta}<title>Forge Dashboard</title>
<style>
 body {{ font-family: Consolas, monospace; margin: 2rem; background: #101418;
        color: #d7dde3; }}
 h1, h2, h3 {{ color: #7ec699; }}
 table {{ border-collapse: collapse; margin: 1rem 0; }}
 td, th {{ border: 1px solid #2a3540; padding: 4px 10px; font-size: 0.9rem; }}
 th {{ background: #1b232b; }}
 .ok {{ color: #7ec699; }} .bad {{ color: #e06c75; }} .pend {{ color: #e5c07b; }}
 a {{ color: #61afef; }}
 .wrap {{ max-width: 1200px; }}
 .graph ul {{ list-style: none; border-left: 1px solid #2a3540; margin-left: 1rem; }}
 .graph li {{ margin: 0.25rem 0; }}
 .node {{ color: #d7dde3; font-weight: bold; }}
 pre {{ background: #0b0f13; border: 1px solid #2a3540; padding: 0.75rem;
        overflow-x: auto; }}
 .meta {{ color: #8b9aa8; }}
</style></head>
<body><div class="wrap">
<h1>Forge Dashboard</h1>
<p class="meta">read-only ledger view &middot; loopback only &middot;
ledger: {ledger_path} &middot; auto-refresh {refresh_note} &middot;
<a href="/">all tasks</a></p>
{content}
</div></body></html>"""

_TASK_ROW = ("<tr><td><a href=\"/?task={id}\">{id}</a></td>"
             "<td>{status}</td><td>{role}</td>"
             "<td>{rev}</td><td>{ret}</td><td>{upd}</td></tr>")

_SECTION = """<h2>Forge Trace: {tid} — {title}</h2>
{detail}
{usage}
<h3>Handoff graph</h3>
{graph}
<h3>Handoff timeline</h3>
{handoffs}
<h3>Gate results</h3>
{gates}
<h3>Gate-result diffs</h3>
{diffs}
<h3>State transitions</h3>
{trans}"""

_HANDOFF_HEAD = ("<table><tr><th>#</th><th>at</th><th>kind</th><th>sender</th>"
                 "<th>receiver</th><th>status</th><th>verified</th>"
                 "<th>model</th><th>prompt</th><th>context refs</th>"
                 "<th>detail</th></tr>")
_GATE_LINE = "<li>{sha} &middot; {at}<br><code>{payload}</code></li>"
_TRANS_LINE = "<li>{frm} &rarr; {to} by {actor}: {reason} <i>({at})</i></li>"
_USAGE_HEAD = ("<table><tr><th>mission</th><th>calls</th><th>prompt</th>"
               "<th>completion</th><th>total</th><th>cost USD</th></tr>")
_USAGE_ROW = ("<tr><td>{mission}</td><td>{calls}</td><td>{prompt}</td>"
              "<td>{completion}</td><td>{total}</td><td>{cost:.6f}</td></tr>")
_TASK_USAGE_HEAD = ("<table><tr><th>at</th><th>role</th><th>model</th>"
                    "<th>prompt</th><th>completion</th><th>total</th>"
                    "<th>cost USD</th></tr>")
_TASK_USAGE_ROW = ("<tr><td>{at}</td><td>{role}</td><td>{model}</td>"
                   "<td>{prompt}</td><td>{completion}</td><td>{total}</td>"
                   "<td>{cost:.6f}</td></tr>")
_MISSION_HEAD = ("<table><tr><th>at</th><th>task</th><th>role</th>"
                 "<th>model</th><th>prompt</th><th>completion</th>"
                 "<th>total</th><th>cost USD</th></tr>")
_MISSION_ROW = ("<tr><td>{at}</td><td><a href=\"/?task={task}\">{task}</a></td>"
                "<td>{role}</td><td>{model}</td><td>{prompt}</td>"
                "<td>{completion}</td><td>{total}</td><td>{cost:.6f}</td></tr>")


def _cls(status: str) -> str:
    if status in ("completed", "APPROVED"):
        return "ok"
    if status in ("failed", "blocked", "BLOCKED_HUMAN", "CANCELLED"):
        return "bad"
    return "pend"


def _task_detail(ledger: TaskLedger, task: TaskRecord) -> str:
    deps = ", ".join(task.depends_on) or "-"
    gate_note = "yes" if task.human_gate else "no"
    return (
        '<table class="task-detail">'
        f"<tr><th>id</th><td>{html.escape(task.id)}</td></tr>"
        f"<tr><th>status</th><td>{html.escape(task.status)}</td></tr>"
        f"<tr><th>role</th><td>{html.escape(task.assignee_role)}</td></tr>"
        f"<tr><th>depends on</th><td>{html.escape(deps)}</td></tr>"
        f"<tr><th>human gate</th><td>{gate_note}</td></tr>"
        f"<tr><th>scope</th><td>{html.escape(task.scope)}</td></tr>"
        f"<tr><th>exclusions</th><td>{html.escape(task.exclusions)}</td></tr>"
        f"<tr><th>acceptance</th><td>{html.escape(task.acceptance)}</td></tr>"
        f"<tr><th>revisions / retries</th><td>{task.revisions_used} / "
        f"{task.retries_used}</td></tr>"
        f"<tr><th>updated</th><td>{html.escape(task.updated_at)}</td></tr>"
        "</table>"
    )


def _handoff_graph(handoffs: list[HandoffRecord]) -> str:
    """Render the parent-linked handoff DAG as a self-contained nested list."""
    if not handoffs:
        return "<p>no handoffs recorded</p>"
    children: dict[str | None, list[HandoffRecord]] = {}
    ids = {h.id for h in handoffs}
    for record in handoffs:
        parent = record.parent_id if record.parent_id in ids else None
        children.setdefault(parent, []).append(record)

    def render(record: HandoffRecord) -> str:
        label = (f'<span class="node">{html.escape(record.sender)} &rarr; '
                 f'{html.escape(record.receiver)}</span> '
                 f'{html.escape(record.kind)} '
                 f'<span class="{_cls(record.status)}">'
                 f'{html.escape(record.status)}</span>')
        nested = "".join(render(child) for child in children.get(record.id, []))
        inner = f"<ul>{nested}</ul>" if nested else ""
        return f"<li>{label}{inner}</li>"

    roots = "".join(render(record) for record in children.get(None, []))
    return f'<div class="graph"><ul>{roots}</ul></div>'


def _gate_diffs(ledger: TaskLedger, task_id: str) -> str:
    """Unified diffs between consecutive results for the same gate."""
    previous: dict[str, dict[str, object]] = {}
    blocks: list[str] = []
    for result in ledger.gate_results(task_id):
        try:
            data = json.loads(result.payload)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        name = str(data.get("gate", "?"))
        old = previous.get(name)
        if old is not None:
            old_text = str(old.get("output_tail", ""))
            new_text = str(data.get("output_tail", ""))
            old_state = "PASS" if old.get("passed") else "FAIL"
            new_state = "PASS" if data.get("passed") else "FAIL"
            diff = list(difflib.unified_diff(
                old_text.splitlines(), new_text.splitlines(),
                fromfile=f"{name} ({old_state})", tofile=f"{name} ({new_state})",
                lineterm="",
            ))
            if diff:
                blocks.append(
                    f"<h4>{html.escape(name)}</h4><pre>"
                    f"{html.escape(chr(10).join(diff))}</pre>"
                )
            elif old_state != new_state:
                blocks.append(
                    f"<h4>{html.escape(name)}</h4><p>{old_state} &rarr; "
                    f"{new_state}</p>"
                )
        previous[name] = data
    return "".join(blocks) or "<p>no gate-result changes to diff</p>"


def _task_section(ledger: TaskLedger, task: TaskRecord) -> str:
    handoffs_records = ledger.handoffs_for(task.id)
    rows = "\n".join(
        f'<tr><td>{h.seq}</td><td>{html.escape(h.at)}</td>'
        f'<td>{html.escape(h.kind)}</td><td>{html.escape(h.sender)}</td>'
        f'<td>{html.escape(h.receiver)}</td>'
        f'<td class="{_cls(h.status)}">{html.escape(h.status)}</td>'
        f'<td class="{"ok" if h.verified else "bad"}">{h.verified}</td>'
        f"<td>{html.escape(h.model)}</td>"
        f"<td>{html.escape(h.prompt_id)}v{h.prompt_version}</td>"
        f"<td>{html.escape(', '.join(h.context_refs))}</td>"
        f"<td>{html.escape(str(h.detail)[:220])}</td></tr>"
        for h in handoffs_records
    )
    handoff_html = (_HANDOFF_HEAD + rows + "</table>") if handoffs_records else \
        "<p>no handoffs recorded</p>"

    gates = "".join(
        _GATE_LINE.format(sha=g.payload_sha256[:16], at=html.escape(g.at),
                          payload=html.escape(g.payload[:300]))
        for g in ledger.gate_results(task.id)
    )
    gates_html = f"<ul>{gates}</ul>" if gates else "<p>no gate results</p>"
    trans = "".join(
        _TRANS_LINE.format(frm=html.escape(tr.from_status or "-"),
                           to=html.escape(tr.to_status),
                           actor=html.escape(tr.actor),
                           reason=html.escape(tr.reason),
                           at=html.escape(tr.at))
        for tr in ledger.history(task.id)
    )
    trans_html = f"<ul>{trans}</ul>" if trans else "<p>no transitions</p>"

    usage_rows = "".join(
        _TASK_USAGE_ROW.format(
            at=html.escape(u.at), role=html.escape(u.role),
            model=html.escape(u.model), prompt=u.prompt_tokens,
            completion=u.completion_tokens, total=u.total_tokens,
            cost=u.cost_usd,
        )
        for u in ledger.usage_for_task(task.id)
    )
    usage_html = (f"<h3>Token/cost usage</h3>{_TASK_USAGE_HEAD}{usage_rows}"
                  "</table>") if usage_rows else ""
    return _SECTION.format(
        tid=html.escape(task.id), title=html.escape(task.title),
        detail=_task_detail(ledger, task),
        usage=usage_html,
        graph=_handoff_graph(handoffs_records),
        handoffs=handoff_html, gates=gates_html,
        diffs=_gate_diffs(ledger, task.id), trans=trans_html,
    )


def _mission_rows(ledger: TaskLedger) -> str:
    rows = []
    for mission in ledger.usage_missions():
        summary = ledger.summarize_usage(ledger.usage_for_mission(mission))
        rows.append(_USAGE_ROW.format(
            mission=(f'<a href="/?mission={html.escape(mission, quote=True)}">'
                     f"{html.escape(mission or '-')}</a>"),
            calls=summary.calls,
            prompt=summary.prompt_tokens, completion=summary.completion_tokens,
            total=summary.total_tokens, cost=summary.cost_usd,
        ))
    return ((_USAGE_HEAD + "".join(rows) + "</table>")
            if rows else "<p>no model usage recorded</p>")


def _mission_view(ledger: TaskLedger, mission_id: str) -> str:
    records = ledger.usage_for_mission(mission_id)
    summary = ledger.summarize_usage(records)
    if not records:
        return (f"<h2>Mission: {html.escape(mission_id)}</h2>"
                "<p>no usage recorded for this mission</p>")
    rows = "".join(
        _MISSION_ROW.format(
            at=html.escape(u.at),
            task=html.escape(u.task_id, quote=True),
            role=html.escape(u.role), model=html.escape(u.model),
            prompt=u.prompt_tokens, completion=u.completion_tokens,
            total=u.total_tokens, cost=u.cost_usd,
        )
        for u in records
    )
    tasks = sorted({u.task_id for u in records})
    task_links = ", ".join(
        f'<a href="/?task={html.escape(tid, quote=True)}">{html.escape(tid)}</a>'
        for tid in tasks
    )
    return (
        f"<h2>Mission timeline: {html.escape(mission_id)}</h2>"
        f"<p>{summary.calls} calls &middot; {summary.total_tokens} tokens "
        f"&middot; ${summary.cost_usd:.6f} &middot; tasks: {task_links}</p>"
        f"{_MISSION_HEAD}{rows}</table>"
    )


def build_dashboard_html(ledger: TaskLedger, task_id: str | None = None,
                         mission_id: str | None = None,
                         refresh_s: int = AUTO_REFRESH_SECONDS) -> str:
    """Render the read-only dashboard, task detail or mission timeline."""
    all_tasks = ledger.list_tasks()
    if task_id is not None:
        tasks = [t for t in all_tasks if t.id == task_id]
        content = "".join(_task_section(ledger, t) for t in tasks)
        if not tasks:
            content = (f"<p>no such task: {html.escape(task_id)}</p>"
                       '<p><a href="/">back to all tasks</a></p>')
        else:
            content = ('<p><a href="/">back to all tasks</a></p>' + content)
    elif mission_id is not None:
        tasks = all_tasks
        content = ('<p><a href="/">back to all tasks</a></p>'
                   + _mission_view(ledger, mission_id))
    else:
        tasks = all_tasks
        task_rows = "\n".join(
            _TASK_ROW.format(
                id=html.escape(t.id, quote=True),
                status=f'<span class="{_cls(t.status)}">'
                f"{html.escape(t.status)}</span>",
                role=html.escape(t.assignee_role), rev=t.revisions_used,
                ret=t.retries_used, upd=html.escape(t.updated_at),
            ) for t in tasks
        )
        sections = "".join(_task_section(ledger, t) for t in tasks)
        content = ("<h2>Mission usage</h2>" + _mission_rows(ledger)
                   + "<h2>Tasks</h2><table><tr><th>id</th><th>status</th>"
                   "<th>role</th><th>revisions</th><th>retries</th>"
                   f"<th>updated</th></tr>{task_rows}</table>"
                   + sections)

    if refresh_s > 0:
        refresh_meta = (f'<meta http-equiv="refresh" content="{refresh_s}">')
        refresh_note = f"{refresh_s}s"
    else:
        refresh_meta = ""
        refresh_note = "off"
    return _PAGE.format(
        refresh_meta=refresh_meta,
        refresh_note=refresh_note,
        ledger_path=html.escape(str(ledger.db_path)),
        content=content,
    )


class DashboardServer:
    """Serve the dashboard on 127.0.0.1. Read-only; GET only."""

    def __init__(self, ledger: TaskLedger, port: int = 0,
                 refresh_s: int = AUTO_REFRESH_SECONDS) -> None:
        self._lock = threading.Lock()
        self.refresh_s = refresh_s
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 (http.server API)
                outer.render(self)

            def log_message(self, *args: object) -> None:
                pass  # keep the terminal quiet

        self.ledger = ledger
        self._serving = False
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)

    @property
    def port(self) -> int:
        return self.httpd.server_address[1]

    def render(self, handler: BaseHTTPRequestHandler) -> None:
        query = parse_qs(urlparse(handler.path).query)
        task_values = query.get("task")
        mission_values = query.get("mission")
        task_id = task_values[0] if task_values else None
        mission_id = mission_values[0] if mission_values else None
        with self._lock:  # serialize cross-thread ledger reads
            page = build_dashboard_html(self.ledger, task_id, mission_id,
                                        self.refresh_s)
        body = page.encode("utf-8")
        handler.send_response(200)
        handler.send_header("Content-Type", "text/html; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    def serve_forever(self) -> None:
        self._serving = True
        try:
            self.httpd.serve_forever()
        finally:
            self._serving = False

    def start_background(self) -> threading.Thread:
        t = threading.Thread(target=self.serve_forever, daemon=True)
        t.start()
        return t

    def close(self) -> None:
        if self._serving:
            self.httpd.shutdown()
        self.httpd.server_close()
