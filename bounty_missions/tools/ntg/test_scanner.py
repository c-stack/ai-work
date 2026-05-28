import unittest

from scanner import (
    GitHubBountyScanner,
    RepoReputationPolicy,
    SearchTarget,
    merge_reputation_policies,
    repo_matches_reputation_policy,
)


class ScannerExclusionTest(unittest.TestCase):
    def test_meta_alert_issue_is_excluded(self) -> None:
        target = SearchTarget(name="wide", keywords=["bounty"])
        issue = {
            "title": "🎯 Bounty Alert: 10 New Opportunities found",
            "body": "### Active Bounty Scan Results\nRequest indexing for upstream repos.",
            "labels": [{"name": "bounty-alert"}],
        }

        self.assertTrue(GitHubBountyScanner._is_excluded(target, issue))

    def test_normal_engineering_issue_is_not_excluded(self) -> None:
        target = SearchTarget(name="wide", keywords=["bounty"])
        issue = {
            "title": "Bug bounty: retry OAuth callback on timeout",
            "body": "Steps to reproduce and expected behavior are included below.",
            "labels": [{"name": "bug"}],
        }

        self.assertFalse(GitHubBountyScanner._is_excluded(target, issue))

    def test_bounty_aggregator_repo_is_blocked_by_reputation_policy(self) -> None:
        scanner = GitHubBountyScanner(
            client=None,  # type: ignore[arg-type]
            reputation_policy=RepoReputationPolicy(),
        )
        target = SearchTarget(name="wide", keywords=["algora"])
        repo_data = {
            "description": "Scouting GitHub for active bounties and sending issue alerts.",
            "stargazers_count": 42,
        }

        blocked = scanner._is_repo_blocked(target, "dev-kp-eloper/BountyScout", repo_data)

        self.assertTrue(blocked)

    def test_low_star_unscoped_repo_is_blocked(self) -> None:
        scanner = GitHubBountyScanner(
            client=None,  # type: ignore[arg-type]
            reputation_policy=RepoReputationPolicy(min_stars_unscoped=15),
        )
        target = SearchTarget(name="wide", keywords=["algora"])
        repo_data = {"description": "Normal product repo", "stargazers_count": 3}

        blocked = scanner._is_repo_blocked(target, "someone/product", repo_data)

        self.assertTrue(blocked)

    def test_seeded_repo_can_bypass_unscoped_star_floor(self) -> None:
        scanner = GitHubBountyScanner(
            client=None,  # type: ignore[arg-type]
            reputation_policy=RepoReputationPolicy(min_stars_unscoped=15),
        )
        target = SearchTarget(name="seeded", repo="someone/product", keywords=["bounty"])
        repo_data = {"description": "Normal product repo", "stargazers_count": 3}

        blocked = scanner._is_repo_blocked(target, "someone/product", repo_data)

        self.assertFalse(blocked)

    def test_merge_reputation_policies_unions_rules_and_keeps_stricter_floor(self) -> None:
        merged = merge_reputation_policies(
            RepoReputationPolicy(
                blocked_repos={"a/b"},
                blocked_repo_patterns=["foo"],
                min_stars_unscoped=10,
            ),
            RepoReputationPolicy(
                blocked_repos={"c/d"},
                blocked_repo_patterns=["bar"],
                min_stars_unscoped=25,
            ),
        )

        self.assertEqual(merged.blocked_repos, {"a/b", "c/d"})
        self.assertIn("foo", merged.blocked_repo_patterns)
        self.assertIn("bar", merged.blocked_repo_patterns)
        self.assertEqual(merged.min_stars_unscoped, 25)

    def test_repo_matches_reputation_policy_blocks_explicit_repo(self) -> None:
        policy = RepoReputationPolicy(blocked_repos={"owner/repo"})

        self.assertTrue(
            repo_matches_reputation_policy(
                "owner/repo",
                policy,
                is_scoped_target=True,
            )
        )


if __name__ == "__main__":
    unittest.main()
