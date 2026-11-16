"""Проверки конфликтов между состоянием Issue и статусом Project."""

import unittest
from scripts.sync_github_project import desired_change, workflow_mode, main
from unittest.mock import patch


def issue(state, changed="2026-11-16T10:00:00Z"):
    return {"state": state, "closedAt": changed if state == "CLOSED" else None,
            "timelineItems": {"nodes": [{"createdAt": changed}] if changed else []}}


def status(name, changed="2026-11-16T11:00:00Z", initial=False):
    return {"name": name, "updatedAt": changed,
            "createdAt": changed if initial else "2026-11-15T10:00:00Z"}


class ProjectSyncTests(unittest.TestCase):
    def test_manual_dry_run_is_available_with_schedule_disabled(self):
        for enabled in ("", "false", "true"):
            for given in ("", "true"):
                self.assertEqual(workflow_mode("workflow_dispatch", enabled, given), "dry-run")
            self.assertEqual(workflow_mode("workflow_dispatch", enabled, "false"), "apply")

    def test_cron_cannot_write_before_explicit_activation(self):
        for enabled in ("", "false"):
            for given in ("", "true", "false"):
                self.assertEqual(workflow_mode("schedule", enabled, given), "skip")
        self.assertEqual(workflow_mode("schedule", "true", ""), "apply")

    def test_unknown_workflow_inputs_fail_closed(self):
        for event, given in (("push", "false"), ("workflow_dispatch", "yes")):
            with self.assertRaises(ValueError):
                workflow_mode(event, "true", given)

    def test_cli_writes_only_with_explicit_apply(self):
        for arguments, expected in (([], True), (["--dry-run"], True), (["--apply"], False)):
            with patch("sys.argv", ["sync", *arguments]), patch.dict("os.environ", {}, clear=True), \
                    patch("scripts.sync_github_project.Sync") as sync:
                main()
                self.assertEqual(sync.call_args.args[-1], expected)
                sync.return_value.run.assert_called_once()

    def test_disabled_workflow_does_not_contact_github(self):
        with patch("sys.argv", ["sync", "--workflow"]), \
                patch.dict("os.environ", {"GITHUB_EVENT_NAME": "schedule"}, clear=True), \
                patch("scripts.sync_github_project.Sync") as sync:
            main()
            sync.assert_not_called()

    def test_done_closes_open_issue(self):
        self.assertEqual(desired_change(issue("OPEN"), status("Done")), ("issue", "CLOSED"))

    def test_moving_out_of_done_reopens_issue(self):
        for name in ("Todo", "In Progress"):
            with self.subTest(name=name):
                self.assertEqual(desired_change(issue("CLOSED"), status(name)), ("issue", "OPEN"))

    def test_later_issue_close_wins(self):
        self.assertEqual(desired_change(issue("CLOSED", "2026-11-16T12:00:00Z"),
                                        status("In Progress")), ("status", "Done"))

    def test_later_reopen_wins_over_done(self):
        self.assertEqual(desired_change(issue("OPEN", "2026-11-16T12:00:00Z"),
                                        status("Done")), ("status", "Todo"))

    def test_initial_todo_does_not_reopen_closed_issue(self):
        self.assertEqual(desired_change(issue("CLOSED"), status("Todo", initial=True)),
                         ("status", "Done"))

    def test_same_timestamp_prefers_issue(self):
        self.assertEqual(desired_change(issue("OPEN", "2026-11-16T11:00:00Z"),
                                        status("Done")), ("status", "Todo"))

    def test_matching_state_is_idempotent(self):
        for state, name in (("OPEN", "Todo"), ("OPEN", "In Progress"), ("CLOSED", "Done")):
            self.assertIsNone(desired_change(issue(state), status(name)))

    def test_missing_status_follows_issue(self):
        for state, name in (("OPEN", "Todo"), ("CLOSED", "Done")):
            self.assertEqual(desired_change(issue(state), None), ("status", name))

    def test_new_draft_converted_in_done_is_closed(self):
        self.assertEqual(desired_change(issue("OPEN", None), status("Done")), ("issue", "CLOSED"))

    def test_unknown_status_is_rejected(self):
        with self.assertRaises(ValueError):
            desired_change(issue("OPEN"), status("Cancelled"))


if __name__ == "__main__":
    unittest.main()
