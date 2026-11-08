"""The merge queue: main only ever moves to a commit that passed the gate.

For each queued lane, in order:
  0. main's tip must itself have a passing gate record (or be the repository's first commit);
     otherwise the run stops: someone moved main outside the queue;
  1. build the candidate: main + lane as a merge commit, in a temporary worktree;
  2. a lane that changes the gate configuration is rejected, unless the operator allowed that
     lane explicitly on the command line; then the new configuration must be valid and the
     candidate must pass under the old AND the new configuration;
  3. run the gate on that exact candidate SHA;
  4. on pass, fast-forward main to the candidate with a compare-and-swap (if main moved in the
     meantime, nothing changes and the lane stays first in the queue).
A lane that conflicts, fails, or errors is removed from the queue with the reason. Problems on
the operator's side (main not gated, main's worktree not clean) stop the run and keep the lane.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import re
import tempfile
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_gates import config as config_mod
from agent_gates.gate import run_gate, verify
from agent_gates.git import Git, GitError, heads
from agent_gates.lanes import QUEUE_FILE, LaneError, branch_for, done_lane, queue_lock
from agent_gates.state import State

QUEUE = QUEUE_FILE
HISTORY = "queue-history.json"
BOT_NAME = "agent-gates merge queue"
BOT_EMAIL = "merge-queue@agent-gates.invalid"


class QueueBlocked(GitError):
    """A problem on the operator's side: the run stops and the lane stays queued."""


@dataclass(frozen=True)
class Outcome:
    lane: str
    result: str  # merged | rejected | skipped | requeued
    reason: str
    candidate: str | None = None
    main_before: str | None = None
    main_after: str | None = None
    cleanup: str | None = None  # what `--cleanup` did (or why it kept the lane)

    def to_json(self) -> dict[str, Any]:
        return {
            "lane": self.lane,
            "result": self.result,
            "reason": self.reason,
            "candidate": self.candidate,
            "main_before": self.main_before,
            "main_after": self.main_after,
            "cleanup": self.cleanup,
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


def _merge_args(git: Git) -> list[str]:
    # Candidate merges are made by the tool, not by a person: they are never signed (a missing
    # key or agent would otherwise fail every merge), and get a bot identity when none is set.
    args = ["-c", "commit.gpgsign=false"]
    if not git.run("config", "user.email", check=False):
        args += ["-c", f"user.name={BOT_NAME}", "-c", f"user.email={BOT_EMAIL}"]
    return args


def _build_candidate(git: Git, main_sha: str, lane_sha: str, lane: str) -> tuple[str | None, str]:
    """(merge commit of main + lane, "") or (None, reason)."""
    with tempfile.TemporaryDirectory(prefix="agent-gates-merge-") as tmp:
        wt = Path(tmp) / "wt"
        git.run("worktree", "add", "--detach", str(wt), main_sha)
        try:
            merged, error = git.attempt(
                *_merge_args(git),
                "merge",
                "--no-ff",
                "--no-edit",
                "-m",
                f"Merge {branch_for(lane)} (gated by agent-gates)",
                lane_sha,
                cwd=wt,
            )
            if not merged:
                conflicted = git.run("diff", "--name-only", "--diff-filter=U", cwd=wt, check=False)
                git.run("merge", "--abort", cwd=wt, check=False)
                if conflicted:
                    files = ", ".join(conflicted.splitlines()[:5])
                    return None, f"merge conflict in {files}"
                return None, f"merge failed: {error}"
            candidate = git.run("rev-parse", "HEAD", cwd=wt)
            # Keep the candidate reachable while it is gated (no garbage collection).
            git.run("update-ref", f"refs/agent-gates/candidates/{lane}", candidate)
            return candidate, ""
        finally:
            git.run("worktree", "remove", "--force", str(wt), check=False)
            git.run("worktree", "prune", check=False)


def _advance_main(git: Git, main: str, old: str, new: str) -> bool:
    """Fast-forward main from `old` to `new`. False only if main is no longer at `old`."""
    ref = heads(main)
    holder = next((w for w in git.worktrees() if w.branch == ref), None)
    if holder is not None:
        if git.resolve(ref) != old:
            return False
        if not git.is_clean(holder.path):
            raise QueueBlocked(
                f"{main} is checked out at {holder.path} with uncommitted or untracked files; "
                "commit, stash or remove them and run the queue again"
            )
        done, error = git.attempt("merge", "--ff-only", "--quiet", new, cwd=holder.path)
        if not done:
            if git.resolve(ref) != old:
                return False
            raise QueueBlocked(f"could not fast-forward {main}: {error}")
    elif not git.ok("update-ref", ref, new, old):
        return False
    return git.resolve(ref) == new


def _check_main_is_gated(git: Git, state: State, cfg: config_mod.Config, main_sha: str) -> None:
    if not git.parents(main_sha):
        return  # the repository's first commit
    ok, message = verify(git, state, cfg, main_sha)
    if not ok:
        raise QueueBlocked(
            f"main's tip is not gated ({message}); run `agent-gates gate run main` and check "
            "why main moved outside the queue"
        )


def _process(
    git: Git, state: State, main: str, lane: str, allow_config_change: Collection[str]
) -> Outcome:
    branch = heads(branch_for(lane))
    if not git.branch_exists(branch):
        return Outcome(lane, "rejected", f"branch {branch} does not exist")
    cfg = config_mod.load(git, main)
    main_sha = git.resolve(heads(main))
    _check_main_is_gated(git, state, cfg, main_sha)
    lane_sha = git.resolve(branch)
    if git.ok("merge-base", "--is-ancestor", lane_sha, main_sha):
        return Outcome(
            lane,
            "skipped",
            "nothing to merge: the lane is already in main",
            main_before=main_sha,
            main_after=main_sha,
        )
    candidate, why = _build_candidate(git, main_sha, lane_sha, lane)
    if candidate is None:
        return Outcome(lane, "rejected", why, main_before=main_sha, main_after=main_sha)
    try:
        configs = [(cfg, main_sha)]
        if config_mod.CONFIG_FILE in git.changed_files(main_sha, candidate):
            if lane not in allow_config_change:
                return Outcome(
                    lane,
                    "rejected",
                    f"the lane changes {config_mod.CONFIG_FILE}; an operator must allow it with "
                    f"`queue run --allow-config-change {lane}`",
                    candidate,
                    main_sha,
                    main_sha,
                )
            text = git.show(candidate, config_mod.CONFIG_FILE)
            try:
                new_cfg = config_mod.parse(text) if text is not None else None
            except config_mod.ConfigError as exc:
                return Outcome(
                    lane,
                    "rejected",
                    f"new configuration is invalid: {exc}",
                    candidate,
                    main_sha,
                    main_sha,
                )
            if new_cfg is None or not new_cfg.steps or new_cfg.main != main:
                return Outcome(
                    lane,
                    "rejected",
                    "new configuration must exist, keep at least one step and the same main",
                    candidate,
                    main_sha,
                    main_sha,
                )
            configs.append((new_cfg, candidate))
        for c, source in configs:
            record = run_gate(git, state, c, candidate, config_from=source)
            if not record.passed:
                failed = next((s.name for s in record.steps if not s.passed), "no steps configured")
                which = "new" if source == candidate else "current"
                return Outcome(
                    lane,
                    "rejected",
                    f"gate failed at step {failed!r} ({which} configuration)",
                    candidate,
                    main_sha,
                    main_sha,
                )
        if not _advance_main(git, main, main_sha, candidate):
            return Outcome(
                lane,
                "requeued",
                f"{main} moved while gating; queued again",
                candidate,
                main_sha,
                git.resolve(heads(main)),
            )
        return Outcome(
            lane, "merged", "gate passed on the candidate", candidate, main_sha, candidate
        )
    finally:
        git.run("update-ref", "-d", f"refs/agent-gates/candidates/{lane}", check=False)


_TEMP = re.compile(re.escape(tempfile.gettempdir()) + r"/agent-gates-[^\s'\":]*")


def _scrub(text: str, git: Git) -> str:
    """Keep stored reasons free of machine-specific paths: temporary worktrees and the
    repository's own location are replaced by placeholders."""
    text = _TEMP.sub("<temporary worktree>", text)
    root = str(git.repo)
    return text.replace(root, "<repo>").replace(str(Path(root).parent), "<repo>/..")


def run(
    git: Git,
    state: State,
    main: str = "main",
    max_requeues: int = 3,
    allow_config_change: Collection[str] = (),
    cleanup: bool = False,
) -> list[Outcome]:
    """Process the queue. With `cleanup`, a merged lane's worktree and branch are removed
    afterwards (only if that is safe: see `lanes.done_lane`; otherwise the lane is kept). The
    outcome is written to the history before the clean-up starts, and a failed clean-up is
    recorded on that entry instead of stopping the run."""
    outcomes: list[Outcome] = []
    requeues = 0
    with contextlib.ExitStack() as stack:
        try:
            stack.enter_context(queue_lock(state))  # one queue runner at a time
        except LaneError as exc:
            raise QueueBlocked(str(exc)) from None
        while True:
            with state.locked():
                queue: list[str] = state.read(QUEUE, [])
                if not queue:
                    break
                lane = queue[0]
            try:
                outcome = _process(git, state, main, lane, allow_config_change)
            except QueueBlocked:
                raise  # operator-side: stop, keep the lane queued
            except Exception as exc:  # one bad lane must not wedge the queue
                outcome = Outcome(lane, "rejected", f"error: {type(exc).__name__}: {exc}")
            outcome = dataclasses.replace(outcome, reason=_scrub(outcome.reason, git))
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
                entry = len(history) - 1
                state.write(HISTORY, history)
            if cleanup and outcome.result == "merged":
                try:
                    note = done_lane(
                        git, state, config_mod.load(git, main), lane, queue_locked=True
                    )
                except Exception as exc:  # clean-up never undoes or hides a merge
                    note = f"lane kept: {exc}"
                note = _scrub(note, git)
                outcome = dataclasses.replace(outcome, cleanup=note)
                with state.locked():
                    history = state.read(HISTORY, [])
                    if entry < len(history) and history[entry]["lane"] == lane:
                        history[entry]["cleanup"] = note
                        state.write(HISTORY, history)
            outcomes.append(outcome)
            if stop:
                break
    return outcomes
