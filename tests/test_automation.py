"""Tests for the GitHub analysis workflow without network access."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

SOURCE = Path(__file__).resolve().parents[1] / "trigger action" / "trigger_action.py"
spec = importlib.util.spec_from_file_location("trigger_action", SOURCE)
automation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(automation)


class CollectDiffTests(unittest.TestCase):
    def test_limits_patch_text_but_retains_all_paths(self):
        files = [SimpleNamespace(filename="first", patch="x" * 100),
                 SimpleNamespace(filename="second", patch="other")]
        text, paths = automation.collect_diff(files, 30)
        self.assertEqual(paths, ["first", "second"])
        self.assertEqual(len(text.split("\n[Diff truncated...]")[0]), 30)
        self.assertTrue(text.endswith("[Diff truncated...]"))

    def test_missing_patch_is_described(self):
        text, paths = automation.collect_diff([SimpleNamespace(filename="binary", patch=None)], 100)
        self.assertIn("[Patch unavailable]", text)
        self.assertEqual(paths, ["binary"])


class ModelOutputTests(unittest.TestCase):
    def test_rejects_invalid_output(self):
        for result in ([], {}, {"issue_title": "title", "issue_body": []},
                       {"issue_title": "title", "issue_body": "body", "severity": None},
                       {"issue_title": "title", "issue_body": "body", "labels": "bug"}):
            with self.subTest(result=result), self.assertRaises(ValueError):
                automation.validate_model_result(result)

    def test_accepts_valid_output(self):
        automation.validate_model_result({"issue_title": "title", "issue_body": "body", "labels": ["bug"]})


class AnalysisTests(unittest.TestCase):
    @patch.dict("os.environ", {"GITHUB_ACTOR": "unknown", "COMMIT_SHA": "abc"}, clear=True)
    def test_unapproved_push_is_not_read_or_sent(self):
        repo = Mock()
        with patch.object(automation, "call_model") as model:
            automation.run_analysis(repo, "owner/repo", "push", "token", {"approved"})
        repo.get_commit.assert_not_called()
        model.assert_not_called()

    @patch.dict("os.environ", {"GITHUB_ACTOR": "approved", "PR_NUMBER": "42"}, clear=True)
    def test_pull_request_uses_head_permalink_and_known_labels(self):
        pr = Mock()
        pr.user.login = "approved"
        pr.title = "Update"
        pr.body = None
        pr.labels = []
        pr.head.sha = "headsha"
        pr.get_files.return_value = [SimpleNamespace(filename="folder/name #1.js", patch="x" * 80)]
        repo = Mock()
        repo.get_pull.return_value = pr
        repo.get_issues.return_value = []
        repo.get_labels.return_value = [SimpleNamespace(name="bug"), SimpleNamespace(name="severity: high")]
        repo.create_issue.return_value = SimpleNamespace(number=3, title="Issue")
        result = {"issue_title": "Issue", "issue_body": "PUT_PERMALINK_HERE", "severity": "high",
                  "affected_file": "folder/name #1.js", "affected_line": 12,
                  "labels": ["bug", "unknown"], "summary": "Summary"}
        with patch.object(automation, "call_model", return_value=result):
            automation.run_analysis(repo, "owner/repo", "pull_request", "token", {"approved"})
        kwargs = repo.create_issue.call_args.kwargs
        self.assertIn("/blob/headsha/folder/name%20%231.js#L12", kwargs["body"])
        self.assertEqual(kwargs["labels"], ["bug", "severity: high"])
        pr.create_issue_comment.assert_called_once()


if __name__ == "__main__":
    unittest.main()
