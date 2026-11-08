"""A thin, typed wrapper around the git command line. Every call is argv-based (no shell).

Branches are always addressed by their full name (`refs/heads/...`). A short name like `main`
is ambiguous in git: a tag called `main` wins over the branch, so a tag could make an ungated
branch look gated. `resolve_user` (for refs typed by a person or an agent) tries the branch
first and only then anything else.
"""

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


def _decode(data: bytes) -> str:
    return data.decode("utf-8", "replace")


def heads(branch: str) -> str:
    """Full ref name of a branch."""
    return branch if branch.startswith("refs/heads/") else f"refs/heads/{branch}"


class Git:
    def __init__(self, repo: Path) -> None:
        self.repo = Path(repo)

    def _proc(self, args: tuple[str, ...], cwd: Path | None) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(  # noqa: S603 - argv list, git from PATH
            ["git", *args],  # noqa: S607
            cwd=cwd or self.repo,
            capture_output=True,
            check=False,
        )

    def run(self, *args: str, cwd: Path | None = None, check: bool = True) -> str:
        proc = self._proc(args, cwd)
        if check and proc.returncode != 0:
            msg = (_decode(proc.stderr) or _decode(proc.stdout)).strip().splitlines()
            raise GitError(f"git {' '.join(args)}: {msg[-1] if msg else proc.returncode}")
        return _decode(proc.stdout).strip()

    def attempt(self, *args: str, cwd: Path | None = None) -> tuple[bool, str]:
        """Run git; return (success, last line of its error output)."""
        proc = self._proc(args, cwd)
        lines = (_decode(proc.stderr) or _decode(proc.stdout)).strip().splitlines()
        return proc.returncode == 0, (lines[-1] if lines else "")

    def ok(self, *args: str, cwd: Path | None = None) -> bool:
        return self._proc(args, cwd).returncode == 0

    def toplevel(self) -> Path:
        return Path(self.run("rev-parse", "--show-toplevel"))

    def common_dir(self) -> Path:
        """The shared .git directory (the same for every worktree of the repository)."""
        return Path(self.run("rev-parse", "--path-format=absolute", "--git-common-dir"))

    def check_object_format(self) -> None:
        fmt = self.run("rev-parse", "--show-object-format", check=False) or "sha1"
        if fmt != "sha1":
            raise GitError(f"{fmt} repositories are not supported yet (only sha1)")

    def resolve(self, ref: str) -> str:
        """Full commit SHA for an exact ref (`refs/heads/main`, a SHA); raises if it is not a
        commit. Refs that could be read as git options are refused."""
        if not ref or ref.startswith("-") or any(c.isspace() or ord(c) < 32 for c in ref):
            raise GitError(f"not a valid ref: {ref!r}")
        sha = self.run(
            "rev-parse", "--verify", "--quiet", "--end-of-options", f"{ref}^{{commit}}", check=False
        )
        if len(sha) != SHA_LEN:
            self.check_object_format()
            raise GitError(f"not a commit: {ref!r}")
        return sha

    def resolve_user(self, ref: str) -> str:
        """A ref typed by a person or an agent: a branch of that name wins over a tag."""
        if ref and not ref.startswith("-") and self.branch_exists(ref):
            return self.resolve(heads(ref))
        return self.resolve(ref)

    def branch_exists(self, branch: str) -> bool:
        return self.ok("show-ref", "--verify", "--quiet", heads(branch))

    def show(self, commit: str, path: str) -> str | None:
        """File content at a commit, or None when the file does not exist there."""
        if not self.ok("cat-file", "-e", f"{commit}:{path}"):
            return None
        return self.run("show", f"{commit}:{path}")

    def parents(self, commit: str) -> list[str]:
        return self.run("rev-list", "--parents", "-n", "1", commit).split()[1:]

    def changed_files(self, a: str, b: str) -> set[str]:
        return set(filter(None, self.run("diff", "--name-only", a, b).splitlines()))

    def refs(self) -> dict[str, str]:
        """Every ref and the object it points to (a snapshot to detect changes)."""
        out = self.run("for-each-ref", "--format=%(refname) %(objectname)")
        return dict(line.split(" ", 1) for line in out.splitlines() if line)

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
        """No staged, unstaged or untracked (not ignored) changes."""
        return self.run("status", "--porcelain", cwd=worktree) == ""
