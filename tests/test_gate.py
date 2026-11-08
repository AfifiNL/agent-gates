from __future__ import annotations

import pytest

from agent_gates import config, gate
from agent_gates.git import GitError
from agent_gates.repo import Repo
from conftest import CONFIG, commit


def own(rec: gate.GateRecord) -> list[gate.StepResult]:
    """The configured steps (the built-in checks come first)."""
    return [s for s in rec.steps if not s.name.startswith("builtin:")]


def test_gate_passes_and_records_by_full_sha(repo: Repo) -> None:
    rec = gate.run_gate(repo.git, repo.state(), repo.config(), "HEAD")
    assert rec.passed
    assert len(rec.sha) == 40
    assert own(rec)[0].exit_code == 0
    stored = gate.get_record(repo.git, repo.state(), rec.sha)
    assert stored == rec
    assert gate.verify(repo.git, repo.state(), repo.config(), "main") == (
        True,
        f"{rec.sha[:12]}: gate passed",
    )


def test_gate_fails_on_a_failing_step(repo: Repo) -> None:
    commit(repo.root, {"calc.py": "def add(a, b):\n    return a - b\n"})
    rec = gate.run_gate(repo.git, repo.state(), repo.config(), "HEAD")
    assert not rec.passed
    assert own(rec)[0].exit_code != 0
    assert "FAIL" in own(rec)[0].output_tail


def test_uncommitted_fix_does_not_count(repo: Repo) -> None:
    commit(repo.root, {"calc.py": "def add(a, b):\n    return a - b\n"})
    (repo.root / "calc.py").write_text("def add(a, b):\n    return a + b\n")  # not committed
    assert not gate.run_gate(repo.git, repo.state(), repo.config(), "HEAD").passed


def test_a_record_never_counts_for_another_commit(repo: Repo) -> None:
    gate.run_gate(repo.git, repo.state(), repo.config(), "HEAD")
    commit(repo.root, {"notes.txt": "harmless"})
    ok, message = gate.verify(repo.git, repo.state(), repo.config(), "HEAD")
    assert not ok
    assert "no gate record" in message


def test_record_under_an_unrelated_config_does_not_verify(repo: Repo) -> None:
    other = config.parse('[[gate.steps]]\nname = "x"\nrun = "true"\n')
    sha = gate.run_gate(repo.git, repo.state(), other, "HEAD").sha
    ok, message = gate.verify(repo.git, repo.state(), repo.config(), sha)
    assert not ok
    assert "different configuration" in message


def test_older_commit_still_verifies_under_its_own_config(repo: Repo) -> None:
    first = gate.run_gate(repo.git, repo.state(), repo.config(), "HEAD").sha
    stricter = CONFIG + '\n[[gate.steps]]\nname = "lint"\nrun = "true"\n'
    commit(repo.root, {".agent-gates.toml": stricter})  # main now has a stricter gate
    # the first commit was gated under the configuration committed in it
    assert gate.verify(repo.git, repo.state(), repo.config(), first)[0]


def test_timeout_and_missing_program(repo: Repo) -> None:
    cfg = config.parse('[[gate.steps]]\nname = "slow"\nrun = ["sleep", "5"]\ntimeout_s = 0.2\n')
    rec = gate.run_gate(repo.git, repo.state(), cfg, "HEAD")
    assert own(rec)[0].timed_out
    assert not rec.passed
    cfg = config.parse('[[gate.steps]]\nname = "x"\nrun = "no-such-program-xyz"\n')
    rec = gate.run_gate(repo.git, repo.state(), cfg, "HEAD")
    assert own(rec)[0].exit_code == 127
    assert not rec.passed


def test_a_gate_without_steps_never_passes(repo: Repo) -> None:
    assert not gate.run_gate(repo.git, repo.state(), config.parse(""), "HEAD").passed


def test_fail_fast_stops_after_the_first_failure(repo: Repo) -> None:
    cfg = config.parse(
        '[[gate.steps]]\nname = "a"\nrun = "false"\n[[gate.steps]]\nname = "b"\nrun = "true"\n'
    )
    rec = gate.run_gate(repo.git, repo.state(), cfg, "HEAD")
    assert [s.name for s in rec.steps] == ["builtin:specs", "builtin:decisions", "a"]


def test_temporary_worktrees_are_cleaned_up(repo: Repo) -> None:
    gate.run_gate(repo.git, repo.state(), repo.config(), "HEAD")
    assert len(repo.git.worktrees()) == 1


@pytest.mark.parametrize("ref", ["--output=/tmp/x", "", "a b", "nope", "main\n"])
def test_bad_refs_are_rejected(repo: Repo, ref: str) -> None:
    with pytest.raises(GitError):
        repo.git.resolve(ref)
