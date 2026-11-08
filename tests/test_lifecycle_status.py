"""Pass 2: lane clean-up (`lane done`, `queue run --cleanup`) and the `status` summary."""

from __future__ import annotations

import json
from pathlib import Path

import anyio
import pytest
from mcp import Client

from agent_gates import gate, lanes, status
from agent_gates import queue as q
from agent_gates.cli import main as cli
from agent_gates.mcp_server import build_server
from agent_gates.repo import Repo
from conftest import commit, git

SPEC = (
    "# Export\n\n## Problem\n\nNo export.\n\n## Acceptance criteria\n\n- [x] CSV.\n"
    "- [ ] PDF later.\n\n## Out of scope\n\n- XML.\n"
)
BROKEN = {"calc.py": "def add(a, b):\n    return 0\n"}


def lane(repo: Repo, name: str, files: dict[str, str]) -> Path:
    path = Path(lanes.new_lane(repo.git, repo.state(), repo.config(), name)["path"])
    commit(path, files, f"work on {name}")
    return path


def merge(repo: Repo, *names: str, cleanup: bool = False) -> list[q.Outcome]:
    for n in names:
        q.add(repo.git, repo.state(), n)
    return q.run(repo.git, repo.state(), cleanup=cleanup)


# --- lane done -------------------------------------------------------------------------------


def test_done_removes_a_merged_lane(repo: Repo) -> None:
    path = lane(repo, "good", {"mul.py": "x = 1\n"})
    assert merge(repo, "good")[0].result == "merged"
    message = lanes.done_lane(repo.git, repo.state(), repo.config(), "good")
    assert message.startswith("lane good: removed worktree")
    assert not path.exists()
    assert not repo.git.branch_exists("lane/good")
    assert lanes.list_lanes(repo.git, repo.state(), "main", repo.config()) == []
    assert len(repo.git.worktrees()) == 1


def test_done_refuses_an_unmerged_lane_unless_forced(repo: Repo) -> None:
    path = lane(repo, "wip", {"wip.txt": "x"})
    with pytest.raises(lanes.LaneError, match="not merged into main"):
        lanes.done_lane(repo.git, repo.state(), repo.config(), "wip")
    assert path.exists()
    assert repo.git.branch_exists("lane/wip")
    lanes.done_lane(repo.git, repo.state(), repo.config(), "wip", force=True)
    assert not path.exists()
    assert not repo.git.branch_exists("lane/wip")


def test_done_refuses_a_dirty_worktree_unless_forced(repo: Repo) -> None:
    path = lane(repo, "good", {"mul.py": "x = 1\n"})
    merge(repo, "good")
    (path / "notes.txt").write_text("not committed")  # untracked counts as dirty
    with pytest.raises(lanes.LaneError, match="has untracked files"):
        lanes.done_lane(repo.git, repo.state(), repo.config(), "good")
    assert (path / "notes.txt").exists()
    lanes.done_lane(repo.git, repo.state(), repo.config(), "good", force=True)
    assert not path.exists()


def test_done_refuses_a_queued_lane_unless_forced(repo: Repo) -> None:
    lanes.new_lane(repo.git, repo.state(), repo.config(), "queued")
    q.add(repo.git, repo.state(), "queued")
    with pytest.raises(lanes.LaneError, match="is queued"):
        lanes.done_lane(repo.git, repo.state(), repo.config(), "queued")
    message = lanes.done_lane(repo.git, repo.state(), repo.config(), "queued", force=True)
    assert "queue entry" in message
    assert q.status(repo.state())["queued"] == []


def test_done_unknown_lane_and_bad_name(repo: Repo) -> None:
    with pytest.raises(lanes.LaneError, match="no such lane"):
        lanes.done_lane(repo.git, repo.state(), repo.config(), "ghost")
    with pytest.raises(lanes.LaneError, match="invalid lane name"):
        lanes.done_lane(repo.git, repo.state(), repo.config(), "../x")


def test_done_from_inside_the_lane_worktree(repo: Repo) -> None:
    path = lane(repo, "good", {"mul.py": "x = 1\n"})
    merge(repo, "good")
    inside = Repo.open(path)
    lanes.done_lane(inside.git, inside.state(), inside.config(), "good")
    assert not path.exists()


def test_done_ignores_a_tag_named_like_the_lane(repo: Repo) -> None:
    lane(repo, "wip", {"wip.txt": "x"})
    git(repo.root, "tag", "lane/wip", "refs/heads/main")  # a tag that IS merged
    with pytest.raises(lanes.LaneError, match="not merged"):
        lanes.done_lane(repo.git, repo.state(), repo.config(), "wip")


def test_cli_lane_done(repo: Repo, capsys: pytest.CaptureFixture[str]) -> None:
    lane(repo, "wip", {"wip.txt": "x"})
    assert cli(["-C", str(repo.root), "lane", "done", "wip"]) == 2
    assert "not merged" in capsys.readouterr().err
    assert cli(["-C", str(repo.root), "lane", "done", "wip", "--force"]) == 0
    assert "removed worktree" in capsys.readouterr().out


def test_queue_cleanup_removes_merged_lanes_only(repo: Repo) -> None:
    good = lane(repo, "good", {"mul.py": "x = 1\n"})
    bad = lane(repo, "bad", BROKEN)
    outcomes = merge(repo, "good", "bad", cleanup=True)
    assert [(o.result, bool(o.cleanup)) for o in outcomes] == [
        ("merged", True),
        ("rejected", False),
    ]
    assert not good.exists()
    assert not repo.git.branch_exists("lane/good")
    assert bad.exists()
    assert repo.git.branch_exists("lane/bad")
    history = q.status(repo.state())["history"]
    assert history[0]["cleanup"].startswith("lane good: removed")


def test_queue_cleanup_keeps_a_dirty_lane(repo: Repo, capsys: pytest.CaptureFixture[str]) -> None:
    path = lane(repo, "good", {"mul.py": "x = 1\n"})
    (path / "scratch.txt").write_text("keep me")
    q.add(repo.git, repo.state(), "good")
    assert cli(["-C", str(repo.root), "queue", "run", "--cleanup"]) == 0
    out = capsys.readouterr().out
    assert "merged" in out
    assert "lane kept:" in out
    assert (path / "scratch.txt").exists()


# --- status ----------------------------------------------------------------------------------


def _scenario(repo: Repo) -> dict[str, str]:
    commit(
        repo.root,
        {
            "docs/specs/001-export.md": SPEC,
            "docs/decisions/0001-use-a-queue.md": "# 0001. Use a queue\n\n- Status: accepted\n",
        },
        "spec and decision",
    )
    gate.run_gate(repo.git, repo.state(), repo.config(), repo.git.resolve("refs/heads/main"))
    lane(repo, "good", {"mul.py": "x = 1\n"})
    lane(repo, "bad", BROKEN)
    idle = lane(repo, "idle", {"idle.txt": "x"})
    outcomes = merge(repo, "good", "bad")
    # someone commits a broken spec straight to main, outside the queue
    commit(repo.root, {"docs/specs/002-draft.md": "no title\n"}, "draft spec")
    (repo.root / "docs" / "specs" / "003-local.md").write_text("not committed\n")  # ignored
    lanes.new_lane(repo.git, repo.state(), repo.config(), "queued")
    q.add(repo.git, repo.state(), "queued")
    sha = repo.git.resolve
    return {
        "main": sha("refs/heads/main")[:12],
        "bad": sha("refs/heads/lane/bad")[:12],
        "good": sha("refs/heads/lane/good")[:12],
        "idle": git(idle, "rev-parse", "HEAD")[:12],
        "queued": sha("refs/heads/lane/queued")[:12],
        "c_good": (outcomes[0].candidate or "")[:12],
        "c_bad": (outcomes[1].candidate or "")[:12],
    }


EXPECTED = """\
# agent-gates status

main: `main` at `{main}`, gate: not gated
gate steps (from main): tests

## Specs on main (2)

| spec | title | criteria | ticked | check |
|---|---|---|---|---|
| docs/specs/001-export.md | Export | 2 | 1 | ok |
| docs/specs/002-draft.md |  | 0 | 0 | 4 error(s) |

- docs/specs/002-draft.md:1: first line must be a '# Title'
- docs/specs/002-draft.md: missing section '## Problem'
- docs/specs/002-draft.md: missing section '## Acceptance criteria'
- docs/specs/002-draft.md: missing section '## Out of scope'

## Lanes (4)

| lane | head | ahead | behind | state | gate on head |
|---|---|---|---|---|---|
| bad | `{bad}` | 1 | 3 | open | not gated |
| good | `{good}` | 0 | 2 | merged | not gated |
| idle | `{idle}` | 1 | 3 | open | not gated |
| queued | `{queued}` | 0 | 0 | no commits yet | not gated |

## Merge queue

queued: queued

Last 2 outcome(s), oldest first:

| lane | result | candidate | reason |
|---|---|---|---|
| good | merged | `{c_good}` | gate passed on the candidate |
| bad | rejected | `{c_bad}` | gate failed at step 'tests' (current configuration) |

## Decisions on main (1)

| # | title | status |
|---|---|---|
| 0001 | Use a queue | accepted |

check: ok
"""


def test_status_markdown_is_exact_and_deterministic(repo: Repo) -> None:
    shas = _scenario(repo)
    first = status.render(status.collect(repo))
    assert first == EXPECTED.format(**shas)
    assert status.render(status.collect(repo)) == first
    # the same text from inside a lane worktree (its own files differ from main's)
    idle = Repo.open(repo.root.parent / "proj.lanes" / "idle")
    assert status.render(status.collect(idle)) == first


def test_status_cli_markdown_json_and_mcp_agree(
    repo: Repo, capsys: pytest.CaptureFixture[str]
) -> None:
    _scenario(repo)
    assert cli(["-C", str(repo.root), "status"]) == 0
    markdown = capsys.readouterr().out
    assert cli(["-C", str(repo.root), "status", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data == json.loads(json.dumps(status.collect(repo)))
    assert status.render(data) == markdown

    async def go() -> object:
        async with Client(build_server(repo)) as client:  # type: ignore[arg-type]
            return (await client.call_tool("repo_status", {})).structured_content

    served = anyio.run(go)
    assert isinstance(served, dict)
    assert served["markdown"] == markdown
    assert served["lanes"] == data["lanes"]


def test_status_without_configuration(tmp_path: Path) -> None:
    path = tmp_path / "bare"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.name", "T")
    git(path, "config", "user.email", "t@example.com")
    commit(path, {"README.md": "x"}, "init")
    out = status.render(status.collect(Repo.open(path)))
    assert "gate: unknown" in out
    assert "gate configuration: ERROR" in out
    assert "No specs." in out
    assert "No lanes." in out
    assert "No decisions." in out


def test_done_when_the_worktree_folder_was_deleted_by_hand(repo: Repo) -> None:
    import shutil

    path = lane(repo, "good", {"mul.py": "x = 1\n"})
    merge(repo, "good")
    shutil.rmtree(path)
    message = lanes.done_lane(repo.git, repo.state(), repo.config(), "good")
    assert "branch lane/good" in message
    assert not repo.git.branch_exists("lane/good")
    assert len(repo.git.worktrees()) == 1
