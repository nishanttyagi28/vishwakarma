"""Forge UI (P-003): a lightweight read-only local dashboard.

Zero dependencies beyond the stdlib: one module renders the ledger (tasks,
Forge Trace handoff timeline, gate results, state transitions) as static
HTML; an http.server serves it on 127.0.0.1 only. No writes, no auth surface
beyond loopback, no Ollama. RAM-light by construction.

CLI:  python -m forge.cli ui [--port N] [--print]
Per-task view: http://127.0.0.1:8765/?task=TASK
"""
from __future__ import annotations

import html
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .ledger import TaskLedger

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Forge Dashboard</title>
<style>
 body {{ font-family: Consolas, monospace; margin: 2rem; background: #101418;
        color: #d7dde3; }}
 h1, h2 {{ color: #7ec699; }}
 table {{ border-collapse: collapse; margin: 1rem 0; }}
 td, th {{ border: 1px solid #2a3540; padding: 4px 10px; font-size: 0.9rem; }}
 th {{ background: #1b232b; }}
 .ok {{ color: #7ec699; }} .bad {{ color: #e06c75; }} .pend {{ color: #e5c07b; }}
 a {{ color: #61afef; }}
 .wrap {{ max-width: 1100px; }}
</style></head>
<body><div class="wrap">
<h1>Forge Dashboard</h1>
<p>read-only ledger view &middot; ledger: {ledger_path}</p>
<h2>Tasks</h2>
<table><tr><th>id</th><th>status</th><th>role</th><th>revisions</th>
<th>retries</th><th>updated</th><th></th></tr>
{task_rows}
</table>
{sections}
</div></body></html>"""

_TASK_ROW = ("<tr><td>{id}</td><td>{status}</td><td>{role}</td>"
             "<td>{rev}</td><td>{ret}</td><td>{upd}</td>"
             '<td><a href="/?task={id}">trace</a></td></tr>')

_SECTION = "<h2>Forge Trace: {tid} — {title}</h2>\n{handoffs}\n{gates}\n{trans}"

_HANDOFF_HEAD = ("<table><tr><th>#</th><th>at</th><th>kind</th><th>sender</th>"
                 "<th>receiver</th><th>status</th><th>verified</th>"
                 "<th>model</th><th>prompt</th><th>context refs</th>"
                 "<th>detail</th></tr>")
_GATE_LINE = "<li>{sha} &middot; {at}<br><code>{payload}</code></li>"
_TRANS_LINE = "<li>{frm} &rarr; {to} by {actor}: {reason} <i>({at})</i></li>"


def _cls(status: str) -> str:
    if status in ("completed", "APPROVED"):
        return "ok"
    if status in ("failed", "blocked", "BLOCKED_HUMAN", "CANCELLED"):
        return "bad"
    return "pend"


def build_dashboard_html(ledger: TaskLedger, task_id: str | None = None) -> str:
    """Render the read-only dashboard, optionally scoped to one task.

    Pure function of the ledger: no writes, no external requests.
    """
    all_tasks = ledger.list_tasks()
    tasks = all_tasks
    if task_id is not None:
        tasks = [t for t in all_tasks if t.id == task_id]
    task_rows = "\n".join(
        _TASK_ROW.format(
            id=html.escape(t.id), status=f'<span class="{_cls(t.status)}">'
            f"{html.escape(t.status)}</span>",
            role=html.escape(t.assignee_role), rev=t.revisions_used,
            ret=t.retries_used, upd=html.escape(t.updated_at),
        ) for t in tasks
    )
    sections = ""
    for t in tasks:
        handoffs = ledger.handoffs_for(t.id)
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
            for h in handoffs
        )
        handoff_html = (_HANDOFF_HEAD + rows + "</table>") if handoffs else \
            "<p>no handoffs recorded</p>"

        gates = "".join(
            _GATE_LINE.format(sha=g.payload_sha256[:16], at=html.escape(g.at),
                              payload=html.escape(g.payload[:200]))
            for g in ledger.gate_results(t.id)
        )
        gates_html = f"<h3>Gate results</h3><ul>{gates}</ul>" if gates else ""
        trans = "".join(
            _TRANS_LINE.format(frm=html.escape(tr.from_status or "-"),
                               to=html.escape(tr.to_status),
                               actor=html.escape(tr.actor),
                               reason=html.escape(tr.reason),
                               at=html.escape(tr.at))
            for tr in ledger.history(t.id)
        )
        trans_html = f"<h3>State transitions</h3><ul>{trans}</ul>" if trans else ""
        sections += _SECTION.format(
            tid=html.escape(t.id), title=html.escape(t.title),
            handoffs=handoff_html, gates=gates_html, trans=trans_html,
        )
    if task_id is not None and not tasks:
        sections = (f"<p>no such task: {html.escape(task_id)}</p>"
                    '<p><a href="/">back to all tasks</a></p>')
    elif task_id is not None:
        sections = '<p><a href="/">back to all tasks</a></p>' + sections
    return _PAGE.format(
        ledger_path=html.escape(str(ledger.db_path)),
        task_rows=task_rows, sections=sections,
    )


class DashboardServer:
    """Serve the dashboard on 127.0.0.1. Read-only; GET only."""

    def __init__(self, ledger: TaskLedger, port: int = 0) -> None:
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 (http.server API)
                outer.render(self)

            def log_message(self, *args: object) -> None:
                pass  # keep the terminal quiet

        self.ledger = ledger
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)

    @property
    def port(self) -> int:
        return self.httpd.server_address[1]

    def render(self, handler: BaseHTTPRequestHandler) -> None:
        query = parse_qs(urlparse(handler.path).query)
        task_values = query.get("task")
        task_id = task_values[0] if task_values else None
        with self._lock:  # serialize cross-thread ledger reads
            page = build_dashboard_html(self.ledger, task_id)
        body = page.encode("utf-8")
        handler.send_response(200)
        handler.send_header("Content-Type", "text/html; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    def serve_forever(self) -> None:
        self.httpd.serve_forever()

    def start_background(self) -> threading.Thread:
        t = threading.Thread(target=self.serve_forever, daemon=True)
        t.start()
        return t

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
