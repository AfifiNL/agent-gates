"""A thin, typed wrapper around the git command line. Every call is argv-based (no shell)."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

SHA_LEN = 40


class GitError(RuntimeError):
    pass


@dataclass(frozen=True)
class Worktree:
    path: Path
    head: str
    branch: str | None  # "refs/heads/..." or None when detached


class Git:
    def __init__(self, repo: Path) -> None:
        self.repo = Path(repo)

    def run(self, *args: str, cwd: Path | None = None, check: bool = True) -> str:
        proc = subprocess.run(  # noqa: S603 - argv list, git from PATH
            ["git", *args],  # noqa: S607
            cwd=cwd or self.repo,
            capture_output=True,
            text=True,
            check=False,
        )
        if check and proc.returncode != 0:
            msg = (proc.stderr or proc.stdout).strip().splitlines()
            raise GitError(f"git {' '.join(args)}: {msg[-1] if msg else proc.returncode}")
        return proc.stdout.strip()

    def ok(self, *args: str, cwd: Path | None = None) -> bool:
        proc = subprocess.run(  # noqa: S603
            ["git", *args],  # noqa: S607
            cwd=cwd or self.repo,
            capture_output=True,
            text=True,
            check=False,
        )
        return proc.returncode == 0

    def toplevel(self) -> Path:
        return Path(self.run("rev-parse", "--show-toplevel"))

    def common_dir(self) -> Path:
        """The shared .git directory (the same for every worktree of the repository)."""
        return Path(self.run("rev-parse", "--path-format=absolute", "--git-common-dir"))

    def resolve(self, ref: str) -> str:
        """Full commit SHA for a ref; raises if it is not a commit."""
        if not ref or ref.startswith("-") or any(c.isspace() or ord(c) < 32 for c in ref):
            raise GitError(f"not a valid ref: {ref!r}")
        sha = self.run("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False)
        if len(sha) != SHA_LEN:
            raise GitError(f"not a commit: {ref!r}")
        return sha

    def branch_exists(self, branch: str) -> bool:
        return self.ok("show-ref", "--verify", "--quiet", f"refs/heads/{branch}")

    def show(self, ref: str, path: str) -> str | None:
        """File content at a ref, or None when the file does not exist there."""
        if not self.ok("cat-file", "-e", f"{ref}:{path}"):
            return None
        return self.run("show", f"{ref}:{path}")

    def worktrees(self) -> list[Worktree]:
        out: list[Worktree] = []
        path: Path | None = None
        head = ""
        branch: str | None = None
        for line in [*self.run("worktree", "list", "--porcelain").splitlines(), ""]:
            if line.startswith("worktree "):
                path, head, branch = Path(line[len("worktree ") :]), "", None
            elif line.startswith("HEAD "):
                head = line[len("HEAD ") :]
            elif line.startswith("branch "):
                branch = line[len("branch ") :]
            elif not line and path is not None:
                out.append(Worktree(path, head, branch))
                path = None
        return out

    def is_clean(self, worktree: Path) -> bool:
        return self.run("status", "--porcelain", "--untracked-files=no", cwd=worktree) == ""
