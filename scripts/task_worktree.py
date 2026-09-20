#!/usr/bin/env python3
"""Manage isolated writable CustomAgent tasks with Git checkpoints and rollback."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any


TASK_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
WORKTREE_DIRECTORY = ".codex-worktrees"
EXCLUDE_LINE = f"/{WORKTREE_DIRECTORY}/"
MAX_FAILURES = 5


class WorktreeError(RuntimeError):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


def configure_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8")
            except (AttributeError, OSError, ValueError):
                pass


configure_utf8_stdio()


def run_git(
    repo: Path,
    *args: str,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        check=False,
    )
    if check and completed.returncode != 0:
        raise WorktreeError(
            "git_failed",
            f"Git command failed: git {' '.join(args)}",
            {"stderr": completed.stderr[-1200:]},
        )
    return completed


def repository_root(value: str | Path) -> Path:
    candidate = Path(value).expanduser().resolve()
    completed = run_git(candidate, "rev-parse", "--show-toplevel")
    return Path(completed.stdout.strip()).resolve()


def validate_task_id(task_id: str) -> str:
    if not TASK_ID_PATTERN.fullmatch(task_id):
        raise WorktreeError(
            "invalid_task_id",
            "Task ID must use letters, digits, dots, underscores, or hyphens and be at most 64 characters.",
        )
    return task_id


def common_git_dir(repo: Path) -> Path:
    value = run_git(repo, "rev-parse", "--git-common-dir").stdout.strip()
    path = Path(value)
    return (repo / path).resolve() if not path.is_absolute() else path.resolve()


def task_state_dir(repo: Path, task_id: str) -> Path:
    return common_git_dir(repo) / "codex-custom-agent" / "tasks" / validate_task_id(task_id)


def manifest_path(repo: Path, task_id: str) -> Path:
    return task_state_dir(repo, task_id) / "manifest.json"


def worktree_path(repo: Path, task_id: str) -> Path:
    return repo / WORKTREE_DIRECTORY / validate_task_id(task_id)


def branch_name(task_id: str) -> str:
    return f"codex/custom-agent/{validate_task_id(task_id)}"


def is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def read_manifest(repo: Path, task_id: str) -> dict[str, Any]:
    path = manifest_path(repo, task_id)
    if not path.is_file():
        raise WorktreeError("task_missing", f"No managed task exists for {task_id}.")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorktreeError("manifest_invalid", "Task manifest is unreadable.") from exc
    if not isinstance(payload, dict):
        raise WorktreeError("manifest_invalid", "Task manifest is not an object.")
    return payload


def normalized_scopes(values: list[str]) -> list[str]:
    scopes = values or ["."]
    normalized: list[str] = []
    for value in scopes:
        path = Path(value.replace("\\", "/"))
        if path.is_absolute() or ".." in path.parts:
            raise WorktreeError("invalid_scope", f"Write scope must be repository-relative: {value}")
        text = path.as_posix().strip("/") or "."
        if text not in normalized:
            normalized.append(text)
    return normalized


def path_in_scope(path: str, scopes: list[str]) -> bool:
    normalized = Path(path.replace("\\", "/")).as_posix().strip("/")
    return any(scope == "." or normalized == scope or normalized.startswith(f"{scope}/") for scope in scopes)


def ensure_local_exclude(repo: Path) -> None:
    raw = run_git(repo, "rev-parse", "--git-path", "info/exclude").stdout.strip()
    exclude = Path(raw)
    if not exclude.is_absolute():
        exclude = repo / exclude
    exclude = exclude.resolve()
    exclude.parent.mkdir(parents=True, exist_ok=True)
    lines = exclude.read_text(encoding="utf-8").splitlines() if exclude.is_file() else []
    if EXCLUDE_LINE not in lines:
        with exclude.open("a", encoding="utf-8", newline="\n") as handle:
            if lines and exclude.stat().st_size:
                handle.write("\n")
            handle.write(f"{EXCLUDE_LINE}\n")


def remove_local_exclude_if_unused(repo: Path) -> None:
    root = repo / WORKTREE_DIRECTORY
    if root.exists() and any(root.iterdir()):
        return
    if root.is_dir():
        root.rmdir()
    raw = run_git(repo, "rev-parse", "--git-path", "info/exclude").stdout.strip()
    exclude = Path(raw)
    if not exclude.is_absolute():
        exclude = repo / exclude
    exclude = exclude.resolve()
    if not exclude.is_file():
        return
    lines = exclude.read_text(encoding="utf-8").splitlines()
    updated = [line for line in lines if line != EXCLUDE_LINE]
    exclude.write_text("\n".join(updated).rstrip() + ("\n" if updated else ""), encoding="utf-8")


def require_clean(repo: Path, label: str) -> None:
    status = run_git(repo, "status", "--porcelain=v1", "--untracked-files=all").stdout
    if status.strip():
        raise WorktreeError(
            "dirty_worktree",
            f"{label} must be clean before this operation.",
            {"status": status.splitlines()[:50]},
        )


def start_task(
    repo_value: str,
    task_id: str,
    summary: str,
    acceptance: str,
    scopes: list[str],
) -> dict[str, Any]:
    repo = repository_root(repo_value)
    task_id = validate_task_id(task_id)
    ensure_local_exclude(repo)
    require_clean(repo, "Primary worktree")
    state = task_state_dir(repo, task_id)
    worktree = worktree_path(repo, task_id)
    branch = branch_name(task_id)
    if state.exists() or worktree.exists():
        raise WorktreeError("task_exists", f"Task {task_id} already has managed state.")
    if run_git(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False).returncode == 0:
        raise WorktreeError("branch_exists", f"Managed branch already exists: {branch}")
    base = run_git(repo, "rev-parse", "HEAD").stdout.strip()
    base_branch_result = run_git(repo, "symbolic-ref", "--short", "-q", "HEAD", check=False)
    base_branch = base_branch_result.stdout.strip() or None
    worktree.parent.mkdir(parents=True, exist_ok=True)
    try:
        run_git(repo, "worktree", "add", "-b", branch, str(worktree), base)
        payload = {
            "schema_version": 1,
            "task_id": task_id,
            "status": "active",
            "repo": str(repo),
            "worktree": str(worktree),
            "branch": branch,
            "base_branch": base_branch,
            "base_commit": base,
            "summary": summary[:4000],
            "acceptance": acceptance[:8000],
            "write_scopes": normalized_scopes(scopes),
            "counters": {
                "attempt_failures": 0,
                "parent_redirects": 0,
                "review_rejections": 0,
            },
            "checkpoints": [],
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        atomic_json(state / "manifest.json", payload)
    except Exception:
        run_git(repo, "worktree", "remove", "--force", str(worktree), check=False)
        run_git(repo, "branch", "-D", branch, check=False)
        if state.exists() and is_within(state, common_git_dir(repo) / "codex-custom-agent" / "tasks"):
            shutil.rmtree(state)
        raise
    return {
        "status": "active",
        "task_id": task_id,
        "repo": str(repo),
        "worktree": str(worktree),
        "branch": branch,
        "base_commit": base,
        "write_scopes": payload["write_scopes"],
    }


def staged_paths(worktree: Path) -> list[str]:
    output = run_git(worktree, "diff", "--cached", "--name-only", "-z").stdout
    return sorted({item for item in output.split("\0") if item})


def checkpoint_task(
    repo_value: str,
    task_id: str,
    attempt: int,
    note: str,
    evidence: list[str],
    attempt_failed: bool,
    parent_redirect: bool,
    review_rejected: bool,
) -> dict[str, Any]:
    repo = repository_root(repo_value)
    payload = read_manifest(repo, task_id)
    if payload.get("status") != "active":
        raise WorktreeError("task_not_active", f"Task status is {payload.get('status')}.")
    checkpoints = payload.setdefault("checkpoints", [])
    expected_attempt = len(checkpoints) + 1
    if attempt != expected_attempt or attempt > MAX_FAILURES:
        raise WorktreeError(
            "invalid_attempt",
            f"Expected attempt {expected_attempt}; attempts cannot exceed {MAX_FAILURES}.",
        )
    worktree = Path(payload["worktree"]).resolve()
    if not worktree.is_dir() or not is_within(worktree, repo / WORKTREE_DIRECTORY):
        raise WorktreeError("worktree_missing", "Managed task worktree is missing or outside the repository.")
    run_git(worktree, "add", "-A")
    changed = staged_paths(worktree)
    scopes = payload.get("write_scopes") or ["."]
    violations = [path for path in changed if not path_in_scope(path, scopes)]
    commit_message = f"CustomAgent {task_id} attempt {attempt}"
    run_git(
        worktree,
        "-c",
        "user.name=Codex CustomAgent",
        "-c",
        "user.email=codex-custom-agent@localhost",
        "commit",
        "--allow-empty",
        "-m",
        commit_message,
    )
    commit = run_git(worktree, "rev-parse", "HEAD").stdout.strip()
    counters = payload["counters"]
    counters["attempt_failures"] += int(attempt_failed)
    counters["parent_redirects"] += int(parent_redirect)
    counters["review_rejections"] += int(review_rejected)
    checkpoints.append(
        {
            "attempt": attempt,
            "commit": commit,
            "changed_files": changed,
            "scope_violations": violations,
            "note": note[:4000],
            "evidence": [item[:2000] for item in evidence[:20]],
            "recorded_at": datetime.now().isoformat(timespec="seconds"),
        }
    )
    exhausted = any(value >= MAX_FAILURES for value in counters.values())
    if exhausted:
        payload["status"] = "handoff_required"
    atomic_json(manifest_path(repo, task_id), payload)
    return {
        "status": payload["status"],
        "task_id": task_id,
        "attempt": attempt,
        "commit": commit,
        "changed_files": changed,
        "scope_violations": violations,
        "counters": counters,
        "parent_must_take_over": exhausted,
    }


def final_changed_paths(worktree: Path, base: str) -> list[str]:
    output = run_git(worktree, "diff", "--name-only", "-z", f"{base}..HEAD").stdout
    return sorted({item for item in output.split("\0") if item})


def integrate_task(repo_value: str, task_id: str, evidence: list[str]) -> dict[str, Any]:
    repo = repository_root(repo_value)
    payload = read_manifest(repo, task_id)
    if payload.get("status") != "active":
        raise WorktreeError("task_not_active", f"Task status is {payload.get('status')}.")
    if not payload.get("checkpoints"):
        raise WorktreeError("checkpoint_missing", "At least one checkpoint is required before integration.")
    worktree = Path(payload["worktree"]).resolve()
    require_clean(worktree, "Task worktree")
    require_clean(repo, "Primary worktree")
    current = run_git(repo, "rev-parse", "HEAD").stdout.strip()
    if current != payload["base_commit"]:
        raise WorktreeError(
            "base_changed",
            "Primary HEAD changed after task start; refusing automatic integration.",
            {"expected": payload["base_commit"], "actual": current},
        )
    changed = final_changed_paths(worktree, payload["base_commit"])
    violations = [path for path in changed if not path_in_scope(path, payload["write_scopes"])]
    if violations:
        raise WorktreeError(
            "scope_violation",
            "Final task tree contains changes outside the approved write scope.",
            {"paths": violations},
        )
    tree = run_git(worktree, "rev-parse", "HEAD^{tree}").stdout.strip()
    env = dict(os.environ)
    env.setdefault("GIT_AUTHOR_NAME", "Codex CustomAgent")
    env.setdefault("GIT_AUTHOR_EMAIL", "codex-custom-agent@localhost")
    env.setdefault("GIT_COMMITTER_NAME", "Codex CustomAgent")
    env.setdefault("GIT_COMMITTER_EMAIL", "codex-custom-agent@localhost")
    final_commit = run_git(
        repo,
        "commit-tree",
        tree,
        "-p",
        payload["base_commit"],
        "-m",
        f"Apply CustomAgent task {task_id}",
        env=env,
    ).stdout.strip()
    cherry_pick = run_git(repo, "cherry-pick", final_commit, check=False, env=env)
    if cherry_pick.returncode != 0:
        run_git(repo, "cherry-pick", "--abort", check=False)
        raise WorktreeError(
            "integration_failed",
            "Automatic cherry-pick failed; the isolated task was preserved.",
            {"stderr": cherry_pick.stderr[-1200:]},
        )
    integration_commit = run_git(repo, "rev-parse", "HEAD").stdout.strip()
    payload["status"] = "integrated"
    payload["integration_commit"] = integration_commit
    payload["accepted_evidence"] = [item[:2000] for item in evidence[:20]]
    payload["integrated_at"] = datetime.now().isoformat(timespec="seconds")
    atomic_json(manifest_path(repo, task_id), payload)
    return {
        "status": "integrated",
        "task_id": task_id,
        "integration_commit": integration_commit,
        "changed_files": changed,
        "post_integration_verification_required": True,
    }


def cleanup_task(repo: Path, payload: dict[str, Any], force: bool) -> None:
    worktree = Path(payload["worktree"]).resolve()
    expected_root = (repo / WORKTREE_DIRECTORY).resolve()
    if not is_within(worktree, expected_root):
        raise WorktreeError("unsafe_cleanup", "Refusing to remove a worktree outside the managed root.")
    args = ["worktree", "remove"]
    if force:
        args.append("--force")
    args.append(str(worktree))
    run_git(repo, *args)
    run_git(repo, "branch", "-D", payload["branch"])
    state = task_state_dir(repo, payload["task_id"])
    state_root = common_git_dir(repo) / "codex-custom-agent" / "tasks"
    if state.exists():
        if not is_within(state, state_root):
            raise WorktreeError("unsafe_cleanup", "Refusing to remove task state outside the managed root.")
        shutil.rmtree(state)
    for directory in (state_root, state_root.parent):
        if directory.is_dir() and not any(directory.iterdir()):
            directory.rmdir()
    remove_local_exclude_if_unused(repo)


def finalize_task(repo_value: str, task_id: str) -> dict[str, Any]:
    repo = repository_root(repo_value)
    payload = read_manifest(repo, task_id)
    if payload.get("status") != "integrated":
        raise WorktreeError("task_not_integrated", "Task must be integrated before final cleanup.")
    require_clean(repo, "Primary worktree")
    current = run_git(repo, "rev-parse", "HEAD").stdout.strip()
    if current != payload.get("integration_commit"):
        raise WorktreeError("integration_moved", "Primary HEAD moved after integration; cleanup was stopped.")
    result = {
        "status": "accepted",
        "task_id": task_id,
        "integration_commit": current,
        "backups_removed": True,
    }
    cleanup_task(repo, payload, force=False)
    return result


def abort_task(repo_value: str, task_id: str) -> dict[str, Any]:
    repo = repository_root(repo_value)
    payload = read_manifest(repo, task_id)
    if payload.get("status") not in {"active", "handoff_required"}:
        raise WorktreeError("task_not_abortable", f"Task status is {payload.get('status')}.")
    result = {
        "status": "rolled_back",
        "task_id": task_id,
        "base_commit": payload["base_commit"],
        "parent_must_take_over": payload.get("status") == "handoff_required",
        "backups_removed": True,
    }
    cleanup_task(repo, payload, force=True)
    return result


def rollback_integrated_task(repo_value: str, task_id: str) -> dict[str, Any]:
    repo = repository_root(repo_value)
    payload = read_manifest(repo, task_id)
    if payload.get("status") != "integrated":
        raise WorktreeError("task_not_integrated", "Only an integrated task can be reverted.")
    require_clean(repo, "Primary worktree")
    current = run_git(repo, "rev-parse", "HEAD").stdout.strip()
    if current != payload.get("integration_commit"):
        raise WorktreeError(
            "integration_moved",
            "Primary HEAD moved after integration; refusing to revert unrelated work.",
        )
    env = dict(os.environ)
    env.setdefault("GIT_AUTHOR_NAME", "Codex CustomAgent")
    env.setdefault("GIT_AUTHOR_EMAIL", "codex-custom-agent@localhost")
    env.setdefault("GIT_COMMITTER_NAME", "Codex CustomAgent")
    env.setdefault("GIT_COMMITTER_EMAIL", "codex-custom-agent@localhost")
    run_git(repo, "revert", "--no-edit", payload["integration_commit"], env=env)
    revert_commit = run_git(repo, "rev-parse", "HEAD").stdout.strip()
    result = {
        "status": "reverted",
        "task_id": task_id,
        "reverted_commit": payload["integration_commit"],
        "revert_commit": revert_commit,
        "backups_removed": True,
    }
    cleanup_task(repo, payload, force=False)
    return result


def task_status(repo_value: str, task_id: str) -> dict[str, Any]:
    repo = repository_root(repo_value)
    payload = read_manifest(repo, task_id)
    return {"status": payload.get("status", "unknown"), **payload}


def emit(payload: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(payload.get("status", "unknown"))
        for key, value in payload.items():
            if key != "status":
                print(f"{key}: {value}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("start", "checkpoint", "integrate", "finalize", "abort", "rollback-integrated", "status"))
    parser.add_argument("--repo", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--summary", default="")
    parser.add_argument("--acceptance", default="")
    parser.add_argument("--path", action="append", default=[])
    parser.add_argument("--attempt", type=int)
    parser.add_argument("--note", default="")
    parser.add_argument("--evidence", action="append", default=[])
    parser.add_argument("--attempt-failed", action="store_true")
    parser.add_argument("--parent-redirect", action="store_true")
    parser.add_argument("--review-rejected", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "start":
            payload = start_task(args.repo, args.task_id, args.summary, args.acceptance, args.path)
        elif args.command == "checkpoint":
            if args.attempt is None:
                raise WorktreeError("attempt_required", "checkpoint requires --attempt.")
            payload = checkpoint_task(
                args.repo,
                args.task_id,
                args.attempt,
                args.note,
                args.evidence,
                args.attempt_failed,
                args.parent_redirect,
                args.review_rejected,
            )
        elif args.command == "integrate":
            payload = integrate_task(args.repo, args.task_id, args.evidence)
        elif args.command == "finalize":
            payload = finalize_task(args.repo, args.task_id)
        elif args.command == "abort":
            payload = abort_task(args.repo, args.task_id)
        elif args.command == "rollback-integrated":
            payload = rollback_integrated_task(args.repo, args.task_id)
        else:
            payload = task_status(args.repo, args.task_id)
        emit(payload, args.json)
        return 0
    except WorktreeError as exc:
        emit({"status": exc.code, "message": str(exc), **exc.details}, args.json)
        return 2
    except Exception as exc:
        emit({"status": "failed", "message": f"{type(exc).__name__}: {exc}"}, args.json)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
