from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
from collections import Counter, defaultdict

from algora_source import AlgoraSource
from github_auth import load_github_token
from rich.console import Console
from scanner import (
    GitHubBountyScanner,
    GitHubClient,
    load_repo_filters,
    load_reputation_policy,
    load_targets,
    merge_reputation_policies,
    repo_matches_reputation_policy,
)
from source_discovery import SourceDiscovery
from triager import GitHubIssueTriager, TriagedOpportunity, build_triage_policy

console = Console()
UTC = timezone.utc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the bounty discovery pipeline end-to-end."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("bounty_missions/tools/ntg/targets.example.yaml"),
        help="Scanner target config.",
    )
    parser.add_argument(
        "--auth-file",
        type=Path,
        help="Optional auth file containing a GitHub token.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("bounty_missions/tools/ntg/out"),
        help="Directory for scanner, triage, and queue artifacts.",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=14,
        help="Only scan issues created within the last N days.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=5,
        help="Maximum number of items fetched per keyword search.",
    )
    parser.add_argument(
        "--min-score",
        type=int,
        default=25,
        help="Drop low-signal issues under this scanner score.",
    )
    parser.add_argument(
        "--triage-limit",
        type=int,
        default=10,
        help="Maximum number of discovered items to triage.",
    )
    parser.add_argument(
        "--algora-org-file",
        type=Path,
        help="Optional file with one Algora org slug per line.",
    )
    parser.add_argument(
        "--repo-seed-file",
        type=Path,
        help="Optional file with one owner/name repo per line to extend GitHub scanning.",
    )
    parser.add_argument(
        "--refresh-repo-seeds",
        action="store_true",
        help="Refresh the repo seed file from public community sources before scanning.",
    )
    parser.add_argument(
        "--snapshot-root",
        type=Path,
        default=Path("bounty_missions/tools/ntg/out/runs"),
        help="Directory for timestamped run snapshots.",
    )
    parser.add_argument(
        "--triage-profile",
        default="strict",
        help="Triage policy profile.",
    )
    parser.add_argument("--skip-competition-risk", type=int)
    parser.add_argument("--pursue-min-bounty-confidence", type=int)
    parser.add_argument("--pursue-min-actionability", type=int)
    parser.add_argument("--pursue-max-competition-risk", type=int)
    parser.add_argument("--review-min-bounty-confidence", type=int)
    parser.add_argument("--review-min-actionability", type=int)
    parser.add_argument("--weak-bounty-penalty-threshold", type=int)
    parser.add_argument(
        "--reputation-config",
        type=Path,
        help="Optional YAML file with repo reputation filters.",
    )
    parser.add_argument(
        "--learned-reputation-path",
        type=Path,
        help="Optional JSON/YAML file with learned repo reputation filters.",
    )
    parser.add_argument("--history-window-runs", type=int, default=12)
    parser.add_argument("--history-min-skip-runs", type=int, default=4)
    parser.add_argument("--history-min-unique-issues", type=int, default=2)
    return parser.parse_args()


def default_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--config", type=Path, default=Path("bounty_missions/tools/ntg/targets.example.yaml"))
    parser.add_argument("--auth-file", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path("bounty_missions/tools/ntg/out"))
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--min-score", type=int, default=25)
    parser.add_argument("--triage-limit", type=int, default=10)
    parser.add_argument("--algora-org-file", type=Path)
    parser.add_argument("--repo-seed-file", type=Path)
    parser.add_argument("--refresh-repo-seeds", action="store_true")
    parser.add_argument("--snapshot-root", type=Path, default=Path("bounty_missions/tools/ntg/out/runs"))
    parser.add_argument("--triage-profile", default="strict")
    parser.add_argument("--skip-competition-risk", type=int)
    parser.add_argument("--pursue-min-bounty-confidence", type=int)
    parser.add_argument("--pursue-min-actionability", type=int)
    parser.add_argument("--pursue-max-competition-risk", type=int)
    parser.add_argument("--review-min-bounty-confidence", type=int)
    parser.add_argument("--review-min-actionability", type=int)
    parser.add_argument("--weak-bounty-penalty-threshold", type=int)
    parser.add_argument("--reputation-config", type=Path)
    parser.add_argument("--learned-reputation-path", type=Path)
    parser.add_argument("--history-window-runs", type=int, default=12)
    parser.add_argument("--history-min-skip-runs", type=int, default=4)
    parser.add_argument("--history-min-unique-issues", type=int, default=2)
    return parser.parse_args([])


def save_queue_markdown(items: list[TriagedOpportunity], output_path: Path) -> None:
    filtered = [item for item in items if item.recommendation in {"pursue", "review"}]
    filtered.sort(key=lambda item: item.total_score, reverse=True)

    lines = [
        "# Mission Queue",
        "",
        "| Recommendation | Total | Repo | Issue | Language | Why | Signals | Link |",
        "| --- | ---: | --- | ---: | --- | --- | --- | --- |",
    ]

    for item in filtered:
        reason = item.decision_reason.replace("|", "/")
        signals = ", ".join(item.decision_signals[:4]) or "manual-review"

        lines.append(
            f"| {item.recommendation} | {item.total_score} | {item.repo} | {item.number} | "
            f"{item.repo_primary_language or '-'} | {reason} | {signals} | [open]({item.url}) |"
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def save_current_queue_json(
    items: list[TriagedOpportunity],
    output_path: Path,
    triage_profile: str,
) -> None:
    queue_items = [
        {
            "repo": item.repo,
            "number": item.number,
            "title": item.title,
            "url": item.url,
            "recommendation": item.recommendation,
            "total_score": item.total_score,
            "repo_primary_language": item.repo_primary_language,
            "bounty_confidence": item.bounty_confidence,
            "actionability": item.actionability,
            "bounty_amount_usd": item.bounty_amount_usd,
            "bounty_amount_signal": item.bounty_amount_signal,
            "competition_risk": item.competition_risk,
            "decision_reason": item.decision_reason,
            "decision_signals": item.decision_signals,
        }
        for item in items
        if item.recommendation in {"pursue", "review"}
    ]
    payload = {
        "generated_at_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "triage_profile": triage_profile,
        "count": len(queue_items),
        "items": queue_items,
    }
    write_json(payload, output_path)


def load_algora_orgs(org_file: Path | None) -> list[str]:
    if org_file is None or not org_file.exists():
        return []
    orgs: list[str] = []
    for line in org_file.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            orgs.append(stripped)
    return orgs


def merge_opportunities(*groups: list[dict]) -> list[dict]:
    seen: dict[str, dict] = {}
    for group in groups:
        for item in group:
            url = item["url"]
            current = seen.get(url)
            if current is None or item.get("score", 0) > current.get("score", 0):
                seen[url] = item
    return sorted(seen.values(), key=lambda item: item.get("score", 0), reverse=True)


def filter_opportunities_by_reputation(
    items: list[dict],
    reputation_policy: object,
) -> list[dict]:
    filtered: list[dict] = []
    for item in items:
        repo = item.get("repo")
        if not repo:
            continue
        if repo_matches_reputation_policy(
            str(repo),
            reputation_policy,  # type: ignore[arg-type]
            is_scoped_target=True,
        ):
            continue
        filtered.append(item)
    return filtered


def write_json(payload: object, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load_json(input_path: Path) -> object:
    return json.loads(input_path.read_text(encoding="utf-8"))


def get_previous_snapshot(snapshot_root: Path, current_snapshot: Path) -> Path | None:
    candidates = sorted(
        path
        for path in snapshot_root.iterdir()
        if path.is_dir() and path != current_snapshot
    )
    return candidates[-1] if candidates else None


def summarize_run(
    github_items: list[dict],
    algora_items: list[dict],
    merged_items: list[dict],
    triaged: list[TriagedOpportunity],
) -> dict:
    recommendations = {"pursue": 0, "review": 0, "skip": 0}
    for item in triaged:
        recommendations[item.recommendation] = recommendations.get(item.recommendation, 0) + 1

    return {
        "generated_at_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_counts": {
            "github": len(github_items),
            "algora": len(algora_items),
            "merged": len(merged_items),
        },
        "recommendations": recommendations,
        "top_candidates": [
            {
                "repo": item.repo,
                "number": item.number,
                "title": item.title,
                "recommendation": item.recommendation,
                "total_score": item.total_score,
                "bounty_amount_usd": item.bounty_amount_usd,
                "competition_risk": item.competition_risk,
                "decision_reason": item.decision_reason,
                "decision_signals": item.decision_signals,
            }
            for item in triaged[:10]
        ],
    }


def diff_triaged_runs(previous_items: list[dict], current_items: list[dict]) -> dict:
    previous_by_url = {item["url"]: item for item in previous_items}
    current_by_url = {item["url"]: item for item in current_items}

    new_items = [
        compact_item(item)
        for url, item in current_by_url.items()
        if url not in previous_by_url
    ]
    dropped_items = [
        compact_item(item)
        for url, item in previous_by_url.items()
        if url not in current_by_url
    ]

    recommendation_changes = []
    score_changes = []
    for url, current in current_by_url.items():
        previous = previous_by_url.get(url)
        if previous is None:
            continue
        if previous.get("recommendation") != current.get("recommendation"):
            recommendation_changes.append(
                {
                    "repo": current["repo"],
                    "number": current["number"],
                    "title": current["title"],
                    "url": current["url"],
                    "previous_recommendation": previous.get("recommendation"),
                    "current_recommendation": current.get("recommendation"),
                }
            )
        if previous.get("total_score") != current.get("total_score"):
            score_changes.append(
                {
                    "repo": current["repo"],
                    "number": current["number"],
                    "title": current["title"],
                    "url": current["url"],
                    "previous_score": previous.get("total_score"),
                    "current_score": current.get("total_score"),
                }
            )

    return {
        "new_items": sorted(new_items, key=lambda item: item["total_score"], reverse=True),
        "dropped_items": sorted(dropped_items, key=lambda item: item["total_score"], reverse=True),
        "recommendation_changes": recommendation_changes,
        "score_changes": score_changes,
    }


def compact_item(item: dict) -> dict:
    return {
        "repo": item["repo"],
        "number": item["number"],
        "title": item["title"],
        "url": item["url"],
        "recommendation": item.get("recommendation"),
        "total_score": item.get("total_score"),
    }


def write_changes_markdown(changes: dict, output_path: Path) -> None:
    lines = [
        "# Run Changes",
        "",
        "## New Items",
        "",
    ]

    if changes["new_items"]:
        for item in changes["new_items"]:
            lines.append(
                f"- `{item['recommendation']}` `{item['repo']}#{item['number']}` "
                f"score={item['total_score']} [{item['title']}]({item['url']})"
            )
    else:
        lines.append("_No new items._")

    lines.extend(["", "## Dropped Items", ""])
    if changes["dropped_items"]:
        for item in changes["dropped_items"]:
            lines.append(
                f"- `{item['recommendation']}` `{item['repo']}#{item['number']}` "
                f"score={item['total_score']} [{item['title']}]({item['url']})"
            )
    else:
        lines.append("_No dropped items._")

    lines.extend(["", "## Recommendation Changes", ""])
    if changes["recommendation_changes"]:
        for item in changes["recommendation_changes"]:
            lines.append(
                f"- `{item['repo']}#{item['number']}` "
                f"`{item['previous_recommendation']}` -> `{item['current_recommendation']}` "
                f"[{item['title']}]({item['url']})"
            )
    else:
        lines.append("_No recommendation changes._")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def refresh_repo_seeds(repo_seed_file: Path, snapshot_dir: Path) -> None:
    discovery = SourceDiscovery()
    seeds = discovery.fetch_awesome_bounties()
    discovery.save_repo_list(seeds, repo_seed_file)
    discovery.save_json(seeds, snapshot_dir / "repo_seeds.json")


def collect_recent_triaged_items(
    snapshot_root: Path,
    *,
    max_runs: int,
    exclude_snapshot: Path | None = None,
) -> list[dict]:
    if not snapshot_root.exists():
        return []

    runs = sorted(
        path for path in snapshot_root.iterdir()
        if path.is_dir() and path != exclude_snapshot
    )[-max_runs:]
    items: list[dict] = []
    for run in runs:
        triaged_path = run / "triaged.json"
        if not triaged_path.exists():
            continue
        try:
            payload = load_json(triaged_path)
        except Exception:
            continue
        if isinstance(payload, list):
            items.extend(item for item in payload if isinstance(item, dict))
    return items


def build_learned_reputation_payload(
    triaged_items: list[dict],
    *,
    max_runs: int,
    min_skip_runs: int,
    min_unique_issues: int,
) -> dict:
    repo_stats: dict[str, dict] = {}
    grouped: dict[str, list[dict]] = defaultdict(list)
    for item in triaged_items:
        repo = item.get("repo")
        if not repo:
            continue
        grouped[str(repo)].append(item)

    blocked_repos: list[str] = []
    for repo, items in grouped.items():
        recommendations = Counter(str(item.get("recommendation", "skip")) for item in items)
        unique_issues = sorted({int(item["number"]) for item in items if "number" in item})
        repo_stats[repo] = {
            "total_seen": len(items),
            "skip_count": recommendations.get("skip", 0),
            "review_count": recommendations.get("review", 0),
            "pursue_count": recommendations.get("pursue", 0),
            "unique_issues": unique_issues,
        }
        if (
            recommendations.get("skip", 0) >= min_skip_runs
            and recommendations.get("review", 0) == 0
            and recommendations.get("pursue", 0) == 0
            and len(unique_issues) >= min_unique_issues
        ):
            blocked_repos.append(repo.lower())

    blocked_repos.sort()
    top_repeat_skips = sorted(
        (
            {
                "repo": repo,
                **stats,
            }
            for repo, stats in repo_stats.items()
        ),
        key=lambda item: (item["skip_count"], item["total_seen"], item["repo"]),
        reverse=True,
    )[:20]

    return {
        "generated_at_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "history_window_runs": max_runs,
        "history_min_skip_runs": min_skip_runs,
        "history_min_unique_issues": min_unique_issues,
        "blocked_repos": blocked_repos,
        "repo_stats": repo_stats,
        "top_repeat_skips": top_repeat_skips,
    }


def main() -> int:
    args = parse_args()
    result = run_pipeline(args)
    return result["exit_code"]


def run_pipeline(args: argparse.Namespace) -> dict:
    token, token_source = load_github_token(args.auth_file)
    if token_source:
        console.print(f"[green]auth[/green] using GitHub token from {token_source}")

    run_stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    snapshot_dir = args.snapshot_root / run_stamp
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    if args.refresh_repo_seeds:
        repo_seed_file = args.repo_seed_file or (args.output_dir / "repo_seeds.txt")
        refresh_repo_seeds(repo_seed_file, snapshot_dir)
        console.print(f"[green]seeds[/green] refreshed {repo_seed_file}")
        args.repo_seed_file = repo_seed_file

    learned_history_items = collect_recent_triaged_items(
        args.snapshot_root,
        max_runs=max(1, int(getattr(args, "history_window_runs", 12))),
        exclude_snapshot=snapshot_dir,
    )
    learned_reputation_payload = build_learned_reputation_payload(
        learned_history_items,
        max_runs=max(1, int(getattr(args, "history_window_runs", 12))),
        min_skip_runs=max(1, int(getattr(args, "history_min_skip_runs", 4))),
        min_unique_issues=max(1, int(getattr(args, "history_min_unique_issues", 2))),
    )
    if getattr(args, "learned_reputation_path", None):
        write_json(learned_reputation_payload, args.learned_reputation_path)
    write_json(learned_reputation_payload, args.output_dir / "learned_reputation.json")
    write_json(learned_reputation_payload, snapshot_dir / "learned_reputation.json")

    repo_filters = load_repo_filters(args.repo_seed_file, [])
    targets = load_targets(args.config, repo_filters)
    reputation_policy = merge_reputation_policies(
        load_reputation_policy(getattr(args, "reputation_config", None)),
        load_reputation_policy(getattr(args, "learned_reputation_path", None)),
    )
    scanner = GitHubBountyScanner(
        GitHubClient(token=token),
        reputation_policy=reputation_policy,
    )
    result = scanner.discover(
        targets=targets,
        days=args.days,
        limit=args.limit,
        min_score=args.min_score,
    )
    scanner.render(result.opportunities)

    if result.searches_succeeded == 0:
        console.print("[bold red]No successful GitHub searches completed.[/bold red]")
        github_items: list[dict] = []
    else:
        github_items = [asdict(item) for item in result.opportunities]

    opportunities_json = args.output_dir / "opportunities.json"
    opportunities_md = args.output_dir / "opportunities.md"
    scanner.save_json(result.opportunities, opportunities_json)
    scanner.save_markdown(result.opportunities, opportunities_md)
    scanner.save_json(result.opportunities, snapshot_dir / "github_opportunities.json")
    scanner.save_markdown(result.opportunities, snapshot_dir / "github_opportunities.md")

    algora_items: list[dict] = []
    algora_orgs = load_algora_orgs(args.algora_org_file)
    if algora_orgs:
        algora = AlgoraSource()
        fetched = []
        for org in algora_orgs:
            console.print(f"[bold blue]fetch[/bold blue] algora:{org}")
            try:
                fetched.extend(algora.fetch_org(org))
            except Exception as exc:
                console.print(f"[bold red]Algora fetch failed[/bold red] {org}: {exc}")
        algora_items = [asdict(item) for item in fetched]
        algora_json = args.output_dir / "algora_opportunities.json"
        algora_md = args.output_dir / "algora_opportunities.md"
        algora.save_json(fetched, algora_json)
        algora.save_markdown(fetched, algora_md)
        algora.save_json(fetched, snapshot_dir / "algora_opportunities.json")
        algora.save_markdown(fetched, snapshot_dir / "algora_opportunities.md")

    merged_items = merge_opportunities(github_items, algora_items)
    merged_items = filter_opportunities_by_reputation(merged_items, reputation_policy)
    if not merged_items:
        console.print("[bold red]No opportunities available after source merge.[/bold red]")
        return {
            "exit_code": 2,
            "snapshot_dir": str(snapshot_dir),
            "output_dir": str(args.output_dir),
            "merged_count": 0,
        }
    write_json(merged_items, args.output_dir / "merged_opportunities.json")
    write_json(merged_items, snapshot_dir / "merged_opportunities.json")

    triage_policy = build_triage_policy(
        profile=getattr(args, "triage_profile", "strict"),
        overrides={
            "skip_competition_risk": getattr(args, "skip_competition_risk", None),
            "pursue_min_bounty_confidence": getattr(args, "pursue_min_bounty_confidence", None),
            "pursue_min_actionability": getattr(args, "pursue_min_actionability", None),
            "pursue_max_competition_risk": getattr(args, "pursue_max_competition_risk", None),
            "review_min_bounty_confidence": getattr(args, "review_min_bounty_confidence", None),
            "review_min_actionability": getattr(args, "review_min_actionability", None),
            "weak_bounty_penalty_threshold": getattr(args, "weak_bounty_penalty_threshold", None),
        },
    )
    triager = GitHubIssueTriager(token=token, policy=triage_policy)
    triaged = triager.triage(merged_items, args.triage_limit)
    triager.render(triaged)

    triaged_json = args.output_dir / "triaged.json"
    triaged_md = args.output_dir / "triaged.md"
    queue_md = args.output_dir / "mission_queue.md"
    queue_json = args.output_dir / "current_queue.json"

    triager.save_json(triaged, triaged_json)
    triager.save_markdown(triaged, triaged_md)
    save_queue_markdown(triaged, queue_md)
    save_current_queue_json(triaged, queue_json, triage_policy.profile)
    triager.save_json(triaged, snapshot_dir / "triaged.json")
    triager.save_markdown(triaged, snapshot_dir / "triaged.md")
    save_queue_markdown(triaged, snapshot_dir / "mission_queue.md")
    save_current_queue_json(triaged, snapshot_dir / "current_queue.json", triage_policy.profile)

    summary = summarize_run(github_items, algora_items, merged_items, triaged)
    summary["triage_policy"] = asdict(triage_policy)
    write_json(summary, args.output_dir / "run_summary.json")
    write_json(summary, snapshot_dir / "run_summary.json")

    previous_snapshot = get_previous_snapshot(args.snapshot_root, snapshot_dir)
    previous_triaged = []
    if previous_snapshot is not None:
        previous_triaged_path = previous_snapshot / "triaged.json"
        if previous_triaged_path.exists():
            previous_triaged = load_json(previous_triaged_path)

    current_triaged = [asdict(item) for item in triaged]
    changes = diff_triaged_runs(previous_triaged, current_triaged)
    write_json(changes, args.output_dir / "changes_summary.json")
    write_json(changes, snapshot_dir / "changes_summary.json")
    write_changes_markdown(changes, args.output_dir / "changes_summary.md")
    write_changes_markdown(changes, snapshot_dir / "changes_summary.md")

    learned_with_current = build_learned_reputation_payload(
        learned_history_items + current_triaged,
        max_runs=max(1, int(getattr(args, "history_window_runs", 12))),
        min_skip_runs=max(1, int(getattr(args, "history_min_skip_runs", 4))),
        min_unique_issues=max(1, int(getattr(args, "history_min_unique_issues", 2))),
    )
    if getattr(args, "learned_reputation_path", None):
        write_json(learned_with_current, args.learned_reputation_path)
    write_json(learned_with_current, args.output_dir / "learned_reputation.json")
    write_json(learned_with_current, snapshot_dir / "learned_reputation.json")

    pursue_count = sum(item.recommendation == "pursue" for item in triaged)
    review_count = sum(item.recommendation == "review" for item in triaged)
    console.print(
        f"[green]saved[/green] {opportunities_json}, {triaged_json}, {queue_md}, "
        f"{args.output_dir / 'run_summary.json'}, {args.output_dir / 'changes_summary.json'} "
        f"and snapshot {snapshot_dir} "
        f"(pursue={pursue_count}, review={review_count})"
    )
    return {
        "exit_code": 0,
        "snapshot_dir": str(snapshot_dir),
        "output_dir": str(args.output_dir),
        "merged_count": len(merged_items),
        "pursue_count": pursue_count,
        "review_count": review_count,
        "queue_count": pursue_count + review_count,
        "triage_profile": triage_policy.profile,
        "learned_blocked_repo_count": len(learned_with_current.get("blocked_repos", [])),
    }


if __name__ == "__main__":
    raise SystemExit(main())
