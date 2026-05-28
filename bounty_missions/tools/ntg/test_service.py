import tempfile
import unittest
from pathlib import Path
from unittest import mock

from service import run_automation, select_automation_items


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


if __name__ == "__main__":
    unittest.main()
