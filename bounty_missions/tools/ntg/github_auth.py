from __future__ import annotations

import os
import subprocess
from pathlib import Path


def parse_auth_credentials(auth_file: Path) -> dict[str, str]:
    entries: list[str] = []
    fields: dict[str, str] = {}

    for raw_line in auth_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        entries.append(line)

        normalized = line.replace("：", ":")
        for key in (
            "token",
            "github_token",
            "gh_token",
            "username",
            "user",
            "login",
            "github_username",
            "password",
            "pass",
            "github_password",
        ):
            for separator in (":", "="):
                prefix = f"{key}{separator}"
                if normalized.lower().startswith(prefix):
                    value = normalized.split(separator, 1)[1].strip()
                    if value:
                        fields[key.lower()] = value

    if "token" not in fields:
        for line in entries:
            if line.startswith("ghp_") or line.startswith("github_pat_"):
                fields["token"] = line
                break

    if len(entries) >= 3:
        fields["username"] = fields.get("username") or entries[0]
        fields["password"] = fields.get("password") or entries[1]
        fields["token"] = fields.get("token") or entries[-1]

    if "username" not in fields:
        fields["username"] = (
            fields.get("user")
            or fields.get("login")
            or fields.get("github_username")
            or ""
        )
    if "password" not in fields:
        fields["password"] = fields.get("pass") or fields.get("github_password") or ""

    return {
        "username": fields.get("username", ""),
        "password": fields.get("password", ""),
        "token": fields.get("token", ""),
    }


def load_github_token(auth_file: Path | None = None) -> tuple[str | None, str | None]:
    env_token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    if env_token:
        return env_token.strip(), "environment"

    keychain_token = load_github_token_from_keychain()
    if keychain_token:
        return keychain_token, "macos-keychain"

    resolved = auth_file or find_default_auth_file()
    if resolved is None or not resolved.exists():
        return None, None

    token = parse_token_from_auth_file(resolved)
    if token:
        return token, str(resolved)

    return None, None


def find_default_auth_file(start: Path | None = None) -> Path | None:
    current = (start or Path.cwd()).resolve()
    for candidate in [current, *current.parents]:
        for name in ("auth.local", "auth"):
            auth_path = candidate / name
            if auth_path.exists():
                return auth_path
    return None


def parse_token_from_auth_file(auth_file: Path) -> str | None:
    token = parse_auth_credentials(auth_file).get("token", "").strip()
    return token or None


def load_github_token_from_keychain(
    service: str = "codex-github-token", account: str | None = None
) -> str | None:
    keychain_account = account or os.getenv("USER")
    if not keychain_account:
        return None

    try:
        result = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-a",
                keychain_account,
                "-s",
                service,
                "-w",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return None

    if result.returncode != 0:
        return None

    token = result.stdout.strip()
    return token or None
