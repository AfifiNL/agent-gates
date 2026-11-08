"""Tests for the hardening after the first review: config changes, untrusted steps, refs,
output decoding, path checks, built-in checks, merge errors and the compare-and-swap path."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from agent_gates import config, gate, lanes
from agent_gates import queue as q
from agent_gates.cli import main as cli
from agent_gates.git import GitError
from agent_gates.repo import Repo
from conftest import CALC, CONFIG, PY, commit, git

WEAK = 'main = "main"\n[[gate.steps]]\nname = "tests"\nrun = "true"\n'


def lane(repo: Repo, name: str, files: dict[str, str]) -> Path:
    path = Path(lanes.new_lane(repo.git, repo.state(), repo.config(), name)["path"])
    commit(path, files, f"work on {name}")
    return path


def queue(repo: Repo, *names: str, allow: tuple[str, ...] = ()) -> list[q.Outcome]:
    for n in names:
        q.add(repo.git, repo.state(), n)
    return q.run(repo.git, repo.state(), allow_config_change=allow)


# --- configuration changes -------------------------------------------------------------------


def test_allowed_config_change_must_pass_old_and_new_and_then_verifies(repo: Repo) -> None:
    stricter = CONFIG + '\n[[gate.steps]]\nname = "lint"\nrun = "true"\n'
    lane(repo, "stricter", {".agent-gates.toml": stricter})
    (o,) = queue(repo, "stricter", allow=("stricter",))
    assert o.result == "merged"
    assert [s.name for s in repo.config().steps] == ["tests", "lint"]
    recs = gate.records(repo.git, repo.state(), o.candidate or "")
    assert {r.config_from for r in recs} == {o.main_before, o.candidate}
    # after a legitimate configuration change, main still verifies
    assert gate.verify(repo.git, repo.state(), repo.config(), "refs/heads/main")[0]
    # and the next lane is gated under the new configuration
    lane(repo, "next", {"n.txt": "x"})
    (o2,) = queue(repo, "next")
    assert o2.result == "merged"


def test_allowed_config_change_still_needs_the_old_gate(repo: Repo) -> None:
    lane(repo, "weaker", {".agent-gates.toml": WEAK, "calc.py": "def add(a, b):\n    return 0\n"})
    (o,) = queue(repo, "weaker", allow=("weaker",))
    assert o.result == "rejected"
    assert "current configuration" in o.reason


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ('main = "main"\n', "at least one step"),
        ("main = = 1\n", "invalid"),
        (CONFIG.replace('main = "main"', 'main = "trunk"'), "same main"),
    ],
)
def test_allowed_config_change_must_be_valid(repo: Repo, text: str, reason: str) -> None:
    lane(repo, "cfg", {".agent-gates.toml": text})
    (o,) = queue(repo, "cfg", allow=("cfg",))
    assert o.result == "rejected"
    assert reason in o.reason


def test_cli_flag_allows_a_config_change(repo: Repo, capsys: pytest.CaptureFixture[str]) -> None:
    stricter = CONFIG + '\n[[gate.steps]]\nname = "lint"\nrun = "true"\n'
    lane(repo, "cfg", {".agent-gates.toml": stricter})
    assert cli(["-C", str(repo.root), "queue", "add", "cfg"]) == 0
    assert cli(["-C", str(repo.root), "queue", "run", "--allow-config-change", "cfg"]) == 0
    assert "merged" in capsys.readouterr().out


# --- untrusted steps -------------------------------------------------------------------------


def test_step_that_moves_a_ref_fails_and_main_stays(repo: Repo) -> None:
    sneaky = (
        "import subprocess, unittest\n\n\nclass T(unittest.TestCase):\n"
        "    def test_x(self):\n"
        "        subprocess.run(['git', 'update-ref', 'refs/heads/main', 'HEAD'], check=True)\n"
    )
    lane(repo, "sneaky", {"test_sneaky.py": sneaky, "calc.py": "def add(a, b):\n    return 0\n"})
    before = repo.git.resolve("refs/heads/main")
    (o,) = queue(repo, "sneaky")
    assert o.result == "rejected"
    rec = gate.get_record(repo.git, repo.state(), o.candidate or "")
    assert rec is not None
    assert "changed git refs: refs/heads/main" in rec.steps[-1].output_tail
    # the step did move main; the queue does not build on it (it is not gated)
    assert repo.git.resolve("refs/heads/main") != before
    lane(repo, "after", {"a.txt": "a"})
    with pytest.raises(q.QueueBlocked, match="not gated"):
        queue(repo, "after")


def test_steps_get_a_minimal_environment(repo: Repo, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AG_TEST_SECRET", "s3cr3t-marker")
    real_home = str(Path.home())
    probe = (
        "import os, sys; "
        f"sys.exit(1 if 'AG_TEST_SECRET' in os.environ or os.environ['HOME'] == '{real_home}' "
        "else 0)"
    )
    cfg = config.parse(f'[[gate.steps]]\nname = "env"\nrun = ["python3", "-c", "{probe}"]\n')
    assert gate.run_gate(repo.git, repo.state(), cfg, "HEAD").passed
    env = gate.step_env(cfg, Path("/x"), "abc")
    assert "AG_TEST_SECRET" not in env
    assert env["HOME"] == "/x"
    allowed = config.parse(
        '[gate]\nenv = ["AG_TEST_SECRET"]\n[[gate.steps]]\nname="a"\nrun="true"\n'
    )
    assert gate.step_env(allowed, Path("/x"), "abc")["AG_TEST_SECRET"] == "s3cr3t-marker"


def test_shadowed_test_runner_is_not_used(repo: Repo) -> None:
    # A change that ships its own `unittest` package (exiting 0) does not replace the runner,
    # because the starter command runs Python with -I.
    lane(
        repo,
        "shadow",
        {
            "unittest/__init__.py": "",
            "unittest/__main__.py": "raise SystemExit(0)\n",
            "calc.py": "def add(a, b):\n    return 0\n",
        },
    )
    assert '"-I"' in PY
    (o,) = queue(repo, "shadow")
    assert o.result == "rejected"


# --- refs: branches win over tags ------------------------------------------------------------


def test_tag_named_main_cannot_make_an_ungated_branch_look_gated(repo: Repo) -> None:
    gated = gate.run_gate(repo.git, repo.state(), repo.config(), "HEAD").sha
    git(repo.root, "tag", "main", gated)
    commit(repo.root, {"x.txt": "ungated"})
    assert cli(["-C", str(repo.root), "gate", "verify", "main"]) == 1


def test_tag_named_like_a_lane_is_ignored(repo: Repo) -> None:
    lane(repo, "good", {"mul.py": "x = 1\n"})
    git(repo.root, "tag", "lane/good", "refs/heads/main")  # a tag that is already "in main"
    (o,) = queue(repo, "good")
    assert o.result == "merged"
    assert (repo.root / "mul.py").exists()


def test_sha256_repositories_get_a_clear_error(tmp_path: Path) -> None:
    r = subprocess.run(
        ["git", "init", "-q", "--object-format=sha256", str(tmp_path / "s")],
        capture_output=True,
        check=False,
    )
    if r.returncode != 0:
        pytest.skip("git without sha256 support")
    with pytest.raises(GitError, match="sha256 repositories are not supported"):
        Repo.open(tmp_path / "s")


# --- output decoding and errors --------------------------------------------------------------


def test_binary_output_does_not_wedge_the_queue(repo: Repo) -> None:
    noisy = (
        "import sys, unittest\n\n\nclass T(unittest.TestCase):\n    def test_x(self):\n"
        "        sys.stdout.buffer.write(b'\\xff\\xfe')\n        sys.stdout.flush()\n"
        "        self.fail('boom')\n"
    )
    lane(repo, "noisy", {"test_noisy.py": noisy})
    (o,) = queue(repo, "noisy")
    assert o.result == "rejected"
    assert q.status(repo.state())["queued"] == []


def test_unexpected_error_rejects_the_lane_and_empties_the_queue(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    lane(repo, "x", {"a.txt": "a"})

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr(q, "run_gate", boom)
    (o,) = queue(repo, "x")
    assert o.result == "rejected"
    assert "RuntimeError: disk full" in o.reason
    assert q.status(repo.state())["queued"] == []
    assert repo.git.run("for-each-ref", "refs/agent-gates/") == ""


def test_timeout_keeps_stderr(repo: Repo) -> None:
    code = "import sys, time; sys.stderr.write('slow-marker'); sys.stderr.flush(); time.sleep(5)"
    cfg = config.parse(
        f'[[gate.steps]]\nname = "slow"\nrun = ["python3", "-c", "{code}"]\ntimeout_s = 0.5\n'
    )
    rec = gate.run_gate(repo.git, repo.state(), cfg, "HEAD")
    assert rec.steps[-1].timed_out
    assert "slow-marker" in rec.steps[-1].output_tail


def test_worktrees_are_removed_when_a_step_raises(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    def crash(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(gate, "_run_step", crash)
    with pytest.raises(KeyboardInterrupt):
        gate.run_gate(repo.git, repo.state(), repo.config(), "HEAD")
    assert len(repo.git.worktrees()) == 1


# --- paths -----------------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["../outside", "/etc", "a/../../b", "~/x", "..", "a\\b"])
def test_dirs_must_stay_inside_the_repository(value: str) -> None:
    with pytest.raises(config.ConfigError):
        config.parse(f'decisions_dir = "{value}"\n'.replace("\\", "\\\\"))
    with pytest.raises(config.ConfigError):
        config.parse(f'specs_dir = "{value}"\n'.replace("\\", "\\\\"))


def test_symlinked_dir_pointing_outside_is_refused(repo: Repo, tmp_path: Path) -> None:
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "001-x.md").write_text("# x\n")
    (repo.root / "docs").mkdir()
    os.symlink(outside, repo.root / "docs" / "specs")
    assert cli(["-C", str(repo.root), "spec", "check"]) == 2


# --- built-in checks -------------------------------------------------------------------------


def test_two_lanes_with_the_same_decision_number_cannot_both_merge(repo: Repo) -> None:
    for name in ("one", "two"):
        path = Path(lanes.new_lane(repo.git, repo.state(), repo.config(), name)["path"])
        assert cli(["-C", str(path), "decision", "new", f"Decision from {name}"]) == 0
        git(path, "add", "-A")
        git(path, "commit", "-q", "-m", f"decision {name}")
    outcomes = queue(repo, "one", "two")
    assert [o.result for o in outcomes] == ["merged", "rejected"]
    assert "builtin:decisions" in outcomes[1].reason


def test_broken_spec_fails_the_gate(repo: Repo) -> None:
    lane(repo, "spec", {"docs/specs/001-x.md": "no title\n"})
    (o,) = queue(repo, "spec")
    assert o.result == "rejected"
    assert "builtin:specs" in o.reason


# --- merges and the compare-and-swap ---------------------------------------------------------


def test_signing_config_does_not_break_candidate_merges(repo: Repo) -> None:
    git(repo.root, "config", "commit.gpgsign", "true")
    git(repo.root, "config", "gpg.program", "false")  # signing would fail
    git(repo.root, "config", "commit.gpgsign", "false")  # ... but lane commits need to work
    lane(repo, "x", {"a.txt": "a"})
    git(repo.root, "config", "commit.gpgsign", "true")
    (o,) = queue(repo, "x")
    assert o.result == "merged"


def test_failing_merge_hook_is_reported_as_such(repo: Repo) -> None:
    lane(repo, "x", {"a.txt": "a"})
    hook = repo.git.common_dir() / "hooks" / "pre-merge-commit"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text("#!/bin/sh\necho blocked-by-hook >&2\nexit 1\n")
    hook.chmod(0o755)
    (o,) = queue(repo, "x")
    assert o.result == "rejected"
    assert o.reason.startswith("merge failed")
    assert "conflict" not in o.reason


def test_untracked_file_in_main_worktree_blocks_with_a_clear_message(repo: Repo) -> None:
    lane(repo, "x", {"new.txt": "from the lane"})
    (repo.root / "new.txt").write_text("untracked local file")
    with pytest.raises(q.QueueBlocked, match="untracked"):
        queue(repo, "x")
    assert q.status(repo.state())["queued"] == ["x"]


def test_compare_and_swap_with_main_not_checked_out(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    git(repo.root, "switch", "-q", "-c", "work")  # main is checked out nowhere now
    lane(repo, "good", {"mul.py": "x = 1\n"})
    real = q.run_gate
    calls = {"n": 0}

    def race(*args, **kwargs):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 1:  # a gated change lands on main through another clone
            tmp = repo.root.parent / "other-wt"
            git(repo.root, "worktree", "add", "-q", str(tmp), "main")
            sha = commit(tmp, {"other.txt": "x"})
            git(repo.root, "worktree", "remove", "--force", str(tmp))
            real(repo.git, repo.state(), repo.config(), sha)
        return real(*args, **kwargs)

    monkeypatch.setattr(q, "run_gate", race)
    outcomes = queue(repo, "good")
    assert [o.result for o in outcomes] == ["requeued", "merged"]
    main = repo.git.resolve("refs/heads/main")
    assert main == outcomes[-1].candidate
    assert repo.git.show(main, "other.txt") == "x"
    assert q._advance_main(repo.git, "main", "0" * 40, main) is False  # stale old value
    assert repo.git.resolve("refs/heads/main") == main


def test_failed_gate_leaves_no_worktree_or_candidate_ref(repo: Repo) -> None:
    lane(repo, "bad", {"calc.py": CALC.replace("a + b", "a - b")})
    (o,) = queue(repo, "bad")
    assert o.result == "rejected"
    assert len(repo.git.worktrees()) == 2  # main + the lane
    assert repo.git.run("for-each-ref", "refs/agent-gates/") == ""
