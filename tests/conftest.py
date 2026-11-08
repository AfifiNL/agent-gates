from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from agent_gates.repo import Repo

ROOT = Path(__file__).resolve().parents[1]
PY = json.dumps([sys.executable, "-I", "-m", "unittest", "discover", "-q"])
CONFIG = f"""\
main = "main"

[[gate.steps]]
name = "tests"
run = {PY}
timeout_s = 60
"""
CALC = "def add(a, b):\n    return a + b\n"
TESTS = (
    "import unittest\n\nfrom calc import add\n\n\n"
    "class T(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n"
)


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def commit(cwd: Path, files: dict[str, str], message: str = "change") -> str:
    for name, text in files.items():
        p = cwd / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    git(cwd, "add", "-A")
    git(cwd, "commit", "-q", "-m", message)
    return git(cwd, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    path = tmp_path / "proj"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.name", "Test Dev")
    git(path, "config", "user.email", "dev@example.com")
    git(path, "config", "commit.gpgsign", "false")
    commit(path, {".agent-gates.toml": CONFIG, "calc.py": CALC, "test_calc.py": TESTS}, "init")
    return Repo.open(path)
