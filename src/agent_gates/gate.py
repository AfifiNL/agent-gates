"""Run the gate on one exact commit and keep a record keyed by its full SHA.

The commit is checked out into a fresh temporary worktree (detached HEAD), so nothing from a
developer's working copy (uncommitted edits, untracked files, build output) can make it pass.
The steps come from the configuration on the main branch. The record is written under
`.git/agent-gates/gates/<sha>.json`; a record for one SHA never counts for another, so any new
commit, including a rebase that changes nothing visible, must pass again.
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

from agent_gates.config import Config
from agent_gates.git import Git
from agent_gates.state import State

OUTPUT_TAIL = 2_000


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
    steps: list[StepResult]

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> GateRecord:
        steps = [StepResult(**s) for s in data["steps"]]
        return cls(**{**data, "steps": steps})


def _now() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run_step(
    argv: tuple[str, ...], cwd: Path, timeout_s: float, sha: str
) -> tuple[int | None, bool, str]:
    env = {**os.environ, "AGENT_GATES_SHA": sha}
    try:
        proc = subprocess.run(  # noqa: S603 - argv from the main branch's committed config
            list(argv),
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raw = exc.stdout or b""
        out = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        return None, True, out[-OUTPUT_TAIL:]
    except OSError as exc:
        return 127, False, f"could not start {argv[0]!r}: {exc.strerror}"
    return proc.returncode, False, (proc.stdout + proc.stderr)[-OUTPUT_TAIL:]


def run_gate(git: Git, state: State, config: Config, ref: str) -> GateRecord:
    sha = git.resolve(ref)
    started, t0 = _now(), time.monotonic()
    results: list[StepResult] = []
    with tempfile.TemporaryDirectory(prefix="agent-gates-") as tmp:
        wt = Path(tmp) / "wt"
        git.run("worktree", "add", "--detach", str(wt), sha)
        try:
            if git.run("rev-parse", "HEAD", cwd=wt) != sha:  # pragma: no cover - defensive
                raise RuntimeError("worktree is not at the requested commit")
            for step in config.steps:
                s0 = time.monotonic()
                code, timed_out, tail = _run_step(step.argv, wt, step.timeout_s, sha)
                result = StepResult(
                    step.name,
                    list(step.argv),
                    code,
                    timed_out,
                    round(time.monotonic() - s0, 3),
                    tail,
                )
                results.append(result)
                if not result.passed:
                    break  # fail fast: later steps would not change the verdict
        finally:
            git.run("worktree", "remove", "--force", str(wt), check=False)
            git.run("worktree", "prune", check=False)
    passed = (
        bool(config.steps) and len(results) == len(config.steps) and all(r.passed for r in results)
    )
    record = GateRecord(
        sha=sha,
        passed=passed,
        started_at=started,
        duration_s=round(time.monotonic() - t0, 3),
        config_sha256=config.sha256,
        steps=results,
    )
    with state.locked():
        state.write(f"gates/{sha}.json", record.to_json())
    return record


def get_record(git: Git, state: State, ref: str) -> GateRecord | None:
    sha = git.resolve(ref)
    data = state.read(f"gates/{sha}.json", None)
    return GateRecord.from_json(data) if data else None


def verify(git: Git, state: State, config: Config, ref: str) -> tuple[bool, str]:
    """Did this exact commit pass the gate under the CURRENT configuration?"""
    sha = git.resolve(ref)
    record = get_record(git, state, sha)
    if record is None:
        return False, f"{sha[:12]}: no gate record"
    if record.config_sha256 != config.sha256:
        return False, f"{sha[:12]}: gated under a different configuration; run the gate again"
    if not record.passed:
        return False, f"{sha[:12]}: gate failed"
    return True, f"{sha[:12]}: gate passed"
