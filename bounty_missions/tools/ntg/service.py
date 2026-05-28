from __future__ import annotations

import argparse
import json
import os
import subprocess
import threading
import time
import warnings
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

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
UTC = timezone.utc
DEFAULT_HISTORY_LIMIT = 200
DEFAULT_WEB_HOST = "127.0.0.1"
DEFAULT_WEB_PORT = 8765
DEFAULT_BUSINESS_CONFIG = {
    "history_limit": DEFAULT_HISTORY_LIMIT,
    "pursue_avg_payout_usd": 300.0,
    "review_avg_payout_usd": 120.0,
    "pursue_close_rate": 0.18,
    "review_close_rate": 0.07,
}


class SafeFormatDict(dict):
    def __missing__(self, key: str) -> str:
        return ""


class ServiceController:
    def __init__(self) -> None:
        self.trigger_event = threading.Event()
        self._lock = threading.RLock()
        self.output_dir: Path | None = None
        self.dashboard_target: Path | None = None
        self.web_url = ""
        self.state: dict[str, Any] = {
            "run_in_progress": False,
            "last_run_started_at_utc": "",
            "last_run_finished_at_utc": "",
            "last_exit_code": None,
            "last_error": "",
            "manual_trigger_count": 0,
            "last_manual_trigger_at_utc": "",
            "web_url": "",
        }

    def configure(
        self,
        *,
        output_dir: Path,
        dashboard_target: Path,
        web_url: str = "",
    ) -> None:
        with self._lock:
            self.output_dir = output_dir
            self.dashboard_target = dashboard_target
            self.web_url = web_url
            self.state["web_url"] = web_url
            self._write_runtime_status_locked()

    def mark_run_started(self) -> None:
        with self._lock:
            self.state["run_in_progress"] = True
            self.state["last_run_started_at_utc"] = utc_now()
            self.state["last_error"] = ""
            self._write_runtime_status_locked()

    def mark_run_finished(self, *, exit_code: int) -> None:
        with self._lock:
            self.state["run_in_progress"] = False
            self.state["last_run_finished_at_utc"] = utc_now()
            self.state["last_exit_code"] = exit_code
            self._write_runtime_status_locked()

    def mark_run_failed(self, detail: str) -> None:
        with self._lock:
            self.state["run_in_progress"] = False
            self.state["last_run_finished_at_utc"] = utc_now()
            self.state["last_exit_code"] = 1
            self.state["last_error"] = detail
            self._write_runtime_status_locked()

    def request_run_now(self) -> dict[str, Any]:
        with self._lock:
            self.state["manual_trigger_count"] = int(self.state.get("manual_trigger_count", 0)) + 1
            self.state["last_manual_trigger_at_utc"] = utc_now()
            self._write_runtime_status_locked()
        self.trigger_event.set()
        return self.snapshot()

    def wait_for_next_cycle(self, interval_seconds: int) -> str:
        triggered = self.trigger_event.wait(max(0, interval_seconds))
        if triggered:
            self.trigger_event.clear()
            return "manual"
        return "timer"

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            payload = dict(self.state)
            if self.output_dir is not None:
                payload["output_dir"] = str(self.output_dir)
            if self.dashboard_target is not None:
                payload["dashboard_target"] = str(self.dashboard_target)
            return payload

    def _write_runtime_status_locked(self) -> None:
        if self.output_dir is None:
            return
        runtime_path = self.output_dir / "runtime_status.json"
        write_json(runtime_path, self.snapshot())


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


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


def load_config(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_previous_queue(output_dir: Path) -> dict:
    return load_json_file(output_dir / "current_queue.json", {"count": 0, "items": []})


def load_business_config(config: dict) -> dict[str, float | int]:
    payload = dict(DEFAULT_BUSINESS_CONFIG)
    payload.update(config.get("business", {}) or {})
    payload["history_limit"] = max(20, int(payload.get("history_limit", DEFAULT_HISTORY_LIMIT)))
    payload["pursue_avg_payout_usd"] = float(payload.get("pursue_avg_payout_usd", 300.0))
    payload["review_avg_payout_usd"] = float(payload.get("review_avg_payout_usd", 120.0))
    payload["pursue_close_rate"] = float(payload.get("pursue_close_rate", 0.18))
    payload["review_close_rate"] = float(payload.get("review_close_rate", 0.07))
    return payload


def clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def estimate_queue_item_value(item: dict, business_config: dict[str, float | int]) -> dict:
    recommendation = str(item.get("recommendation", "skip"))
    if recommendation == "pursue":
        average_payout = float(business_config["pursue_avg_payout_usd"])
        close_rate = float(business_config["pursue_close_rate"])
    elif recommendation == "review":
        average_payout = float(business_config["review_avg_payout_usd"])
        close_rate = float(business_config["review_close_rate"])
    else:
        average_payout = 0.0
        close_rate = 0.0

    bounty_confidence = float(item.get("bounty_confidence", 0))
    actionability = float(item.get("actionability", 0))
    competition_risk = float(item.get("competition_risk", 0))

    confidence_factor = clamp(bounty_confidence / 18.0, 0.45, 1.6)
    actionability_factor = clamp(actionability / 10.0, 0.5, 1.4)
    competition_factor = clamp(1.0 - (competition_risk / 70.0), 0.2, 1.0)
    expected_value = average_payout * close_rate * confidence_factor * actionability_factor * competition_factor

    return {
        **item,
        "average_payout_usd": round(average_payout, 2),
        "close_rate": close_rate,
        "confidence_factor": round(confidence_factor, 3),
        "actionability_factor": round(actionability_factor, 3),
        "competition_factor": round(competition_factor, 3),
        "estimated_value_usd": round(expected_value, 2),
    }


def summarize_queue_value(queue_payload: dict, business_config: dict[str, float | int]) -> dict:
    valued_items = [
        estimate_queue_item_value(item, business_config)
        for item in queue_payload.get("items", [])
        if item.get("recommendation") in {"pursue", "review"}
    ]
    valued_items.sort(key=lambda item: item["estimated_value_usd"], reverse=True)
    estimated_total = round(sum(item["estimated_value_usd"] for item in valued_items), 2)
    return {
        "count": len(valued_items),
        "estimated_active_revenue_usd": estimated_total,
        "items": valued_items,
    }


def compact_automation_results(results: list[dict]) -> list[dict]:
    compacted: list[dict] = []
    for item in results:
        workspace_dir = Path(item.get("workspace_dir", "")) if item.get("workspace_dir") else None
        worklog_path = workspace_dir / "WORKLOG.md" if workspace_dir else None
        codex_message_path = workspace_dir / "codex-last-message.txt" if workspace_dir else None
        compacted.append(
            {
                "repo": item.get("repo"),
                "number": item.get("number"),
                "status": item.get("status", "unknown"),
                "exit_code": item.get("exit_code"),
                "detail": item.get("detail", ""),
                "workspace_dir": item.get("workspace_dir", ""),
                "worklog_exists": bool(worklog_path and worklog_path.exists()),
                "worklog_path": str(worklog_path) if worklog_path and worklog_path.exists() else "",
                "codex_message_exists": bool(codex_message_path and codex_message_path.exists()),
                "codex_message_path": str(codex_message_path) if codex_message_path and codex_message_path.exists() else "",
            }
        )
    return compacted


def summarize_automation(compact_results: list[dict]) -> dict:
    summary = {
        "total": len(compact_results),
        "ok": 0,
        "failed": 0,
        "timeout": 0,
        "skipped": 0,
        "worklogs": 0,
    }
    for item in compact_results:
        status = str(item.get("status", "unknown"))
        if status in summary:
            summary[status] += 1
        summary["worklogs"] += 1 if item.get("worklog_exists") else 0
    return summary


def append_monitoring_history(
    output_dir: Path,
    *,
    run_entry: dict,
    history_limit: int,
) -> dict:
    history_path = output_dir / "monitoring_history.json"
    payload = load_json_file(history_path, {"runs": []})
    runs = payload.get("runs", [])
    if not isinstance(runs, list):
        runs = []
    runs.append(run_entry)
    runs = runs[-history_limit:]
    payload = {
        "generated_at_utc": utc_now(),
        "history_limit": history_limit,
        "runs": runs,
    }
    write_json(history_path, payload)
    return payload


def build_monitoring_metrics(
    *,
    history_payload: dict,
    queue_value_summary: dict,
    business_config: dict[str, float | int],
) -> dict:
    runs = history_payload.get("runs", [])
    if not isinstance(runs, list):
        runs = []

    successful_runs = sum(1 for item in runs if int(item.get("exit_code", 1)) == 0)
    total_duration = sum(float(item.get("duration_seconds", 0.0)) for item in runs)
    total_candidates = sum(int(item.get("merged_count", 0)) for item in runs)
    total_queue_count = sum(int(item.get("queue_count", 0)) for item in runs)
    total_ai_runs = sum(int(item.get("automation", {}).get("total", 0)) for item in runs)
    total_ai_ok = sum(int(item.get("automation", {}).get("ok", 0)) for item in runs)
    total_ai_worklogs = sum(int(item.get("automation", {}).get("worklogs", 0)) for item in runs)

    cutoff_7d = datetime.now(UTC) - timedelta(days=7)
    recent_7d_runs = [
        item for item in runs
        if (parse_utc(str(item.get("finished_at_utc", ""))) or datetime.min.replace(tzinfo=UTC)) >= cutoff_7d
    ]
    estimated_new_revenue_7d = round(
        sum(float(item.get("estimated_new_queue_revenue_usd", 0.0)) for item in recent_7d_runs),
        2,
    )

    recent_runs = list(reversed(runs[-20:]))
    recent_automation: list[dict] = []
    for run in reversed(runs):
        for item in run.get("automation_results", []):
            recent_automation.append(
                {
                    **item,
                    "run_finished_at_utc": run.get("finished_at_utc", ""),
                }
            )
    recent_automation = recent_automation[:20]

    return {
        "generated_at_utc": utc_now(),
        "business_model": {
            "pursue_avg_payout_usd": float(business_config["pursue_avg_payout_usd"]),
            "review_avg_payout_usd": float(business_config["review_avg_payout_usd"]),
            "pursue_close_rate": float(business_config["pursue_close_rate"]),
            "review_close_rate": float(business_config["review_close_rate"]),
        },
        "totals": {
            "runs": len(runs),
            "successful_runs": successful_runs,
            "success_rate": round((successful_runs / len(runs)) * 100, 1) if runs else 0.0,
            "average_duration_seconds": round(total_duration / len(runs), 2) if runs else 0.0,
            "total_candidates": total_candidates,
            "total_queue_count": total_queue_count,
            "total_ai_runs": total_ai_runs,
            "total_ai_ok": total_ai_ok,
            "total_ai_worklogs": total_ai_worklogs,
            "estimated_active_revenue_usd": round(queue_value_summary.get("estimated_active_revenue_usd", 0.0), 2),
            "estimated_new_revenue_7d_usd": estimated_new_revenue_7d,
        },
        "current_queue_value": queue_value_summary,
        "recent_runs": recent_runs,
        "recent_automation": recent_automation,
    }


def build_run_entry(
    *,
    result: dict,
    queue_payload: dict,
    previous_queue_payload: dict,
    prepared_workspaces: list[dict],
    automation_results: list[dict],
    duration_seconds: float,
    run_started_at_utc: str,
    run_finished_at_utc: str,
    business_config: dict[str, float | int],
    notification_payload: dict,
) -> tuple[dict, dict]:
    queue_value_summary = summarize_queue_value(queue_payload, business_config)
    valued_by_url = {
        item.get("url"): item
        for item in queue_value_summary.get("items", [])
        if item.get("url")
    }
    previous_urls = {item.get("url") for item in previous_queue_payload.get("items", [])}
    estimated_new_queue_revenue_usd = round(
        sum(
            float(item.get("estimated_value_usd", 0.0))
            for url, item in valued_by_url.items()
            if url not in previous_urls
        ),
        2,
    )

    compact_results = compact_automation_results(automation_results)
    automation_summary = summarize_automation(compact_results)
    run_entry = {
        "run_id": str(result.get("snapshot_dir", run_finished_at_utc)).split("/")[-1],
        "started_at_utc": run_started_at_utc,
        "finished_at_utc": run_finished_at_utc,
        "duration_seconds": round(duration_seconds, 2),
        "exit_code": int(result.get("exit_code", 1)),
        "merged_count": int(result.get("merged_count", 0)),
        "queue_count": int(queue_payload.get("count", 0)),
        "pursue_count": int(result.get("pursue_count", 0)),
        "review_count": int(result.get("review_count", 0)),
        "prepared_workspace_count": len(prepared_workspaces),
        "new_queue_count": int(notification_payload.get("new_queue_count", 0)),
        "changed_queue_count": int(notification_payload.get("changed_queue_count", 0)),
        "estimated_active_revenue_usd": round(queue_value_summary.get("estimated_active_revenue_usd", 0.0), 2),
        "estimated_new_queue_revenue_usd": estimated_new_queue_revenue_usd,
        "automation": automation_summary,
        "automation_results": compact_results,
    }
    return run_entry, queue_value_summary


def write_service_state(output_dir: Path, payload: dict) -> None:
    state_path = output_dir / "service_state.json"
    state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def build_web_config(config: dict) -> dict[str, Any]:
    web_config = config.get("web", {}) or {}
    return {
        "enabled": bool(web_config.get("enabled", False)),
        "host": str(web_config.get("host", DEFAULT_WEB_HOST)),
        "port": int(web_config.get("port", DEFAULT_WEB_PORT)),
    }


def start_control_server(
    *,
    output_dir: Path,
    dashboard_target: Path,
    controller: ServiceController,
    web_config: dict[str, Any],
) -> ThreadingHTTPServer | None:
    if not web_config.get("enabled"):
        return None

    host = str(web_config["host"])
    port = int(web_config["port"])

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return

        def _send_json(self, payload: dict, status: int = HTTPStatus.OK) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_text(self, text: str, content_type: str, status: int = HTTPStatus.OK) -> None:
            body = text.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path in {"/", "/index.html"}:
                if dashboard_target.exists():
                    self._send_text(
                        dashboard_target.read_text(encoding="utf-8"),
                        "text/html; charset=utf-8",
                    )
                    return
                self._send_text(
                    "<html><body><h1>NTG Radar</h1><p>Dashboard not generated yet.</p></body></html>",
                    "text/html; charset=utf-8",
                    HTTPStatus.SERVICE_UNAVAILABLE,
                )
                return
            if path == "/api/status":
                payload = {
                    "runtime": controller.snapshot(),
                    "service_state": load_json_file(output_dir / "service_state.json", {}),
                    "monitoring_metrics": load_json_file(output_dir / "monitoring_metrics.json", {}),
                }
                self._send_json(payload)
                return
            if path == "/healthz":
                self._send_json({"ok": True, "generated_at_utc": utc_now()})
                return
            self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/api/run-now":
                payload = controller.request_run_now()
                self._send_json(
                    {
                        "accepted": True,
                        "message": "manual run requested",
                        "runtime": payload,
                    }
                )
                return
            self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)

    server = ThreadingHTTPServer((host, port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    console.print(f"[green]web[/green] dashboard at http://{host}:{port}/")
    return server


def run_once(config_path: Path, controller: ServiceController | None = None) -> int:
    config = load_config(config_path)
    pipeline_args = build_pipeline_args(config)
    output_dir = Path(pipeline_args.output_dir)
    repo_root = config_path.resolve().parents[3]
    service_config = config.get("service", {})
    business_config = load_business_config(config)
    web_config = build_web_config(config)
    dashboard_target = Path(
        service_config.get("dashboard_target", "bounty_missions/tools/ntg/out/site/index.html")
    )
    workspace_dir = Path(service_config.get("workspace_dir", "bounty_missions/workspaces"))
    web_url = f"http://{web_config['host']}:{web_config['port']}/" if web_config.get("enabled") else ""

    if controller is not None:
        controller.configure(output_dir=output_dir, dashboard_target=dashboard_target, web_url=web_url)
        controller.mark_run_started()

    previous_queue = load_previous_queue(output_dir)
    run_started_at_utc = utc_now()
    started_at = time.perf_counter()
    notification_payload = {
        "queue_count": 0,
        "new_queue_count": 0,
        "changed_queue_count": 0,
        "delivery_status": "not-run",
        "delivery_detail": "",
    }

    try:
        result = run_pipeline(pipeline_args)
        prepared = auto_prepare_workspaces(config, output_dir)
        queue_payload = load_json_file(output_dir / "current_queue.json", {"count": 0, "items": []})
        run_summary = load_json_file(output_dir / "run_summary.json", {})
        changes_summary = load_json_file(output_dir / "changes_summary.json", {})

        state_payload = {
            "config": str(config_path),
            "pipeline_result": result,
            "prepared_workspaces": prepared,
            "dashboard": str(dashboard_target),
            "current_queue": str(output_dir / "current_queue.json"),
            "web_url": web_url,
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
        notification_payload = {
            "queue_count": notification_result.queue_count,
            "new_queue_count": notification_result.new_queue_count,
            "changed_queue_count": notification_result.changed_queue_count,
            "delivery_status": notification_result.delivery_status,
            "delivery_detail": notification_result.delivery_detail,
            "latest_json_path": notification_result.latest_json_path,
            "latest_markdown_path": notification_result.latest_markdown_path,
        }
        automation_results = run_automation(
            config,
            queue_payload=queue_payload,
            previous_queue_payload=previous_queue,
            prepared_workspaces=prepared,
            repo_root=repo_root,
            output_dir=output_dir,
        )
        duration_seconds = time.perf_counter() - started_at
        run_finished_at_utc = utc_now()
        run_entry, queue_value_summary = build_run_entry(
            result=result,
            queue_payload=queue_payload,
            previous_queue_payload=previous_queue,
            prepared_workspaces=prepared,
            automation_results=automation_results,
            duration_seconds=duration_seconds,
            run_started_at_utc=run_started_at_utc,
            run_finished_at_utc=run_finished_at_utc,
            business_config=business_config,
            notification_payload=notification_payload,
        )
        history_payload = append_monitoring_history(
            output_dir,
            run_entry=run_entry,
            history_limit=int(business_config["history_limit"]),
        )
        monitoring_metrics = build_monitoring_metrics(
            history_payload=history_payload,
            queue_value_summary=queue_value_summary,
            business_config=business_config,
        )
        write_json(output_dir / "monitoring_metrics.json", monitoring_metrics)

        state_payload["notifications"] = notification_payload
        state_payload["automation"] = automation_results
        state_payload["monitoring"] = {
            "history_path": str(output_dir / "monitoring_history.json"),
            "metrics_path": str(output_dir / "monitoring_metrics.json"),
            "estimated_active_revenue_usd": queue_value_summary.get("estimated_active_revenue_usd", 0.0),
        }
        state_payload["runtime"] = controller.snapshot() if controller is not None else {}
        write_service_state(output_dir, state_payload)
        render_dashboard(output_dir, workspace_dir, dashboard_target)

        if controller is not None:
            controller.mark_run_finished(exit_code=int(result.get("exit_code", 0)))
        console.print(f"[green]dashboard[/green] {dashboard_target}")
        return int(result.get("exit_code", 0))
    except Exception as exc:
        duration_seconds = time.perf_counter() - started_at
        run_finished_at_utc = utc_now()
        failure_result = {
            "exit_code": 1,
            "snapshot_dir": "",
            "output_dir": str(output_dir),
            "merged_count": 0,
            "pursue_count": 0,
            "review_count": 0,
        }
        queue_payload = {"count": 0, "items": []}
        run_entry, queue_value_summary = build_run_entry(
            result=failure_result,
            queue_payload=queue_payload,
            previous_queue_payload=previous_queue,
            prepared_workspaces=[],
            automation_results=[],
            duration_seconds=duration_seconds,
            run_started_at_utc=run_started_at_utc,
            run_finished_at_utc=run_finished_at_utc,
            business_config=business_config,
            notification_payload=notification_payload,
        )
        run_entry["error"] = str(exc)
        history_payload = append_monitoring_history(
            output_dir,
            run_entry=run_entry,
            history_limit=int(business_config["history_limit"]),
        )
        monitoring_metrics = build_monitoring_metrics(
            history_payload=history_payload,
            queue_value_summary=queue_value_summary,
            business_config=business_config,
        )
        write_json(output_dir / "monitoring_metrics.json", monitoring_metrics)
        failure_state = {
            "config": str(config_path),
            "pipeline_result": failure_result,
            "prepared_workspaces": [],
            "dashboard": str(dashboard_target),
            "current_queue": str(output_dir / "current_queue.json"),
            "web_url": web_url,
            "error": str(exc),
            "monitoring": {
                "history_path": str(output_dir / "monitoring_history.json"),
                "metrics_path": str(output_dir / "monitoring_metrics.json"),
                "estimated_active_revenue_usd": 0.0,
            },
            "runtime": controller.snapshot() if controller is not None else {},
        }
        write_service_state(output_dir, failure_state)
        render_dashboard(output_dir, workspace_dir, dashboard_target)
        if controller is not None:
            controller.mark_run_failed(str(exc))
        console.print(f"[bold red]service run failed[/bold red] {exc}")
        return 1


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
    controller = ServiceController()
    service_config = config.get("service", {})
    output_dir = Path(service_config.get("output_dir", "bounty_missions/tools/ntg/out"))
    dashboard_target = Path(
        service_config.get("dashboard_target", "bounty_missions/tools/ntg/out/site/index.html")
    )
    web_config = build_web_config(config)
    web_url = f"http://{web_config['host']}:{web_config['port']}/" if web_config.get("enabled") else ""
    controller.configure(output_dir=output_dir, dashboard_target=dashboard_target, web_url=web_url)
    server = start_control_server(
        output_dir=output_dir,
        dashboard_target=dashboard_target,
        controller=controller,
        web_config=web_config,
    )

    try:
        if not args.watch:
            return run_once(args.config, controller=controller)

        while True:
            run_once(args.config, controller=controller)
            wake_reason = controller.wait_for_next_cycle(interval)
            if wake_reason == "manual":
                console.print("[cyan]manual trigger[/cyan] run requested from dashboard")
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
