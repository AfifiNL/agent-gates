"""Lanes: one branch and one worktree per unit of work, so parallel agents never share a
working copy. A lane always starts from the current main branch."""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Any

from agent_gates.config import Config
from agent_gates.gate import get_record
from agent_gates.git import Git, GitError
from agent_gates.state import State

NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")


class LaneError(ValueError):
    pass


def branch_for(name: str) -> str:
    return f"lane/{name}"


def main_worktree(git: Git) -> Path:
    return git.worktrees()[0].path


def new_lane(
    git: Git, state: State, config: Config, name: str, path: Path | None = None
) -> dict[str, Any]:
    if not NAME.fullmatch(name) or ".." in name:
        raise LaneError(f"invalid lane name {name!r}: use lowercase letters, digits, '.', '_', '-'")
    branch = branch_for(name)
    if git.branch_exists(branch):
        raise LaneError(f"lane {name!r} exists already (branch {branch})")
    base = git.resolve(f"refs/heads/{config.main}")
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
        lanes = state.read("lanes.json", {})
        lanes[name] = entry
        state.write("lanes.json", lanes)
    return entry


def list_lanes(git: Git, state: State, config: Config) -> list[dict[str, Any]]:
    out = []
    main = f"refs/heads/{config.main}"
    for _name, entry in sorted(state.read("lanes.json", {}).items()):
        row = dict(entry)
        try:
            head = git.resolve(entry["branch"])
        except GitError:
            row.update(head=None, status="branch deleted")
            out.append(row)
            continue
        record = get_record(git, state, head)
        merged = git.ok("merge-base", "--is-ancestor", head, main)
        ahead = int(git.run("rev-list", "--count", f"{main}..{head}"))
        row.update(
            head=head,
            ahead=ahead,
            status="merged" if merged else "open",
            gate=None if record is None else ("passed" if record.passed else "failed"),
        )
        out.append(row)
    return out
