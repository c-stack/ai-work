from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

import requests
from rich.console import Console
from rich.table import Table

console = Console()

AWESOME_BOUNTIES_URL = "https://raw.githubusercontent.com/JuanM94/awesome-bounties/main/README.md"


@dataclass
class RepoSeed:
    repo: str
    tech: str
    bounty_range: str
    difficulty: str
    source: str


class SourceDiscovery:
    def __init__(self, timeout: int = 20):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "ntg-source-discovery"})

    def fetch_awesome_bounties(self) -> list[RepoSeed]:
        response = self.session.get(AWESOME_BOUNTIES_URL, timeout=self.timeout)
        response.raise_for_status()
        markdown = response.text

        seeds: list[RepoSeed] = []
        pattern = re.compile(
            r"\| \[([^\]]+)\]\(https://github\.com/([^)\s]+)\) \| ([^|]+) \| ([^|]+) \| ([^|]+) \|"
        )
        for _label, repo, bounty_range, tech, difficulty in pattern.findall(markdown):
            if "/labels/" in repo:
                repo = repo.split("/labels/", 1)[0]
            repo = repo.strip("/")
            if repo.count("/") != 1:
                continue
            seeds.append(
                RepoSeed(
                    repo=repo,
                    tech=tech.strip(),
                    bounty_range=bounty_range.strip(),
                    difficulty=difficulty.strip(),
                    source="awesome-bounties",
                )
            )

        deduped: dict[str, RepoSeed] = {}
        for seed in seeds:
            current = deduped.get(seed.repo)
            if current is None or len(seed.bounty_range) > len(current.bounty_range):
                deduped[seed.repo] = seed
        return sorted(deduped.values(), key=lambda item: item.repo.lower())

    @staticmethod
    def render(seeds: list[RepoSeed]) -> None:
        if not seeds:
            console.print("[yellow]No repo seeds discovered.[/yellow]")
            return
        table = Table(title="Discovered Repo Seeds")
        table.add_column("Repo", style="cyan")
        table.add_column("Bounty", style="green")
        table.add_column("Tech", style="white")
        table.add_column("Difficulty", style="yellow")
        for seed in seeds:
            table.add_row(seed.repo, seed.bounty_range, seed.tech, seed.difficulty)
        console.print(table)

    @staticmethod
    def save_json(seeds: list[RepoSeed], output_path: Path) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps([asdict(seed) for seed in seeds], indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def save_repo_list(seeds: list[RepoSeed], output_path: Path) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("\n".join(seed.repo for seed in seeds) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Discover bounty repo seeds from public community lists."
    )
    parser.add_argument("--json-output", type=Path, help="Write structured seeds to JSON.")
    parser.add_argument("--repo-list-output", type=Path, help="Write repo-only seed list.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    discovery = SourceDiscovery()
    seeds = discovery.fetch_awesome_bounties()
    discovery.render(seeds)

    if args.json_output:
        discovery.save_json(seeds, args.json_output)
        console.print(f"[green]saved json[/green] {args.json_output}")
    if args.repo_list_output:
        discovery.save_repo_list(seeds, args.repo_list_output)
        console.print(f"[green]saved repos[/green] {args.repo_list_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
