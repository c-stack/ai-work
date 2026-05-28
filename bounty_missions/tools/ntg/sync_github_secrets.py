from __future__ import annotations

import argparse
import subprocess
from pathlib import Path
import importlib.util

import requests

from github_auth import parse_auth_credentials


def set_secret(repo: str, name: str, value: str, github_token: str) -> None:
    if command_exists("gh"):
        subprocess.run(
            ["gh", "secret", "set", name, "-R", repo],
            input=value,
            text=True,
            check=True,
        )
        return

    set_secret_via_api(repo, name, value, github_token)


def command_exists(name: str) -> bool:
    return subprocess.run(
        ["which", name],
        capture_output=True,
        text=True,
        check=False,
    ).returncode == 0


def load_nacl():
    if not importlib.util.find_spec("nacl"):
        raise SystemExit("PyNaCl is required when gh is unavailable. Install it with: python3 -m pip install PyNaCl")
    from nacl import encoding, public
    return encoding, public


def get_repo_public_key(repo: str, token: str) -> tuple[str, str]:
    response = requests.get(
        f"https://api.github.com/repos/{repo}/actions/secrets/public-key",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    return payload["key_id"], payload["key"]


def encrypt_secret(value: str, public_key_b64: str) -> str:
    encoding, public = load_nacl()
    sealed_box = public.SealedBox(
        public.PublicKey(public_key_b64.encode("utf-8"), encoding.Base64Encoder())
    )
    encrypted = sealed_box.encrypt(value.encode("utf-8"))
    return encoding.Base64Encoder.encode(encrypted).decode("utf-8")


def set_secret_via_api(repo: str, name: str, value: str, github_token: str) -> None:
    key_id, key = get_repo_public_key(repo, github_token)
    encrypted_value = encrypt_secret(value, key)
    response = requests.put(
        f"https://api.github.com/repos/{repo}/actions/secrets/{name}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {github_token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        json={"encrypted_value": encrypted_value, "key_id": key_id},
        timeout=20,
    )
    response.raise_for_status()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync NTG GitHub credentials from a local auth file into repo secrets."
    )
    parser.add_argument("repo", help="Target GitHub repo in owner/name format.")
    parser.add_argument(
        "--auth-file",
        type=Path,
        default=Path("auth"),
        help="Path to local auth file.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    credentials = parse_auth_credentials(args.auth_file)

    token = credentials.get("token", "").strip()
    if not token:
        raise SystemExit("No GitHub token could be parsed from auth file.")

    set_secret(args.repo, "NTG_GITHUB_TOKEN", token, token)
    print("set secret: NTG_GITHUB_TOKEN")

    username = credentials.get("username", "").strip()
    if username:
        set_secret(args.repo, "NTG_GITHUB_USERNAME", username, token)
        print("set secret: NTG_GITHUB_USERNAME")

    password = credentials.get("password", "").strip()
    if password:
        set_secret(args.repo, "NTG_GITHUB_PASSWORD", password, token)
        print("set secret: NTG_GITHUB_PASSWORD")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
