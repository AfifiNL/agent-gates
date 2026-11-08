from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from agent_gates import config, decisions, specs
from agent_gates.repo import Repo
from conftest import CONFIG, commit


def test_parse_steps() -> None:
    cfg = config.parse(
        'main = "trunk"\n[[gate.steps]]\nname = "lint"\nrun = "ruff check ."\n'
        '[[gate.steps]]\nname = "t"\nrun = ["pytest", "-q"]\ntimeout_s = 5\n'
    )
    assert cfg.main == "trunk"
    assert [(s.name, s.argv, s.timeout_s) for s in cfg.steps] == [
        ("lint", ("ruff", "check", "."), 600.0),
        ("t", ("pytest", "-q"), 5.0),
    ]
    assert len(cfg.sha256) == 64


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("[[gate.steps]]\nrun = 'x'\n", "needs `name` and `run`"),
        ("[[gate.steps]]\nname = 'a'\nrun = []\n", "needs `name` and `run`"),
        ("[[gate.steps]]\nname = 'a'\nrun = 'x'\ntimeout_s = 0\n", "positive"),
        ("[[gate.steps]]\nname='a'\nrun='x'\n[[gate.steps]]\nname='a'\nrun='y'\n", "unique"),
        ("main = \n", "agent-gates.toml"),
        ("[gate]\nsteps = 3\n", "array of tables"),
    ],
)
def test_config_errors(text: str, message: str) -> None:
    with pytest.raises(config.ConfigError, match=message):
        config.parse(text)


def test_config_comes_from_main_not_the_working_tree(repo: Repo) -> None:
    (repo.root / ".agent-gates.toml").write_text(CONFIG.replace("tests", "edited"))
    assert repo.config().steps[0].name == "tests"


def test_config_main_mismatch(repo: Repo) -> None:
    commit(repo.root, {".agent-gates.toml": CONFIG.replace('main = "main"', 'main = "x"')})
    with pytest.raises(config.ConfigError, match="must match"):
        repo.config()


def test_missing_config(tmp_path: Path, repo: Repo) -> None:
    other = Repo(repo.git, main="nope")
    with pytest.raises(config.ConfigError, match="not committed"):
        other.config()


GOOD_SPEC = """\
# Add export

## Problem

Users cannot export.

## Acceptance criteria

- [ ] CSV export works.
- [x] Button exists.

## Out of scope

- PDF.
"""


def test_good_spec() -> None:
    r = specs.check_text("docs/specs/001-add-export.md", GOOD_SPEC)
    assert r.ok
    assert (r.number, r.title, r.criteria, r.done) == ("001", "Add export", 2, 1)


@pytest.mark.parametrize(
    ("name", "text", "error"),
    [
        ("1-x.md", GOOD_SPEC, "file name"),
        ("001-x.md", GOOD_SPEC.replace("# Add export", "Add export"), "'# Title'"),
        ("001-x.md", GOOD_SPEC.replace("## Out of scope", "## Notes"), "Out of scope"),
        ("001-x.md", GOOD_SPEC.replace("Users cannot export.", ""), "'## Problem' is empty"),
        ("001-x.md", GOOD_SPEC.replace("- [ ] CSV", "- CSV"), "must be checklist items"),
        (
            "001-x.md",
            GOOD_SPEC.replace("- [ ] CSV export works.\n- [x] Button exists.\n", "Soon.\n"),
            "no checklist item",
        ),
        ("001-x.md", GOOD_SPEC + "\n## Problem\n\nagain\n", "duplicate section"),
    ],
)
def test_bad_specs(name: str, text: str, error: str) -> None:
    r = specs.check_text(name, text)
    assert any(error in e for e in r.errors), r.errors


def test_duplicate_spec_numbers(tmp_path: Path) -> None:
    d = tmp_path / "docs" / "specs"
    d.mkdir(parents=True)
    (d / "001-a.md").write_text(GOOD_SPEC)
    (d / "001-b.md").write_text(GOOD_SPEC)
    (d / "README.md").write_text("not a spec")
    results = specs.check_dir(tmp_path, "docs/specs")
    assert len(results) == 2
    assert any("also used by" in e for r in results for e in r.errors)


def test_decision_new_and_check(tmp_path: Path) -> None:
    today = dt.date(2026, 1, 2)
    p1 = decisions.new(tmp_path, "d", "Use SQLite!", today=today)
    p2 = decisions.new(tmp_path, "d", "  Gate   every merge ", today=today)
    assert p1.name == "0001-use-sqlite.md"
    assert p2.name == "0002-gate-every-merge.md"
    assert p2.read_text().startswith("# 0002. Gate every merge\n\n- Status: proposed\n")
    assert decisions.check(tmp_path, "d") == []
    with pytest.raises(decisions.DecisionError):
        decisions.new(tmp_path, "d", "   ")


def _write(tmp_path: Path, name: str, number: str, status: str, extra: str = "") -> None:
    d = tmp_path / "d"
    d.mkdir(exist_ok=True)
    (d / name).write_text(f"# {number}. T\n\n- Status: {status}\n{extra}\n")


@pytest.mark.parametrize(
    ("files", "error"),
    [
        ([("0001-a.md", "0001", "accepted"), ("0003-c.md", "0003", "accepted")], "without gaps"),
        ([("0001-a.md", "0001", "accepted"), ("0001-b.md", "0001", "accepted")], "duplicate"),
        ([("0001-a.md", "0001", "maybe")], "status must be one of"),
        ([("0001-a.md", "0002", "accepted")], "heading number"),
        ([("0001-a.md", "0001", "superseded")], "no '- Superseded by"),
        ([("0001-a.md", "0001", "superseded", "- Superseded by: 0009")], "does not exist"),
        ([("1-a.md", "0001", "accepted")], "file name"),
    ],
)
def test_decision_check_errors(tmp_path: Path, files: list[tuple[str, ...]], error: str) -> None:
    for f in files:
        _write(tmp_path, *f)
    errors = decisions.check(tmp_path, "d")
    assert any(error in e for e in errors), errors


def test_superseded_chain_is_valid(tmp_path: Path) -> None:
    _write(tmp_path, "0001-a.md", "0001", "superseded", "- Superseded by: 0002")
    _write(tmp_path, "0002-b.md", "0002", "accepted")
    assert decisions.check(tmp_path, "d") == []
