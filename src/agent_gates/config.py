"""`.agent-gates.toml`: what the gate runs and where specs and decisions live.

The gate configuration is read from the TARGET branch (normally `main`), never from the commit
being gated. Otherwise a lane could weaken its own gate by editing the file in the same change.
"""

from __future__ import annotations

import hashlib
import shlex
import tomllib
from dataclasses import dataclass, field

from agent_gates.git import Git

CONFIG_FILE = ".agent-gates.toml"

EXAMPLE = """\
# agent-gates configuration. The gate reads this file from the main branch, not from the
# commit under test, so a change cannot weaken its own gate.
main = "main"
specs_dir = "docs/specs"
decisions_dir = "docs/decisions"

[[gate.steps]]
name = "tests"
run = "python3 -m unittest -q"
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
    source: str = ""  # raw text, hashed into every gate record

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.source.encode()).hexdigest()


def parse(text: str) -> Config:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{CONFIG_FILE}: {exc}") from None
    steps: list[Step] = []
    raw_steps = data.get("gate", {}).get("steps", [])
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
    return Config(
        main=str(data.get("main", "main")),
        specs_dir=str(data.get("specs_dir", "docs/specs")),
        decisions_dir=str(data.get("decisions_dir", "docs/decisions")),
        steps=tuple(steps),
        source=text,
    )


def load(git: Git, main: str = "main") -> Config:
    """Read the configuration committed on the main branch. The branch name comes from the
    caller (`--main`), never from a file a lane could edit."""
    text = git.show(f"refs/heads/{main}", CONFIG_FILE)
    if text is None:
        raise ConfigError(f"{CONFIG_FILE} is not committed on {main!r}; run `agent-gates init`")
    cfg = parse(text)
    if cfg.main != main:
        raise ConfigError(f"{CONFIG_FILE} on {main!r} says main = {cfg.main!r}; they must match")
    return cfg
