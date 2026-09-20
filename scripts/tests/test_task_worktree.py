from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "task_worktree.py"
SPEC = importlib.util.spec_from_file_location("task_worktree_under_test", SCRIPT)
assert SPEC and SPEC.loader
WORKTREES = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = WORKTREES
SPEC.loader.exec_module(WORKTREES)


class TaskWorktreeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name) / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Test User")
        self.git("config", "user.email", "test@example.invalid")
        (self.repo / "app.txt").write_text("base\n", encoding="utf-8")
        (self.repo / "other.txt").write_text("other\n", encoding="utf-8")
        self.git("add", "app.txt", "other.txt")
        self.git("commit", "-q", "-m", "base")
        self.base = self.git("rev-parse", "HEAD").stdout.strip()

    def git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(self.repo), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            check=check,
        )

    def start(self, task_id: str = "task-one") -> dict[str, object]:
        return WORKTREES.start_task(
            str(self.repo),
            task_id,
            "Update app behavior",
            "app.txt contains accepted",
            ["app.txt"],
        )

    def checkpoint(self, task_id: str, attempt: int, rejected: bool = False) -> dict[str, object]:
        return WORKTREES.checkpoint_task(
            str(self.repo),
            task_id,
            attempt,
            f"attempt {attempt}",
            ["test command passed"],
            rejected,
            False,
            rejected,
        )

    def test_integrate_and_finalize_use_one_main_commit_and_remove_backups(self) -> None:
        started = self.start()
        worktree = Path(started["worktree"])
        (worktree / "app.txt").write_text("accepted\n", encoding="utf-8")
        checkpoint = self.checkpoint("task-one", 1)

        self.assertEqual((self.repo / "app.txt").read_text(encoding="utf-8"), "base\n")
        integrated = WORKTREES.integrate_task(str(self.repo), "task-one", ["functional test passed"])

        self.assertEqual((self.repo / "app.txt").read_text(encoding="utf-8"), "accepted\n")
        self.assertNotEqual(integrated["integration_commit"], checkpoint["commit"])
        self.assertEqual(self.git("rev-list", "--count", f"{self.base}..HEAD").stdout.strip(), "1")
        finalized = WORKTREES.finalize_task(str(self.repo), "task-one")
        self.assertTrue(finalized["backups_removed"])
        self.assertFalse(worktree.exists())
        self.assertFalse(WORKTREES.manifest_path(self.repo, "task-one").exists())

    def test_fifth_rejection_requires_parent_handoff_and_abort_discards_changes(self) -> None:
        started = self.start("five-failures")
        worktree = Path(started["worktree"])
        result = None
        for attempt in range(1, 6):
            (worktree / "app.txt").write_text(f"attempt {attempt}\n", encoding="utf-8")
            result = self.checkpoint("five-failures", attempt, rejected=True)

        assert result is not None
        self.assertEqual(result["status"], "handoff_required")
        self.assertTrue(result["parent_must_take_over"])
        with self.assertRaises(WORKTREES.WorktreeError):
            WORKTREES.integrate_task(str(self.repo), "five-failures", [])
        aborted = WORKTREES.abort_task(str(self.repo), "five-failures")
        self.assertTrue(aborted["parent_must_take_over"])
        self.assertEqual((self.repo / "app.txt").read_text(encoding="utf-8"), "base\n")
        self.assertEqual(self.git("rev-parse", "HEAD").stdout.strip(), self.base)
        self.assertFalse(worktree.exists())

    def test_rollback_integrated_restores_pre_task_content(self) -> None:
        started = self.start("rollback")
        worktree = Path(started["worktree"])
        (worktree / "app.txt").write_text("bad after integration\n", encoding="utf-8")
        self.checkpoint("rollback", 1)
        WORKTREES.integrate_task(str(self.repo), "rollback", ["initial test passed"])

        reverted = WORKTREES.rollback_integrated_task(str(self.repo), "rollback")

        self.assertEqual(reverted["status"], "reverted")
        self.assertEqual((self.repo / "app.txt").read_text(encoding="utf-8"), "base\n")
        self.assertEqual(self.git("rev-list", "--count", f"{self.base}..HEAD").stdout.strip(), "2")
        self.assertFalse(worktree.exists())

    def test_scope_violation_blocks_integration(self) -> None:
        started = self.start("scope")
        worktree = Path(started["worktree"])
        (worktree / "other.txt").write_text("outside scope\n", encoding="utf-8")
        checkpoint = self.checkpoint("scope", 1)

        self.assertEqual(checkpoint["scope_violations"], ["other.txt"])
        with self.assertRaises(WORKTREES.WorktreeError) as caught:
            WORKTREES.integrate_task(str(self.repo), "scope", [])
        self.assertEqual(caught.exception.code, "scope_violation")
        WORKTREES.abort_task(str(self.repo), "scope")

    def test_dirty_primary_worktree_is_rejected(self) -> None:
        (self.repo / "app.txt").write_text("dirty\n", encoding="utf-8")

        with self.assertRaises(WORKTREES.WorktreeError) as caught:
            self.start("dirty")

        self.assertEqual(caught.exception.code, "dirty_worktree")


if __name__ == "__main__":
    unittest.main()
