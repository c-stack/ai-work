from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass
from html import unescape
from pathlib import Path

import requests
from rich.console import Console
from rich.table import Table

console = Console()


@dataclass
class AlgoraOpportunity:
    source: str
    org_slug: str
    org_name: str
    repo: str
    number: int
    title: str
    url: str
    reward_usd: int
    score: int


class AlgoraSource:
    def __init__(self, timeout: int = 20):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "ntg-algora-source"})

    def fetch_org(self, org_slug: str) -> list[AlgoraOpportunity]:
        html = ""
        resolved_url = ""
        for candidate_url in self.build_candidate_urls(org_slug):
            response = self.session.get(candidate_url, timeout=self.timeout)
            response.raise_for_status()
            candidate_html = response.text
            section = self.extract_open_bounties_section(candidate_html)
            if section:
                html = candidate_html
                resolved_url = candidate_url
                break
        if not html:
            return []

        org_name = self.parse_org_name(html, org_slug)
        section = self.extract_open_bounties_section(html)
        seen: dict[str, AlgoraOpportunity] = {}
        pattern = re.compile(
            r"\$([0-9,]+).*?<a href=\"(https://github\.com/([^\"/]+/[^\"/]+)/issues/(\d+))\"[^>]*>\s*([^<]+?)\s*</a>",
            re.DOTALL,
        )
        for reward, issue_url, repo, number, title in pattern.findall(section):
            normalized_title = self.clean_text(title)
            seen[issue_url] = AlgoraOpportunity(
                source=resolved_url,
                org_slug=org_slug,
                org_name=org_name,
                repo=repo,
                number=int(number),
                title=normalized_title,
                url=issue_url,
                reward_usd=int(reward.replace(",", "")),
                score=self.score_reward(int(reward.replace(",", ""))),
            )

        return sorted(seen.values(), key=lambda item: item.reward_usd, reverse=True)

    @staticmethod
    def parse_org_name(html: str, fallback: str) -> str:
        match = re.search(r"<title[^>]*>\s*(.*?)\s*\|\s*Algora\s*</title>", html, re.DOTALL)
        if not match:
            return fallback
        return AlgoraSource.clean_text(match.group(1))

    @staticmethod
    def extract_open_bounties_section(html: str) -> str:
        match = re.search(
            r"<!-- Bounties Section -->(.*?)<!-- Completed Bounties -->",
            html,
            re.DOTALL,
        )
        return match.group(1) if match else ""

    @staticmethod
    def build_candidate_urls(org_slug: str) -> list[str]:
        return [
            f"https://algora.io/{org_slug}/bounties?status=open",
            f"https://algora.io/{org_slug}/bounties",
            f"https://algora.io/{org_slug}/home",
            f"https://algora.io/{org_slug}",
        ]

    @staticmethod
    def clean_text(value: str) -> str:
        return " ".join(unescape(value).split())

    @staticmethod
    def score_reward(reward_usd: int) -> int:
        if reward_usd >= 1000:
            return 95
        if reward_usd >= 500:
            return 85
        if reward_usd >= 200:
            return 75
        if reward_usd >= 100:
            return 65
        if reward_usd >= 50:
            return 55
        return 45

    @staticmethod
    def render(items: list[AlgoraOpportunity]) -> None:
        if not items:
            console.print("[yellow]No Algora opportunities found.[/yellow]")
            return

        table = Table(title="Algora Opportunities")
        table.add_column("Org", style="cyan")
        table.add_column("Reward", style="green", justify="right")
        table.add_column("Repo", style="white")
        table.add_column("Issue", style="magenta", justify="right")
        table.add_column("Title", style="white")
        for item in items:
            table.add_row(
                item.org_slug,
                f"${item.reward_usd}",
                item.repo,
                str(item.number),
                item.title,
            )
        console.print(table)

    @staticmethod
    def save_json(items: list[AlgoraOpportunity], output_path: Path) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps([asdict(item) for item in items], indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def save_markdown(items: list[AlgoraOpportunity], output_path: Path) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            "# Algora Opportunities",
            "",
            "| Org | Reward | Repo | Issue | Title | Link |",
            "| --- | ---: | --- | ---: | --- | --- |",
        ]
        for item in items:
            title = item.title.replace("|", "/")
            lines.append(
                f"| {item.org_slug} | ${item.reward_usd} | {item.repo} | {item.number} | {title} | [open]({item.url}) |"
            )
        output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch open bounties from public Algora org pages."
    )
    parser.add_argument(
        "--org",
        action="append",
        default=[],
        help="Algora org slug. May be supplied multiple times.",
    )
    parser.add_argument(
        "--org-file",
        type=Path,
        help="Optional file containing one Algora org slug per line.",
    )
    parser.add_argument("--json-output", type=Path, help="Write results to JSON.")
    parser.add_argument("--markdown-output", type=Path, help="Write results to Markdown.")
    return parser.parse_args()


def load_orgs(args: argparse.Namespace) -> list[str]:
    orgs = list(args.org)
    if args.org_file and args.org_file.exists():
        for line in args.org_file.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                orgs.append(stripped)
    return orgs


def main() -> int:
    args = parse_args()
    orgs = load_orgs(args)
    if not orgs:
        console.print("[bold red]No orgs provided.[/bold red]")
        return 2

    source = AlgoraSource()
    all_items: list[AlgoraOpportunity] = []
    for org in orgs:
        console.print(f"[bold blue]fetch[/bold blue] algora:{org}")
        try:
            all_items.extend(source.fetch_org(org))
        except requests.RequestException as exc:
            console.print(f"[bold red]Algora fetch failed[/bold red] {org}: {exc}")

    all_items.sort(key=lambda item: (item.reward_usd, item.repo, item.number), reverse=True)
    source.render(all_items)

    if args.json_output:
        source.save_json(all_items, args.json_output)
        console.print(f"[green]saved json[/green] {args.json_output}")

    if args.markdown_output:
        source.save_markdown(all_items, args.markdown_output)
        console.print(f"[green]saved markdown[/green] {args.markdown_output}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
