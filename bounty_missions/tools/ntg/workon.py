from __future__ import annotations

import argparse
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from github_auth import load_github_token
from rich.console import Console

console = Console()


@dataclass
class WorkContext:
    repo: str
    issue_number: int
    workspace_dir: Path
    repo_dir: Path
    context_dir: Path


class GitHubWorkspacePreparer:
    def __init__(self, token: str | None = None, timeout: int = 20):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/vnd.github+json",
                "User-Agent": "ntg-workon",
            }
        )
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"

    def prepare(self, repo: str, issue_number: int, base_dir: Path) -> WorkContext:
        repo_slug = repo.replace("/", "__")
        workspace_dir = base_dir / f"{repo_slug}__issue{issue_number}"
        repo_dir = workspace_dir / "repo"
        context_dir = workspace_dir / "context"
        context_dir.mkdir(parents=True, exist_ok=True)

        repo_payload = self.get_json(f"https://api.github.com/repos/{repo}")
        issue_payload = self.get_json(f"https://api.github.com/repos/{repo}/issues/{issue_number}")
        comments_payload = self.get_json(
            f"https://api.github.com/repos/{repo}/issues/{issue_number}/comments?per_page=20"
        )
        languages_payload = self.get_json(f"https://api.github.com/repos/{repo}/languages")

        self.ensure_repo_checkout(
            clone_url=repo_payload["clone_url"],
            default_branch=repo_payload["default_branch"],
            repo_dir=repo_dir,
        )

        searches = self.run_initial_searches(repo_dir, issue_payload, comments_payload)

        (context_dir / "repo.json").write_text(
            json.dumps(repo_payload, indent=2), encoding="utf-8"
        )
        (context_dir / "issue.json").write_text(
            json.dumps(issue_payload, indent=2), encoding="utf-8"
        )
        (context_dir / "comments.json").write_text(
            json.dumps(comments_payload, indent=2), encoding="utf-8"
        )
        (context_dir / "languages.json").write_text(
            json.dumps(languages_payload, indent=2), encoding="utf-8"
        )
        (context_dir / "searches.json").write_text(
            json.dumps(searches, indent=2), encoding="utf-8"
        )
        (workspace_dir / "README.md").write_text(
            self.render_summary(repo_payload, issue_payload, comments_payload, searches),
            encoding="utf-8",
        )

        return WorkContext(
            repo=repo,
            issue_number=issue_number,
            workspace_dir=workspace_dir,
            repo_dir=repo_dir,
            context_dir=context_dir,
        )

    def get_json(self, url: str) -> Any:
        response = self.session.get(url, timeout=self.timeout)
        response.raise_for_status()
        return response.json()

    def ensure_repo_checkout(self, clone_url: str, default_branch: str, repo_dir: Path) -> None:
        if not repo_dir.exists():
            repo_dir.parent.mkdir(parents=True, exist_ok=True)
            self.run_cmd(
                [
                    "git",
                    "clone",
                    "--depth",
                    "1",
                    "--branch",
                    default_branch,
                    clone_url,
                    str(repo_dir),
                ]
            )
            return

        self.run_cmd(["git", "-C", str(repo_dir), "fetch", "--depth", "1", "origin", default_branch])
        self.run_cmd(["git", "-C", str(repo_dir), "checkout", default_branch])
        self.run_cmd(["git", "-C", str(repo_dir), "pull", "--ff-only", "origin", default_branch])

    def run_initial_searches(
        self,
        repo_dir: Path,
        issue_payload: dict[str, Any],
        comments_payload: list[dict[str, Any]],
    ) -> dict[str, list[str]]:
        combined = "\n".join(
            [
                issue_payload.get("title", ""),
                issue_payload.get("body") or "",
                *[(comment.get("body") or "") for comment in comments_payload],
            ]
        ).lower()

        patterns: list[str] = []
        for term in ("passkey", "webauthn", "login", "password", "auth", "signin"):
            if term in combined:
                patterns.append(term)

        if not patterns:
            patterns = self.extract_keywords(issue_payload.get("title", "") + " " + (issue_payload.get("body") or ""))

        searches: dict[str, list[str]] = {}
        for pattern in patterns[:6]:
            matches = self.ripgrep(repo_dir, pattern)
            if matches:
                searches[pattern] = matches[:20]
        return searches

    @staticmethod
    def extract_keywords(text: str) -> list[str]:
        tokens = re.findall(r"[A-Za-z][A-Za-z0-9_-]{3,}", text.lower())
        skip = {
            "summary",
            "what",
            "happened",
            "expected",
            "behavior",
            "response",
            "issue",
            "steps",
            "additional",
            "context",
            "none",
        }
        result: list[str] = []
        for token in tokens:
            if token in skip or token in result:
                continue
            result.append(token)
        return result

    def ripgrep(self, repo_dir: Path, pattern: str) -> list[str]:
        result = subprocess.run(
            [
                "rg",
                "-n",
                "-m",
                "20",
                "--max-columns",
                "240",
                "--hidden",
                "--glob",
                "!.git",
                "--glob",
                "!**/go.sum",
                "--glob",
                "!**/*.sum",
                "--glob",
                "!**/*.md",
                "--glob",
                "!**/.github/**",
                "--glob",
                "!**/docs/static/**",
                "--glob",
                "!docs/static/swagger-ui.js",
                "--glob",
                "!**/*.min.js",
                "--glob",
                "!**/node_modules/**",
                "--glob",
                "!**/vendor/**",
                "--glob",
                "!**/dist/**",
                "--glob",
                "!**/build/**",
                "--glob",
                "!**/ts-client/**",
                pattern,
                str(repo_dir),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode not in (0, 1):
            return []
        return [line for line in result.stdout.splitlines() if line.strip()]

    def run_cmd(self, cmd: list[str]) -> None:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise RuntimeError(
                f"command failed: {' '.join(cmd)}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )

    @staticmethod
    def render_summary(
        repo_payload: dict[str, Any],
        issue_payload: dict[str, Any],
        comments_payload: list[dict[str, Any]],
        searches: dict[str, list[str]],
    ) -> str:
        lines = [
            f"# Work Context: {repo_payload['full_name']}#{issue_payload['number']}",
            "",
            f"- Title: {issue_payload['title']}",
            f"- URL: {issue_payload['html_url']}",
            f"- Default branch: {repo_payload['default_branch']}",
            f"- Primary language: {repo_payload.get('language') or '-'}",
            f"- Opened: {issue_payload['created_at']}",
            f"- Updated: {issue_payload['updated_at']}",
            f"- Comments: {issue_payload.get('comments', 0)}",
            "",
            "## Issue Summary",
            "",
            (issue_payload.get("body") or "_No body_").strip(),
            "",
            "## Comments",
            "",
        ]

        if comments_payload:
            for comment in comments_payload:
                body = (comment.get("body") or "").strip() or "_No body_"
                lines.append(
                    f"- {comment['user']['login']} at {comment['created_at']}: {body}"
                )
        else:
            lines.append("_No comments_")

        lines.extend(["", "## Initial Code Search", ""])
        if searches:
            for pattern, matches in searches.items():
                lines.append(f"### `{pattern}`")
                lines.append("")
                for match in matches[:10]:
                    lines.append(f"- `{match}`")
                lines.append("")
        else:
            lines.append("_No product-code hits found after filtering generated/docs artifacts._")

        return "\n".join(lines).rstrip() + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prepare a local workspace for a selected bounty issue."
    )
    parser.add_argument("--repo", required=True, help="GitHub repo in owner/name format.")
    parser.add_argument("--issue", type=int, required=True, help="GitHub issue number.")
    parser.add_argument(
        "--base-dir",
        type=Path,
        default=Path("bounty_missions/workspaces"),
        help="Base directory for prepared workspaces.",
    )
    parser.add_argument(
        "--auth-file",
        type=Path,
        help="Optional auth file containing a GitHub token.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    token, token_source = load_github_token(args.auth_file)
    if token_source:
        console.print(f"[green]auth[/green] using GitHub token from {token_source}")

    preparer = GitHubWorkspacePreparer(token=token)
    context = preparer.prepare(args.repo, args.issue, args.base_dir)
    console.print(f"[green]workspace[/green] {context.workspace_dir}")
    console.print(f"[green]repo[/green] {context.repo_dir}")
    console.print(f"[green]context[/green] {context.context_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
