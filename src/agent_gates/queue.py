"""The merge queue: main only ever moves to a commit that passed the gate.

For each queued lane, in order:
  1. build the candidate: main + lane as a merge commit, in a temporary worktree;
  2. run the gate on that exact candidate SHA (with the configuration on main);
  3. on pass, fast-forward main to the candidate with a compare-and-swap (if main moved in the
     meantime, nothing changes and the lane goes back to the front of the queue).
A lane that conflicts or fails is removed from the queue with the reason and the gate record.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_gates import config as config_mod
from agent_gates.gate import run_gate
from agent_gates.git import Git, GitError
from agent_gates.lanes import branch_for
from agent_gates.state import State

QUEUE = "queue.json"
HISTORY = "queue-history.json"
BOT_NAME = "agent-gates merge queue"
BOT_EMAIL = "merge-queue@agent-gates.invalid"


@dataclass(frozen=True)
class Outcome:
    lane: str
    result: str  # merged | rejected | skipped | requeued
    reason: str
    candidate: str | None = None
    main_before: str | None = None
    main_after: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "lane": self.lane,
            "result": self.result,
            "reason": self.reason,
            "candidate": self.candidate,
            "main_before": self.main_before,
            "main_after": self.main_after,
            "at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }


def add(git: Git, state: State, lane: str) -> list[str]:
    if not git.branch_exists(branch_for(lane)):
        raise GitError(f"no such lane: {lane!r}")
    with state.locked():
        queue: list[str] = state.read(QUEUE, [])
        if lane not in queue:
            queue.append(lane)
        state.write(QUEUE, queue)
        return queue


def status(state: State) -> dict[str, Any]:
    return {"queued": state.read(QUEUE, []), "history": state.read(HISTORY, [])[-20:]}


def _identity_args(git: Git) -> list[str]:
    if git.run("config", "user.email", check=False):
        return []
    return ["-c", f"user.name={BOT_NAME}", "-c", f"user.email={BOT_EMAIL}"]


def _build_candidate(git: Git, main_sha: str, lane_sha: str, lane: str) -> str | None:
    """Merge commit of main + lane, or None on a conflict."""
    with tempfile.TemporaryDirectory(prefix="agent-gates-merge-") as tmp:
        wt = Path(tmp) / "wt"
        git.run("worktree", "add", "--detach", str(wt), main_sha)
        try:
            merged = git.ok(
                *_identity_args(git),
                "merge",
                "--no-ff",
                "--no-edit",
                "-m",
                f"Merge {branch_for(lane)} (gated by agent-gates)",
                lane_sha,
                cwd=wt,
            )
            if not merged:
                git.run("merge", "--abort", cwd=wt, check=False)
                return None
            candidate = git.run("rev-parse", "HEAD", cwd=wt)
            # Keep the candidate reachable while it is gated (no garbage collection).
            git.run("update-ref", f"refs/agent-gates/candidates/{lane}", candidate)
            return candidate
        finally:
            git.run("worktree", "remove", "--force", str(wt), check=False)
            git.run("worktree", "prune", check=False)


def _advance_main(git: Git, main: str, old: str, new: str) -> bool:
    """Fast-forward main from `old` to `new`; False if main is no longer at `old`."""
    ref = f"refs/heads/{main}"
    holder = next((w for w in git.worktrees() if w.branch == ref), None)
    if holder is not None:
        if git.resolve(ref) != old:
            return False
        if not git.is_clean(holder.path):
            raise GitError(f"{main} is checked out with uncommitted changes at {holder.path}")
        if not git.ok("merge", "--ff-only", "--quiet", new, cwd=holder.path):
            return False
    elif not git.ok("update-ref", ref, new, old):
        return False
    return git.resolve(ref) == new


def _process(git: Git, state: State, main: str, lane: str) -> Outcome:
    branch = branch_for(lane)
    if not git.branch_exists(branch):
        return Outcome(lane, "rejected", f"branch {branch} does not exist")
    cfg = config_mod.load(git, main)
    main_sha = git.resolve(f"refs/heads/{main}")
    lane_sha = git.resolve(branch)
    if git.ok("merge-base", "--is-ancestor", lane_sha, main_sha):
        return Outcome(
            lane,
            "skipped",
            "nothing to merge: the lane is already in main",
            main_before=main_sha,
            main_after=main_sha,
        )
    candidate = _build_candidate(git, main_sha, lane_sha, lane)
    if candidate is None:
        return Outcome(
            lane,
            "rejected",
            f"merge conflict with {main}",
            main_before=main_sha,
            main_after=main_sha,
        )
    try:
        record = run_gate(git, state, cfg, candidate)
        if not record.passed:
            failed = next((s.name for s in record.steps if not s.passed), "no steps configured")
            return Outcome(
                lane, "rejected", f"gate failed at step {failed!r}", candidate, main_sha, main_sha
            )
        if not _advance_main(git, main, main_sha, candidate):
            return Outcome(
                lane,
                "requeued",
                f"{main} moved while gating; queued again",
                candidate,
                main_sha,
                git.resolve(f"refs/heads/{main}"),
            )
        return Outcome(
            lane, "merged", "gate passed on the candidate", candidate, main_sha, candidate
        )
    finally:
        git.run("update-ref", "-d", f"refs/agent-gates/candidates/{lane}", check=False)


def run(git: Git, state: State, main: str = "main", max_requeues: int = 3) -> list[Outcome]:
    lock = state.root / ".queue.lock"
    outcomes: list[Outcome] = []
    requeues = 0
    with lock.open("w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)  # one queue runner at a time
        except BlockingIOError:
            raise GitError("another merge-queue run is in progress") from None
        while True:
            with state.locked():
                queue: list[str] = state.read(QUEUE, [])
                if not queue:
                    break
                lane = queue[0]
            outcome = _process(git, state, main, lane)
            outcomes.append(outcome)
            with state.locked():
                queue = state.read(QUEUE, [])
                if queue and queue[0] == lane:
                    queue.pop(0)
                stop = False
                if outcome.result == "requeued":
                    # The lane stays first in line. After too many races with other pushes,
                    # stop and leave it queued for the next run instead of spinning.
                    queue.insert(0, lane)
                    requeues += 1
                    stop = requeues >= max_requeues
                state.write(QUEUE, queue)
                history: list[dict[str, Any]] = state.read(HISTORY, [])
                history.append(outcome.to_json())
                state.write(HISTORY, history)
            if stop:
                break
    return outcomes
