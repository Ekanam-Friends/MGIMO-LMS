"""Проверки конфликтов между состоянием Issue и статусом Project."""

import unittest
from scripts.sync_github_project import desired_change


def issue(state, changed="2026-11-16T10:00:00Z"):
    return {"state": state, "closedAt": changed if state == "CLOSED" else None,
            "timelineItems": {"nodes": [{"createdAt": changed}] if changed else []}}


def status(name, changed="2026-11-16T11:00:00Z", initial=False):
    return {"name": name, "updatedAt": changed,
            "createdAt": changed if initial else "2026-11-15T10:00:00Z"}


class ProjectSyncTests(unittest.TestCase):
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
