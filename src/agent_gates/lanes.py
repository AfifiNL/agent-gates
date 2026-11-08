"""Lanes: one branch and one worktree per unit of work, so parallel agents never share a
working copy. A lane always starts from the current main branch, and is removed with
`lane done` once main contains it."""

from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from agent_gates.config import Config
from agent_gates.gate import check
from agent_gates.git import Git, GitError, heads
from agent_gates.state import State

NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
LANES_FILE = "lanes.json"
QUEUE_FILE = "queue.json"


class LaneError(ValueError):
    pass


def branch_for(name: str) -> str:
    return f"lane/{name}"


def main_worktree(git: Git) -> Path:
    return git.worktrees()[0].path


def _valid(name: str) -> None:
    if not NAME.fullmatch(name) or ".." in name:
        raise LaneError(f"invalid lane name {name!r}: use lowercase letters, digits, '.', '_', '-'")


def new_lane(
    git: Git, state: State, config: Config, name: str, path: Path | None = None
) -> dict[str, Any]:
    _valid(name)
    branch = branch_for(name)
    if git.branch_exists(branch):
        raise LaneError(f"lane {name!r} exists already (branch {branch})")
    base = git.resolve(heads(config.main))
    root = main_worktree(git)
    wt = path or root.parent / f"{root.name}.lanes" / name
    if wt.exists():
        raise LaneError(f"path exists already: {wt}")
    git.run("worktree", "add", "-b", branch, str(wt), base)
    entry = {
        "name": name,
        "branch": branch,
        "path": str(wt),
        "base_sha": base,
        "created_at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    with state.locked():
        lanes = state.read(LANES_FILE, {})
        lanes[name] = entry
        state.write(LANES_FILE, lanes)
    return entry


def display(root: Path, path: Path | str) -> str:
    """A path shown relative to the main worktree (never to the process's current directory,
    which may be the worktree that was just removed)."""
    return os.path.relpath(str(path), str(root))


@contextlib.contextmanager
def queue_lock(state: State) -> Iterator[None]:
    """The merge-queue lock. Taken by `queue run` and by `lane done`, so a lane cannot be
    removed while the queue is gating or merging it. Never waits: a running queue is an error."""
    with (state.root / ".queue.lock").open("w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise LaneError("a merge-queue run is in progress; try again when it ends") from None
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _dirty_reason(git: Git, wt: Path) -> str | None:
    out = git.run("status", "--porcelain", "--ignored", "--untracked-files=all", cwd=wt)
    if not out:
        return None
    kinds = {"??": "untracked", "!!": "ignored"}
    found = sorted({kinds.get(line[:2], "uncommitted") for line in out.splitlines()})
    return " and ".join(found)


def done_lane(
    git: Git,
    state: State,
    config: Config,
    name: str,
    force: bool = False,
    *,
    queue_locked: bool = False,
) -> str:
    """Remove a lane's worktree and branch. Without `force`: only when main contains the lane,
    its worktree is on the lane branch and has no uncommitted, untracked or ignored files
    (`git worktree remove` deletes ignored files too), and the lane is not queued. Takes the
    queue lock unless the caller (the queue itself) already holds it."""
    if queue_locked:
        return _done(git, state, config, name, force)
    with queue_lock(state):
        return _done(git, state, config, name, force)


def _done(git: Git, state: State, config: Config, name: str, force: bool) -> str:
    _valid(name)
    root = main_worktree(git)
    g = Git(root)  # never depend on the caller's directory: it may be the lane being removed
    branch = heads(branch_for(name))
    record = state.read(LANES_FILE, {}).get(name)
    exists = g.branch_exists(branch)
    if not exists and record is None:
        raise LaneError(f"no such lane: {name!r}")
    checked_tip = g.resolve(branch) if exists else None
    worktrees = {w.path: w for w in g.worktrees()}
    targets: list[Path] = []
    if record is not None and Path(record["path"]) in worktrees:
        targets.append(Path(record["path"]))
    targets += [p for p, w in worktrees.items() if w.branch == branch and p not in targets]
    if root in targets:
        raise LaneError(f"lane {name!r} is checked out in the main worktree; switch it first")
    if not force:
        if checked_tip is not None and not g.ok(
            "merge-base", "--is-ancestor", checked_tip, heads(config.main)
        ):
            raise LaneError(
                f"lane {name!r} is not merged into {config.main}; use --force to discard it"
            )
        for wt in targets:
            if worktrees[wt].branch != branch:
                on = worktrees[wt].branch or "a detached HEAD"
                raise LaneError(
                    f"the worktree {display(root, wt)} of lane {name!r} is on {on}, not on "
                    f"{branch_for(name)}; switch it back or use --force"
                )
            if wt.exists():
                why = _dirty_reason(g, wt)
                if why:
                    raise LaneError(
                        f"lane {name!r} has {why} files in {display(root, wt)} (removing the "
                        "worktree would delete them); commit or move them, or use --force"
                    )
        if name in state.read(QUEUE_FILE, []):
            raise LaneError(f"lane {name!r} is queued; use --force to remove it from the queue")
    removed = []
    for wt in targets:
        shown = display(root, wt)
        if wt.exists():
            g.run("worktree", "remove", "--force", str(wt))  # checked above unless --force
        removed.append(f"worktree {shown}")
    g.run("worktree", "prune", check=False)
    if checked_tip is not None:
        # Compare-and-swap on the tip that was checked: a commit made since then keeps the branch.
        deleted, _ = g.attempt("update-ref", "-d", branch, checked_tip)
        if not deleted:
            raise LaneError(
                f"lane {name!r}: the branch moved after it was checked, so it was kept "
                f"({', '.join(removed) or 'nothing else'} removed); run `lane done` again"
            )
        removed.append(f"branch {branch_for(name)} (was {checked_tip[:12]})")
    with state.locked():
        lanes = state.read(LANES_FILE, {})
        lanes.pop(name, None)
        state.write(LANES_FILE, lanes)
        queue = state.read(QUEUE_FILE, [])
        if name in queue:
            state.write(QUEUE_FILE, [q for q in queue if q != name])
            removed.append("queue entry")
    return f"lane {name}: removed " + (", ".join(removed) if removed else "its record")


def list_lanes(git: Git, state: State, main: str, config: Config | None) -> list[dict[str, Any]]:
    """Recorded lanes with head, ahead/behind main, state and the gate state of the head
    (`None` when the gate configuration cannot be read)."""
    out = []
    main_ref = heads(main)
    root = main_worktree(git)
    for _name, entry in sorted(state.read(LANES_FILE, {}).items()):
        row = dict(entry)
        row["path"] = display(root, entry["path"])
        try:
            head = git.resolve(heads(entry["branch"]))
        except GitError:
            row.update(head=None, ahead=None, behind=None, status="branch deleted", gate=None)
            out.append(row)
            continue
        ahead = int(git.run("rev-list", "--count", f"{main_ref}..{head}"))
        behind = int(git.run("rev-list", "--count", f"{head}..{main_ref}"))
        if head == entry.get("base_sha"):
            lane_state = "no commits yet"
        elif ahead == 0:
            lane_state = "merged"
        else:
            lane_state = "open"
        row.update(
            head=head,
            ahead=ahead,
            behind=behind,
            status=lane_state,
            gate=check(git, state, config, head)[0] if config is not None else None,
        )
        out.append(row)
    return out
