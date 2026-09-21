"""Server-rendered observability dashboard views.

The dashboard deliberately stays dependency-free: FastAPI owns the route, SQLite owns
state, and this module owns presentation, escaping, and small progressive-enhancement
interactions for approval actions.
"""
from __future__ import annotations

import html
import json
from datetime import datetime
from typing import Any, Dict, Iterable, List, Mapping, Optional
from urllib.parse import quote

import httpx

import db

DEFAULT_VIEW = "intervention"
VIEW_LABELS = {
    "intervention": "Intervention desk",
    "sentinel": "Sentinel board",
    "docket": "Run docket",
}


def esc(value: Any) -> str:
    """Escape dynamic values before placing them in HTML text or attributes."""
    return html.escape("" if value is None else str(value), quote=True)


def pretty_json(value: Any) -> str:
    try:
        return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def parse_json(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}


def timestamp(value: Any, short: bool = False) -> str:
    if not value:
        return "—"
    raw = str(value)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed.strftime("%H:%M UTC" if short else "%Y-%m-%d · %H:%M UTC")
    except ValueError:
        return raw[:16] if short else raw


def status_meta(status: Any) -> tuple[str, str]:
    normalized = str(status or "UNKNOWN").upper()
    values = {
        "COMPLETED": ("completed", "healthy"),
        "APPROVED": ("approved", "healthy"),
        "RUNNING": ("running", "running"),
        "PLANNING": ("planning", "running"),
        "RETRYING": ("retrying", "running"),
        "WAITING": ("approval pending", "waiting"),
        "WAITING_APPROVAL": ("approval pending", "waiting"),
        "PENDING": ("pending", "waiting"),
        "REVIEW_REQUIRED": ("review required", "danger"),
        "FAILED": ("failed", "danger"),
        "COST_CEILING_EXCEEDED": ("cost ceiling", "danger"),
    }
    return values.get(normalized, (normalized.lower().replace("_", " "), "muted"))


def risk_label(risk_level: Any) -> str:
    normalized = str(risk_level or "MODERATE").upper()
    return {
        "SAFE": "Safe action",
        "MODERATE": "Moderate review",
        "DANGEROUS": "High-risk review",
    }.get(normalized, "Review required")


def role_label(role: Any) -> str:
    return str(role or "No agent run").replace("_", " ").title()


def latest_run_label(task: Mapping[str, Any]) -> str:
    run = task.get("latest_run") or {}
    if not run:
        return "No run record"
    label, _ = status_meta(run.get("status"))
    return f"{role_label(run.get('agent_role'))} · {label}"


def task_cost(task: Mapping[str, Any]) -> float:
    try:
        return float(task.get("task_cost") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def cost_ceiling() -> float:
    try:
        return float(__import__("os").getenv("PER_TASK_COST_CEILING_USD", "2.00"))
    except ValueError:
        return 2.0


def cost_label(value: Any, ceiling: Optional[float] = None) -> str:
    try:
        amount = float(value or 0.0)
    except (TypeError, ValueError):
        amount = 0.0
    if ceiling is None:
        return f"US${amount:.4f}"
    return f"US${amount:.2f} / US${ceiling:.2f}"


def decorate_tasks(tasks: Iterable[Mapping[str, Any]], latest_runs: Mapping[str, Mapping[str, Any]], costs: Mapping[str, float], approvals: Mapping[str, Mapping[str, Any]]) -> List[Dict[str, Any]]:
    decorated: List[Dict[str, Any]] = []
    for row in tasks:
        item = dict(row)
        item["latest_run"] = dict(latest_runs.get(item["id"], {}))
        item["task_cost"] = costs.get(item["id"], 0.0)
        item["approval"] = dict(approvals.get(item["id"], {}))
        decorated.append(item)
    return decorated


def collect_dashboard_data() -> Dict[str, Any]:
    """Collect all dashboard data in one read-only snapshot."""
    conn = db.get_db()
    try:
        task_rows = [dict(row) for row in conn.execute("SELECT * FROM tasks ORDER BY datetime(created_at) DESC").fetchall()]
        run_rows = [dict(row) for row in conn.execute("SELECT * FROM agent_runs ORDER BY datetime(started_at) DESC").fetchall()]
        approval_rows = [
            dict(row)
            for row in conn.execute(
                """
                SELECT a.*, t.title AS task_title, t.status AS task_status,
                       t.created_at AS task_created_at, ar.agent_role
                FROM approvals a
                JOIN tasks t ON t.id = a.task_id
                LEFT JOIN agent_runs ar ON ar.id = a.agent_run_id
                WHERE a.status = 'PENDING'
                ORDER BY datetime(a.created_at) ASC
                """
            ).fetchall()
        ]
        usage_rows = [dict(row) for row in conn.execute("SELECT * FROM model_usage").fetchall()]
        message_rows = [dict(row) for row in conn.execute("SELECT m.*, ar.task_id, ar.agent_role FROM messages m JOIN agent_runs ar ON ar.id = m.agent_run_id ORDER BY datetime(m.created_at) ASC").fetchall()]
    finally:
        conn.close()

    latest_runs: Dict[str, Dict[str, Any]] = {}
    runs_by_task: Dict[str, List[Dict[str, Any]]] = {}
    for run in run_rows:
        runs_by_task.setdefault(run["task_id"], []).append(run)
        latest_runs.setdefault(run["task_id"], run)

    costs: Dict[str, float] = {}
    prompt_tokens = 0
    completion_tokens = 0
    total_cost = 0.0
    for usage in usage_rows:
        task_id = usage["task_id"]
        costs[task_id] = costs.get(task_id, 0.0) + float(usage.get("cost_usd") or 0.0)
        prompt_tokens += int(usage.get("prompt_tokens") or 0)
        completion_tokens += int(usage.get("completion_tokens") or 0)
        total_cost += float(usage.get("cost_usd") or 0.0)

    pending_approvals: Dict[str, Dict[str, Any]] = {}
    for approval_row in approval_rows:
        pending_approvals.setdefault(approval_row["task_id"], approval_row)

    task_by_id = {task["id"]: task for task in task_rows}
    decorated = decorate_tasks(task_rows, latest_runs, costs, pending_approvals)
    active_statuses = {"PENDING", "PLANNING", "RUNNING", "RETRYING", "WAITING"}
    open_tasks = [task for task in decorated if str(task.get("status", "")).upper() != "COMPLETED"]
    open_tasks.sort(
        key=lambda task: (
            {"WAITING": 0, "REVIEW_REQUIRED": 1, "FAILED": 1, "RUNNING": 2, "PLANNING": 3, "RETRYING": 3, "PENDING": 4}.get(str(task.get("status", "")).upper(), 5),
            str(task.get("created_at") or ""),
        )
    )
    recent_tasks = decorated[:10]
    active_tasks = [task for task in decorated if str(task.get("status", "")).upper() in active_statuses and str(task.get("status", "")).upper() != "WAITING"]
    review_tasks = [task for task in decorated if str(task.get("status", "")).upper() in {"FAILED", "REVIEW_REQUIRED"}]
    completed_tasks = [task for task in decorated if str(task.get("status", "")).upper() == "COMPLETED"]

    for task in decorated:
        task["task_row"] = task_by_id.get(task["id"], task)

    return {
        "tasks": decorated,
        "recent_tasks": recent_tasks,
        "open_tasks": open_tasks,
        "active_tasks": active_tasks,
        "review_tasks": review_tasks,
        "completed_tasks": completed_tasks,
        "approvals": approval_rows,
        "latest_runs": latest_runs,
        "runs_by_task": runs_by_task,
        "total_tasks": len(task_rows),
        "completed_count": len(completed_tasks),
        "review_count": len(review_tasks),
        "awaiting_approval": len(approval_rows),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "total_cost": total_cost,
        "ceiling": cost_ceiling(),
    }


async def model_endpoint_reachable() -> bool:
    """Return a factual model endpoint signal without failing the dashboard."""
    try:
        from app import MODEL_ENDPOINT  # Imported lazily to avoid an app/module cycle.

        async with httpx.AsyncClient() as client:
            response = await client.get(f"{MODEL_ENDPOINT}/models", timeout=2.0)
            return response.status_code == 200
    except Exception:
        return False


def normalize_view(view: str) -> str:
    return view if view in VIEW_LABELS else DEFAULT_VIEW


def icon(name: str, extra: str = "") -> str:
    return f'<i aria-hidden="true" class="ti ti-{esc(name)} icon {esc(extra)}"></i>'


def status_mark(status: Any, label: Optional[str] = None, extra: str = "") -> str:
    text, variant = status_meta(status)
    return f'<span class="status status-{variant} {esc(extra)}"><span class="status-mark" aria-hidden="true"></span>{esc(label or text)}</span>'


def view_switcher(current: str) -> str:
    links = []
    for key, label in VIEW_LABELS.items():
        active = " view-link-active" if key == current else ""
        links.append(f'<a class="view-link{active}" href="/v1/dashboard?view={esc(key)}">{esc(label)}</a>')
    return '<nav class="view-switcher" aria-label="Dashboard view"><span class="view-label">View</span>' + "".join(links) + "</nav>"


def task_href(task_id: Any, view: str) -> str:
    return f"/v1/dashboard/tasks/{quote(str(task_id), safe='')}?view={quote(normalize_view(view), safe='')}"


def task_link(task: Mapping[str, Any], view: str, label: Optional[str] = None, extra: str = "") -> str:
    task_id = task.get("id", "")
    text = label if label is not None else task.get("title")
    return f'<a class="task-link {esc(extra)}" href="{esc(task_href(task_id, view))}">{esc(text)}</a>'


def base_head(title: str, view: str, theme: str) -> str:
    return f"""<!doctype html>
<html lang=\"en\">
<head>
<meta charset=\"utf-8\">
<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
<title>{esc(title)}</title>
<link rel=\"stylesheet\" href=\"/v1/dashboard/assets/fonts.css\">
<link rel=\"stylesheet\" href=\"/v1/dashboard/assets/tabler-icons.min.css\">
<style>{BASE_CSS}</style>
</head>
<body class=\"dashboard-shell {esc(theme)}\" data-dashboard-view=\"{esc(view)}\">
"""


BASE_CSS = r"""
:root { color-scheme: dark; }
* { box-sizing: border-box; }
html { background: #101514; }
body { margin: 0; min-width: 320px; }
button, summary, a { -webkit-tap-highlight-color: transparent; }
button, a { font: inherit; }
button { cursor: pointer; }
.icon { width: 1em; height: 1em; display: inline-flex; align-items: center; justify-content: center; line-height: 1; }
.mono { font-family: 'Commit Mono', 'Recursive', ui-monospace, SFMono-Regular, Menlo, monospace; font-variant-numeric: tabular-nums; }
.dashboard-shell { min-height: 100vh; }
.dashboard-shell a { color: inherit; }
.dashboard-shell .focus-ring:focus-visible, .dashboard-shell button:focus-visible, .dashboard-shell summary:focus-visible, .dashboard-shell [tabindex="0"]:focus-visible { outline: 2px solid var(--focus); outline-offset: 3px; }
.dashboard-shell .view-switcher { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; }
.dashboard-shell .view-label { color: var(--muted); font-size: 10px; letter-spacing: .11em; text-transform: uppercase; }
.dashboard-shell .view-link { border: 1px solid transparent; color: var(--muted); font-size: 11px; padding: 5px 8px; text-decoration: none; transition: color .14s ease, border-color .14s ease, background .14s ease; }
.dashboard-shell .view-link:hover { color: var(--ink); border-color: var(--rule); }
.dashboard-shell .view-link-active { color: var(--ink); border-color: var(--rule); background: var(--surface); }
.dashboard-shell .status { align-items: center; display: inline-flex; gap: 7px; white-space: nowrap; }
.dashboard-shell .status-mark { background: currentColor; display: inline-block; flex: 0 0 auto; height: 8px; width: 8px; }
.dashboard-shell .status-healthy { color: var(--healthy); }
.dashboard-shell .status-running { color: var(--running); }
.dashboard-shell .status-waiting { color: var(--waiting); }
.dashboard-shell .status-danger { color: var(--danger); }
.dashboard-shell .status-muted { color: var(--muted); }
.dashboard-shell .status-card { background: var(--surface); border: 1px solid var(--rule); }
.dashboard-shell .approval-card { background: var(--raised); border: 1px solid var(--rule); border-top: 2px solid var(--waiting); }
.dashboard-shell .quiet-note { border: 1px dashed var(--rule); color: var(--muted); }
.dashboard-shell .approval-form button { border: 1px solid transparent; min-height: 40px; padding: 9px 14px; transition: background .14s ease, border-color .14s ease, color .14s ease, filter .14s ease; }
.dashboard-shell .approve-button { background: var(--waiting); color: var(--canvas); font-weight: 700; }
.dashboard-shell .approve-button:hover { filter: brightness(1.08); }
.dashboard-shell .reject-button { background: transparent; border-color: var(--danger) !important; color: var(--danger); }
.dashboard-shell .reject-button:hover { background: color-mix(in srgb, var(--danger) 10%, transparent); color: var(--ink); }
.dashboard-shell .approval-form button:disabled { cursor: wait; opacity: .62; }
.dashboard-shell table { border-collapse: collapse; width: 100%; }
.dashboard-shell th { text-align: left; }
.dashboard-shell .table-scroll { overflow-x: auto; }
.dashboard-shell tbody tr { transition: background .14s ease; }
.dashboard-shell tbody tr:hover { background: color-mix(in srgb, var(--ink) 4%, transparent); }
.dashboard-shell details summary { cursor: pointer; list-style: none; }
.dashboard-shell details summary::-webkit-details-marker { display: none; }
.dashboard-shell details[open] .chevron { transform: rotate(180deg); }
.dashboard-shell .empty-block { border: 1px dashed var(--rule); }
.dashboard-shell .progress-track { background: color-mix(in srgb, var(--rule) 58%, var(--canvas)); height: 5px; }
.dashboard-shell .progress-fill { background: var(--running); height: 100%; }
.dashboard-shell .sr-only { height: 1px; margin: -1px; overflow: hidden; position: absolute; width: 1px; clip: rect(0,0,0,0); }
.dashboard-shell .approval-form { margin: 0; }
.dashboard-shell .approval-feedback { color: var(--muted); font-size: 11px; min-height: 1.2em; }
@media (prefers-reduced-motion: reduce) { *, *::before, *::after { scroll-behavior: auto !important; transition-duration: .01ms !important; } }

/* Shared dashboard layout and the three visual systems. */
.dashboard-shell > .view-switcher { justify-content: flex-end; margin: 0 auto; max-width: 1500px; padding: 10px 32px 0; }
.view-container { margin: 0 auto; max-width: 1500px; padding: 24px 32px 42px; }
.view-container h1, .view-container h2, .view-container h3, .view-container p { margin-top: 0; }
.section-heading { align-items: flex-end; border-bottom: 1px solid var(--rule); display: flex; gap: 16px; justify-content: space-between; padding-bottom: 12px; }
.section-heading h2 { color: var(--ink); font-size: 20px; letter-spacing: -.025em; margin: 4px 0 0; }
.section-heading h2 span { color: var(--muted); font-weight: 400; }
.section-count { color: var(--muted); font-size: 11px; text-align: right; }
.eyebrow { color: var(--muted); font-size: 10px; font-weight: 600; letter-spacing: .15em; line-height: 1.2; margin: 0; text-transform: uppercase; }
.accent-healthy { color: var(--healthy) !important; }
.accent-running { color: var(--running) !important; }
.accent-waiting { color: var(--waiting) !important; }
.accent-danger { color: var(--danger) !important; }
.accent-brass { color: var(--brass) !important; }
.muted-cell { color: var(--muted); }
.quiet-cell { color: var(--quiet, var(--muted)); }
.identity-icon { align-items: center; border: 1px solid var(--focus); color: var(--focus); display: inline-flex; flex: 0 0 auto; height: 32px; justify-content: center; width: 32px; }
.identity { align-items: flex-start; display: flex; gap: 14px; }
.identity-title { align-items: baseline; display: flex; gap: 12px; }
.identity-title h1 { color: var(--ink); font-size: 22px; letter-spacing: -.035em; margin: 0; }
.identity-title span, .identity p, .masthead-health, .checked { color: var(--muted); font-size: 12px; }
.identity p { margin: 4px 0 0; }
.health-summary { align-items: center; display: flex; flex-wrap: wrap; gap: 16px; }
.health-separator { color: var(--rule); }
.masthead-health { align-items: flex-end; display: flex; flex-direction: column; gap: 8px; }
.checked { font-family: 'Commit Mono', monospace; font-size: 10px; }
.intervention-masthead, .sentinel-masthead, .docket-masthead { align-items: flex-start; border-bottom: 1px solid var(--rule); display: flex; gap: 24px; justify-content: space-between; padding-bottom: 20px; }
.intervention-grid { display: grid; gap: 16px; grid-template-columns: minmax(230px, .84fr) minmax(420px, 1.7fr) minmax(230px, .84fr); margin-top: 24px; }
.intervention-grid > * { min-width: 0; }
.attention-panel, .execution-panel, .condition-panel { min-width: 0; }
.approval-card, .execution-card { margin-top: 12px; }
.approval-card { padding: 16px; }
.approval-header, .execution-card-head, .slip-top, .approval-panel-body .section-heading { align-items: flex-start; display: flex; gap: 12px; justify-content: space-between; }
.approval-header h3, .execution-card-head h3, .approval-panel-body h3 { color: var(--ink); font-size: 17px; letter-spacing: -.02em; margin: 0; }
.approval-header p, .execution-card-head p { color: var(--muted); font-size: 11px; margin: 5px 0 0; }
.approval-header .approval-held { border: 1px solid color-mix(in srgb, var(--waiting) 70%, transparent); color: var(--waiting); font-size: 10px; letter-spacing: .11em; padding: 5px 8px; text-transform: uppercase; }
.approval-body, .approval-panel-body { border-top: 1px solid var(--rule); margin-top: 16px; padding-top: 14px; }
.tool-call { align-items: center; display: flex; gap: 8px; }
.tool-call code { color: var(--ink); font-size: 13px; }
.approval-body > p, .approval-panel-body > p { color: var(--muted); font-size: 12px; line-height: 1.55; margin: 8px 0 0; }
.approval-detail-meta { border-bottom: 1px solid var(--rule); border-top: 1px solid var(--rule); display: grid; gap: 8px; grid-template-columns: 1fr; margin-top: 14px; padding: 12px 0; }
.approval-detail-meta span { color: var(--muted); font-size: 11px; overflow: hidden; }
.approval-detail-meta b { color: var(--muted); display: inline-block; font-weight: 400; min-width: 52px; }
.approval-detail-meta code { color: var(--ink); }
.truncate-value { display: inline-block; max-width: calc(100% - 60px); overflow: hidden; text-overflow: ellipsis; vertical-align: bottom; white-space: nowrap; }
.argument-details { border-bottom: 1px solid var(--rule); margin-top: 12px; padding-bottom: 12px; }
.argument-details summary { align-items: center; color: var(--muted); display: flex; font-size: 11px; justify-content: space-between; }
.chevron { transition: transform .14s ease; }
.argument-code { background: var(--surface); color: var(--muted); font-size: 10px; line-height: 1.6; margin: 10px 0 0; overflow-x: auto; padding: 10px; white-space: pre-wrap; }
.approval-actions-compact { margin-top: 12px; }
.approval-form-row { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 14px; }
.approval-feedback { margin: 4px 0 0; }
.wait-meta { align-items: center; color: var(--muted); display: flex; flex-wrap: wrap; font-size: 10px; gap: 6px; margin: 12px 0 0; }
.empty-copy { display: flex; flex-direction: column; gap: 5px; line-height: 1.55; padding: 15px; }
.empty-copy strong { color: var(--ink); font-size: 12px; }
.empty-copy span { color: var(--muted); font-size: 11px; }
.execution-card { background: var(--surface); border: 1px solid var(--rule); }
.execution-card-head { border-bottom: 1px solid var(--rule); padding: 20px; }
.execution-card-head h3 { font-size: 18px; }
.execution-card-head p { align-items: center; display: flex; flex-wrap: wrap; gap: 12px; }
.execution-card-head p .status { color: var(--running); }
.cost-summary { color: var(--ink); font-size: 14px; text-align: right; }
.cost-summary small { color: var(--muted); display: block; font-family: inherit; font-size: 9px; letter-spacing: .1em; margin-top: 5px; text-transform: uppercase; }
.runline { padding: 20px; position: relative; }
.runline::before { background: var(--rule); bottom: 26px; content: ''; left: 29px; position: absolute; top: 24px; width: 1px; }
.runline-row { display: flex; gap: 14px; min-height: 52px; position: relative; }
.runline-row > div { flex: 1; min-width: 0; }
.runline-marker { align-items: center; background: var(--surface); border: 1px solid var(--rule); display: flex; flex: 0 0 auto; height: 18px; justify-content: center; position: relative; width: 18px; z-index: 1; }
.runline-marker .icon { font-size: 10px; }
.runline-complete { background: var(--healthy); border-color: var(--healthy); color: var(--canvas); }
.runline-current { background: var(--surface); border: 2px solid var(--focus); box-shadow: inset 0 0 0 4px var(--running); height: 20px; width: 20px; }
.runline-title { align-items: center; display: flex; justify-content: space-between; }
.runline-title strong { color: var(--ink); font-size: 13px; }
.runline-title span { color: var(--muted); font-size: 10px; }
.runline-row p { color: var(--muted); font-size: 11px; margin: 5px 0 0; }
.runline-row:has(.runline-current) > div { background: color-mix(in srgb, var(--running) 10%, var(--surface)); border: 1px solid color-mix(in srgb, var(--running) 50%, transparent); padding: 10px 12px; }
.evidence-row { border-top: 1px solid var(--rule); display: grid; gap: 10px; grid-template-columns: repeat(3, 1fr); padding: 14px 20px; }
.evidence-row span { border-right: 1px solid var(--rule); display: flex; flex-direction: column; gap: 5px; min-width: 0; padding-right: 10px; }
.evidence-row span:last-child { border-right: 0; }
.evidence-row b { color: var(--muted); font-size: 10px; font-weight: 500; letter-spacing: .12em; text-transform: uppercase; }
.evidence-row em { font-size: 11px; font-style: normal; }
.policy-note { align-items: flex-start; background: var(--surface); border: 1px solid var(--rule); display: flex; gap: 8px; margin-top: 14px; padding: 12px; }
.policy-note > span { display: flex; flex-direction: column; gap: 4px; }
.policy-note strong { color: var(--ink); font-size: 11px; font-weight: 500; }
.policy-note small { color: var(--muted); font-size: 10px; line-height: 1.5; }
.condition-list { border-bottom: 1px solid var(--rule); border-top: 1px solid var(--rule); margin-top: 12px; }
.condition-list > div { align-items: center; border-bottom: 1px solid var(--rule); display: flex; gap: 12px; justify-content: space-between; min-height: 58px; }
.condition-list > div:last-child { border-bottom: 0; }
.condition-list > div > span { color: var(--ink); display: flex; flex-direction: column; font-size: 12px; gap: 3px; min-width: 0; }
.condition-list small { color: var(--muted); font-size: 10px; }
.condition-list > div > .mono { color: var(--ink); font-size: 11px; text-align: right; }
.intervention-ledger, .sentinel-history, .docket-history { margin-top: 32px; }
.history-section table, .sentinel-history table, .docket-history table { min-width: 720px; }
.history-section th, .sentinel-history th, .docket-history th { border-bottom: 1px solid var(--rule); color: var(--muted); font-size: 10px; font-weight: 500; padding: 12px 12px 10px 0; }
.history-section td, .sentinel-history td, .docket-history td { border-bottom: 1px solid var(--rule); font-size: 11px; padding: 13px 12px 13px 0; vertical-align: top; }
.task-title-cell { min-width: 220px; }
.task-title-cell strong { color: var(--ink); display: block; font-size: 12px; font-weight: 600; }
.task-title-cell span { color: var(--muted); display: block; font-size: 10px; margin-top: 4px; }
.table-note { color: var(--muted); font-size: 10px; margin: 13px 0 0; }
.empty-table { color: var(--muted); padding: 26px 0 !important; }
.empty-table code { color: var(--ink); }
.theme-intervention { --canvas: #101514; --surface: #18201F; --raised: #202A28; --rule: #35403D; --ink: #F0F2EA; --muted: #AAB5AD; --healthy: #4FAE78; --running: #4B86C5; --waiting: #D79B38; --danger: #C45C52; --focus: #A4C9B4; --brass: #A4C9B4; background: var(--canvas); color: var(--ink); font-family: 'Recursive', ui-sans-serif, system-ui, sans-serif; }
.theme-intervention .view-switcher { font-family: 'Recursive', sans-serif; }
.theme-sentinel { --canvas: #12141B; --surface: #1C2029; --raised: #242A35; --rule: #3A4350; --ink: #E7E9E5; --muted: #9EA8B5; --quiet: #76818E; --healthy: #78B39A; --running: #6E9ED6; --waiting: #D89A48; --danger: #CA655D; --focus: #C4CCD3; --brass: #C4CCD3; background: var(--canvas); color: var(--ink); font-family: 'Atkinson Hyperlegible Next', system-ui, sans-serif; }
.theme-sentinel .view-switcher, .theme-sentinel .mono { font-family: 'Commit Mono', ui-monospace, monospace; }
.theme-sentinel .view-container { max-width: 1500px; padding-top: 24px; }
.sentinel-masthead h1, .docket-masthead h1 { color: var(--ink); font-size: 28px; letter-spacing: -.025em; margin: 8px 0 0; }
.sentinel-masthead p, .docket-masthead p { color: var(--muted); font-size: 13px; margin: 5px 0 0; }
.freshness, .docket-health { align-items: flex-end; color: var(--muted); display: flex; flex-direction: column; font-size: 11px; gap: 5px; }
.freshness > span:last-child { align-items: center; display: flex; gap: 7px; }
.sentinel-health-line { color: var(--muted); font-size: 13px; line-height: 1.6; margin: 16px 0 0; }
.system-path { border-bottom: 1px solid var(--rule); border-top: 1px solid var(--rule); margin-top: 20px; padding: 20px 0 16px; }
.title-with-icon { align-items: center; display: flex; gap: 8px; }
.title-with-icon h2 { margin: 0; }
.system-path .section-heading p { color: var(--muted); font-size: 11px; line-height: 1.5; margin: 5px 0 0; max-width: 620px; }
.legend { color: var(--muted); display: flex; flex-wrap: wrap; font-family: 'Commit Mono', monospace; font-size: 10px; gap: 16px; }
.legend span { align-items: center; display: inline-flex; gap: 6px; }
.station-mark { border: 1px solid currentColor; display: inline-block; height: 9px; width: 9px; }
.station-healthy { background: var(--healthy); border-color: var(--healthy); color: var(--healthy); }
.station-running { background: var(--running); border-color: var(--running); color: var(--running); }
.station-waiting { background: var(--waiting); border-color: var(--waiting); color: var(--waiting); }
.station-none { background: transparent; border-color: var(--muted); color: var(--muted); }
.station-unavailable { background: var(--danger); border-color: var(--danger); color: var(--danger); }
.path-grid { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); margin-top: 22px; }
.path-station { min-height: 118px; padding: 0 16px 0 0; position: relative; }
.path-station::after { border-top: 1px solid var(--rule); content: ''; left: 36px; position: absolute; right: 0; top: 4px; z-index: 0; }
.path-station:nth-child(3)::after { border-top-style: dashed; border-top-color: var(--waiting); }
.path-station:last-child::after { display: none; }
.station-name { align-items: center; background: var(--canvas); color: var(--ink); display: flex; gap: 7px; max-width: 100%; padding-right: 10px; position: relative; width: fit-content; z-index: 1; }
.station-name .icon { color: var(--muted); }
.station-name strong { font-size: 13px; white-space: nowrap; }
.station-state { display: block; font-size: 10px; margin-top: 18px; }
.station-state-healthy { color: var(--healthy); }
.station-state-running { color: var(--running); }
.station-state-waiting { color: var(--waiting); }
.station-state-unavailable { color: var(--danger); }
.station-state-none { color: var(--muted); }
.path-station p { color: var(--muted); font-size: 11px; line-height: 1.4; margin: 5px 0 0; max-width: 160px; }
.path-station small { color: var(--quiet); display: block; font-size: 9px; margin-top: 8px; }
.sentinel-control-grid { display: grid; gap: 28px; grid-template-columns: 7fr 5fr; margin-top: 28px; }
.queue-list { margin-top: 1px; }
.queue-row { align-items: center; border-bottom: 1px solid var(--rule); display: flex; gap: 14px; justify-content: space-between; min-height: 70px; padding: 10px 12px; }
.queue-row:hover { background: var(--surface); }
.queue-row > div { min-width: 0; }
.queue-row strong { color: var(--ink); display: block; font-size: 14px; }
.queue-row > div > span { align-items: center; color: var(--muted); display: flex; flex-wrap: wrap; font-size: 11px; gap: 12px; margin-top: 5px; }
.queue-row > div > span small { color: var(--muted); }
.queue-row time { color: var(--muted); font-size: 10px; white-space: nowrap; }
.approval-panel { background: var(--raised); border: 1px solid var(--rule); padding: 18px; }
.approval-panel .section-heading { border-bottom-color: var(--rule); }
.approval-panel-body h3 { font-size: 20px; margin-top: 14px; }
.approval-panel-body > p { font-size: 12px; line-height: 1.6; }
.accounting-section { border-bottom: 1px solid var(--rule); border-top: 1px solid var(--rule); margin-top: 28px; padding: 18px 0; }
.accounting-section .section-heading > span { color: var(--muted); font-size: 10px; }
.accounting-grid { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); margin-top: 16px; }
.accounting-cell { border-right: 1px solid var(--rule); display: flex; flex-direction: column; gap: 10px; min-height: 70px; padding: 0 16px; }
.accounting-cell:first-child { padding-left: 0; }
.accounting-cell:last-child { border-right: 0; }
.accounting-cell span { color: var(--muted); font-size: 11px; }
.accounting-cell strong { color: var(--ink); font-size: 18px; font-weight: 400; }
.ceiling-row { align-items: center; border-top: 1px solid var(--rule); display: flex; flex-wrap: wrap; gap: 14px; margin-top: 16px; padding-top: 14px; }
.ceiling-row > strong { color: var(--ink); font-size: 12px; font-weight: 500; }
.ceiling-row > code { color: var(--muted); font-size: 10px; }
.ceiling-row .progress-track { flex: 1; min-width: 160px; }
.theme-docket { --canvas: #171616; --surface: #211F1D; --raised: #2A2723; --rule: #4B4640; --ink: #F1ECE3; --muted: #B9B0A5; --healthy: #6EAA87; --running: #7394B5; --waiting: #D18A4B; --danger: #BE625C; --focus: #D8C18E; --brass: #D8C18E; background: var(--canvas); color: var(--ink); font-family: 'Work Sans', ui-sans-serif, system-ui, sans-serif; }
.theme-docket .view-switcher, .theme-docket .mono { font-family: 'Iosevka', 'Commit Mono', ui-monospace, monospace; }
.docket-masthead h1 { font-size: 31px; }
.docket-masthead .eyebrow { color: var(--muted); font-family: 'Alegreya Sans SC', sans-serif; }
.docket-health { gap: 9px; }
.docket-grid { display: grid; gap: 32px; grid-template-columns: minmax(420px, .86fr) minmax(620px, 1.14fr); margin-top: 28px; }
.docket-grid > * { min-width: 0; }
.run-slip { background: var(--surface); border: 1px solid var(--rule); clip-path: polygon(0 0, calc(100% - 18px) 0, 100% 18px, 100% 100%, 0 100%); padding: 18px; position: relative; }
.run-slip:hover { background: var(--raised); }
.slip-list { display: flex; flex-direction: column; gap: 10px; margin-top: 12px; }
.slip-selected { box-shadow: inset 0 2px 0 var(--focus); }
.slip-top { color: var(--muted); }
.slip-top .status { font-family: 'Alegreya Sans SC', sans-serif; font-size: 10px; letter-spacing: .1em; text-transform: uppercase; }
.slip-top time { font-size: 10px; }
.run-slip h3 { color: var(--ink); font-size: 18px; margin: 16px 0 0; }
.run-slip > p { align-items: center; color: var(--muted); display: flex; font-size: 11px; gap: 8px; margin: 6px 0 0; }
.run-slip > p code { color: var(--brass); }
.slip-action { border-top: 1px solid var(--rule); display: flex; flex-wrap: wrap; gap: 8px 14px; margin-top: 16px; padding-top: 14px; }
.slip-action > span { color: var(--muted); font-family: 'Alegreya Sans SC', sans-serif; font-size: 10px; letter-spacing: .12em; text-transform: uppercase; width: 100%; }
.slip-action > code { color: var(--ink); font-size: 11px; }
.slip-action > small { color: var(--muted); font-size: 10px; overflow: hidden; text-overflow: ellipsis; width: 100%; }
.slip-action .approval-form-row { margin-top: 5px; width: 100%; }
.slip-footer { border-top: 1px solid var(--rule); color: var(--muted); display: flex; font-size: 10px; justify-content: space-between; margin-top: 16px; padding-top: 13px; }
.docket-empty-note { margin-top: 12px; }
.trail-card { background: var(--surface); border: 1px solid var(--rule); margin-top: 12px; }
.trail-card-head { align-items: flex-start; border-bottom: 1px solid var(--rule); display: flex; gap: 16px; justify-content: space-between; padding: 22px; }
.trail-card-head h3 { color: var(--ink); font-size: 23px; letter-spacing: -.03em; margin: 0; }
.trail-card-head p { color: var(--muted); font-size: 11px; margin: 8px 0 0; }
.trail-card-head > code { color: var(--brass); font-size: 11px; white-space: nowrap; }
.execution-trail { list-style: none; margin: 0; padding: 22px; position: relative; }
.execution-trail::before { background: var(--rule); bottom: 28px; content: ''; left: 36px; position: absolute; top: 30px; width: 1px; }
.execution-trail li { display: grid; gap: 14px; grid-template-columns: 42px minmax(0, 1fr) 90px; min-height: 64px; position: relative; }
.trail-index { align-items: center; background: var(--healthy); color: var(--canvas); display: flex; font-size: 10px; height: 27px; justify-content: center; position: relative; width: 27px; z-index: 1; }
.trail-index.trail-running, .trail-index.trail-waiting { background: var(--canvas); border: 1px solid var(--focus); color: var(--focus); }
.trail-index.trail-danger { background: var(--danger); }
.trail-index.trail-muted { background: var(--surface); border: 1px solid var(--muted); color: var(--muted); }
.execution-trail strong { color: var(--ink); font-size: 13px; }
.execution-trail em { font-size: 12px; font-style: normal; }
.execution-trail p { color: var(--muted); font-size: 11px; line-height: 1.5; margin: 5px 0 0; }
.execution-trail time { color: var(--muted); font-size: 10px; text-align: right; }
.approval-hold { background: var(--raised); border-left: 2px solid var(--waiting); margin: 0 22px 20px 78px; padding: 16px; }
.approval-hold > p { color: var(--ink); font-size: 12px; line-height: 1.5; margin: 8px 0 0; }
.approval-hold .approval-detail-meta { margin-top: 12px; }
.trail-evidence { border-top: 1px solid var(--rule); display: grid; grid-template-columns: repeat(3, 1fr); }
.trail-evidence span { border-right: 1px solid var(--rule); display: flex; flex-direction: column; gap: 7px; padding: 14px 18px; }
.trail-evidence span:last-child { border-right: 0; }
.trail-evidence b { color: var(--muted); font-family: 'Alegreya Sans SC', sans-serif; font-size: 10px; letter-spacing: .12em; }
.trail-evidence em { color: var(--muted); font-size: 11px; font-style: normal; }
.usage-strip { border-bottom: 1px solid var(--rule); border-top: 1px solid var(--rule); margin-top: 28px; padding: 18px 0; }
.usage-strip .section-heading > span { color: var(--muted); font-size: 10px; }
.usage-grid { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); margin-top: 16px; }
.usage-item { border-right: 1px solid var(--rule); display: flex; flex-direction: column; gap: 9px; min-height: 66px; padding: 0 14px; }
.usage-item:first-child { padding-left: 0; }
.usage-item:last-child { border-right: 0; }
.usage-item span { color: var(--muted); font-family: 'Alegreya Sans SC', sans-serif; font-size: 10px; letter-spacing: .08em; }
.usage-item strong { color: var(--ink); font-size: 18px; font-weight: 400; }
.docket-footer { border-top: 1px solid var(--rule); color: var(--muted); display: flex; font-size: 10px; justify-content: space-between; margin-top: 28px; padding-top: 14px; }
@media (max-width: 1080px) {
  .intervention-grid, .docket-grid, .sentinel-control-grid { grid-template-columns: 1fr; }
  .condition-panel { order: 3; }
  .trail-panel { order: -1; }
  .path-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); row-gap: 18px; }
  .path-station:nth-child(3)::after { display: none; }
  .path-station:nth-child(4)::after { display: block; }
}
@media (max-width: 700px) {
  .dashboard-shell > .view-switcher { justify-content: flex-start; padding: 10px 18px 0; }
  .view-container { padding-left: 18px; padding-right: 18px; }
  .intervention-masthead, .sentinel-masthead, .docket-masthead { flex-direction: column; }
  .masthead-health, .freshness, .docket-health { align-items: flex-start; }
  .intervention-grid { margin-top: 18px; }
  .path-grid { grid-template-columns: 1fr; row-gap: 0; }
  .path-station { min-height: 78px; padding: 0; }
  .path-station::after, .path-station:nth-child(3)::after, .path-station:nth-child(4)::after { border-left: 1px solid var(--rule); border-top: 0; bottom: 4px; display: block; height: 48px; left: 4px; right: auto; top: 28px; width: 1px; }
  .path-station:nth-child(3)::after { border-left-style: dashed; border-left-color: var(--waiting); }
  .path-station:last-child::after { display: none; }
  .station-name { padding-left: 20px; }
  .station-state, .path-station p, .path-station small { margin-left: 20px; }
  .evidence-row, .trail-evidence { grid-template-columns: 1fr; }
  .evidence-row span, .trail-evidence span { border-bottom: 1px solid var(--rule); border-right: 0; padding: 10px 0; }
  .evidence-row span:last-child, .trail-evidence span:last-child { border-bottom: 0; }
  .accounting-grid, .usage-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .accounting-cell, .usage-item { border-bottom: 1px solid var(--rule); border-right: 1px solid var(--rule); min-height: 62px; padding: 12px 10px; }
  .accounting-cell:nth-child(2n), .usage-item:nth-child(2n) { border-right: 0; }
  .accounting-cell:nth-child(n+3), .usage-item:nth-child(n+3) { padding-top: 14px; }
  .accounting-cell:last-child, .usage-item:last-child { border-bottom: 0; }
  .execution-trail li { grid-template-columns: 36px minmax(0, 1fr); }
  .execution-trail time { grid-column: 2; text-align: left; }
  .execution-trail::before { left: 35px; }
  .approval-hold { margin-left: 58px; }
  .trail-card-head { flex-direction: column; }
  .health-summary { gap: 8px 10px; }
  .health-separator { display: none; }
}
"""


DASHBOARD_SCRIPT = r"""
(() => {
  document.querySelectorAll('[data-approval-form]').forEach((form) => {
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const button = form.querySelector('button[type="submit"]');
      const feedback = form.querySelector('[data-approval-feedback]');
      const action = form.dataset.action;
      const approvalId = form.dataset.approvalId;
      if (!button || !approvalId) return;
      button.disabled = true;
      button.textContent = action === 'approve' ? 'Approving…' : 'Rejecting…';
      if (feedback) feedback.textContent = 'Updating approval state…';
      try {
        const response = await fetch(`/v1/approvals/${encodeURIComponent(approvalId)}/${action}`, { method: 'POST' });
        if (!response.ok) throw new Error(`Request failed (${response.status})`);
        window.location.reload();
      } catch (error) {
        button.disabled = false;
        button.textContent = action === 'approve' ? 'Approve action' : 'Reject action';
        if (feedback) feedback.textContent = error.message || 'Could not update approval.';
      }
    });
  });
})();
"""


def approval_actions(approval_row: Mapping[str, Any], compact: bool = False) -> str:
    approval_id = esc(approval_row.get("id"))
    classes = " approval-actions-compact" if compact else ""
    return f"""
<div class=\"approval-form-row{classes}\">
  <form class=\"approval-form\" method=\"post\" action=\"/v1/approvals/{approval_id}/approve\" data-approval-form data-approval-id=\"{approval_id}\" data-action=\"approve\">
    <button class=\"approve-button\" type=\"submit\" aria-label=\"Approve {esc(approval_row.get('tool_name'))} action\">{('Approve' if compact else 'Approve action')}</button>
  </form>
  <form class=\"approval-form\" method=\"post\" action=\"/v1/approvals/{approval_id}/reject\" data-approval-form data-approval-id=\"{approval_id}\" data-action=\"reject\">
    <button class=\"reject-button\" type=\"submit\" aria-label=\"Reject {esc(approval_row.get('tool_name'))} action\">{('Reject' if compact else 'Reject action')}</button>
  </form>
</div>
<p class=\"approval-feedback\" data-approval-feedback role=\"status\" aria-live=\"polite\"></p>
"""


def approval_details(approval_row: Mapping[str, Any], compact: bool = False) -> str:
    args = parse_json(approval_row.get("tool_args"))
    target = args.get("path") or args.get("command") or args.get("args") or "No target recorded"
    return f"""
<div class=\"approval-detail-meta\">
  <span><b>Risk</b> {esc(risk_label(approval_row.get('risk_level')))}</span>
  <span><b>Tool</b> <code class=\"mono\">{esc(approval_row.get('tool_name'))}</code></span>
  <span><b>Target</b> <code class=\"mono truncate-value\">{esc(target)}</code></span>
</div>
<details class=\"argument-details\">
  <summary class=\"focus-ring\"><span>Show tool arguments</span>{icon('chevron-down', 'chevron')}</summary>
  <pre class=\"mono argument-code\">{esc(pretty_json(args))}</pre>
</details>
{approval_actions(approval_row, compact=compact)}
"""


def health_summary(data: Mapping[str, Any], model_reachable: bool) -> str:
    model_label = "reachable" if model_reachable else "unavailable"
    model_class = "status-healthy" if model_reachable else "status-danger"
    active_count = len(data["active_tasks"])
    return f"""
<div class=\"health-summary\">
  <span class=\"status status-healthy\"><span class=\"status-mark\" aria-hidden=\"true\"></span>Gateway reachable</span>
  <span class=\"health-separator\">·</span>
  <span class=\"status {model_class}\"><span class=\"status-mark\" aria-hidden=\"true\"></span>Model {model_label}</span>
  <span class=\"health-separator\">·</span>
  <span class=\"status status-waiting\"><span class=\"status-mark\" aria-hidden=\"true\"></span>{data['awaiting_approval']} approval{'' if data['awaiting_approval'] == 1 else 's'} held</span>
  <span class=\"health-separator\">·</span>
  <span class=\"status status-running\"><span class=\"status-mark\" aria-hidden=\"true\"></span>{active_count} task{'' if active_count == 1 else 's'} in execution</span>
</div>
"""


def task_stage(task: Mapping[str, Any]) -> str:
    run = task.get("latest_run") or {}
    return latest_run_label(task) if run else "No run record"


def render_task_table(tasks: Iterable[Mapping[str, Any]], view: str, total_count: Optional[int] = None) -> str:
    task_list = list(tasks)
    rows = []
    for task in task_list:
        label, variant = status_meta(task.get("status"))
        approval = task.get("approval") or {}
        if approval:
            label = "approval pending"
            variant = "waiting"
        row_class = "archive-row"
        rows.append(f"""
<tr class=\"{row_class}\">
  <td class=\"task-title-cell\"><strong>{task_link(task, view)}</strong><span>{esc('human decision required' if approval else task_stage(task))}</span></td>
  <td>{status_mark(task.get('status'), label)}</td>
  <td class=\"muted-cell\">{esc(task_stage(task))}</td>
  <td class=\"mono muted-cell\">{esc(timestamp(task.get('created_at'), short=True))}</td>
  <td class=\"mono quiet-cell\">{esc(str(task.get('id', ''))[:8])}</td>
</tr>
""")
    if not rows:
        rows.append('<tr><td class="empty-table" colspan="5">No tasks recorded yet. Submit one from your editor or <code class="mono">POST /v1/agent/run</code>.</td></tr>')
    table_label = "Recent runs" if view == "docket" else "Recent task history"
    section_eyebrow = "04 / Recent archive" if view == "docket" else ("Evidence ledger" if view == "intervention" else "Archive")
    return f"""
<section class=\"history-section\" aria-labelledby=\"history-title\">
  <div class=\"section-heading\"><div><p class=\"eyebrow\">{esc(section_eyebrow)}</p><h2 id=\"history-title\">{table_label}</h2></div><span class=\"mono section-count\">showing {len(task_list)}{f' of {total_count}' if total_count is not None and total_count > len(task_list) else ''} records</span></div>
  <div class=\"table-scroll\"><table aria-label=\"Recent task history\"><thead><tr><th>Task</th><th>State</th><th>Latest stage</th><th>Created</th><th>Task ID</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
  <p class=\"table-note\">Task state is shown with text and a mark. Values come from local task and agent-run records.</p>
</section>
"""


def render_intervention(data: Mapping[str, Any], model_reachable: bool) -> str:
    approval = data["approvals"][0] if data["approvals"] else None
    active = data["active_tasks"][0] if data["active_tasks"] else None
    stages = ["planner", "coder", "tester", "security", "reviewer"]
    stage_rows = []
    current_role = str((active or {}).get("latest_run", {}).get("agent_role", "")).lower()
    active_task_id = (active or {}).get("id")
    task_runs = data.get("runs_by_task", {}).get(active_task_id, [])
    latest_by_role: Dict[str, Dict[str, Any]] = {}
    for run in task_runs:
        latest_by_role[run.get("agent_role", "")] = run
    for stage in stages:
        run = latest_by_role.get(stage)
        status = str(run.get("status", "NOT_OBSERVED")).upper() if run else "NOT_OBSERVED"
        if status == "COMPLETED":
            marker = "complete"
            status_label = "completed"
        elif active and stage == current_role:
            marker = "current"
            status_label = "running"
        else:
            marker = "empty"
            status_label = "not observed"
        stage_rows.append(f"""
<div class=\"runline-row\"><span class=\"runline-marker runline-{marker}\">{icon('check' if marker == 'complete' else 'circle', '')}</span><div><div class=\"runline-title\"><strong>{esc(role_label(stage))}</strong><span class=\"mono\">{esc(status_label)}</span></div><p>{esc('Recorded agent run.' if run else 'Awaiting the recorded run.')}</p></div></div>
""")
    if approval:
        decision_block = f"""
<article class=\"approval-card\"><div class=\"approval-header\"><div><p class=\"eyebrow accent-waiting\">Approval request</p><h3>{esc(approval.get('task_title'))}</h3><p class=\"mono quiet-cell\">{esc(str(approval.get('task_id', ''))[:8])} · {esc(role_label(approval.get('agent_role')))}</p></div><span class=\"approval-held\">held</span></div><div class=\"approval-body\"><div class=\"tool-call\">{icon('file-pencil', 'accent-waiting')}<code class=\"mono\">{esc(approval.get('tool_name'))}</code></div><p>{esc(risk_label(approval.get('risk_level')))} · changes a file in the isolated task worktree.</p>{approval_details(approval, compact=False)}</div><p class=\"wait-meta\">{icon('clock')}Waiting since {esc(timestamp(approval.get('created_at'), short=True))} · approval <code class=\"mono\">{esc(str(approval.get('id', ''))[:8])}</code></p></article>
"""
    else:
        decision_block = '<div class="empty-block empty-copy"><strong>No decisions waiting</strong><span>Approvals will appear here when a tool needs your judgment.</span></div>'
    if active:
        active_title = esc(active.get("title"))
        active_summary = f"""
<article class=\"execution-card\"><div class=\"execution-card-head\"><div><h3>{active_title}</h3><p>{status_mark(active.get('status'))} <code class=\"mono\">task {esc(str(active.get('id', ''))[:8])}</code><span>created {esc(timestamp(active.get('created_at'), short=True))}</span></p></div><div class=\"cost-summary mono\">{esc(cost_label(task_cost(active), data['ceiling']))}<small>task spend · ceiling</small></div></div><div class=\"runline\">{''.join(stage_rows)}</div><div class=\"evidence-row\"><span><b>Worktree</b><em class=\"status-healthy\">{esc('recorded' if active.get('worktree_path') or active.get('branch_name') else 'not observed')}</em></span><span><b>Sandbox</b><em class=\"status-muted\">not observed</em></span><span><b>Security review</b><em class=\"status-muted\">{esc('recorded' if any(run.get('agent_role') == 'security' for run in data['latest_runs'].values()) else 'not observed')}</em></span></div></article>
<div class=\"empty-block empty-copy\"><strong>No additional execution records</strong><span>This runline only shows stages recorded by the gateway; no progress is invented between them.</span></div>
"""
    else:
        active_summary = '<div class="empty-block empty-copy"><strong>No task is running</strong><span>Active agent stages and evidence will appear here when work begins.</span></div>'
    health_model = "reachable" if model_reachable else "unavailable"
    return f"""
<div class=\"intervention-view view-container\">
  <header class=\"intervention-masthead\"><div class=\"identity\">{icon('terminal-2', 'identity-icon')}<div><div class=\"identity-title\"><h1>Personal AI Engineer</h1><span>operator view</span></div><p>A quiet control plane for self-hosted coding runs.</p></div></div><div class=\"masthead-health\">{health_summary(data, model_reachable)}<span class=\"mono checked\">checked just now</span></div></header>
  <div class=\"intervention-grid\"><aside class=\"attention-panel\"><div class=\"section-heading\"><div><p class=\"eyebrow accent-waiting\">Attention rail</p><h2>Needs a decision <span>· {data['awaiting_approval']}</span></h2></div>{icon('alert-triangle', 'accent-waiting')}</div>{decision_block}</aside><section class=\"execution-panel\"><div class=\"section-heading\"><div><p class=\"eyebrow accent-running\">Execution field</p><h2>Running now</h2></div><span class=\"section-count\">{len(data['active_tasks'])} active</span></div>{active_summary}</section><aside class=\"condition-panel\"><div class=\"section-heading\"><div><p class=\"eyebrow accent-healthy\">Condition ledger</p><h2>System condition</h2></div></div><div class=\"condition-list\"><div><span>Gateway<small>local health check</small></span>{status_mark('COMPLETED', 'reachable')}</div><div><span>Model endpoint<small>{esc('qwen3-coder-next')}</small></span>{status_mark('COMPLETED' if model_reachable else 'FAILED', 'reachable' if model_reachable else 'unavailable')}</div><div><span>Total Tokens<small>prompt + completion</small></span><code class=\"mono\">{data['total_tokens']:,}</code></div><div><span>Total Cost (USD)<small>usage to date</small></span><code class=\"mono\">${data['total_cost']:.4f}</code></div><div><span>Active task spend<small>{esc(active.get('title') if active else 'no active task')}</small></span><code class=\"mono\">{esc(cost_label(task_cost(active), data['ceiling']) if active else '—')}</code></div><div><span>Pending approvals<small>human decisions</small></span><code class=\"mono accent-waiting\">{data['awaiting_approval']} waiting</code></div></div><div class=\"policy-note\">{icon('shield-check', 'accent-healthy')}<span><strong>Evidence is explicit</strong><small>Unobserved sandbox and security states are shown as such, never inferred as healthy.</small></span></div></aside></div>
  <section class=\"intervention-ledger\">{render_task_table(data['recent_tasks'], 'intervention', data['total_tasks'])}</section>
</div>
"""


def render_sentinel(data: Mapping[str, Any], model_reachable: bool) -> str:
    approval = data["approvals"][0] if data["approvals"] else None
    active = data["active_tasks"][0] if data["active_tasks"] else None
    model_state = "Observed" if model_reachable else "Unavailable"
    model_variant = "healthy" if model_reachable else "unavailable"
    path_stations = [
        ("Gateway", "server-2", "Observed", "HTTP health route", "healthy"),
        ("Model", "cpu", model_state, "qwen3-coder-next", model_variant),
        ("Execution", "player-pause", "Interrupted" if approval else ("Observed" if active else "Not observed"), "write_file held for review" if approval else "No active task", "waiting" if approval else ("running" if active else "none")),
        ("Sandbox", "box", "Not observed", "No sandbox record in this run", "none"),
        ("Worktree", "git-branch", "Observed" if active and (active.get('worktree_path') or active.get('branch_name')) else "Not observed", "isolated task record" if active else "No recent worktree", "healthy" if active and (active.get('worktree_path') or active.get('branch_name')) else "none"),
        ("Security review", "shield-x", "Observed" if any(run.get('agent_role') == 'security' for run in data['latest_runs'].values()) else "Not observed", "agent run recorded" if any(run.get('agent_role') == 'security' for run in data['latest_runs'].values()) else "No security record", "healthy" if any(run.get('agent_role') == 'security' for run in data['latest_runs'].values()) else "none"),
    ]
    stations = []
    for index, (name, icon_name, state, evidence, variant) in enumerate(path_stations):
        stations.append(f"<div class=\"path-station {('path-interrupted' if variant == 'waiting' else '')}\"><div class=\"station-name\"><span class=\"station-mark station-{esc(variant)}\"></span>{icon(icon_name)}<strong>{esc(name)}</strong></div><code class=\"mono station-state station-state-{esc(variant)}\">{esc(state)}</code><p>{esc(evidence)}</p><small class=\"mono\">{esc('checked just now' if state in {'Observed', 'Interrupted'} else '—')}</small></div>")
    queue_tasks = data["open_tasks"][:4]
    completed = data["completed_tasks"][:1]
    queue_tasks = queue_tasks + [task for task in completed if task not in queue_tasks]
    queue_rows = []
    for task in queue_tasks:
        approval_row = task.get("approval") or {}
        label = "Awaiting approval" if approval_row else status_meta(task.get("status"))[0].title()
        variant = "waiting" if approval_row else status_meta(task.get("status"))[1]
        queue_rows.append(f"<div class=\"queue-row\"><div><strong>{task_link(task, 'sentinel')}</strong><span>{status_mark(task.get('status'), label)} <small>Latest stage: {esc(task_stage(task))}</small></span></div><time class=\"mono\">{esc(timestamp(task.get('created_at'), short=True))}</time></div>")
    if not queue_rows:
        queue_rows.append('<div class="empty-block empty-copy"><strong>No tasks have entered the queue.</strong><span>Run a task from your editor or send one to <code class="mono">POST /v1/agent/run</code>.</span></div>')
    approval_panel = f"""
<aside class=\"approval-panel\"><div class=\"section-heading\"><div><p class=\"eyebrow\">Human gate</p><h2>Approval gate</h2></div><span class=\"mono accent-waiting\">{data['awaiting_approval']} waiting</span></div>{f'<div class="approval-panel-body"><span class="status status-waiting"><span class="status-mark"></span>Awaiting approval</span><h3>{esc(approval.get("task_title"))}</h3><p>{esc(role_label(approval.get("agent_role")))} is requesting permission to use <code class="mono">{esc(approval.get("tool_name"))}</code> in the isolated task worktree.</p>{approval_details(approval)}</div>' if approval else '<div class="empty-copy"><strong>No approvals waiting.</strong><span>New human decisions will appear here without replacing task history.</span></div>'}</aside>
"""
    accounting_cells = "".join([
        f'<div class="accounting-cell"><span>{label}</span><strong class="mono {cls}">{value}</strong></div>'
        for label, value, cls in [
            ("Prompt tokens", f"{data['prompt_tokens']:,}", ""),
            ("Completion tokens", f"{data['completion_tokens']:,}", ""),
            ("Total Tokens", f"{data['total_tokens']:,}", ""),
            ("Total Cost (USD)", f"US${data['total_cost']:.4f}", ""),
            ("Approvals held", str(data["awaiting_approval"]), "accent-waiting"),
        ]
    ])
    return f"""
<div class=\"sentinel-view view-container\"><header class=\"sentinel-masthead\"><div><p class=\"eyebrow\">Self-hosted control plane</p><h1>Personal AI Engineer</h1><p>Operator view <span>·</span> gateway, agents, approvals, and model usage</p></div><div class=\"freshness\"><span class=\"eyebrow\">Data freshness</span><span class=\"mono\">checked just now</span><span>{status_mark('COMPLETED', 'Local history available')}</span></div></header><p class=\"sentinel-health-line\">{health_summary(data, model_reachable)}</p><section class=\"system-path\" aria-labelledby=\"system-path-title\"><div class=\"section-heading\"><div><div class=\"title-with-icon\">{icon('route')}<h2 id=\"system-path-title\">System path</h2></div><p>Observed states are backed by a health check or recorded run. No record is not the same as an outage.</p></div><div class=\"legend\"><span><i class=\"station-mark station-healthy\"></i>Observed</span><span><i class=\"station-mark station-none\"></i>Not observed</span><span><i class=\"station-mark station-unavailable\"></i>Unavailable</span></div></div><div class=\"path-grid\">{''.join(stations)}</div></section><section class=\"sentinel-control-grid\"><section class=\"queue-panel\"><div class=\"section-heading\"><div><p class=\"eyebrow\">Queue</p><h2>Work queue</h2></div><span class=\"mono section-count\">{len(queue_rows)} records</span></div><div class=\"queue-list\">{''.join(queue_rows)}</div></section>{approval_panel}</section><section class=\"accounting-section\"><div class=\"section-heading\"><div><p class=\"eyebrow\">Accounting</p><h2>Usage & limits</h2></div><span>Recorded model usage to date · costs shown to four decimals</span></div><div class=\"accounting-grid\">{accounting_cells}</div><div class=\"ceiling-row\"><span class=\"status status-running\"><span class=\"status-mark\"></span>Task ceiling</span><strong>{esc(active.get('title') if active else 'No active task')}</strong><code class=\"mono\">{esc(cost_label(task_cost(active), data['ceiling']) if active else '—')}</code><div class=\"progress-track\"><div class=\"progress-fill\" style=\"width:{min(100, (task_cost(active) / data['ceiling'] * 100) if active and data['ceiling'] else 0):.1f}%\"></div></div></div></section><section class=\"sentinel-history\">{render_task_table(data['recent_tasks'], 'sentinel')}</section></div>
"""


def runline_entries(task: Mapping[str, Any], runs_by_task: Mapping[str, List[Mapping[str, Any]]]) -> str:
    task_runs = list(runs_by_task.get(task.get("id"), []))
    task_runs.sort(key=lambda run: str(run.get("started_at") or ""))
    if not task_runs:
        task_runs = [{"agent_role": "planner", "status": "NOT_OBSERVED"}]
    entries = []
    for index, run in enumerate(task_runs, start=1):
        status_label, variant = status_meta(run.get("status"))
        if str(run.get("status", "")).upper() not in {"COMPLETED", "RUNNING", "WAITING", "WAITING_APPROVAL", "REVIEW_REQUIRED", "FAILED"}:
            status_label, variant = "not observed", "muted"
        entries.append(f"<li><span class=\"trail-index trail-{esc(variant)} mono\">{index:02d}</span><div><strong>{esc(role_label(run.get('agent_role')))}</strong> <em class=\"{esc('accent-' + variant)}\">{esc(status_label)}</em><p>{esc('Agent run recorded by the gateway.' if str(run.get('status', '')).upper() != 'NOT_OBSERVED' else 'No run record available.')}</p></div><time class=\"mono\">{esc(timestamp(run.get('started_at'), short=True))}</time></li>")
    return "".join(entries)


def docket_slip(task: Mapping[str, Any]) -> str:
    approval = task.get("approval") or {}
    status = "WAITING" if approval else task.get("status")
    label, variant = status_meta(status)
    if approval:
        label = "held for approval"
        variant = "waiting"
    action = ""
    if approval:
        action = f"<div class=\"slip-action\"><span>Requested action</span><code class=\"mono\">{esc(approval.get('tool_name'))}</code><small>{esc((parse_json(approval.get('tool_args')) or {}).get('path') or 'target recorded')}</small>{approval_actions(approval, compact=True)}</div>"
    return f"""
<article class=\"run-slip slip-{esc(variant)}{' slip-selected' if approval else ''}\"><div class=\"slip-top\">{status_mark(status, label)}<time class=\"mono\">{esc(timestamp(task.get('created_at'), short=True))}</time></div><h3>{task_link(task, 'docket')}</h3><p><code class=\"mono\">{esc(role_label((task.get('latest_run') or {}).get('agent_role')))}</code><span>·</span>{esc('waiting on human decision' if approval else latest_run_label(task))}</p>{action if action else f'<div class="slip-footer"><code class="mono">task-{esc(str(task.get("id", ""))[:8])}</code><span>{esc("worktree observed" if task.get("worktree_path") else "no run evidence")}</span></div>'}</article>
"""


def render_docket(data: Mapping[str, Any], model_reachable: bool) -> str:
    selected = data["approvals"][0] if data["approvals"] else (data["open_tasks"][0] if data["open_tasks"] else (data["recent_tasks"][0] if data["recent_tasks"] else None))
    selected_task = next((task for task in data["tasks"] if selected and task.get("id") == selected.get("task_id", selected.get("id"))), None) if selected else None
    if selected and selected_task is None and selected.get("task_id"):
        selected_task = next((task for task in data["tasks"] if task.get("id") == selected.get("task_id")), None)
    if selected_task is None:
        trail_content = '<div class="empty-block empty-copy"><strong>No run record</strong><span>Agent stages, approvals, sandbox activity, and security reviews will attach to each task as they occur.</span></div>'
        selected_title = "No selected task"
        selected_status = "No open work"
    else:
        selected_title = esc(selected_task.get("title"))
        selected_status = status_meta(selected_task.get("status"))[0]
        approval = selected_task.get("approval") or {}
        hold = f"<div class=\"approval-hold\"><p class=\"eyebrow accent-waiting\">Approval hold · {esc(risk_label(approval.get('risk_level')))}</p><p>Agent requested permission to continue with <code class=\"mono\">{esc(approval.get('tool_name'))}</code>.</p>{approval_details(approval)}</div>" if approval else ""
        trail_content = f"<ol class=\"execution-trail\">{runline_entries(selected_task, data['runs_by_task'])}</ol>{hold}<div class=\"trail-evidence\"><span><b>Worktree</b><em class=\"accent-healthy\">{esc('Observed' if selected_task.get('worktree_path') or selected_task.get('branch_name') else 'Not observed')}</em></span><span><b>Sandbox</b><em>Not observed</em></span><span><b>Security review</b><em>{esc('Recorded' if any(run.get('agent_role') == 'security' for run in data['runs_by_task'].get(selected_task.get('id'), [])) else 'Not observed')}</em></span></div>"
    slips = data["open_tasks"][:3]
    slips_html = "".join(docket_slip(task) for task in slips) or '<div class="empty-block empty-copy"><strong>No open work</strong><span>Tasks submitted from your editor or <code class="mono">POST /v1/agent/run</code> appear here.</span></div>'
    usage = "".join(f'<div class="usage-item"><span>{label}</span><strong class="mono {cls}">{value}</strong></div>' for label, value, cls in [("Tasks", data["total_tasks"], ""), ("Completed", data["completed_count"], "accent-healthy"), ("Review required", data["review_count"], "accent-danger"), ("Approval held", data["awaiting_approval"], "accent-waiting"), ("Total Tokens", f"{data['total_tokens']:,}", ""), ("Total Cost (USD)", f"US${data['total_cost']:.4f}", "accent-brass")])
    model_state = "reachable" if model_reachable else "unavailable"
    return f"""
<div class=\"docket-view view-container\"><header class=\"docket-masthead\"><div><p class=\"eyebrow accent-brass\">Self-hosted control plane</p><h1>Personal AI Engineer</h1><p>Run docket · operator view</p></div><div class=\"docket-health\">{health_summary(data, model_reachable)}<span class=\"mono\">checked just now · model {model_state}</span></div></header><div class=\"docket-grid\"><section class=\"open-docket\"><div class=\"section-heading\"><div><p class=\"eyebrow\">01 / Open docket</p><h2>Open work</h2></div><span class=\"mono section-count\">{len(slips)} records<br>ordered by action</span></div><div class=\"slip-list\">{slips_html}</div><div class=\"empty-block empty-copy docket-empty-note\"><strong>No open work</strong><span>When the docket clears, task history remains in the archive below.</span></div></section><section class=\"trail-panel\"><div class=\"section-heading\"><div><p class=\"eyebrow\">02 / Selected record</p><h2>Execution trail</h2></div><span class=\"status status-waiting\"><span class=\"status-mark\"></span>{esc(selected_status)}</span></div><div class=\"trail-card\"><div class=\"trail-card-head\"><div><h3>{selected_title}</h3><p>Planner → Coder → Tester · created {esc(timestamp(selected_task.get('created_at') if selected_task else None, short=True))}</p></div><code class=\"mono\">{esc(cost_label(task_cost(selected_task), data['ceiling']) if selected_task else '—')}</code></div>{trail_content}</div></section></div><section class=\"usage-strip\"><div class=\"section-heading\"><div><p class=\"eyebrow\">03 / Usage to date</p></div><span class=\"mono\">local memory · all recorded runs</span></div><div class=\"usage-grid\">{usage}</div></section><section class=\"docket-history\"><div class=\"section-heading\"><div><p class=\"eyebrow\">04 / Recent archive</p><h2>Recent runs</h2></div><span class=\"mono section-count\">latest 10 records</span></div>{render_task_table(data['recent_tasks'], 'docket')}</section><footer class=\"docket-footer\"><span>Evidence-first observability · local task memory</span><code class=\"mono\">/v1/dashboard</code></footer></div>
"""


def render_dashboard(view: str, data: Mapping[str, Any], model_reachable: bool) -> str:
    normalized = normalize_view(view)
    if normalized == "sentinel":
        theme = "theme-sentinel"
        body = render_sentinel(data, model_reachable)
    elif normalized == "docket":
        theme = "theme-docket"
        body = render_docket(data, model_reachable)
    else:
        theme = "theme-intervention"
        body = render_intervention(data, model_reachable)
    return base_head(f"Personal AI Engineer — Observability Dashboard · {VIEW_LABELS[normalized]}", normalized, theme) + view_switcher(normalized) + body + f"<script>{DASHBOARD_SCRIPT}</script></body></html>"
