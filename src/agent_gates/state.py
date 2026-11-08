"""Local state (lanes, queue, gate records) under the repository's shared .git directory.

It lives in `.git/agent-gates/`, so every worktree sees the same state and nothing is committed.
Writes are atomic (write to a temp file, then rename) and serialised with a file lock.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
from collections.abc import Iterator
from typing import Any

from agent_gates.git import Git


class State:
    def __init__(self, git: Git) -> None:
        self.root = git.common_dir() / "agent-gates"
        (self.root / "gates").mkdir(parents=True, exist_ok=True)

    @contextlib.contextmanager
    def locked(self) -> Iterator[None]:
        with (self.root / ".lock").open("w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def read(self, name: str, default: Any) -> Any:
        path = self.root / name
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))

    def write(self, name: str, data: Any) -> None:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
