from __future__ import annotations

from pathlib import Path

import pytest

from agent_gates import config, gate, lanes
from agent_gates import queue as q
from agent_gates.git import GitError
from agent_gates.repo import Repo
from conftest import commit, git

BROKEN = {"calc.py": "def add(a, b):\n    return a - b\n"}


def lane(repo: Repo, name: str, files: dict[str, str]) -> Path:
    entry = lanes.new_lane(repo.git, repo.state(), repo.config(), name)
    path = Path(entry["path"])
    commit(path, files, f"work on {name}")
    return path


def test_lane_new_and_list(repo: Repo) -> None:
    path = lane(repo, "feature-a", {"a.txt": "a"})
    assert git(path, "branch", "--show-current") == "lane/feature-a"
    rows = lanes.list_lanes(repo.git, repo.state(), repo.config())
    assert [(r["name"], r["status"], r["ahead"], r["behind"], r["gate"]) for r in rows] == [
        ("feature-a", "open", 1, 0, "not gated")
    ]
    with pytest.raises(lanes.LaneError, match="exists already"):
        lanes.new_lane(repo.git, repo.state(), repo.config(), "feature-a")


@pytest.mark.parametrize("name", ["Bad", "../x", "-x", "a b", "", "x" * 70])
def test_invalid_lane_names(repo: Repo, name: str) -> None:
    with pytest.raises(lanes.LaneError, match="invalid lane name"):
        lanes.new_lane(repo.git, repo.state(), repo.config(), name)


def test_queue_merges_only_gated_candidates(repo: Repo) -> None:
    lane(repo, "good", {"mul.py": "def mul(a, b):\n    return a * b\n"})
    lane(repo, "bad", BROKEN)
    before = repo.git.resolve("main")
    for name in ("good", "bad"):
        q.add(repo.git, repo.state(), name)
    outcomes = q.run(repo.git, repo.state())
    assert [(o.lane, o.result) for o in outcomes] == [("good", "merged"), ("bad", "rejected")]
    main = repo.git.resolve("main")
    assert main == outcomes[0].candidate
    # main moved to the exact commit that was gated, a merge whose first parent is old main
    assert repo.git.run("rev-parse", f"{main}^1") == before
    assert gate.get_record(repo.git, repo.state(), main) is not None
    assert gate.verify(repo.git, repo.state(), repo.config(), main)[0]
    # the main worktree was fast-forwarded too, and is clean
    assert (repo.root / "mul.py").exists()
    assert repo.git.is_clean(repo.root)
    assert q.status(repo.state())["queued"] == []
    assert [h["result"] for h in q.status(repo.state())["history"]] == ["merged", "rejected"]


WEAK = 'main = "main"\n[[gate.steps]]\nname = "tests"\nrun = "true"\n'


def test_a_lane_cannot_weaken_its_own_gate(repo: Repo) -> None:
    # The lane changes ONLY the configuration; its tests still pass.
    path = lane(repo, "sneaky", {".agent-gates.toml": WEAK})
    head = git(path, "rev-parse", "HEAD")
    assert gate.run_gate(repo.git, repo.state(), config.parse(WEAK), head).passed
    q.add(repo.git, repo.state(), "sneaky")
    (outcome,) = q.run(repo.git, repo.state())
    assert outcome.result == "rejected"
    assert "--allow-config-change sneaky" in outcome.reason
    assert repo.git.resolve("refs/heads/main") == outcome.main_before
    assert "true" not in repo.config().steps[0].argv  # later lanes keep the real gate


def test_conflicting_lane_is_rejected(repo: Repo) -> None:
    lane(repo, "one", {"calc.py": "def add(a, b):\n    return b + a\n"})
    lane(repo, "two", {"calc.py": "def add(a, b):\n    return a + b + 0\n"})
    q.add(repo.git, repo.state(), "one")
    q.add(repo.git, repo.state(), "two")
    outcomes = q.run(repo.git, repo.state())
    assert [o.result for o in outcomes] == ["merged", "rejected"]
    assert "merge conflict" in outcomes[1].reason
    assert len(repo.git.worktrees()) == 3  # main + 2 lanes; no temporary worktree left


def test_already_merged_lane_is_skipped(repo: Repo) -> None:
    lanes.new_lane(repo.git, repo.state(), repo.config(), "empty")
    q.add(repo.git, repo.state(), "empty")
    (outcome,) = q.run(repo.git, repo.state())
    assert outcome.result == "skipped"


def test_main_moving_during_the_gate_requeues(repo: Repo, monkeypatch: pytest.MonkeyPatch) -> None:
    lane(repo, "good", {"mul.py": "x = 1\n"})
    real = q.run_gate
    calls = {"n": 0}

    def gate_and_race(*args, **kwargs):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 1:  # another gated change lands on main while this one is gated
            sha = commit(repo.root, {"other.txt": "pushed meanwhile"})
            real(repo.git, repo.state(), repo.config(), sha)
        return real(*args, **kwargs)

    monkeypatch.setattr(q, "run_gate", gate_and_race)
    q.add(repo.git, repo.state(), "good")
    outcomes = q.run(repo.git, repo.state())
    assert [o.result for o in outcomes] == ["requeued", "merged"]
    assert (repo.root / "other.txt").exists()
    assert (repo.root / "mul.py").exists()


def test_endless_races_stop_the_run_and_keep_the_lane(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    lane(repo, "good", {"mul.py": "x = 1\n"})
    real = q.run_gate
    n = {"i": 0}

    def always_race(*args, **kwargs):  # type: ignore[no-untyped-def]
        n["i"] += 1
        sha = commit(repo.root, {f"race{n['i']}.txt": "x"})
        real(repo.git, repo.state(), repo.config(), sha)
        return real(*args, **kwargs)

    monkeypatch.setattr(q, "run_gate", always_race)
    q.add(repo.git, repo.state(), "good")
    outcomes = q.run(repo.git, repo.state(), max_requeues=2)
    assert [o.result for o in outcomes] == ["requeued", "requeued"]
    assert q.status(repo.state())["queued"] == ["good"]


def test_dirty_main_worktree_stops_the_queue(repo: Repo) -> None:
    lane(repo, "good", {"mul.py": "x = 1\n"})
    (repo.root / "calc.py").write_text("# local edit\n")
    q.add(repo.git, repo.state(), "good")
    with pytest.raises(GitError, match="uncommitted or untracked"):
        q.run(repo.git, repo.state())
    assert repo.git.resolve("main") != repo.git.resolve("lane/good")


def test_queue_add_unknown_lane(repo: Repo) -> None:
    with pytest.raises(GitError, match="no such lane"):
        q.add(repo.git, repo.state(), "ghost")
