from __future__ import annotations

import argparse
import json
from html import escape
from pathlib import Path


def load_json(path: Path, default: object) -> object:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def fmt_money(value: object) -> str:
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return "$0.00"


def fmt_pct(value: object) -> str:
    try:
        return f"{float(value):.1f}%"
    except (TypeError, ValueError):
        return "0.0%"


def render_status_options(current: str) -> str:
    options = [
        "discovered",
        "queued",
        "workspace_prepared",
        "codex_attempted",
        "patch_ready",
        "submitted",
        "merged",
        "paid",
        "declined",
        "automation_failed",
        "automation_timeout",
    ]
    rendered = []
    for item in options:
        selected = " selected" if item == current else ""
        rendered.append(f"<option value='{escape(item)}'{selected}>{escape(item)}</option>")
    return "".join(rendered)


def render_dashboard(
    output_dir: Path,
    workspace_dir: Path,
    target_path: Path,
) -> None:
    run_summary = load_json(output_dir / "run_summary.json", {})
    changes = load_json(output_dir / "changes_summary.json", {})
    triaged = load_json(output_dir / "triaged.json", [])
    alert_summary = load_json(output_dir / "alerts/latest_delivery.json", {})
    monitoring_metrics = load_json(output_dir / "monitoring_metrics.json", {})
    service_state = load_json(output_dir / "service_state.json", {})
    runtime_status = load_json(output_dir / "runtime_status.json", {})

    workspaces = []
    if workspace_dir.exists():
        for path in sorted(workspace_dir.iterdir()):
            if path.is_dir():
                readme = path / "README.md"
                worklog = path / "WORKLOG.md"
                workspaces.append(
                    {
                        "name": path.name,
                        "path": str(path),
                        "readme": str(readme) if readme.exists() else "",
                        "worklog": str(worklog) if worklog.exists() else "",
                    }
                )

    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(
        build_html(
            run_summary=run_summary,
            changes=changes,
            triaged=triaged,
            workspaces=workspaces,
            alert_summary=alert_summary,
            monitoring_metrics=monitoring_metrics,
            service_state=service_state,
            runtime_status=runtime_status,
        ),
        encoding="utf-8",
    )


def build_html(
    *,
    run_summary: dict,
    changes: dict,
    triaged: list[dict],
    workspaces: list[dict],
    alert_summary: dict,
    monitoring_metrics: dict,
    service_state: dict,
    runtime_status: dict,
) -> str:
    source_counts = run_summary.get("source_counts", {})
    recommendations = run_summary.get("recommendations", {})
    top_candidates = run_summary.get("top_candidates", [])
    triage_policy = run_summary.get("triage_policy", {})
    active_queue_count = int(recommendations.get("pursue", 0)) + int(recommendations.get("review", 0))
    totals = monitoring_metrics.get("totals", {})
    current_queue_value = monitoring_metrics.get("current_queue_value", {})
    recent_runs = monitoring_metrics.get("recent_runs", [])
    recent_automation = monitoring_metrics.get("recent_automation", [])
    business_model = monitoring_metrics.get("business_model", {})
    ledger = monitoring_metrics.get("ledger", {})
    ledger_summary = ledger.get("summary", {})
    ledger_items = ledger.get("items", [])
    web_url = service_state.get("web_url") or runtime_status.get("web_url") or ""

    change_items = []
    for item in changes.get("new_items", []):
        change_items.append(
            f"<li>new: {escape(item['repo'])}#{item['number']} score={item.get('total_score')}</li>"
        )
    for item in changes.get("dropped_items", []):
        change_items.append(
            f"<li>dropped: {escape(item['repo'])}#{item['number']} score={item.get('total_score')}</li>"
        )
    for item in changes.get("recommendation_changes", []):
        change_items.append(
            f"<li>reco: {escape(item['repo'])}#{item['number']} "
            f"{escape(str(item['previous_recommendation']))} -> {escape(str(item['current_recommendation']))}</li>"
        )
    if not change_items:
        change_items.append("<li>No changes since the previous run.</li>")

    candidate_rows = []
    for item in top_candidates or triaged[:10]:
        reason = item.get("decision_reason", "-")
        signal_text = ", ".join(item.get("decision_signals", [])[:4]) or "-"
        candidate_rows.append(
            "<tr>"
            f"<td>{escape(item['repo'])}</td>"
            f"<td>{escape(str(item['number']))}</td>"
            f"<td>{escape(item['title'])}</td>"
            f"<td>{escape(str(item.get('recommendation', '-')))}</td>"
            f"<td>{escape(str(item.get('total_score', '-')))}</td>"
            f"<td>{escape(str(item.get('competition_risk', '-')))}</td>"
            f"<td>{escape(str(reason))}<br><span class='muted'>{escape(signal_text)}</span></td>"
            "</tr>"
        )
    if not candidate_rows:
        candidate_rows.append("<tr><td colspan='7'>No candidates.</td></tr>")

    workspace_rows = []
    for item in workspaces:
        workspace_rows.append(
            "<tr>"
            f"<td>{escape(item['name'])}</td>"
            f"<td><code>{escape(item['path'])}</code></td>"
            f"<td><code>{escape(item['readme'] or '-')}</code></td>"
            f"<td><code>{escape(item['worklog'] or '-')}</code></td>"
            "</tr>"
        )
    if not workspace_rows:
        workspace_rows.append("<tr><td colspan='4'>No workspaces prepared yet.</td></tr>")

    queue_value_rows = []
    for item in current_queue_value.get("items", [])[:10]:
        queue_value_rows.append(
            "<tr>"
            f"<td>{escape(item['repo'])}</td>"
            f"<td>{escape(str(item['number']))}</td>"
            f"<td>{escape(item.get('recommendation', '-'))}</td>"
            f"<td>{escape(str(item.get('bounty_confidence', '-')))}</td>"
            f"<td>{escape(str(item.get('actionability', '-')))}</td>"
            f"<td>{escape(str(item.get('competition_risk', '-')))}</td>"
            f"<td>{fmt_money(item.get('estimated_value_usd', 0.0))}</td>"
            "</tr>"
        )
    if not queue_value_rows:
        queue_value_rows.append("<tr><td colspan='7'>No active queue value yet.</td></tr>")

    recent_run_rows = []
    for item in recent_runs[:15]:
        recent_run_rows.append(
            "<tr>"
            f"<td>{escape(str(item.get('finished_at_utc', '-')))}</td>"
            f"<td>{escape(str(item.get('duration_seconds', '-')))}s</td>"
            f"<td>{escape(str(item.get('merged_count', '-')))}</td>"
            f"<td>{escape(str(item.get('queue_count', '-')))}</td>"
            f"<td>{escape(str(item.get('new_queue_count', '-')))}</td>"
            f"<td>{escape(str(item.get('automation', {}).get('total', 0)))}</td>"
            f"<td>{fmt_money(item.get('estimated_new_queue_revenue_usd', 0.0))}</td>"
            f"<td>{escape(str(item.get('exit_code', '-')))}</td>"
            "</tr>"
        )
    if not recent_run_rows:
        recent_run_rows.append("<tr><td colspan='8'>No run history yet.</td></tr>")

    automation_rows = []
    for item in recent_automation[:15]:
        automation_rows.append(
            "<tr>"
            f"<td>{escape(str(item.get('run_finished_at_utc', '-')))}</td>"
            f"<td>{escape(str(item.get('repo', '-')))}#{escape(str(item.get('number', '-')))}</td>"
            f"<td>{escape(str(item.get('status', '-')))}</td>"
            f"<td>{escape(str(item.get('exit_code', '-')))}</td>"
            f"<td>{'yes' if item.get('worklog_exists') else 'no'}</td>"
            f"<td><code>{escape(str(item.get('worklog_path') or item.get('workspace_dir') or '-'))}</code></td>"
            "</tr>"
        )
    if not automation_rows:
        automation_rows.append("<tr><td colspan='6'>No AI automation runs yet.</td></tr>")

    ledger_rows = []
    for item in ledger_items[:25]:
        issue_key = escape(str(item.get("key", "")))
        ledger_rows.append(
            "<tr>"
            f"<td><strong>{escape(str(item.get('repo', '-')))}#{escape(str(item.get('number', '-')))}</strong><br><span class='muted'>{escape(str(item.get('title', '-')))}</span></td>"
            f"<td>{escape(str(item.get('latest_recommendation', '-')))}</td>"
            f"<td>{escape(str(item.get('last_automation_status', '-')))}</td>"
            f"<td>{fmt_money(item.get('estimated_value_usd', 0.0))}</td>"
            f"<td><input class='ledger-input ledger-claimed' data-key='{issue_key}' value='{escape(str(item.get('claimed_value_usd', 0.0)))}'></td>"
            f"<td><input class='ledger-input ledger-actual' data-key='{issue_key}' value='{escape(str(item.get('actual_revenue_usd', 0.0)))}'></td>"
            f"<td><select class='ledger-select ledger-status' data-key='{issue_key}'>{render_status_options(str(item.get('status', 'discovered')))}</select></td>"
            f"<td><input class='ledger-input ledger-notes' data-key='{issue_key}' value='{escape(str(item.get('notes', '')))}'></td>"
            f"<td><button class='ledger-save' data-key='{issue_key}'>Save</button></td>"
            "</tr>"
        )
    if not ledger_rows:
        ledger_rows.append("<tr><td colspan='9'>No ledger items tracked yet.</td></tr>")

    generated_at = run_summary.get("generated_at_utc", "-")
    alert_status = alert_summary.get("delivery_status", "-")
    alert_new_queue_count = alert_summary.get("new_queue_count", 0)
    alert_changed_queue_count = alert_summary.get("changed_queue_count", 0)
    run_in_progress = bool(runtime_status.get("run_in_progress"))
    last_run_finished = runtime_status.get("last_run_finished_at_utc", "-")
    last_error = runtime_status.get("last_error", "")
    manual_triggers = runtime_status.get("manual_trigger_count", 0)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>NTG Radar</title>
  <style>
    :root {{
      --bg: #0b1217;
      --panel: rgba(16, 24, 32, 0.9);
      --text: #ecf3f9;
      --muted: #97a6ba;
      --accent: #7ed39b;
      --warn: #f1ca72;
      --danger: #ef9b9b;
      --border: #293646;
      --mono: "SFMono-Regular", Menlo, Consolas, monospace;
    }}
    body {{
      margin: 0;
      background:
        radial-gradient(circle at top right, rgba(54, 92, 126, 0.22), transparent 28%),
        linear-gradient(180deg, #0a1016 0%, #0f1822 100%);
      color: var(--text);
      font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    .wrap {{
      max-width: 1280px;
      margin: 0 auto;
      padding: 28px 20px 56px;
    }}
    .topbar {{
      display: flex;
      gap: 16px;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      margin-bottom: 18px;
    }}
    .statusbox {{
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      align-items: center;
    }}
    .pill {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      border: 1px solid var(--border);
      border-radius: 999px;
      padding: 7px 12px;
      background: rgba(15, 22, 29, 0.88);
    }}
    .dot {{
      width: 9px;
      height: 9px;
      border-radius: 50%;
      background: var(--warn);
      box-shadow: 0 0 14px rgba(241, 202, 114, 0.5);
    }}
    .dot.live {{
      background: var(--accent);
      box-shadow: 0 0 14px rgba(126, 211, 155, 0.5);
    }}
    h1, h2 {{
      margin: 0 0 12px;
    }}
    .muted {{
      color: var(--muted);
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
      gap: 16px;
      margin: 16px 0 24px;
    }}
    .card {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 16px;
      box-shadow: 0 14px 32px rgba(0, 0, 0, 0.22);
      backdrop-filter: blur(8px);
    }}
    .metric {{
      font-size: 30px;
      font-weight: 700;
      margin-top: 6px;
    }}
    .metric.small {{
      font-size: 20px;
    }}
    .section {{
      margin-top: 28px;
    }}
    .sectionhead {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 10px;
      flex-wrap: wrap;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 16px;
      overflow: hidden;
    }}
    th, td {{
      text-align: left;
      padding: 10px 12px;
      border-bottom: 1px solid var(--border);
      vertical-align: top;
    }}
    th {{
      color: var(--muted);
      font-weight: 600;
    }}
    tr:last-child td, tr:last-child th {{
      border-bottom: 0;
    }}
    code {{
      font-family: var(--mono);
      font-size: 12px;
      white-space: pre-wrap;
      word-break: break-all;
    }}
    ul {{
      margin: 0;
      padding-left: 18px;
    }}
    button {{
      border: 0;
      border-radius: 12px;
      padding: 10px 14px;
      background: linear-gradient(135deg, #7ed39b 0%, #4cb87d 100%);
      color: #06110b;
      font-weight: 700;
      cursor: pointer;
    }}
    button:disabled {{
      opacity: 0.65;
      cursor: wait;
    }}
    .error {{
      color: var(--danger);
    }}
    .hint {{
      font-size: 12px;
      color: var(--muted);
    }}
    input, select {{
      width: 100%;
      box-sizing: border-box;
      padding: 8px 9px;
      border-radius: 10px;
      border: 1px solid var(--border);
      background: rgba(9, 14, 19, 0.8);
      color: var(--text);
    }}
    .ledger-save {{
      width: 100%;
      padding: 8px 10px;
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="topbar">
      <div>
        <h1>NTG Radar Control</h1>
        <div class="muted">Generated at {escape(str(generated_at))}</div>
      </div>
      <div class="statusbox">
        <div class="pill"><span class="dot {'live' if run_in_progress else ''}"></span><span id="run-state">{'Running' if run_in_progress else 'Idle'}</span></div>
        <div class="pill">Last finish: <span id="last-finish">{escape(str(last_run_finished))}</span></div>
        <div class="pill">Manual triggers: <span id="manual-count">{escape(str(manual_triggers))}</span></div>
        <button id="run-now-btn" {'disabled' if not web_url else ''}>Run Now</button>
      </div>
    </div>

    <div class="card">
      <div class="muted">Control endpoint</div>
      <div class="metric small">{escape(str(web_url or 'web server disabled'))}</div>
      <div class="hint" id="run-now-feedback">{escape(str(last_error or 'Click Run Now to trigger an immediate scan without waiting for the next interval.'))}</div>
    </div>

    <div class="grid">
      <div class="card"><div class="muted">Total runs</div><div class="metric">{escape(str(totals.get('runs', 0)))}</div></div>
      <div class="card"><div class="muted">Run success rate</div><div class="metric">{fmt_pct(totals.get('success_rate', 0.0))}</div></div>
      <div class="card"><div class="muted">Avg run time</div><div class="metric">{escape(str(totals.get('average_duration_seconds', 0.0)))}s</div></div>
      <div class="card"><div class="muted">Candidates scanned</div><div class="metric">{escape(str(totals.get('total_candidates', 0)))}</div></div>
      <div class="card"><div class="muted">AI runs</div><div class="metric">{escape(str(totals.get('total_ai_runs', 0)))}</div></div>
      <div class="card"><div class="muted">AI runs with worklog</div><div class="metric">{escape(str(totals.get('total_ai_worklogs', 0)))}</div></div>
      <div class="card"><div class="muted">Active queue EV</div><div class="metric">{fmt_money(totals.get('estimated_active_revenue_usd', 0.0))}</div></div>
      <div class="card"><div class="muted">7d new queue EV</div><div class="metric">{fmt_money(totals.get('estimated_new_revenue_7d_usd', 0.0))}</div></div>
    </div>

    <div class="grid">
      <div class="card"><div class="muted">Tracked issues</div><div class="metric">{escape(str(totals.get('tracked_issues', 0)))}</div></div>
      <div class="card"><div class="muted">AI completed</div><div class="metric">{escape(str(totals.get('ai_completed_issues', 0)))}</div></div>
      <div class="card"><div class="muted">Submitted</div><div class="metric">{escape(str(totals.get('submitted_issues', 0)))}</div></div>
      <div class="card"><div class="muted">Paid</div><div class="metric">{escape(str(totals.get('paid_issues', 0)))}</div></div>
      <div class="card"><div class="muted">Claimed revenue</div><div class="metric">{fmt_money(totals.get('claimed_revenue_usd', 0.0))}</div></div>
      <div class="card"><div class="muted">Actual revenue</div><div class="metric">{fmt_money(totals.get('actual_revenue_usd', 0.0))}</div></div>
      <div class="card"><div class="muted">Patch ready</div><div class="metric">{escape(str(ledger_summary.get('patch_ready_issues', 0)))}</div></div>
      <div class="card"><div class="muted">Pipeline EV</div><div class="metric">{fmt_money(ledger_summary.get('estimated_pipeline_revenue_usd', 0.0))}</div></div>
    </div>

    <div class="grid">
      <div class="card"><div class="muted">GitHub candidates</div><div class="metric">{escape(str(source_counts.get('github', 0)))}</div></div>
      <div class="card"><div class="muted">Algora candidates</div><div class="metric">{escape(str(source_counts.get('algora', 0)))}</div></div>
      <div class="card"><div class="muted">Merged candidates</div><div class="metric">{escape(str(source_counts.get('merged', 0)))}</div></div>
      <div class="card"><div class="muted">Pursue / Review / Skip</div><div class="metric">{escape(str(recommendations.get('pursue', 0)))}/{escape(str(recommendations.get('review', 0)))}/{escape(str(recommendations.get('skip', 0)))}</div></div>
      <div class="card"><div class="muted">Active queue</div><div class="metric">{escape(str(active_queue_count))}</div></div>
      <div class="card"><div class="muted">Triage profile</div><div class="metric">{escape(str(triage_policy.get('profile', '-')))}</div></div>
      <div class="card"><div class="muted">Alert delivery</div><div class="metric">{escape(str(alert_status))}</div></div>
      <div class="card"><div class="muted">New / Changed queue</div><div class="metric">{escape(str(alert_new_queue_count))}/{escape(str(alert_changed_queue_count))}</div></div>
    </div>

    <div class="section">
      <div class="sectionhead">
        <h2>Revenue Assumptions</h2>
      </div>
      <div class="card">
        <div class="muted">This is heuristic expected value, not realized income.</div>
        <ul>
          <li>`pursue` avg payout: {fmt_money(business_model.get('pursue_avg_payout_usd', 0.0))} with close rate {fmt_pct(float(business_model.get('pursue_close_rate', 0.0)) * 100)}</li>
          <li>`review` avg payout: {fmt_money(business_model.get('review_avg_payout_usd', 0.0))} with close rate {fmt_pct(float(business_model.get('review_close_rate', 0.0)) * 100)}</li>
          <li>Each issue estimate is adjusted by bounty confidence, actionability, and competition risk.</li>
        </ul>
      </div>
    </div>

    <div class="section">
      <div class="sectionhead">
        <h2>Current Queue Value</h2>
        <div class="muted">Estimated active pipeline value: {fmt_money(current_queue_value.get('estimated_active_revenue_usd', 0.0))}</div>
      </div>
      <table>
        <thead>
          <tr><th>Repo</th><th>Issue</th><th>Reco</th><th>Bty</th><th>Act</th><th>Risk</th><th>EV</th></tr>
        </thead>
        <tbody>
          {''.join(queue_value_rows)}
        </tbody>
      </table>
    </div>

    <div class="section">
      <div class="sectionhead">
        <h2>Bounty Ledger</h2>
        <div class="muted">Edit status and revenue directly here. This is your actual经营台账.</div>
      </div>
      <table>
        <thead>
          <tr><th>Issue</th><th>Reco</th><th>AI</th><th>EV</th><th>Claimed</th><th>Actual</th><th>Status</th><th>Notes</th><th>Save</th></tr>
        </thead>
        <tbody>
          {''.join(ledger_rows)}
        </tbody>
      </table>
    </div>

    <div class="section">
      <h2>Changes</h2>
      <div class="card"><ul>{''.join(change_items)}</ul></div>
    </div>

    <div class="section">
      <h2>Recent Runs</h2>
      <table>
        <thead>
          <tr><th>Finished At</th><th>Duration</th><th>Candidates</th><th>Queue</th><th>New Queue</th><th>AI Runs</th><th>New EV</th><th>Exit</th></tr>
        </thead>
        <tbody>
          {''.join(recent_run_rows)}
        </tbody>
      </table>
    </div>

    <div class="section">
      <h2>Recent AI Automation</h2>
      <table>
        <thead>
          <tr><th>Run Time</th><th>Target</th><th>Status</th><th>Exit</th><th>Worklog</th><th>Path</th></tr>
        </thead>
        <tbody>
          {''.join(automation_rows)}
        </tbody>
      </table>
    </div>

    <div class="section">
      <h2>Top Candidates</h2>
      <table>
        <thead>
          <tr><th>Repo</th><th>Issue</th><th>Title</th><th>Reco</th><th>Score</th><th>Risk</th><th>Why</th></tr>
        </thead>
        <tbody>
          {''.join(candidate_rows)}
        </tbody>
      </table>
    </div>

    <div class="section">
      <h2>Prepared Workspaces</h2>
      <table>
        <thead>
          <tr><th>Name</th><th>Path</th><th>README</th><th>WORKLOG</th></tr>
        </thead>
        <tbody>
          {''.join(workspace_rows)}
        </tbody>
      </table>
    </div>
  </div>
  <script>
    const button = document.getElementById("run-now-btn");
    const feedback = document.getElementById("run-now-feedback");
    const runState = document.getElementById("run-state");
    const lastFinish = document.getElementById("last-finish");
    const manualCount = document.getElementById("manual-count");
    const ledgerButtons = Array.from(document.querySelectorAll(".ledger-save"));

    async function refreshRuntime() {{
      try {{
        const response = await fetch("/api/status", {{ cache: "no-store" }});
        if (!response.ok) return;
        const payload = await response.json();
        const runtime = payload.runtime || {{}};
        runState.textContent = runtime.run_in_progress ? "Running" : "Idle";
        lastFinish.textContent = runtime.last_run_finished_at_utc || "-";
        manualCount.textContent = String(runtime.manual_trigger_count || 0);
        if (runtime.last_error) {{
          feedback.textContent = runtime.last_error;
          feedback.classList.add("error");
        }}
      }} catch (error) {{
      }}
    }}

    if (button && {str(bool(web_url)).lower()}) {{
      button.addEventListener("click", async () => {{
        button.disabled = true;
        feedback.textContent = "Triggering manual run...";
        feedback.classList.remove("error");
        try {{
          const response = await fetch("/api/run-now", {{ method: "POST" }});
          const payload = await response.json();
          feedback.textContent = payload.message || "manual run requested";
        }} catch (error) {{
          feedback.textContent = "manual trigger failed";
          feedback.classList.add("error");
        }} finally {{
          setTimeout(() => {{
            button.disabled = false;
            refreshRuntime();
          }}, 1200);
        }}
      }});
      refreshRuntime();
      setInterval(refreshRuntime, 15000);
    }}

    for (const saveButton of ledgerButtons) {{
      saveButton.addEventListener("click", async () => {{
        const key = saveButton.dataset.key;
        const claimed = document.querySelector(`.ledger-claimed[data-key="${{key}}"]`);
        const actual = document.querySelector(`.ledger-actual[data-key="${{key}}"]`);
        const status = document.querySelector(`.ledger-status[data-key="${{key}}"]`);
        const notes = document.querySelector(`.ledger-notes[data-key="${{key}}"]`);
        saveButton.disabled = true;
        feedback.textContent = `Saving ${{key}} ...`;
        feedback.classList.remove("error");
        try {{
          const response = await fetch("/api/ledger/update", {{
            method: "POST",
            headers: {{ "Content-Type": "application/json" }},
            body: JSON.stringify({{
              key,
              status: status ? status.value : "",
              claimed_value_usd: claimed ? claimed.value : "",
              actual_revenue_usd: actual ? actual.value : "",
              notes: notes ? notes.value : "",
            }}),
          }});
          const payload = await response.json();
          if (!response.ok || !payload.ok) {{
            throw new Error(payload.error || "save failed");
          }}
          feedback.textContent = `Saved ${{key}}`;
          setTimeout(() => window.location.reload(), 500);
        }} catch (error) {{
          feedback.textContent = error.message || "save failed";
          feedback.classList.add("error");
        }} finally {{
          saveButton.disabled = false;
        }}
      }});
    }}
  </script>
</body>
</html>
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render a static NTG dashboard.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("bounty_missions/tools/ntg/out"),
        help="Directory containing latest service outputs.",
    )
    parser.add_argument(
        "--workspace-dir",
        type=Path,
        default=Path("bounty_missions/workspaces"),
        help="Workspace root.",
    )
    parser.add_argument(
        "--target",
        type=Path,
        default=Path("bounty_missions/tools/ntg/out/site/index.html"),
        help="Output HTML path.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    render_dashboard(args.output_dir, args.workspace_dir, args.target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
