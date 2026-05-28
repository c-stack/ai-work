import tempfile
import unittest
from pathlib import Path

from github_auth import parse_auth_credentials, parse_token_from_auth_file


class GitHubAuthTest(unittest.TestCase):
    def test_parse_three_line_auth_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "auth"
            path.write_text("# comment\nuser123\npass456\nghp_exampletoken\n", encoding="utf-8")

            creds = parse_auth_credentials(path)

        self.assertEqual(creds["username"], "user123")
        self.assertEqual(creds["password"], "pass456")
        self.assertEqual(creds["token"], "ghp_exampletoken")

    def test_parse_explicit_token_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "auth"
            path.write_text("token: github_pat_example\n", encoding="utf-8")

            token = parse_token_from_auth_file(path)

        self.assertEqual(token, "github_pat_example")


if __name__ == "__main__":
    unittest.main()
