from __future__ import annotations

import argparse
import json
from html import escape
from pathlib import Path


def load_json(path: Path, default: object) -> object:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def render_dashboard(
    output_dir: Path,
    workspace_dir: Path,
    target_path: Path,
) -> None:
    run_summary = load_json(output_dir / "run_summary.json", {})
    changes = load_json(output_dir / "changes_summary.json", {})
    triaged = load_json(output_dir / "triaged.json", [])
    alert_summary = load_json(output_dir / "alerts/latest_delivery.json", {})

    workspaces = []
    if workspace_dir.exists():
        for path in sorted(workspace_dir.iterdir()):
            if path.is_dir():
                readme = path / "README.md"
                workspaces.append(
                    {
                        "name": path.name,
                        "path": str(path),
                        "readme": str(readme) if readme.exists() else "",
                    }
                )

    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(
        build_html(run_summary, changes, triaged, workspaces, alert_summary),
        encoding="utf-8",
    )


def build_html(
    run_summary: dict,
    changes: dict,
    triaged: list[dict],
    workspaces: list[dict],
    alert_summary: dict,
) -> str:
    source_counts = run_summary.get("source_counts", {})
    recommendations = run_summary.get("recommendations", {})
    top_candidates = run_summary.get("top_candidates", [])
    triage_policy = run_summary.get("triage_policy", {})
    active_queue_count = int(recommendations.get("pursue", 0)) + int(recommendations.get("review", 0))
    def row(label: str, value: object) -> str:
        return f"<tr><th>{escape(label)}</th><td>{escape(str(value))}</td></tr>"

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
        link = item["readme"] or item["path"]
        workspace_rows.append(
            "<tr>"
            f"<td>{escape(item['name'])}</td>"
            f"<td>{escape(item['path'])}</td>"
            f"<td><code>{escape(link)}</code></td>"
            "</tr>"
        )
    if not workspace_rows:
        workspace_rows.append("<tr><td colspan='3'>No workspaces prepared yet.</td></tr>")

    generated_at = run_summary.get("generated_at_utc", "-")
    alert_status = alert_summary.get("delivery_status", "-")
    alert_new_queue_count = alert_summary.get("new_queue_count", 0)
    alert_changed_queue_count = alert_summary.get("changed_queue_count", 0)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>NTG Radar</title>
  <style>
    :root {{
      --bg: #0c0f14;
      --panel: #151a22;
      --panel-2: #1d2430;
      --text: #e7edf5;
      --muted: #97a6ba;
      --accent: #8fcf7a;
      --warn: #f3c969;
      --danger: #ef8f8f;
      --border: #283140;
      --mono: "SFMono-Regular", Menlo, Consolas, monospace;
    }}
    body {{
      margin: 0;
      background: linear-gradient(180deg, #0b1017 0%, #111722 100%);
      color: var(--text);
      font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    .wrap {{
      max-width: 1200px;
      margin: 0 auto;
      padding: 32px 20px 48px;
    }}
    h1, h2 {{
      margin: 0 0 12px;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 16px;
      margin: 16px 0 24px;
    }}
    .card {{
      background: rgba(21, 26, 34, 0.92);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 16px;
      box-shadow: 0 10px 30px rgba(0,0,0,0.2);
    }}
    .metric {{
      font-size: 28px;
      font-weight: 700;
      margin-top: 6px;
    }}
    .muted {{
      color: var(--muted);
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      background: rgba(21, 26, 34, 0.92);
      border: 1px solid var(--border);
      border-radius: 14px;
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
    .section {{
      margin-top: 28px;
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>NTG Radar</h1>
    <div class="muted">Generated at {escape(str(generated_at))}</div>

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
      <h2>Changes</h2>
      <div class="card"><ul>{''.join(change_items)}</ul></div>
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
          <tr><th>Name</th><th>Path</th><th>README</th></tr>
        </thead>
        <tbody>
          {''.join(workspace_rows)}
        </tbody>
      </table>
    </div>
  </div>
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
        help="Directory containing prepared workspaces.",
    )
    parser.add_argument(
        "--target",
        type=Path,
        default=Path("bounty_missions/tools/ntg/out/site/index.html"),
        help="Dashboard HTML output path.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    render_dashboard(args.output_dir, args.workspace_dir, args.target)
    print(args.target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
