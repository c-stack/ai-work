from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
import warnings
from pathlib import Path

import yaml
warnings.filterwarnings(
    "ignore",
    message="urllib3 v2 only supports OpenSSL 1.1.1+",
    module="urllib3",
)

from dashboard import render_dashboard
from github_auth import load_github_token
from notifier import load_json as load_json_file
from notifier import notify
from pipeline import default_args as default_pipeline_args
from pipeline import run_pipeline
from rich.console import Console
from workon import GitHubWorkspacePreparer

console = Console()


class SafeFormatDict(dict):
    def __missing__(self, key: str) -> str:
        return ""


def build_pipeline_args(config: dict) -> argparse.Namespace:
    args = default_pipeline_args()
    service_config = config.get("service", {})
    pipeline_config = config.get("pipeline", {})

    args.auth_file = Path(service_config["auth_file"]) if service_config.get("auth_file") else None
    args.output_dir = Path(service_config.get("output_dir", "bounty_missions/tools/ntg/out"))
    args.snapshot_root = Path(service_config.get("snapshot_root", "bounty_missions/tools/ntg/out/runs"))
    args.config = Path(pipeline_config.get("targets_config", "bounty_missions/tools/ntg/targets.example.yaml"))
    args.algora_org_file = Path(pipeline_config["algora_org_file"]) if pipeline_config.get("algora_org_file") else None
    args.repo_seed_file = Path(pipeline_config["repo_seed_file"]) if pipeline_config.get("repo_seed_file") else None
    args.reputation_config = Path(pipeline_config["reputation_config"]) if pipeline_config.get("reputation_config") else None
    args.learned_reputation_path = Path(pipeline_config["learned_reputation_path"]) if pipeline_config.get("learned_reputation_path") else None
    args.refresh_repo_seeds = bool(pipeline_config.get("refresh_repo_seeds", False))
    args.days = int(pipeline_config.get("days", 1))
    args.limit = int(pipeline_config.get("limit", 1))
    args.min_score = int(pipeline_config.get("min_score", 25))
    args.triage_limit = int(pipeline_config.get("triage_limit", 12))
    args.history_window_runs = int(pipeline_config.get("history_window_runs", 12))
    args.history_min_skip_runs = int(pipeline_config.get("history_min_skip_runs", 4))
    args.history_min_unique_issues = int(pipeline_config.get("history_min_unique_issues", 2))
    triage_config = config.get("triage", {})
    args.triage_profile = str(triage_config.get("profile", "strict"))
    args.skip_competition_risk = triage_config.get("skip_competition_risk")
    args.pursue_min_bounty_confidence = triage_config.get("pursue_min_bounty_confidence")
    args.pursue_min_actionability = triage_config.get("pursue_min_actionability")
    args.pursue_max_competition_risk = triage_config.get("pursue_max_competition_risk")
    args.review_min_bounty_confidence = triage_config.get("review_min_bounty_confidence")
    args.review_min_actionability = triage_config.get("review_min_actionability")
    args.weak_bounty_penalty_threshold = triage_config.get("weak_bounty_penalty_threshold")
    return args


def auto_prepare_workspaces(config: dict, output_dir: Path) -> list[dict]:
    queue_path = output_dir / "current_queue.json"
    if queue_path.exists():
        queue_payload = json.loads(queue_path.read_text(encoding="utf-8"))
        triaged = queue_payload.get("items", [])
    else:
        triaged_path = output_dir / "triaged.json"
        if not triaged_path.exists():
            return []
        triaged = json.loads(triaged_path.read_text(encoding="utf-8"))

    if not triaged:
        return []
    service_config = config.get("service", {})
    workspace_config = config.get("workspace", {})
    allowed_recos = set(workspace_config.get("recommendations", ["pursue", "review"]))
    max_items = int(workspace_config.get("max_items", 3))
    workspace_dir = Path(service_config.get("workspace_dir", "bounty_missions/workspaces"))

    token, _ = load_github_token(
        Path(service_config["auth_file"]) if service_config.get("auth_file") else None
    )
    preparer = GitHubWorkspacePreparer(token=token)

    prepared = []
    for item in triaged:
        if item.get("recommendation") not in allowed_recos:
            continue
        context = preparer.prepare(item["repo"], int(item["number"]), workspace_dir)
        prepared.append(
            {
                "repo": item["repo"],
                "number": item["number"],
                "workspace_dir": str(context.workspace_dir),
            }
        )
        if len(prepared) >= max_items:
            break
    return prepared


def queue_item_key(item: dict) -> str:
    return f"{item['repo']}#{int(item['number'])}"


def select_automation_items(
    queue_payload: dict,
    previous_queue_payload: dict,
    *,
    trigger_mode: str,
    allowed_recommendations: set[str],
) -> list[dict]:
    current_items = queue_payload.get("items", [])
    if trigger_mode == "all_queue_items":
        candidates = current_items
    else:
        previous_urls = {item["url"] for item in previous_queue_payload.get("items", [])}
        candidates = [item for item in current_items if item.get("url") not in previous_urls]

    return [
        item for item in candidates
        if item.get("recommendation") in allowed_recommendations
    ]


def run_automation(
    config: dict,
    *,
    queue_payload: dict,
    previous_queue_payload: dict,
    prepared_workspaces: list[dict],
    repo_root: Path,
    output_dir: Path,
) -> list[dict]:
    automation_config = config.get("automation", {})
    command_template = automation_config.get("command") or []
    if not command_template:
        return []

    trigger_mode = str(automation_config.get("trigger_mode", "new_queue_items"))
    allowed_recommendations = set(automation_config.get("trigger_on", ["pursue"]))
    max_items = int(automation_config.get("max_items", 1))
    timeout_seconds = int(automation_config.get("timeout_seconds", 1800))
    extra_env = automation_config.get("extra_env") or {}

    selected_items = select_automation_items(
        queue_payload,
        previous_queue_payload,
        trigger_mode=trigger_mode,
        allowed_recommendations=allowed_recommendations,
    )
    prepared_index = {
        queue_item_key(item): item["workspace_dir"]
        for item in prepared_workspaces
    }

    results: list[dict] = []
    runs_dir = output_dir / "automation"
    runs_dir.mkdir(parents=True, exist_ok=True)

    for item in selected_items[:max_items]:
        item_key = queue_item_key(item)
        workspace_dir = prepared_index.get(item_key, "")
        if not workspace_dir:
            results.append(
                {
                    "repo": item["repo"],
                    "number": int(item["number"]),
                    "status": "skipped",
                    "detail": "workspace not prepared",
                }
            )
            continue

        context = SafeFormatDict(
            {
                **item,
                "number": int(item["number"]),
                "workspace_dir": workspace_dir,
                "repo_root": str(repo_root),
                "output_dir": str(output_dir),
            }
        )
        command = [str(part).format_map(context) for part in command_template]
        env = os.environ.copy()
        env.update({key: str(value).format_map(context) for key, value in extra_env.items()})

        try:
            completed = subprocess.run(
                command,
                cwd=workspace_dir,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
            status = "ok" if completed.returncode == 0 else "failed"
            result = {
                "repo": item["repo"],
                "number": int(item["number"]),
                "status": status,
                "exit_code": completed.returncode,
                "command": command,
                "workspace_dir": workspace_dir,
                "stdout_tail": completed.stdout[-4000:],
                "stderr_tail": completed.stderr[-4000:],
            }
        except subprocess.TimeoutExpired as exc:
            result = {
                "repo": item["repo"],
                "number": int(item["number"]),
                "status": "timeout",
                "command": command,
                "workspace_dir": workspace_dir,
                "detail": str(exc),
            }

        result_path = runs_dir / f"{item['repo'].replace('/', '__')}__issue{int(item['number'])}.json"
        result_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
        results.append(result)

    (runs_dir / "latest_runs.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    return results


def write_service_state(output_dir: Path, payload: dict) -> None:
    state_path = output_dir / "service_state.json"
    state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load_config(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_previous_queue(output_dir: Path) -> dict:
    return load_json_file(output_dir / "current_queue.json", {"count": 0, "items": []})


def run_once(config_path: Path) -> int:
    config = load_config(config_path)
    pipeline_args = build_pipeline_args(config)
    output_dir = Path(pipeline_args.output_dir)
    repo_root = config_path.resolve().parents[3]
    previous_queue = load_previous_queue(output_dir)
    result = run_pipeline(pipeline_args)
    prepared = auto_prepare_workspaces(config, output_dir)

    service_config = config.get("service", {})
    dashboard_target = Path(
        service_config.get("dashboard_target", "bounty_missions/tools/ntg/out/site/index.html")
    )
    workspace_dir = Path(service_config.get("workspace_dir", "bounty_missions/workspaces"))

    queue_payload = load_json_file(output_dir / "current_queue.json", {"count": 0, "items": []})
    run_summary = load_json_file(output_dir / "run_summary.json", {})
    changes_summary = load_json_file(output_dir / "changes_summary.json", {})

    state_payload = {
        "config": str(config_path),
        "pipeline_result": result,
        "prepared_workspaces": prepared,
        "dashboard": str(dashboard_target),
        "current_queue": str(output_dir / "current_queue.json"),
    }
    notification_result = notify(
        output_dir=output_dir,
        queue_payload=queue_payload,
        previous_queue_payload=previous_queue,
        run_summary=run_summary,
        changes_summary=changes_summary,
        service_state=state_payload,
        notification_config=config.get("notifications", {}),
    )
    automation_results = run_automation(
        config,
        queue_payload=queue_payload,
        previous_queue_payload=previous_queue,
        prepared_workspaces=prepared,
        repo_root=repo_root,
        output_dir=output_dir,
    )
    state_payload["notifications"] = {
        "queue_count": notification_result.queue_count,
        "new_queue_count": notification_result.new_queue_count,
        "changed_queue_count": notification_result.changed_queue_count,
        "delivery_status": notification_result.delivery_status,
        "delivery_detail": notification_result.delivery_detail,
        "latest_json_path": notification_result.latest_json_path,
        "latest_markdown_path": notification_result.latest_markdown_path,
    }
    state_payload["automation"] = automation_results
    render_dashboard(output_dir, workspace_dir, dashboard_target)

    write_service_state(output_dir, state_payload)
    console.print(f"[green]dashboard[/green] {dashboard_target}")
    return int(result.get("exit_code", 0))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run NTG as a repeatable service.")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("bounty_missions/tools/ntg/service.example.yaml"),
        help="Service config file.",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Run continuously using the configured interval.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_config(args.config)
    interval = int(config.get("service", {}).get("interval_seconds", 3600))

    if not args.watch:
        return run_once(args.config)

    while True:
        run_once(args.config)
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
