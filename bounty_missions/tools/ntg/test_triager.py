import unittest
from unittest import mock

import requests

from triager import GitHubIssueTriager, build_triage_policy


class TriagerHeuristicsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.triager = GitHubIssueTriager(policy=build_triage_policy(profile="aggressive"))
        self.base_item = {
            "repo": "example/repo",
            "number": 42,
            "title": "placeholder",
            "url": "https://github.com/example/repo/issues/42",
            "score": 70,
        }
        self.repo = {"language": "TypeScript"}

    def test_support_ticket_with_bounty_title_is_skipped(self) -> None:
        issue = {
            "title": "[Bug]: BUG BOUNTY",
            "body": """### Summary

For the login method for my account, I added a passkey but still need a password again.
Please provide a solution for my device.

### Med Version

_No response_

### Expected Behavior

_No response_

### Relevant log output

```shell

```
""",
            "labels": [{"name": "bug"}],
            "created_at": "2026-05-27T15:24:07Z",
            "comments": 2,
        }
        comments = [
            {"body": "/Claim #42"},
            {
                "body": "Could you also clarify whether this issue is part of a contributor reward/bounty program, or handled as a regular community contribution?"
            },
            {"body": "Thank you for response"},
        ]

        result = self.triager.score_issue(self.base_item, issue, comments, self.repo, ["TypeScript"])

        self.assertEqual(result.recommendation, "skip")
        self.assertIn("bounty-unclear", result.bounty_uncertainty_signals)
        self.assertGreaterEqual(result.empty_response_count, 2)

    def test_direct_engineering_bounty_remains_actionable(self) -> None:
        issue = {
            "title": "Add OAuth callback retry handling for bounty",
            "body": """### Summary

Bug bounty: login callback fails with 500 when the provider returns a temporary timeout.

### Steps to reproduce

1. Start the dev server.
2. Configure `AUTH_CALLBACK_TIMEOUT_MS=50`.
3. Trigger `/api/auth/callback/github`.

### Expected behavior

The callback should retry once and return a typed error instead of a 500.

### Relevant log output

```text
callback timeout after 50ms
```
""",
            "labels": [{"name": "bug"}],
            "created_at": "2026-05-27T15:24:07Z",
            "comments": 0,
        }
        comments = []

        result = self.triager.score_issue(self.base_item, issue, comments, self.repo, ["TypeScript"])

        self.assertIn(result.recommendation, {"pursue", "review"})
        self.assertFalse(result.support_request_signals)
        self.assertGreater(result.actionability, 10)

    def test_explicit_bounty_amount_is_extracted_from_issue_text(self) -> None:
        issue = {
            "title": "Fix retry loop for $300 bounty",
            "body": """This payout is $300 for the contributor who fixes the retry loop.

### Steps to reproduce

1. Run the failing sync task.
2. Observe the infinite retry.
""",
            "labels": [{"name": "bug"}],
            "created_at": "2026-05-27T15:24:07Z",
            "comments": 0,
        }

        result = self.triager.score_issue(self.base_item, issue, [], self.repo, ["TypeScript"])

        self.assertEqual(result.bounty_amount_usd, 300)
        self.assertIn(result.bounty_amount_signal, {"contextual_amount", "generic_amount"})

    def test_algora_reward_is_preferred_when_present(self) -> None:
        base_item = dict(self.base_item)
        base_item["reward_usd"] = 500
        issue = {
            "title": "Reward issue",
            "body": "Fix the login race condition.",
            "labels": [{"name": "bug"}],
            "created_at": "2026-05-27T15:24:07Z",
            "comments": 0,
        }

        result = self.triager.score_issue(base_item, issue, [], self.repo, ["TypeScript"])

        self.assertEqual(result.bounty_amount_usd, 500)
        self.assertEqual(result.bounty_amount_signal, "algora_reward")

    def test_bounty_alert_repo_issue_is_skipped(self) -> None:
        issue = {
            "title": "🎯 Bounty Alert: 10 New Opportunityies found",
            "body": """### Active Bounty Scan Results

Bug bounty program watchers found new opportunities.
This issue is only an alert feed with links to external repositories.
""",
            "labels": [{"name": "bounty-alert"}],
            "created_at": "2026-05-27T15:24:07Z",
            "comments": 0,
        }
        comments = []

        result = self.triager.score_issue(self.base_item, issue, comments, self.repo, ["Python"])

        self.assertEqual(result.recommendation, "skip")
        self.assertIn("bounty-alert", result.meta_bounty_signals)

    def test_issue_comments_404_is_treated_as_empty(self) -> None:
        response = mock.Mock()
        response.raise_for_status.side_effect = requests.HTTPError(response=mock.Mock(status_code=404))

        with mock.patch.object(self.triager.session, "get", return_value=response):
            comments = self.triager.get_issue_comments("example/repo", 42, limit=10)

        self.assertEqual(comments, [])

    def test_repo_languages_404_falls_back_to_primary_language(self) -> None:
        response = mock.Mock()
        response.raise_for_status.side_effect = requests.HTTPError(response=mock.Mock(status_code=404))

        with mock.patch.object(self.triager.session, "get", return_value=response):
            with mock.patch.object(self.triager, "get_repo", return_value={"language": "Go"}):
                languages = self.triager.get_repo_languages("example/repo")

        self.assertEqual(languages, ["Go"])


if __name__ == "__main__":
    unittest.main()
