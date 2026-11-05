"""Human-readable alert text. Telegram HTML mode: everything user-controlled is escaped."""

from __future__ import annotations

from html import escape
from typing import Any

_ICONS = {
    "run.started": "▶️",
    "run.succeeded": "✅",
    "run.failed": "❌",
    "task.retrying": "🔁",
    "test": "🔔",
}
MAX_TELEGRAM_CHARS = 4096


def telegram_html(payload: dict[str, Any], *, public_base_url: str) -> str:
    event = str(payload.get("event", ""))
    if event == "test":
        return f"{_ICONS['test']} <b>NuroFlow test notification</b>\nThis channel works."
    workflow = payload.get("workflow") or {}
    run = payload.get("run") or {}
    lines = [
        f"{_ICONS.get(event, '•')} <b>{escape(str(workflow.get('name', 'workflow')))}</b>"
        f" · <code>{escape(event)}</code>",
        f"Run <code>{escape(str(run.get('id', '')))}</code> ({escape(str(run.get('status')))})",
    ]
    task = payload.get("task")
    if task:
        lines.append(
            f"Task <code>{escape(str(task.get('key')))}</code>, attempt {task.get('attempt')}"
        )
    if payload.get("error"):
        lines.append(f"<pre>{escape(str(payload['error'])[:1500])}</pre>")
    lines.append(f"{escape(public_base_url.rstrip('/'))}/api/v1/runs/{escape(str(run.get('id')))}")
    return "\n".join(lines)[:MAX_TELEGRAM_CHARS]
