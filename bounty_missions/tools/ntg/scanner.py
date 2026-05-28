from __future__ import annotations

import argparse
import json
import math
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import requests
import yaml
from github_auth import load_github_token
from rich.console import Console
from rich.table import Table

console = Console()
UTC = timezone.utc

DEFAULT_KEYWORDS = ["bounty", "reward", "algora", "polar", "good first issue"]

META_ISSUE_PATTERNS = [
    r"\bbounty alert\b",
    r"\bactive bounty scan results\b",
    r"\bnew opportunit(?:y|ies) found\b",
    r"\brequest indexing\b",
    r"\bsubmission process\b",
    r"\bbug bounty program\b",
    r"\breporting page\b",
]

META_ISSUE_LABELS = {
    "bounty-alert",
}

DEFAULT_BLOCKED_REPO_PATTERNS = [
    r"(^|/)bountyscout$",
    r"(^|/)pd-hunter$",
    r"(^|/)bounty-autopilot$",
    r"(^|/)issuehunt-stats$",
    r"(^|/)anthropicnotification$",
    r"(^|/)claude-builders-bounty$",
]

DEFAULT_BLOCKED_DESCRIPTION_PATTERNS = [
    r"\bbounty alert\b",
    r"\bissue alerts?\b",
    r"\bnotification\b",
    r"\bautopilot\b",
    r"\bbounty board\b",
    r"\bscout(?:ing)? github for active bounties\b",
    r"\bopportunit(?:y|ies)\b",
]


@dataclass
class RepoReputationPolicy:
    blocked_repos: set[str] = field(default_factory=set)
    blocked_owners: set[str] = field(default_factory=set)
    blocked_repo_patterns: list[str] = field(
        default_factory=lambda: list(DEFAULT_BLOCKED_REPO_PATTERNS)
    )
    blocked_description_patterns: list[str] = field(
        default_factory=lambda: list(DEFAULT_BLOCKED_DESCRIPTION_PATTERNS)
    )
    min_stars_unscoped: int = 15

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None = None) -> "RepoReputationPolicy":
        data = payload or {}
        return cls(
            blocked_repos={item.lower() for item in data.get("blocked_repos", [])},
            blocked_owners={item.lower() for item in data.get("blocked_owners", [])},
            blocked_repo_patterns=data.get("blocked_repo_patterns", list(DEFAULT_BLOCKED_REPO_PATTERNS)),
            blocked_description_patterns=data.get(
                "blocked_description_patterns",
                list(DEFAULT_BLOCKED_DESCRIPTION_PATTERNS),
            ),
            min_stars_unscoped=int(data.get("min_stars_unscoped", 15)),
        )


def merge_reputation_policies(*policies: RepoReputationPolicy) -> RepoReputationPolicy:
    blocked_repos: set[str] = set()
    blocked_owners: set[str] = set()
    blocked_repo_patterns: list[str] = []
    blocked_description_patterns: list[str] = []
    min_stars_unscoped = 0

    for policy in policies:
        blocked_repos.update(policy.blocked_repos)
        blocked_owners.update(policy.blocked_owners)
        min_stars_unscoped = max(min_stars_unscoped, policy.min_stars_unscoped)
        for pattern in policy.blocked_repo_patterns:
            if pattern not in blocked_repo_patterns:
                blocked_repo_patterns.append(pattern)
        for pattern in policy.blocked_description_patterns:
            if pattern not in blocked_description_patterns:
                blocked_description_patterns.append(pattern)

    return RepoReputationPolicy(
        blocked_repos=blocked_repos,
        blocked_owners=blocked_owners,
        blocked_repo_patterns=blocked_repo_patterns,
        blocked_description_patterns=blocked_description_patterns,
        min_stars_unscoped=min_stars_unscoped,
    )


def repo_matches_reputation_policy(
    repo: str,
    policy: RepoReputationPolicy,
    *,
    repo_description: str = "",
    stars: int = 0,
    is_scoped_target: bool = False,
) -> bool:
    normalized_repo = repo.lower()
    owner = normalized_repo.split("/", 1)[0]

    if normalized_repo in policy.blocked_repos:
        return True
    if owner in policy.blocked_owners:
        return True
    if any(re.search(pattern, normalized_repo) for pattern in policy.blocked_repo_patterns):
        return True

    description = repo_description.lower()
    if description and any(
        re.search(pattern, description) for pattern in policy.blocked_description_patterns
    ):
        return True

    if not is_scoped_target and stars < policy.min_stars_unscoped:
        return True
    return False


@dataclass
class SearchTarget:
    name: str
    repo: str | None = None
    labels: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    exclude_keywords: list[str] = field(default_factory=list)
    state: str = "open"
    kind: str = "issue"


@dataclass
class BountyOpportunity:
    search_name: str
    repo: str
    number: int
    title: str
    url: str
    created_at: str
    updated_at: str
    age_days: int
    comments: int
    labels: list[str]
    assignees: list[str]
    score: int
    keyword: str
    matched_labels: list[str]


@dataclass
class DiscoveryResult:
    opportunities: list[BountyOpportunity]
    searches_attempted: int
    searches_succeeded: int
    searches_failed: int


class GitHubClient:
    def __init__(self, token: str | None = None, timeout: int = 20):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/vnd.github+json",
                "User-Agent": "ntg-bounty-scanner",
            }
        )
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"
        self._repo_cache: dict[str, dict[str, Any]] = {}
        self._server_now: datetime | None = None

    def search_issues(self, query: str, limit: int) -> list[dict[str, Any]]:
        response = self.session.get(
            "https://api.github.com/search/issues",
            params={"q": query, "per_page": min(limit, 100), "page": 1},
            timeout=self.timeout,
        )
        self._capture_server_time(response.headers.get("Date"))
        response.raise_for_status()
        payload = response.json()
        return payload.get("items", [])

    def get_repo(self, repo_full_name: str) -> dict[str, Any]:
        if repo_full_name not in self._repo_cache:
            response = self.session.get(
                f"https://api.github.com/repos/{repo_full_name}",
                timeout=self.timeout,
            )
            self._capture_server_time(response.headers.get("Date"))
            response.raise_for_status()
            self._repo_cache[repo_full_name] = response.json()
        return self._repo_cache[repo_full_name]

    def get_server_now(self) -> datetime:
        if self._server_now is not None:
            return self._server_now

        try:
            response = self.session.get("https://api.github.com/rate_limit", timeout=self.timeout)
            self._capture_server_time(response.headers.get("Date"))
        except requests.RequestException:
            return datetime.now(UTC)

        return self._server_now or datetime.now(UTC)

    def _capture_server_time(self, value: str | None) -> None:
        if not value:
            return
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        self._server_now = parsed.astimezone(UTC)


class GitHubBountyScanner:
    def __init__(
        self,
        client: GitHubClient,
        reputation_policy: RepoReputationPolicy | None = None,
    ):
        self.client = client
        self.reputation_policy = reputation_policy or RepoReputationPolicy()

    def discover(
        self,
        targets: list[SearchTarget],
        days: int,
        limit: int,
        min_score: int,
    ) -> DiscoveryResult:
        seen: dict[str, BountyOpportunity] = {}
        now = self.client.get_server_now()
        searches_attempted = 0
        searches_succeeded = 0
        searches_failed = 0

        for target in targets:
            keywords = target.keywords or DEFAULT_KEYWORDS
            for keyword in keywords:
                searches_attempted += 1
                query = self._build_query(target, keyword, days, now)
                console.print(f"[bold blue]search[/bold blue] {target.name}: {keyword}")

                try:
                    issues = self.client.search_issues(query=query, limit=limit)
                except requests.HTTPError as exc:
                    searches_failed += 1
                    status_code = exc.response.status_code if exc.response is not None else None
                    hint = " Set GITHUB_TOKEN or GH_TOKEN to raise the rate limit." if status_code == 403 else ""
                    console.print(
                        f"[bold red]GitHub search failed[/bold red] {target.name}: {exc}.{hint}"
                    )
                    if status_code in {403, 422}:
                        break
                    continue
                except requests.RequestException as exc:
                    searches_failed += 1
                    console.print(
                        f"[bold red]Network error[/bold red] while scanning {target.name}: {exc}"
                    )
                    continue
                searches_succeeded += 1

                for issue in issues:
                    if self._is_excluded(target, issue):
                        continue
                    normalized = self._normalize_issue(target, keyword, issue, now)
                    if normalized is None:
                        continue
                    if normalized.score < min_score:
                        continue
                    current = seen.get(normalized.url)
                    if current is None or normalized.score > current.score:
                        seen[normalized.url] = normalized

        return DiscoveryResult(
            opportunities=sorted(seen.values(), key=lambda item: item.score, reverse=True),
            searches_attempted=searches_attempted,
            searches_succeeded=searches_succeeded,
            searches_failed=searches_failed,
        )

    def render(self, opportunities: list[BountyOpportunity]) -> None:
        if not opportunities:
            console.print("[yellow]No opportunities matched the current search.[/yellow]")
            return

        table = Table(title="Community Bounty Opportunities")
        table.add_column("Score", style="green", justify="right")
        table.add_column("Repo", style="cyan")
        table.add_column("Issue", style="magenta", justify="right")
        table.add_column("Age(d)", style="white", justify="right")
        table.add_column("Comments", style="white", justify="right")
        table.add_column("Labels", style="yellow")
        table.add_column("Title", style="white")

        for item in opportunities:
            table.add_row(
                str(item.score),
                item.repo,
                str(item.number),
                str(item.age_days),
                str(item.comments),
                ", ".join(item.labels[:3]),
                item.title,
            )

        console.print(table)

    def save_json(self, opportunities: list[BountyOpportunity], output_path: Path) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        payload = [asdict(item) for item in opportunities]
        output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def save_markdown(
        self, opportunities: list[BountyOpportunity], output_path: Path
    ) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            "# Community Bounty Opportunities",
            "",
            f"_Generated: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}_",
            "",
            "| Score | Repo | Issue | Age (days) | Comments | Labels | Title | Link |",
            "| ---: | --- | ---: | ---: | ---: | --- | --- | --- |",
        ]

        for item in opportunities:
            title = item.title.replace("|", "/")
            labels = ", ".join(item.labels[:3]).replace("|", "/")
            lines.append(
                f"| {item.score} | {item.repo} | {item.number} | {item.age_days} | "
                f"{item.comments} | {labels} | {title} | [open]({item.url}) |"
            )

        output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _build_query(
        self, target: SearchTarget, keyword: str, days: int, now: datetime
    ) -> str:
        created_after = (now - timedelta(days=days)).strftime("%Y-%m-%d")
        parts = [
            f"is:{target.state}",
            f"is:{target.kind}",
            f'created:>={created_after}',
            f'"{keyword}" in:title,body',
        ]
        if target.repo:
            parts.append(f"repo:{target.repo}")
        for label in target.labels:
            parts.append(f'label:"{label}"')
        return " ".join(parts)

    def _normalize_issue(
        self, target: SearchTarget, keyword: str, issue: dict[str, Any], now: datetime
    ) -> BountyOpportunity | None:
        created_at = self._parse_datetime(issue["created_at"])
        updated_at = self._parse_datetime(issue["updated_at"])
        repo = issue["repository_url"].removeprefix("https://api.github.com/repos/")
        repo_data = self.client.get_repo(repo)
        if self._is_repo_blocked(target, repo, repo_data):
            return None
        labels = [label["name"] for label in issue.get("labels", [])]
        matched_labels = [label for label in labels if label in target.labels]
        assignees = [item["login"] for item in issue.get("assignees", [])]

        score = self._score_issue(
            repo_stars=repo_data.get("stargazers_count", 0),
            age_days=max(0, (now - created_at).days),
            comments=issue.get("comments", 0),
            label_matches=len(matched_labels),
            has_assignee=bool(assignees),
            updated_recently=(now - updated_at).days <= 3,
        )

        return BountyOpportunity(
            search_name=target.name,
            repo=repo,
            number=issue["number"],
            title=issue["title"],
            url=issue["html_url"],
            created_at=issue["created_at"],
            updated_at=issue["updated_at"],
            age_days=max(0, (now - created_at).days),
            comments=issue.get("comments", 0),
            labels=labels,
            assignees=assignees,
            score=score,
            keyword=keyword,
            matched_labels=matched_labels,
        )

    @staticmethod
    def _parse_datetime(value: str) -> datetime:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)

    @staticmethod
    def _is_excluded(target: SearchTarget, issue: dict[str, Any]) -> bool:
        haystack = " ".join(
            [
                issue.get("title", ""),
                issue.get("body") or "",
            ]
        ).lower()
        labels = {label["name"].lower() for label in issue.get("labels", [])}

        if any(keyword.lower() in haystack for keyword in target.exclude_keywords):
            return True
        if labels & META_ISSUE_LABELS:
            return True
        return any(re.search(pattern, haystack) for pattern in META_ISSUE_PATTERNS)

    def _is_repo_blocked(
        self,
        target: SearchTarget,
        repo: str,
        repo_data: dict[str, Any],
    ) -> bool:
        return repo_matches_reputation_policy(
            repo,
            self.reputation_policy,
            repo_description=repo_data.get("description") or "",
            stars=int(repo_data.get("stargazers_count", 0) or 0),
            is_scoped_target=target.repo is not None,
        )

    @staticmethod
    def _score_issue(
        repo_stars: int,
        age_days: int,
        comments: int,
        label_matches: int,
        has_assignee: bool,
        updated_recently: bool,
    ) -> int:
        freshness = max(0, 35 - min(age_days, 35))
        repo_signal = min(20, int(math.log10(repo_stars + 10) * 7))
        engagement = min(15, comments * 2)
        label_signal = min(20, label_matches * 10)
        assignment_signal = 0 if has_assignee else 10
        recency_signal = 10 if updated_recently else 0
        return freshness + repo_signal + engagement + label_signal + assignment_signal + recency_signal


def load_targets(config_path: Path | None, repo_filters: list[str]) -> list[SearchTarget]:
    targets: list[SearchTarget] = []

    if config_path and config_path.exists():
        payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        for raw_target in payload.get("targets", []):
            targets.append(
                SearchTarget(
                    name=raw_target["name"],
                    repo=raw_target.get("repo"),
                    labels=raw_target.get("labels", []),
                    keywords=raw_target.get("keywords", []),
                    exclude_keywords=raw_target.get("exclude_keywords", []),
                    state=raw_target.get("state", "open"),
                    kind=raw_target.get("kind", "issue"),
                )
            )

    if repo_filters:
        for repo in repo_filters:
            targets.append(
                SearchTarget(
                    name=repo,
                    repo=repo,
                    # Repo seeds are already bounty-biased; keep search cost low.
                    keywords=["bounty", "reward"],
                )
            )

    if not targets:
        targets.append(
            SearchTarget(
                name="projectdiscovery/nuclei-templates",
                repo="projectdiscovery/nuclei-templates",
                labels=["💎 Bounty"],
                keywords=["bounty", "kev", "cve"],
            )
        )

    return targets


def load_repo_filters(repo_file: Path | None, inline_repos: list[str]) -> list[str]:
    repos = list(inline_repos)
    if repo_file and repo_file.exists():
        for line in repo_file.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                repos.append(stripped)
    deduped: list[str] = []
    for repo in repos:
        if repo not in deduped:
            deduped.append(repo)
    return deduped


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Discover community bounty issues on GitHub and rank them."
    )
    parser.add_argument(
        "--config",
        type=Path,
        help="YAML config that defines search targets.",
    )
    parser.add_argument(
        "--repo",
        action="append",
        default=[],
        help="Additional GitHub repo in owner/name format. May be supplied multiple times.",
    )
    parser.add_argument(
        "--repo-file",
        type=Path,
        help="Optional file containing one owner/name repo per line.",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="Only scan issues created within the last N days.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum number of items fetched per keyword search.",
    )
    parser.add_argument(
        "--min-score",
        type=int,
        default=30,
        help="Drop low-signal issues under this score.",
    )
    parser.add_argument(
        "--json-output",
        type=Path,
        help="Write normalized results to JSON.",
    )
    parser.add_argument(
        "--auth-file",
        type=Path,
        help="Optional auth file containing a GitHub token.",
    )
    parser.add_argument(
        "--markdown-output",
        type=Path,
        help="Write a Markdown opportunity board.",
    )
    parser.add_argument(
        "--reputation-config",
        type=Path,
        help="Optional YAML file with repo reputation filters.",
    )
    return parser.parse_args()


def load_reputation_policy(config_path: Path | None) -> RepoReputationPolicy:
    if config_path is None or not config_path.exists():
        return RepoReputationPolicy()
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    return RepoReputationPolicy.from_dict(payload)


def main() -> int:
    args = parse_args()
    token, token_source = load_github_token(args.auth_file)
    if token_source:
        console.print(f"[green]auth[/green] using GitHub token from {token_source}")
    repo_filters = load_repo_filters(args.repo_file, args.repo)
    targets = load_targets(args.config, repo_filters)
    reputation_policy = load_reputation_policy(args.reputation_config)
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
        console.print("[bold red]No successful GitHub searches completed. Existing output files were not updated.[/bold red]")
        return 2

    if args.json_output:
        scanner.save_json(result.opportunities, args.json_output)
        console.print(f"[green]saved json[/green] {args.json_output}")

    if args.markdown_output:
        scanner.save_markdown(result.opportunities, args.markdown_output)
        console.print(f"[green]saved markdown[/green] {args.markdown_output}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
