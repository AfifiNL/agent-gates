"""Lanes: one branch and one worktree per unit of work, so parallel agents never share a
working copy. A lane always starts from the current main branch, and is removed with
`lane done` once main contains it."""

from __future__ import annotations

import datetime as dt
import os
import re
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


def _lane_worktree(git: Git, name: str) -> Path | None:
    ref = heads(branch_for(name))
    return next((w.path for w in git.worktrees() if w.branch == ref), None)


def done_lane(git: Git, state: State, config: Config, name: str, force: bool = False) -> str:
    """Remove a lane's worktree and branch. Without `force`, only when main contains the lane,
    its worktree has no uncommitted or untracked files, and it is not queued."""
    _valid(name)
    branch = heads(branch_for(name))
    exists = git.branch_exists(branch)
    wt = _lane_worktree(git, name) if exists else None
    if not force:
        if exists:
            tip = git.resolve(branch)
            if not git.ok("merge-base", "--is-ancestor", tip, heads(config.main)):
                raise LaneError(
                    f"lane {name!r} is not merged into {config.main}; use --force to discard it"
                )
        if wt is not None and wt.exists() and not git.is_clean(wt):
            raise LaneError(
                f"lane {name!r} has uncommitted or untracked files in {wt}; "
                "commit or remove them, or use --force"
            )
        if name in state.read(QUEUE_FILE, []):
            raise LaneError(f"lane {name!r} is queued; use --force to remove it from the queue")
    if not exists and name not in state.read(LANES_FILE, {}):
        raise LaneError(f"no such lane: {name!r}")
    # Run everything from the main worktree: the caller may be inside the lane being removed.
    root = main_worktree(git)
    g = Git(root)
    removed = []
    if wt is not None and wt.exists():
        g.run("worktree", "remove", *(["--force"] if force else []), str(wt))
        removed.append(f"worktree {os.path.relpath(wt)}")
    g.run("worktree", "prune", check=False)
    if exists:
        tip = g.resolve(branch)
        g.run("update-ref", "-d", branch, tip)  # compare-and-swap on the tip
        removed.append(f"branch {branch_for(name)} (was {tip[:12]})")
    with state.locked():
        lanes = state.read(LANES_FILE, {})
        lanes.pop(name, None)
        state.write(LANES_FILE, lanes)
        queue = state.read(QUEUE_FILE, [])
        if name in queue:
            state.write(QUEUE_FILE, [q for q in queue if q != name])
            removed.append("queue entry")
    return f"lane {name}: removed " + (", ".join(removed) if removed else "its record")


def list_lanes(git: Git, state: State, config: Config) -> list[dict[str, Any]]:
    """Recorded lanes with head, ahead/behind main, merge state and the gate state of the head."""
    out = []
    main = heads(config.main)
    for _name, entry in sorted(state.read(LANES_FILE, {}).items()):
        row = dict(entry)
        try:
            head = git.resolve(heads(entry["branch"]))
        except GitError:
            row.update(head=None, ahead=None, behind=None, status="branch deleted", gate=None)
            out.append(row)
            continue
        merged = git.ok("merge-base", "--is-ancestor", head, main)
        ahead = int(git.run("rev-list", "--count", f"{main}..{head}"))
        behind = int(git.run("rev-list", "--count", f"{head}..{main}"))
        row.update(
            head=head,
            ahead=ahead,
            behind=behind,
            status="merged" if merged else "open",
            gate=check(git, state, config, head)[0],
        )
        out.append(row)
    return out
