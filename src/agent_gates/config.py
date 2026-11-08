"""`.agent-gates.toml`: what the gate runs and where specs and decisions live.

The gate configuration is read from the main branch, never from the commit being gated.
Otherwise a lane could weaken its own gate by editing the file in the same change. A lane that
changes the file is refused by the merge queue unless an operator allows it explicitly.
"""

from __future__ import annotations

import hashlib
import re
import shlex
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from agent_gates.git import Git, heads

CONFIG_FILE = ".agent-gates.toml"
ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")

EXAMPLE = """\
# agent-gates configuration. The gate reads this file from the main branch, not from the
# commit under test, so a change cannot weaken its own gate.
main = "main"
specs_dir = "docs/specs"
decisions_dir = "docs/decisions"

# Gate steps run in a clean checkout of the exact commit, without a shell, with a minimal
# environment (PATH, locale, a temporary HOME). List extra variables a step needs in `env`.
[gate]
env = []

[[gate.steps]]
name = "tests"
# -I: Python ignores the checkout's directory when it looks up `unittest` itself, so a file in
# the change cannot replace the test runner. Discovery still imports the tests from the checkout.
run = "python3 -I -m unittest discover -q"
timeout_s = 600
"""


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Step:
    name: str
    argv: tuple[str, ...]
    timeout_s: float = 600.0


@dataclass(frozen=True)
class Config:
    main: str = "main"
    specs_dir: str = "docs/specs"
    decisions_dir: str = "docs/decisions"
    steps: tuple[Step, ...] = field(default_factory=tuple)
    env: tuple[str, ...] = ()
    source: str = ""  # raw text, hashed into every gate record

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.source.encode()).hexdigest()


def safe_relative_dir(value: object, key: str) -> str:
    """A directory inside the repository: relative, no `..`, no absolute path."""
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{key} must be a non-empty relative path")
    p = PurePosixPath(value)
    if p.is_absolute() or "\\" in value or ".." in p.parts or value.startswith("~"):
        raise ConfigError(f"{key} must be a relative path inside the repository: {value!r}")
    return str(p)


def inside(root: Path, rel: str) -> Path:
    """Resolve `rel` under `root`, refusing anything that ends up outside (symlinks too)."""
    base = root.resolve()
    target = (base / rel).resolve()
    if not target.is_relative_to(base):
        raise ConfigError(f"{rel!r} points outside the repository")
    return target


def parse(text: str) -> Config:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{CONFIG_FILE}: {exc}") from None
    gate = data.get("gate", {})
    if not isinstance(gate, dict):
        raise ConfigError("[gate] must be a table")
    steps: list[Step] = []
    raw_steps = gate.get("steps", [])
    if not isinstance(raw_steps, list):
        raise ConfigError("[[gate.steps]] must be an array of tables")
    for i, s in enumerate(raw_steps, 1):
        if not isinstance(s, dict) or not s.get("name") or not s.get("run"):
            raise ConfigError(f"gate step {i}: needs `name` and `run`")
        run = s["run"]
        argv = tuple(shlex.split(run)) if isinstance(run, str) else tuple(str(a) for a in run)
        if not argv:
            raise ConfigError(f"gate step {s['name']!r}: empty `run`")
        timeout = float(s.get("timeout_s", 600))
        if timeout <= 0:
            raise ConfigError(f"gate step {s['name']!r}: timeout_s must be positive")
        steps.append(Step(str(s["name"]), argv, timeout))
    names = [s.name for s in steps]
    if len(names) != len(set(names)):
        raise ConfigError("gate step names must be unique")
    env = gate.get("env", [])
    if not isinstance(env, list) or not all(isinstance(e, str) and ENV_NAME.match(e) for e in env):
        raise ConfigError("[gate] env must be a list of variable names (A-Z, 0-9, _)")
    main = data.get("main", "main")
    if not isinstance(main, str) or not main or main.startswith(("-", "refs/")):
        raise ConfigError("main must be a branch name such as 'main'")
    return Config(
        main=main,
        specs_dir=safe_relative_dir(data.get("specs_dir", "docs/specs"), "specs_dir"),
        decisions_dir=safe_relative_dir(
            data.get("decisions_dir", "docs/decisions"), "decisions_dir"
        ),
        steps=tuple(steps),
        env=tuple(env),
        source=text,
    )


def load(git: Git, main: str = "main") -> Config:
    """Read the configuration committed on the main branch. The branch name comes from the
    caller (`--main`), never from a file a lane could edit."""
    text = git.show(heads(main), CONFIG_FILE)
    if text is None:
        raise ConfigError(f"{CONFIG_FILE} is not committed on {main!r}; run `agent-gates init`")
    cfg = parse(text)
    if cfg.main != main:
        raise ConfigError(f"{CONFIG_FILE} on {main!r} says main = {cfg.main!r}; they must match")
    return cfg


def at_commit(git: Git, commit: str) -> Config | None:
    """The configuration committed in `commit` (None if absent or invalid)."""
    text = git.show(commit, CONFIG_FILE)
    if text is None:
        return None
    try:
        return parse(text)
    except ConfigError:
        return None
