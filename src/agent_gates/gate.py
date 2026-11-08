"""Run the gate on one exact commit and keep a record keyed by its full SHA.

The commit is checked out into a fresh temporary worktree (detached HEAD), so nothing from a
developer's working copy (uncommitted edits, untracked files, build output) can make it pass.
Two built-in checks run first (spec format and decision-log numbering, on the commit's own
files); then the configured steps, from the configuration on main.

Steps run code from the commit under test. They get a minimal environment (PATH, locale, a
temporary HOME, plus variables the configuration lists), never the caller's full environment.
Every ref is snapshotted before the steps and compared after each one: a step that moves a
branch or a tag fails the gate. This limits accidents and simple tricks; it is not a sandbox
(see the README's limits).

Records live under `.git/agent-gates/gates/<sha>/<config-sha256>.json`. A record for one SHA
never counts for another, and a record made under one configuration is only accepted where
that configuration applies (`verify`).
"""

from __future__ import annotations

import datetime as dt
import os
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from agent_gates import config as config_mod
from agent_gates import decisions, specs
from agent_gates.config import Config
from agent_gates.git import Git
from agent_gates.state import State

OUTPUT_TAIL = 2_000
BASE_ENV = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TERM")


@dataclass(frozen=True)
class StepResult:
    name: str
    argv: list[str]
    exit_code: int | None
    timed_out: bool
    duration_s: float
    output_tail: str

    @property
    def passed(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


@dataclass(frozen=True)
class GateRecord:
    sha: str
    passed: bool
    started_at: str
    duration_s: float
    config_sha256: str
    config_from: str  # the commit the configuration was read from
    steps: list[StepResult]

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> GateRecord:
        steps = [StepResult(**s) for s in data["steps"]]
        return cls(**{**data, "steps": steps})


def _now() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _text(raw: bytes | str | None) -> str:
    if raw is None:
        return ""
    return raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw


def step_env(config: Config, home: Path, sha: str) -> dict[str, str]:
    env = {k: os.environ[k] for k in (*BASE_ENV, *config.env) if k in os.environ}
    env.update(HOME=str(home), AGENT_GATES_SHA=sha)
    return env


def _run_step(
    argv: tuple[str, ...], cwd: Path, timeout_s: float, env: dict[str, str]
) -> tuple[int | None, bool, str]:
    try:
        proc = subprocess.run(  # noqa: S603 - argv from the main branch's committed config
            list(argv),
            cwd=cwd,
            env=env,
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return None, True, (_text(exc.stdout) + _text(exc.stderr))[-OUTPUT_TAIL:]
    except OSError as exc:
        return 127, False, f"could not start {argv[0]!r}: {exc.strerror}"
    return proc.returncode, False, (_text(proc.stdout) + _text(proc.stderr))[-OUTPUT_TAIL:]


def _builtin(name: str, errors: list[str], seconds: float) -> StepResult:
    return StepResult(
        name, [], 1 if errors else 0, False, round(seconds, 3), "\n".join(errors)[-OUTPUT_TAIL:]
    )


def _builtin_checks(wt: Path, config: Config) -> list[StepResult]:
    t0 = time.monotonic()
    try:
        results = specs.check_dir(wt, config.specs_dir)
        spec_errors = [e for r in results for e in r.errors]
    except (config_mod.ConfigError, OSError, UnicodeDecodeError) as exc:
        spec_errors = [str(exc)]
    out = [_builtin("builtin:specs", spec_errors, time.monotonic() - t0)]
    t0 = time.monotonic()
    try:
        decision_errors = decisions.check(wt, config.decisions_dir)
    except (config_mod.ConfigError, OSError, UnicodeDecodeError) as exc:
        decision_errors = [str(exc)]
    out.append(_builtin("builtin:decisions", decision_errors, time.monotonic() - t0))
    return out


def run_gate(git: Git, state: State, config: Config, ref: str, config_from: str = "") -> GateRecord:
    """Gate the commit `ref` resolves to. `ref` must be exact (a SHA or `refs/heads/...`);
    callers resolve names a person typed with `Git.resolve_user` first."""
    sha = git.resolve(ref)
    started, t0 = _now(), time.monotonic()
    results: list[StepResult] = []
    with tempfile.TemporaryDirectory(prefix="agent-gates-") as tmp:
        wt, home = Path(tmp) / "wt", Path(tmp) / "home"
        home.mkdir()
        git.run("worktree", "add", "--detach", str(wt), sha)
        try:
            results.extend(_builtin_checks(wt, config))
            if all(r.passed for r in results):
                env = step_env(config, home, sha)
                before = git.refs()
                for step in config.steps:
                    s0 = time.monotonic()
                    code, timed_out, tail = _run_step(step.argv, wt, step.timeout_s, env)
                    after = git.refs()
                    if after != before:
                        moved = sorted(
                            k for k in before.keys() | after.keys() if before.get(k) != after.get(k)
                        )
                        code, tail = 1, f"the step changed git refs: {', '.join(moved)}\n" + tail
                    result = StepResult(
                        step.name,
                        list(step.argv),
                        code,
                        timed_out,
                        round(time.monotonic() - s0, 3),
                        tail[-OUTPUT_TAIL:],
                    )
                    results.append(result)
                    if not result.passed:
                        break  # fail fast: later steps would not change the verdict
        finally:
            git.run("worktree", "remove", "--force", str(wt), check=False)
            git.run("worktree", "prune", check=False)
    configured = [r for r in results if not r.name.startswith("builtin:")]
    passed = (
        bool(config.steps)
        and len(configured) == len(config.steps)
        and all(r.passed for r in results)
    )
    record = GateRecord(
        sha=sha,
        passed=passed,
        started_at=started,
        duration_s=round(time.monotonic() - t0, 3),
        config_sha256=config.sha256,
        config_from=config_from,
        steps=results,
    )
    with state.locked():
        state.write(f"gates/{sha}/{config.sha256}.json", record.to_json())
    return record


def records(git: Git, state: State, sha: str) -> list[GateRecord]:
    base = state.root / "gates" / git.resolve(sha)
    if not base.is_dir():
        return []
    out = [
        GateRecord.from_json(state.read(str(p.relative_to(state.root)), {}))
        for p in sorted(base.glob("*.json"))
    ]
    return sorted(out, key=lambda r: r.started_at)


def get_record(git: Git, state: State, sha: str, config: Config | None = None) -> GateRecord | None:
    """The latest record for this commit (under `config`, if given)."""
    rs = records(git, state, sha)
    if config is not None:
        rs = [r for r in rs if r.config_sha256 == config.sha256]
    return rs[-1] if rs else None


def accepted_configs(git: Git, current: Config, sha: str) -> dict[str, str]:
    """Configurations under which a record for `sha` counts: the one on main now, and the one
    committed in `sha` itself (the configuration in force from that commit on, which the queue
    gates a configuration change under too)."""
    out = {current.sha256: "the configuration on main"}
    own = config_mod.at_commit(git, sha)
    if own is not None:
        out.setdefault(own.sha256, "the configuration committed in this commit")
    return out


def verify(git: Git, state: State, config: Config, sha: str) -> tuple[bool, str]:
    """Did this exact commit pass the gate under a configuration that applies to it?"""
    sha = git.resolve(sha)
    rs = records(git, state, sha)
    if not rs:
        return False, f"{sha[:12]}: no gate record"
    accepted = accepted_configs(git, config, sha)
    latest = {r.config_sha256: r for r in rs if r.config_sha256 in accepted}  # sorted by time
    if not latest:
        return False, f"{sha[:12]}: gated under a different configuration; run the gate again"
    if not any(r.passed for r in latest.values()):
        return False, f"{sha[:12]}: gate failed"
    return True, f"{sha[:12]}: gate passed"
