from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

UTC = timezone.utc


@dataclass
class NotificationResult:
    generated_at_utc: str
    queue_count: int
    new_queue_count: int
    changed_queue_count: int
    should_send: bool
    delivery_status: str
    delivery_detail: str
    latest_json_path: str
    latest_markdown_path: str
    history_json_path: str
    history_markdown_path: str


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def queue_by_url(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["url"]: item for item in payload.get("items", [])}


def build_alert_payload(
    *,
    queue_payload: dict[str, Any],
    previous_queue_payload: dict[str, Any],
    run_summary: dict[str, Any],
    changes_summary: dict[str, Any],
    service_state: dict[str, Any],
) -> dict[str, Any]:
    current_by_url = queue_by_url(queue_payload)
    previous_by_url = queue_by_url(previous_queue_payload)

    new_items = [
        item
        for url, item in current_by_url.items()
        if url not in previous_by_url
    ]
    changed_items = []
    for url, item in current_by_url.items():
        previous = previous_by_url.get(url)
        if not previous:
            continue
        if previous.get("recommendation") != item.get("recommendation"):
            changed_items.append(
                {
                    "repo": item["repo"],
                    "number": item["number"],
                    "title": item["title"],
                    "url": item["url"],
                    "previous_recommendation": previous.get("recommendation"),
                    "current_recommendation": item.get("recommendation"),
                    "decision_reason": item.get("decision_reason"),
                }
            )

    generated_at_utc = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    snapshot_dir = (
        service_state.get("pipeline_result", {}).get("snapshot_dir")
        or run_summary.get("snapshot_dir")
        or ""
    )
    top_item = queue_payload.get("items", [{}])[:1]

    return {
        "generated_at_utc": generated_at_utc,
        "queue_count": queue_payload.get("count", 0),
        "triage_profile": queue_payload.get("triage_profile") or run_summary.get("triage_policy", {}).get("profile"),
        "snapshot_dir": snapshot_dir,
        "new_queue_items": new_items,
        "changed_queue_items": changed_items,
        "changes_summary": {
            "new_items": changes_summary.get("new_items", []),
            "recommendation_changes": changes_summary.get("recommendation_changes", []),
        },
        "top_queue_item": top_item[0] if top_item else {},
        "items": queue_payload.get("items", []),
    }


def should_send_alert(notification_config: dict[str, Any], payload: dict[str, Any]) -> bool:
    mode = str(notification_config.get("send_when", "changes_only"))
    queue_count = int(payload.get("queue_count", 0))
    new_queue_count = len(payload.get("new_queue_items", []))
    changed_queue_count = len(payload.get("changed_queue_items", []))

    if mode == "always":
        return True
    if mode == "nonempty_queue":
        return queue_count > 0
    return new_queue_count > 0 or changed_queue_count > 0


def render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# NTG Alert",
        "",
        f"- generated_at_utc: `{payload.get('generated_at_utc', '-')}`",
        f"- triage_profile: `{payload.get('triage_profile', '-')}`",
        f"- queue_count: `{payload.get('queue_count', 0)}`",
        f"- snapshot_dir: `{payload.get('snapshot_dir', '-')}`",
        "",
        "## New Queue Items",
        "",
    ]

    new_items = payload.get("new_queue_items", [])
    if new_items:
        for item in new_items:
            lines.append(
                f"- `{item['recommendation']}` `{item['repo']}#{item['number']}` "
                f"score={item['total_score']} [{item['title']}]({item['url']})"
            )
    else:
        lines.append("_No new queue items._")

    lines.extend(["", "## Changed Queue Items", ""])
    changed_items = payload.get("changed_queue_items", [])
    if changed_items:
        for item in changed_items:
            lines.append(
                f"- `{item['repo']}#{item['number']}` "
                f"`{item['previous_recommendation']}` -> `{item['current_recommendation']}` "
                f"[{item['title']}]({item['url']})"
            )
    else:
        lines.append("_No queue recommendation changes._")

    lines.extend(["", "## Current Queue", ""])
    queue_items = payload.get("items", [])
    if queue_items:
        for item in queue_items:
            signal_text = ", ".join(item.get("decision_signals", [])[:4]) or "-"
            lines.append(
                f"- `{item['recommendation']}` `{item['repo']}#{item['number']}` "
                f"score={item['total_score']} {item.get('decision_reason', '-')}"
            )
            lines.append(f"  signals: `{signal_text}`")
    else:
        lines.append("_Queue is empty._")

    return "\n".join(lines) + "\n"


def send_webhook(notification_config: dict[str, Any], payload: dict[str, Any]) -> tuple[str, str]:
    webhook_url = notification_config.get("webhook_url")
    if not webhook_url:
        return "disabled", "webhook_url is not configured"

    timeout_seconds = int(notification_config.get("timeout_seconds", 10))
    headers = {
        "Content-Type": "application/json",
        **(notification_config.get("headers") or {}),
    }
    try:
        response = requests.post(
            str(webhook_url),
            headers=headers,
            json=payload,
            timeout=timeout_seconds,
        )
    except Exception as exc:
        return "failed", str(exc)

    if response.ok:
        return "sent", f"http {response.status_code}"
    return "failed", f"http {response.status_code}"


def notify(
    *,
    output_dir: Path,
    queue_payload: dict[str, Any],
    previous_queue_payload: dict[str, Any],
    run_summary: dict[str, Any],
    changes_summary: dict[str, Any],
    service_state: dict[str, Any],
    notification_config: dict[str, Any],
) -> NotificationResult:
    payload = build_alert_payload(
        queue_payload=queue_payload,
        previous_queue_payload=previous_queue_payload,
        run_summary=run_summary,
        changes_summary=changes_summary,
        service_state=service_state,
    )
    should_send = should_send_alert(notification_config, payload)

    alerts_dir = output_dir / "alerts"
    history_dir = alerts_dir / "history"
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    latest_json_path = alerts_dir / "latest_alert.json"
    latest_markdown_path = alerts_dir / "latest_alert.md"
    history_json_path = history_dir / f"{stamp}.json"
    history_markdown_path = history_dir / f"{stamp}.md"

    markdown = render_markdown(payload)
    write_json(latest_json_path, payload)
    write_text(latest_markdown_path, markdown)
    write_json(history_json_path, payload)
    write_text(history_markdown_path, markdown)

    if should_send:
        delivery_status, delivery_detail = send_webhook(notification_config, payload)
    else:
        delivery_status, delivery_detail = "skipped", "send_when condition not met"

    result = NotificationResult(
        generated_at_utc=payload["generated_at_utc"],
        queue_count=int(payload.get("queue_count", 0)),
        new_queue_count=len(payload.get("new_queue_items", [])),
        changed_queue_count=len(payload.get("changed_queue_items", [])),
        should_send=should_send,
        delivery_status=delivery_status,
        delivery_detail=delivery_detail,
        latest_json_path=str(latest_json_path),
        latest_markdown_path=str(latest_markdown_path),
        history_json_path=str(history_json_path),
        history_markdown_path=str(history_markdown_path),
    )
    write_json(alerts_dir / "latest_delivery.json", asdict(result))
    return result
