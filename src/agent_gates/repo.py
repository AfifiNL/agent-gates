"""One repository as the CLI and the MCP server see it."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agent_gates import config as config_mod
from agent_gates.git import Git
from agent_gates.state import State

EXAMPLE_SPEC = """\
# Example: what a spec looks like

## Problem

Describe the problem in a few sentences: who has it, and what happens today.

## Acceptance criteria

- [ ] A reviewer can check this criterion by running a command or reading one file.
- [ ] Every criterion is one line and can be ticked off on its own.

## Out of scope

- What this change will not do, so nobody builds it by accident.
"""

FIRST_DECISION = """\
# 0001. Record decisions in numbered files

- Status: accepted
- Date: {date}

## Context

Decisions made in chat or in pull-request comments get lost, and agents cannot read them.

## Decision

Every decision that changes how the code is built or run gets a numbered file in this folder.
Accepted records are not edited; a new record supersedes them.

## Consequences

The reasons behind the code stay next to it, and a check keeps the numbering intact.
"""


@dataclass
class Repo:
    git: Git
    main: str = "main"

    @classmethod
    def open(cls, path: Path, main: str = "main") -> Repo:
        git = Git(path)
        return cls(Git(git.toplevel()), main)

    @property
    def root(self) -> Path:
        return self.git.repo

    def state(self) -> State:
        return State(self.git)

    def config(self) -> config_mod.Config:
        return config_mod.load(self.git, self.main)

    def working_config(self) -> config_mod.Config:
        """The configuration in this working tree (for spec and decision paths only; the gate
        never uses it)."""
        path = self.root / config_mod.CONFIG_FILE
        if path.exists():
            return config_mod.parse(path.read_text(encoding="utf-8"))
        return config_mod.Config()


def init(root: Path, today: str) -> list[Path]:
    """Write a starter config, an example spec and the first decision; never overwrite."""
    files = {
        root / config_mod.CONFIG_FILE: config_mod.EXAMPLE,
        root / "docs" / "specs" / "001-example.md": EXAMPLE_SPEC,
        root
        / "docs"
        / "decisions"
        / "0001-record-decisions-in-numbered-files.md": FIRST_DECISION.format(date=today),
    }
    written = []
    for path, text in files.items():
        if path.exists():
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        written.append(path)
    return written
