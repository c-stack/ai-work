from __future__ import annotations

import argparse
import json
import math
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from github_auth import load_github_token
from rich.console import Console
from rich.table import Table

console = Console()
UTC = timezone.utc

DIRECT_BOUNTY_TERMS = [
    "bug bounty",
    "bounty",
    "/claim",
    "claim this issue",
    "cash reward",
    "sponsored issue",
    "奖金",
    "悬赏",
]

PLATFORM_BOUNTY_TERMS = [
    "algora",
    "polar.sh",
]

NEGATIVE_BOUNTY_TERMS = [
    "not bounty",
    "support request",
    "funding",
    "blog",
    "article",
    "newsletter",
    "spam",
    "benchmark",
    "benchmarks",
    "migration",
    "polars",
    "settlement",
    "marketing",
    "request indexing",
    "alternatives",
    "organization list",
    "mapping.json",
    "active bounties",
    "algora api",
    "活跃赏金",
    "赏金组织列表",
]

SUPPORT_REQUEST_PATTERNS = [
    ("personal-account-device", r"\bmy (account|device|wallet|phone|browser)\b"),
    ("asks-for-solution", r"please provide (a )?solution"),
    ("asks-for-help", r"\b(can you help|help me|how do i)\b"),
    ("password-login-help", r"\b(password again|login method)\b"),
    ("support-politeness", r"\bthank you\b"),
]

BOUNTY_UNCERTAINTY_PATTERNS = [
    ("bounty-unclear", r"\b(is this issue part of|whether this issue is part of).*\bbounty"),
    ("regular-contribution", r"\bregular community contribution\b"),
    ("reward-unclear", r"\bcontributor reward\b"),
]

META_BOUNTY_PATTERNS = [
    ("bounty-alert", r"\bbounty alert\b"),
    ("scan-results", r"\bactive bounty scan results\b"),
    ("opportunities-found", r"\bnew opportunit(?:y|ies) found\b"),
    ("bounty-program-meta", r"\bbug bounty program\b"),
    ("submission-process", r"\bsubmission process\b"),
    ("reporting-page", r"\b(reporting page|submission page)\b"),
]

ACTIONABLE_TERMS = [
    "steps to reproduce",
    "expected behavior",
    "actual behavior",
    "acceptance criteria",
    "root cause",
    "stack trace",
    "traceback",
    "error",
    "failing",
    "regression",
    "fix",
]

TASK_HINT_TERMS = [
    "fix",
    "support",
    "replace",
    "implement",
    "add ",
    "remove",
    "broken",
    "failing",
    "error",
    "bug",
]

EXISTING_WORK_PATTERNS = [
    r"\bi opened #\d+\b",
    r"\bopened #\d+\b",
    r"\bsubmitted #\d+\b",
    r"\bpull request #\d+\b",
    r"\bpr #\d+\b",
]

CLAIM_TERMS = [
    "working on this",
    "i can take this",
    "assigned",
    "/claim",
    "/attempt",
    "claimed",
    "i'll take this",
    "taking this",
    "still available to take this",
    "i'm investigating",
    "reproducing it locally",
]

LANGUAGE_FIT = {
    "Python": 10,
    "TypeScript": 10,
    "JavaScript": 9,
    "Go": 9,
    "Rust": 8,
    "Java": 7,
    "C#": 7,
    "Ruby": 6,
    "PHP": 6,
}


@dataclass
class TriagedOpportunity:
    repo: str
    number: int
    title: str
    url: str
    scanner_score: int
    bounty_confidence: int
    actionability: int
    language_fit: int
    competition_risk: int
    total_score: int
    recommendation: str
    repo_primary_language: str
    top_languages: list[str]
    matched_positive_terms: list[str]
    matched_platform_terms: list[str]
    matched_negative_terms: list[str]
    matched_actionable_terms: list[str]
    matched_task_terms: list[str]
    claim_signals: list[str]
    existing_work_signals: list[str]
    support_request_signals: list[str]
    bounty_uncertainty_signals: list[str]
    meta_bounty_signals: list[str]
    empty_response_count: int
    decision_reason: str
    decision_signals: list[str]


@dataclass
class TriagePolicy:
    profile: str = "strict"
    skip_competition_risk: int = 14
    pursue_min_bounty_confidence: int = 18
    pursue_min_actionability: int = 10
    pursue_max_competition_risk: int = 12
    review_min_bounty_confidence: int = 8
    review_min_actionability: int = 6
    weak_bounty_penalty_threshold: int = 12


TRIAGE_PROFILES: dict[str, dict[str, int | str]] = {
    "strict": {
        "profile": "strict",
        "skip_competition_risk": 14,
        "pursue_min_bounty_confidence": 18,
        "pursue_min_actionability": 10,
        "pursue_max_competition_risk": 12,
        "review_min_bounty_confidence": 8,
        "review_min_actionability": 6,
        "weak_bounty_penalty_threshold": 12,
    },
    "balanced": {
        "profile": "balanced",
        "skip_competition_risk": 18,
        "pursue_min_bounty_confidence": 18,
        "pursue_min_actionability": 10,
        "pursue_max_competition_risk": 14,
        "review_min_bounty_confidence": 8,
        "review_min_actionability": 6,
        "weak_bounty_penalty_threshold": 12,
    },
    "aggressive": {
        "profile": "aggressive",
        "skip_competition_risk": 24,
        "pursue_min_bounty_confidence": 16,
        "pursue_min_actionability": 10,
        "pursue_max_competition_risk": 16,
        "review_min_bounty_confidence": 8,
        "review_min_actionability": 4,
        "weak_bounty_penalty_threshold": 10,
    },
}


def build_triage_policy(
    profile: str = "strict",
    overrides: dict[str, int | str | None] | None = None,
) -> TriagePolicy:
    base = dict(TRIAGE_PROFILES.get(profile, TRIAGE_PROFILES["strict"]))
    override_values = overrides or {}
    for key, value in override_values.items():
        if value is None:
            continue
        base[key] = value
    return TriagePolicy(**base)


class GitHubIssueTriager:
    def __init__(
        self,
        token: str | None = None,
        timeout: int = 20,
        policy: TriagePolicy | None = None,
    ):
        self.timeout = timeout
        self.policy = policy or build_triage_policy()
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/vnd.github+json",
                "User-Agent": "ntg-bounty-triager",
            }
        )
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"
        self._repo_cache: dict[str, dict[str, Any]] = {}
        self._language_cache: dict[str, list[str]] = {}

    def triage(self, items: list[dict[str, Any]], limit: int) -> list[TriagedOpportunity]:
        results: list[TriagedOpportunity] = []

        for item in items[:limit]:
            try:
                issue = self.get_issue(item["repo"], item["number"])
                comments = self.get_issue_comments(item["repo"], item["number"], limit=10)
                repo = self.get_repo(item["repo"])
                languages = self.get_repo_languages(item["repo"])
            except requests.RequestException as exc:
                console.print(
                    f"[yellow]skip[/yellow] unable to fetch {item['repo']}#{item['number']}: {exc}"
                )
                continue

            results.append(self.score_issue(item, issue, comments, repo, languages))

        return sorted(results, key=lambda value: value.total_score, reverse=True)

    def render(self, items: list[TriagedOpportunity]) -> None:
        if not items:
            console.print("[yellow]No triaged opportunities available.[/yellow]")
            return

        table = Table(title="Triaged Bounty Queue")
        table.add_column("Total", style="green", justify="right")
        table.add_column("Reco", style="cyan")
        table.add_column("Repo", style="white")
        table.add_column("Issue", style="magenta", justify="right")
        table.add_column("Lang", style="yellow")
        table.add_column("Bty", style="white", justify="right")
        table.add_column("Act", style="white", justify="right")
        table.add_column("Risk", style="red", justify="right")
        table.add_column("Title", style="white")

        for item in items:
            table.add_row(
                str(item.total_score),
                item.recommendation,
                item.repo,
                str(item.number),
                item.repo_primary_language or "-",
                str(item.bounty_confidence),
                str(item.actionability),
                str(item.competition_risk),
                item.title,
            )

        console.print(table)

    def save_json(self, items: list[TriagedOpportunity], output_path: Path) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps([asdict(item) for item in items], indent=2),
            encoding="utf-8",
        )

    def save_markdown(self, items: list[TriagedOpportunity], output_path: Path) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            "# Triaged Bounty Queue",
            "",
            "| Total | Recommendation | Repo | Issue | Language | Bounty | Actionability | Risk | Reason | Title | Link |",
            "| ---: | --- | --- | ---: | --- | ---: | ---: | ---: | --- | --- | --- |",
        ]
        for item in items:
            title = item.title.replace("|", "/")
            reason = item.decision_reason.replace("|", "/")
            lines.append(
                f"| {item.total_score} | {item.recommendation} | {item.repo} | {item.number} | "
                f"{item.repo_primary_language or '-'} | {item.bounty_confidence} | {item.actionability} | "
                f"{item.competition_risk} | {reason} | {title} | [open]({item.url}) |"
            )
        output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def get_issue(self, repo: str, number: int) -> dict[str, Any]:
        response = self.session.get(
            f"https://api.github.com/repos/{repo}/issues/{number}",
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()

    def get_issue_comments(self, repo: str, number: int, limit: int) -> list[dict[str, Any]]:
        response = self.session.get(
            f"https://api.github.com/repos/{repo}/issues/{number}/comments",
            params={"per_page": min(limit, 100), "page": 1},
            timeout=self.timeout,
        )
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            status_code = exc.response.status_code if exc.response is not None else None
            if status_code == 404:
                return []
            raise
        return response.json()

    def get_repo(self, repo: str) -> dict[str, Any]:
        if repo not in self._repo_cache:
            response = self.session.get(
                f"https://api.github.com/repos/{repo}",
                timeout=self.timeout,
            )
            response.raise_for_status()
            self._repo_cache[repo] = response.json()
        return self._repo_cache[repo]

    def get_repo_languages(self, repo: str) -> list[str]:
        if repo not in self._language_cache:
            response = self.session.get(
                f"https://api.github.com/repos/{repo}/languages",
                timeout=self.timeout,
            )
            try:
                response.raise_for_status()
            except requests.HTTPError as exc:
                status_code = exc.response.status_code if exc.response is not None else None
                if status_code == 404:
                    repo_payload = self.get_repo(repo)
                    primary = repo_payload.get("language")
                    self._language_cache[repo] = [primary] if primary else []
                    return self._language_cache[repo]
                raise
            payload = response.json()
            self._language_cache[repo] = list(payload.keys())[:5]
        return self._language_cache[repo]

    def score_issue(
        self,
        base_item: dict[str, Any],
        issue: dict[str, Any],
        comments: list[dict[str, Any]],
        repo: dict[str, Any],
        languages: list[str],
    ) -> TriagedOpportunity:
        title = issue.get("title", "")
        body = issue.get("body") or ""
        comment_text = "\n".join(comment.get("body") or "" for comment in comments)
        combined = f"{title}\n{body}\n{comment_text}".lower()
        label_text = " ".join(label["name"] for label in issue.get("labels", []))

        matched_positive = [term for term in DIRECT_BOUNTY_TERMS if term in combined]
        matched_platform = [term for term in PLATFORM_BOUNTY_TERMS if term in combined]
        matched_negative = [term for term in NEGATIVE_BOUNTY_TERMS if term in combined]
        matched_actionable = [term for term in ACTIONABLE_TERMS if term in combined]
        matched_task_terms = [term for term in TASK_HINT_TERMS if term in combined]
        claim_signals = [term for term in CLAIM_TERMS if term in comment_text.lower()]
        existing_work_signals = [
            pattern for pattern in EXISTING_WORK_PATTERNS if re.search(pattern, combined)
        ]
        support_request_signals = [
            name for name, pattern in SUPPORT_REQUEST_PATTERNS if re.search(pattern, combined)
        ]
        bounty_uncertainty_signals = [
            name
            for name, pattern in BOUNTY_UNCERTAINTY_PATTERNS
            if re.search(pattern, comment_text.lower())
        ]
        meta_bounty_signals = [
            name for name, pattern in META_BOUNTY_PATTERNS if re.search(pattern, combined)
        ]
        empty_response_count = body.lower().count("_no response_")
        empty_log_block = bool(
            re.search(r"relevant log output\s*```(?:shell)?\s*```", body, re.IGNORECASE | re.DOTALL)
        )
        image_count = body.lower().count("<img")

        bounty_confidence = (
            min(36, len(matched_positive) * 12)
            + min(12, len(matched_platform) * 5)
            - min(30, len(matched_negative) * 10)
        )
        if "bounty" in title.lower():
            bounty_confidence += 10
        if matched_platform and not matched_positive and "bounty" not in title.lower():
            bounty_confidence -= 12
        bounty_confidence -= min(18, len(support_request_signals) * 4)
        if bounty_uncertainty_signals and not matched_platform:
            bounty_confidence -= 18
        bounty_confidence -= min(24, len(meta_bounty_signals) * 8)
        bounty_confidence = max(0, bounty_confidence)

        actionability = 0
        if len(body) >= 200:
            actionability += 8
        if len(body) >= 600:
            actionability += 6
        if re.search(r"```.+?```", body, re.DOTALL):
            actionability += 6
        if re.search(r"`[^`/]+/[^`]+`", body):
            actionability += 6
        if re.search(r"\b(v?\d+\.\d+(\.\d+)?)\b", body):
            actionability += 4
        actionability += min(20, len(matched_actionable) * 4)
        actionability += min(10, len(matched_task_terms) * 2)
        if "documentation" in label_text.lower() or "blog" in label_text.lower():
            actionability -= 4
        if empty_response_count >= 2:
            actionability -= min(18, empty_response_count * 3)
        if empty_log_block:
            actionability -= 6
        if image_count >= 2 and empty_response_count >= 2:
            actionability -= 4
        actionability -= min(12, len(support_request_signals) * 3)
        actionability -= min(12, len(meta_bounty_signals) * 3)
        if not matched_task_terms and not matched_actionable:
            actionability -= 10
        actionability = max(0, actionability)

        repo_primary_language = repo.get("language") or ""
        language_fit = LANGUAGE_FIT.get(repo_primary_language, 5 if repo_primary_language else 0)

        created_at = self.parse_datetime(issue["created_at"])
        age_days = max(0, (datetime.now(UTC) - created_at).days)
        competition_risk = 0
        comment_count = issue.get("comments", 0) or 0
        competition_risk += min(20, int(math.log2(comment_count + 1) * 3))
        if issue.get("assignees"):
            competition_risk += 8
        competition_risk += min(16, len(claim_signals) * 6)
        if issue.get("pull_request"):
            competition_risk += 10
        competition_risk += min(24, len(existing_work_signals) * 12)
        if age_days > 90:
            competition_risk += 8
        if age_days > 365:
            competition_risk += 8

        total_score = (
            int(base_item.get("score", 0) * 0.5)
            + bounty_confidence
            + actionability
            + language_fit
            - competition_risk
        )
        if bounty_confidence < self.policy.weak_bounty_penalty_threshold:
            total_score -= 20
        if "[test]" in title.lower():
            total_score -= 30

        looks_like_support_request = (
            not matched_platform
            and (
                bool(bounty_uncertainty_signals)
                or (len(support_request_signals) >= 2 and empty_response_count >= 2)
            )
        )
        looks_like_meta_bounty = bool(meta_bounty_signals)

        if (
            looks_like_support_request
            or looks_like_meta_bounty
            or existing_work_signals
            or competition_risk >= self.policy.skip_competition_risk
            or "[test]" in title.lower()
        ):
            recommendation = "skip"
        elif (
            bounty_confidence >= self.policy.pursue_min_bounty_confidence
            and actionability >= self.policy.pursue_min_actionability
            and competition_risk <= self.policy.pursue_max_competition_risk
        ):
            recommendation = "pursue"
        elif (
            bounty_confidence >= self.policy.review_min_bounty_confidence
            and actionability >= self.policy.review_min_actionability
        ):
            recommendation = "review"
        else:
            recommendation = "skip"

        decision_reason, decision_signals = self.build_decision_reason(
            recommendation=recommendation,
            title=title,
            bounty_confidence=bounty_confidence,
            actionability=actionability,
            competition_risk=competition_risk,
            matched_positive=matched_positive,
            matched_platform=matched_platform,
            matched_negative=matched_negative,
            matched_actionable=matched_actionable,
            matched_task_terms=matched_task_terms,
            claim_signals=claim_signals,
            existing_work_signals=existing_work_signals,
            support_request_signals=support_request_signals,
            bounty_uncertainty_signals=bounty_uncertainty_signals,
            meta_bounty_signals=meta_bounty_signals,
            empty_response_count=empty_response_count,
        )

        return TriagedOpportunity(
            repo=base_item["repo"],
            number=base_item["number"],
            title=base_item["title"],
            url=base_item["url"],
            scanner_score=base_item["score"],
            bounty_confidence=bounty_confidence,
            actionability=actionability,
            language_fit=language_fit,
            competition_risk=competition_risk,
            total_score=total_score,
            recommendation=recommendation,
            repo_primary_language=repo_primary_language,
            top_languages=languages,
            matched_positive_terms=matched_positive,
            matched_platform_terms=matched_platform,
            matched_negative_terms=matched_negative,
            matched_actionable_terms=matched_actionable,
            matched_task_terms=matched_task_terms,
            claim_signals=claim_signals,
            existing_work_signals=existing_work_signals,
            support_request_signals=support_request_signals,
            bounty_uncertainty_signals=bounty_uncertainty_signals,
            meta_bounty_signals=meta_bounty_signals,
            empty_response_count=empty_response_count,
            decision_reason=decision_reason,
            decision_signals=decision_signals,
        )

    def build_decision_reason(
        self,
        *,
        recommendation: str,
        title: str,
        bounty_confidence: int,
        actionability: int,
        competition_risk: int,
        matched_positive: list[str],
        matched_platform: list[str],
        matched_negative: list[str],
        matched_actionable: list[str],
        matched_task_terms: list[str],
        claim_signals: list[str],
        existing_work_signals: list[str],
        support_request_signals: list[str],
        bounty_uncertainty_signals: list[str],
        meta_bounty_signals: list[str],
        empty_response_count: int,
    ) -> tuple[str, list[str]]:
        signals: list[str] = []
        signals.extend(self.prefix_terms("bounty", matched_positive, 2))
        signals.extend(self.prefix_terms("platform", matched_platform, 1))
        signals.extend(self.prefix_terms("negative", matched_negative, 2))
        signals.extend(self.prefix_terms("action", matched_actionable, 2))
        signals.extend(self.prefix_terms("task", matched_task_terms, 2))
        if claim_signals:
            signals.append(f"claims:{len(claim_signals)}")
        if existing_work_signals:
            signals.append(f"existing-work:{len(existing_work_signals)}")
        if support_request_signals:
            signals.extend(self.prefix_terms("support", support_request_signals, 2))
        if bounty_uncertainty_signals:
            signals.extend(self.prefix_terms("bounty-check", bounty_uncertainty_signals, 2))
        if meta_bounty_signals:
            signals.extend(self.prefix_terms("meta", meta_bounty_signals, 2))
        if empty_response_count:
            signals.append(f"template-gaps:{empty_response_count}")

        normalized_title = title.lower()
        if recommendation == "pursue":
            return (
                "Direct bounty signals, actionable issue details, and low competition risk.",
                signals,
            )

        if recommendation == "review":
            if support_request_signals:
                return (
                    "Possible bounty wording, but the issue reads partly like end-user support.",
                    signals,
                )
            if matched_negative:
                return (
                    "Bounty signals exist, but noisy wording needs a manual sanity check.",
                    signals,
                )
            if competition_risk >= 8:
                return (
                    "Bounty looks real, but active discussion increases takeover risk.",
                    signals,
                )
            if actionability < 10:
                return (
                    "Likely bounty, but the issue still needs manual scoping before coding.",
                    signals,
                )
            return (
                "Plausible bounty with enough detail to inspect manually.",
                signals,
            )

        if existing_work_signals:
            return (
                "Existing PR or prior work is already referenced on the issue.",
                signals,
            )
        if bounty_uncertainty_signals:
            return (
                "A claimant had to verify whether the issue is actually bounty-backed.",
                signals,
            )
        if meta_bounty_signals:
            return (
                "This looks like bounty-program meta chatter or an alert feed, not a fixable task.",
                signals,
            )
        if support_request_signals:
            return (
                "Looks more like an end-user support request than a contributor bounty.",
                signals,
            )
        if "[test]" in normalized_title:
            return (
                "Looks like a test-focused task rather than a production bounty fix.",
                signals,
            )
        if competition_risk >= 14:
            return (
                "Competition risk is already high from claims, assignees, or comment traffic.",
                signals,
            )
        if matched_negative and bounty_confidence < 12:
            return (
                "The text matches noisy non-bounty patterns more than reliable bounty evidence.",
                signals,
            )
        if bounty_confidence < 8:
            return (
                "Bounty evidence is too weak to justify opening a workspace.",
                signals,
            )
        if actionability < 6:
            return (
                "Issue details are too thin to estimate a quick fix path.",
                signals,
            )
        return (
            "Overall score is not strong enough to justify follow-up right now.",
            signals,
        )

    @staticmethod
    def prefix_terms(prefix: str, values: list[str], limit: int) -> list[str]:
        return [f"{prefix}:{value}" for value in values[:limit]]

    @staticmethod
    def parse_datetime(value: str) -> datetime:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch GitHub issue details and triage bounty opportunities."
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("bounty_missions/tools/ntg/out/opportunities.json"),
        help="JSON output produced by scanner.py.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Maximum number of opportunities to triage from the input file.",
    )
    parser.add_argument(
        "--auth-file",
        type=Path,
        help="Optional auth file containing a GitHub token.",
    )
    parser.add_argument(
        "--profile",
        choices=sorted(TRIAGE_PROFILES.keys()),
        default="strict",
        help="Triage policy profile.",
    )
    parser.add_argument(
        "--json-output",
        type=Path,
        help="Write triaged results to JSON.",
    )
    parser.add_argument(
        "--markdown-output",
        type=Path,
        help="Write triaged results to Markdown.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    token, token_source = load_github_token(args.auth_file)
    if token_source:
        console.print(f"[green]auth[/green] using GitHub token from {token_source}")

    items = json.loads(args.input.read_text(encoding="utf-8"))
    triager = GitHubIssueTriager(token=token, policy=build_triage_policy(profile=args.profile))
    triaged = triager.triage(items, limit=args.limit)
    triager.render(triaged)

    if args.json_output:
        triager.save_json(triaged, args.json_output)
        console.print(f"[green]saved json[/green] {args.json_output}")

    if args.markdown_output:
        triager.save_markdown(triaged, args.markdown_output)
        console.print(f"[green]saved markdown[/green] {args.markdown_output}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
