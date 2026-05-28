import tempfile
import unittest
from pathlib import Path
from unittest import mock

from service import (
    build_template_factory_payload,
    generate_next_template_skeletons,
    load_mission_log_entries,
    parse_next_target_cves_from_mission_log,
    run_automation,
    select_automation_items,
    sync_bounty_ledger,
    sync_external_pr_history,
    update_ledger_entry,
)


class ServiceAutomationTest(unittest.TestCase):
    def test_select_automation_items_only_new_queue_entries(self) -> None:
        current = {
            "items": [
                {"repo": "a/b", "number": 1, "url": "u1", "recommendation": "pursue"},
                {"repo": "a/c", "number": 2, "url": "u2", "recommendation": "review"},
            ]
        }
        previous = {
            "items": [
                {"repo": "a/b", "number": 1, "url": "u1", "recommendation": "pursue"},
            ]
        }

        items = select_automation_items(
            current,
            previous,
            trigger_mode="new_queue_items",
            allowed_recommendations={"pursue", "review"},
        )

        self.assertEqual(items, [{"repo": "a/c", "number": 2, "url": "u2", "recommendation": "review"}])

    def test_run_automation_formats_command_and_uses_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            output_dir = root / "out"
            output_dir.mkdir()
            config = {
                "automation": {
                    "command": ["echo", "{repo}", "{number}", "{workspace_dir}"],
                    "trigger_on": ["pursue"],
                    "trigger_mode": "new_queue_items",
                    "max_items": 1,
                    "timeout_seconds": 10,
                    "extra_env": {"NTG_REPO": "{repo}"},
                }
            }
            queue_payload = {
                "items": [
                    {
                        "repo": "owner/repo",
                        "number": 7,
                        "url": "https://x/7",
                        "recommendation": "pursue",
                        "title": "Fix me",
                    }
                ]
            }
            previous = {"items": []}
            prepared = [{"repo": "owner/repo", "number": 7, "workspace_dir": str(workspace)}]

            with mock.patch("service.subprocess.run") as run:
                run.return_value = mock.Mock(returncode=0, stdout="ok", stderr="")
                results = run_automation(
                    config,
                    queue_payload=queue_payload,
                    previous_queue_payload=previous,
                    prepared_workspaces=prepared,
                    repo_root=root,
                    output_dir=output_dir,
                )

            self.assertEqual(results[0]["status"], "ok")
            run.assert_called_once()
            called = run.call_args.kwargs
            self.assertEqual(called["cwd"], str(workspace))
            self.assertEqual(called["env"]["NTG_REPO"], "owner/repo")

    def test_sync_bounty_ledger_builds_entries_and_preserves_manual_paid_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger_path = root / "bounty_ledger.json"
            ledger_path.write_text(
                """
{
  "items": [
    {
      "key": "owner/repo#7",
      "repo": "owner/repo",
      "number": 7,
      "status": "paid",
      "status_source": "manual",
      "claimed_value_usd": 300,
      "actual_revenue_usd": 280,
      "status_history": []
    }
  ]
}
""".strip(),
                encoding="utf-8",
            )

            payload = sync_bounty_ledger(
                ledger_path=ledger_path,
                triaged_items=[
                    {
                        "repo": "owner/repo",
                        "number": 7,
                        "title": "Fix me",
                        "url": "https://x/7",
                        "recommendation": "pursue",
                        "total_score": 77,
                        "bounty_confidence": 20,
                        "actionability": 18,
                        "bounty_amount_usd": 300,
                        "competition_risk": 7,
                    }
                ],
                queue_value_summary={
                    "items": [
                        {
                            "repo": "owner/repo",
                            "number": 7,
                            "url": "https://x/7",
                            "estimated_value_usd": 54.5,
                            "average_payout_usd": 300,
                        }
                    ]
                },
                prepared_workspaces=[{"repo": "owner/repo", "number": 7, "workspace_dir": str(root / "ws")}],
                automation_results=[{"repo": "owner/repo", "number": 7, "status": "ok", "workspace_dir": str(root / "ws")}],
                run_finished_at_utc="2026-05-28T14:40:00Z",
            )

            item = payload["items"][0]
            self.assertEqual(item["status"], "paid")
            self.assertEqual(item["status_source"], "manual")
            self.assertEqual(item["estimated_value_usd"], 54.5)
            self.assertEqual(payload["summary"]["paid_issues"], 1)

    def test_update_ledger_entry_applies_manual_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger_path = root / "bounty_ledger.json"
            ledger_path.write_text(
                """
{
  "items": [
    {
      "key": "owner/repo#9",
      "repo": "owner/repo",
      "number": 9,
      "status": "patch_ready",
      "status_source": "auto",
      "claimed_value_usd": 0,
      "actual_revenue_usd": 0,
      "status_history": []
    }
  ]
}
""".strip(),
                encoding="utf-8",
            )

            payload = update_ledger_entry(
                ledger_path=ledger_path,
                issue_key="owner/repo#9",
                status="submitted",
                claimed_value_usd=320,
                actual_revenue_usd=0,
                notes="ready to send",
            )

            item = payload["items"][0]
            self.assertEqual(item["status"], "submitted")
            self.assertEqual(item["status_source"], "manual")
            self.assertEqual(item["claimed_value_usd"], 320)
            self.assertEqual(item["notes"], "ready to send")
            self.assertEqual(payload["summary"]["submitted_issues"], 1)

    def test_load_mission_log_entries_parses_pr_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mission_log = root / "mission_log.md"
            mission_log.write_text(
                """
| PR # | Target CVE | Status | Reward Est. | Link |
| 16285 | CVE-2020-10987 | Open / Pending Review | $50 - $100 | [View PR](https://github.com/projectdiscovery/nuclei-templates/pull/16285) |
""".strip(),
                encoding="utf-8",
            )

            entries = load_mission_log_entries(mission_log)

            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["pr_number"], 16285)
            self.assertEqual(entries[0]["cve_id"], "CVE-2020-10987")
            self.assertEqual(entries[0]["reward_estimate_usd"], 75.0)

    def test_parse_next_target_cves_from_mission_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mission_log = root / "mission_log.md"
            mission_log.write_text(
                """
## 📝 Ongoing Research
- Next Targets: CVE-2020-28949 (Archive_Tar), CVE-2020-14871 (Solaris PAM).
""".strip(),
                encoding="utf-8",
            )

            entries = parse_next_target_cves_from_mission_log(mission_log)

            self.assertEqual(
                entries,
                [
                    {"cve_id": "CVE-2020-28949", "hint": "Archive_Tar"},
                    {"cve_id": "CVE-2020-14871", "hint": "Solaris PAM"},
                ],
            )

    def test_build_template_factory_payload_classifies_templates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            template_dir = root / "templates"
            template_dir.mkdir()
            (template_dir / "CVE-2020-28949.yaml").write_text("id: cve-2020-28949\n", encoding="utf-8")
            (template_dir / "CVE-2020-14871.yaml").write_text("id: cve-2020-14871\n", encoding="utf-8")
            upstream_dir = root / "upstream"
            (upstream_dir / "http/cves/2020").mkdir(parents=True)
            (upstream_dir / "http/cves/2020/CVE-2020-14871.yaml").write_text("id: cve-2020-14871\n", encoding="utf-8")
            mission_log = root / "mission_log.md"
            mission_log.write_text(
                """
| PR # | Target CVE | Status | Reward Est. | Link |
| 16285 | CVE-2020-10987 | Open / Pending Review | $50 - $100 | [View PR](https://github.com/projectdiscovery/nuclei-templates/pull/16285) |

## 📝 Ongoing Research
- Next Targets: CVE-2020-28949 (Archive_Tar), CVE-2020-14871 (Solaris PAM), CVE-2020-10987 (Tenda AC15).
""".strip(),
                encoding="utf-8",
            )

            payload = build_template_factory_payload(
                config={
                    "template_factory": {
                        "template_dir": str(template_dir),
                        "upstream_repo_checkout": str(upstream_dir),
                    }
                },
                repo_root=root,
                ledger_payload={
                    "items": [
                        {
                            "latest_recommendation": "external_pr",
                            "claimed_value_usd": 75.0,
                        }
                    ]
                },
                mission_log_path=mission_log,
            )

            self.assertEqual(payload["summary"]["local_template_count"], 2)
            self.assertEqual(payload["summary"]["next_target_count"], 3)
            self.assertEqual(payload["summary"]["next_target_ready_count"], 1)
            self.assertEqual(payload["summary"]["next_target_missing_template_count"], 0)
            actions = {item["cve_id"]: item["action"] for item in payload["next_targets"]}
            self.assertEqual(actions["CVE-2020-28949"], "ready_to_submit")
            self.assertEqual(actions["CVE-2020-14871"], "already_upstream")
            self.assertEqual(actions["CVE-2020-10987"], "tracked_submitted")

    def test_generate_next_template_skeletons_creates_missing_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            template_dir = root / "templates"
            template_dir.mkdir()
            mission_log = root / "mission_log.md"
            mission_log.write_text(
                """
## 📝 Ongoing Research
- Next Targets: CVE-2020-28949 (Archive_Tar), CVE-2020-14871 (Solaris PAM).
""".strip(),
                encoding="utf-8",
            )

            payload = generate_next_template_skeletons(
                config={
                    "template_factory": {
                        "template_dir": str(template_dir),
                        "generate_limit": 5,
                    }
                },
                repo_root=root,
                mission_log_path=mission_log,
            )

            self.assertEqual(payload["created_count"], 2)
            self.assertTrue((template_dir / "CVE-2020-28949.yaml").exists())
            self.assertTrue((template_dir / "CVE-2020-14871.yaml").exists())

    def test_sync_external_pr_history_sets_issue_key_and_submitted_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger_path = root / "bounty_ledger.json"
            mission_log = root / "mission_log.md"
            mission_log.write_text(
                """
| PR # | Target CVE | Status | Reward Est. | Link |
| 16285 | CVE-2020-10987 | Open / Pending Review | $50 - $100 | [View PR](https://github.com/projectdiscovery/nuclei-templates/pull/16285) |
""".strip(),
                encoding="utf-8",
            )

            with mock.patch(
                "service.fetch_pull_request_statuses",
                return_value={
                    16285: {
                        "state": "open",
                        "merged_at": None,
                        "title": "Add template for CVE-2020-10987 (Tenda AC15 RCE)",
                        "html_url": "https://github.com/projectdiscovery/nuclei-templates/pull/16285",
                    }
                },
            ):
                payload = sync_external_pr_history(
                    ledger_path=ledger_path,
                    mission_log_path=mission_log,
                    github_token="token",
                    run_finished_at_utc="2026-05-29T00:00:00Z",
                )

            item = payload["items"][0]
            self.assertEqual(item["key"], "projectdiscovery/nuclei-templates#pr-16285")
            self.assertEqual(item["issue_key"], "projectdiscovery/nuclei-templates#pr-16285")
            self.assertEqual(item["status"], "submitted")
            self.assertEqual(payload["summary"]["submitted_issues"], 1)


if __name__ == "__main__":
    unittest.main()
