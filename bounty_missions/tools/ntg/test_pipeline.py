import unittest

from pipeline import build_learned_reputation_payload, filter_opportunities_by_reputation
from scanner import RepoReputationPolicy


class PipelineLearningTest(unittest.TestCase):
    def test_repos_with_repeat_skip_history_become_learned_blocks(self) -> None:
        items = [
            {"repo": "repeat/repo", "number": 1, "recommendation": "skip"},
            {"repo": "repeat/repo", "number": 2, "recommendation": "skip"},
            {"repo": "repeat/repo", "number": 3, "recommendation": "skip"},
            {"repo": "repeat/repo", "number": 4, "recommendation": "skip"},
            {"repo": "mixed/repo", "number": 1, "recommendation": "skip"},
            {"repo": "mixed/repo", "number": 2, "recommendation": "review"},
        ]

        payload = build_learned_reputation_payload(
            items,
            max_runs=12,
            min_skip_runs=4,
            min_unique_issues=2,
        )

        self.assertIn("repeat/repo", payload["blocked_repos"])
        self.assertNotIn("mixed/repo", payload["blocked_repos"])

    def test_reputation_filter_drops_blocked_merged_items(self) -> None:
        items = [
            {"repo": "blocked/repo", "number": 1, "score": 80},
            {"repo": "ok/repo", "number": 2, "score": 70},
        ]
        policy = RepoReputationPolicy(blocked_repos={"blocked/repo"})

        filtered = filter_opportunities_by_reputation(items, policy)

        self.assertEqual(filtered, [{"repo": "ok/repo", "number": 2, "score": 70}])


if __name__ == "__main__":
    unittest.main()
