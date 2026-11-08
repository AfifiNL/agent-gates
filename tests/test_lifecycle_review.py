"""Tests for the second review: removing lanes safely from anywhere, and status from any tree."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from agent_gates import lanes, status
from agent_gates import queue as q
from agent_gates.cli import main as cli
from agent_gates.git import Git
from agent_gates.repo import Repo
from conftest import CONFIG, commit, git


def lane(repo: Repo, name: str, files: dict[str, str]) -> Path:
    path = Path(lanes.new_lane(repo.git, repo.state(), repo.config(), name)["path"])
    commit(path, files, f"work on {name}")
    return path


def merge(repo: Repo, *names: str, cleanup: bool = False) -> list[q.Outcome]:
    for n in names:
        q.add(repo.git, repo.state(), n)
    return q.run(repo.git, repo.state(), cleanup=cleanup)


def test_lane_done_run_from_inside_the_lane_folder(
    repo: Repo, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = lane(repo, "good", {"mul.py": "x = 1\n"})
    merge(repo, "good")
    monkeypatch.chdir(path)
    assert cli(["lane", "done", "good"]) == 0
    assert "removed worktree ../proj.lanes/good" in capsys.readouterr().out
    assert not path.exists()
    assert not repo.git.branch_exists("lane/good")
    assert repo.state().read("lanes.json", {}) == {}


def test_queue_cleanup_run_from_inside_a_merged_lane(
    repo: Repo, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    x = lane(repo, "x", {"x.txt": "x"})
    lane(repo, "y", {"y.txt": "y"})
    q.add(repo.git, repo.state(), "x")
    q.add(repo.git, repo.state(), "y")
    monkeypatch.chdir(x)
    assert cli(["queue", "run", "--cleanup"]) == 0
    out = capsys.readouterr().out
    assert re.search(r"x\s+merged", out)
    assert re.search(r"y\s+merged", out)
    history = q.status(repo.state())["history"]
    assert [(h["lane"], h["result"]) for h in history] == [("x", "merged"), ("y", "merged")]
    assert all(h["cleanup"].startswith(f"lane {h['lane']}: removed") for h in history)
    assert q.status(repo.state())["queued"] == []
    assert not x.exists()


def test_history_is_written_before_cleanup_and_cleanup_errors_are_recorded(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    lane(repo, "x", {"x.txt": "x"})

    def broken_cleanup(*args: Any, **kwargs: Any) -> str:
        assert q.status(repo.state())["history"][-1]["result"] == "merged"  # already recorded
        raise RuntimeError("cleanup exploded")

    monkeypatch.setattr(q, "done_lane", broken_cleanup)
    (o,) = merge(repo, "x", cleanup=True)
    assert o.result == "merged"
    assert o.cleanup == "lane kept: cleanup exploded"
    assert q.status(repo.state())["history"][-1]["cleanup"] == "lane kept: cleanup exploded"


def test_a_commit_made_after_the_check_keeps_the_branch(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = lane(repo, "good", {"mul.py": "x = 1\n"})
    merge(repo, "good")
    real_run = Git.run
    late: dict[str, str] = {}

    def run(self: Git, *args: str, **kwargs: Any) -> str:
        if args[:2] == ("worktree", "remove") and "sha" not in late:
            late["sha"] = commit(path, {"late.txt": "written after the merged check"})
        return real_run(self, *args, **kwargs)

    monkeypatch.setattr(Git, "run", run)
    with pytest.raises(lanes.LaneError, match="branch moved after it was checked"):
        lanes.done_lane(repo.git, repo.state(), repo.config(), "good")
    assert repo.git.resolve("refs/heads/lane/good") == late["sha"]


def test_ignored_files_count_as_dirty(repo: Repo) -> None:
    path = lane(repo, "good", {".gitignore": ".env\n", "mul.py": "x = 1\n"})
    (path / ".env").write_text("LOCAL_SETTING=1\n")
    (o,) = merge(repo, "good", cleanup=True)
    assert o.result == "merged"
    assert o.cleanup is not None
    assert o.cleanup.startswith("lane kept:")
    assert "ignored" in o.cleanup
    assert (path / ".env").exists()
    with pytest.raises(lanes.LaneError, match="has ignored files"):
        lanes.done_lane(repo.git, repo.state(), repo.config(), "good")
    lanes.done_lane(repo.git, repo.state(), repo.config(), "good", force=True)
    assert not path.exists()


def test_lane_done_waits_for_no_queue_run(repo: Repo) -> None:
    lane(repo, "wip", {"w.txt": "w"})
    with lanes.queue_lock(repo.state()):
        with pytest.raises(lanes.LaneError, match="merge-queue run is in progress"):
            lanes.done_lane(repo.git, repo.state(), repo.config(), "wip", force=True)
        with pytest.raises(q.QueueBlocked, match="merge-queue run is in progress"):
            q.run(repo.git, repo.state())
    assert repo.git.branch_exists("lane/wip")


@pytest.mark.parametrize("switch", [["switch", "-q", "-c", "other"], ["switch", "-q", "--detach"]])
def test_worktree_not_on_the_lane_branch(repo: Repo, switch: list[str]) -> None:
    path = lane(repo, "good", {"mul.py": "x = 1\n"})
    merge(repo, "good")
    git(path, *switch)
    with pytest.raises(lanes.LaneError, match="not on lane/good"):
        lanes.done_lane(repo.git, repo.state(), repo.config(), "good")
    assert path.exists()
    lanes.done_lane(repo.git, repo.state(), repo.config(), "good", force=True)
    assert not path.exists()
    assert not repo.git.branch_exists("lane/good")
    lanes.new_lane(repo.git, repo.state(), repo.config(), "good")  # the name is free again


def test_lane_without_commits_is_not_called_merged(repo: Repo) -> None:
    lanes.new_lane(repo.git, repo.state(), repo.config(), "empty")
    (row,) = lanes.list_lanes(repo.git, repo.state(), "main", repo.config())
    assert row["status"] == "no commits yet"


def test_status_lists_lanes_when_main_config_is_unreadable(repo: Repo) -> None:
    lane(repo, "x", {"x.txt": "x"})
    commit(repo.root, {".agent-gates.toml": CONFIG.replace('main = "main"', 'main = "x"')})
    out = status.render(status.collect(repo))
    assert "gate configuration: ERROR" in out
    assert "## Lanes (1)" in out
    assert re.search(r"\| x \| `[0-9a-f]{12}` \| 1 \| 1 \| open \| unknown \|", out)


def test_stored_reasons_and_notes_have_no_machine_paths(repo: Repo, tmp_path: Path) -> None:
    import tempfile

    scrubbed = q._scrub(f"failed in {tempfile.gettempdir()}/agent-gates-merge-abc/wt: x", repo.git)
    assert scrubbed == "failed in <temporary worktree>: x"
    assert q._scrub(f"see {repo.git.repo}/a.txt", repo.git) == "see <repo>/a.txt"
    path = lane(repo, "good", {"mul.py": "x = 1\n"})
    (path / "scratch.txt").write_text("keep")
    (o,) = merge(repo, "good", cleanup=True)
    assert o.cleanup is not None
    assert "../proj.lanes/good" in o.cleanup
    assert str(tmp_path) not in o.cleanup


def test_long_lane_names_stay_aligned(repo: Repo, capsys: pytest.CaptureFixture[str]) -> None:
    long = "a-rather-long-lane-name-for-alignment"
    lane(repo, long, {"l.txt": "l"})
    lane(repo, "s", {"s.txt": "s"})
    q.add(repo.git, repo.state(), long)
    q.add(repo.git, repo.state(), "s")
    assert cli(["-C", str(repo.root), "queue", "run"]) == 0
    lines = [ln for ln in capsys.readouterr().out.splitlines() if "merged" in ln]
    columns = {ln.index("merged") for ln in lines}
    assert len(lines) == 2
    assert len(columns) == 1
