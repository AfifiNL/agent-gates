# agent-gates

Guard rails for AI coding agents working on a git repository. Work starts from a written spec
with checkable acceptance criteria, each unit of work gets its own branch and worktree (a
"lane"), a gate runs the project's checks on one exact commit, and a merge queue moves the main
branch only to a commit that passed that gate. Decisions go into a numbered log.

- **CLI** (`agent-gates`): `spec check`, `lane new|list`, `gate run|status|verify`,
  `queue add|run|status`, `decision new|check|list`, `init`, `serve`, `demo`.
- **MCP server** (stdio) so an agent can read the specs, a commit's gate status, the queue and
  the decision log, and propose a decision. It cannot run the gate or move a branch.
- **GitHub workflow template** (`templates/github/gate.yml`) that runs the same checks on the
  exact commit under test, plus a secret scan, dependency review and CodeQL; every action is
  pinned by commit SHA.
- Python 3.12, standard library plus the MCP SDK. Gate state lives in `.git/agent-gates/`, so
  nothing extra is committed and every worktree sees the same records.

## What it does

`make demo` builds a throwaway repository with three lanes and runs the merge queue once.
`multiply` adds a feature with a test; `broken` changes behaviour and breaks a test;
`weaken-gate` breaks a test and also edits the gate configuration to skip the tests. Output
(abridged):

```text
== the merge queue gates each candidate merge commit
$ agent-gates queue run
  multiply             merged    gate passed on the candidate (candidate caf0ea6f650d)
  broken               rejected  gate failed at step 'tests' (candidate df78fe9a83e5)
  weaken-gate          rejected  gate failed at step 'tests' (candidate 09b906223529)
main is at caf0ea6f650d

== main is at a commit that passed the gate
$ agent-gates gate verify main
caf0ea6f650d: gate passed
```

`weaken-gate` is rejected because the gate reads its configuration from main, not from the lane
it is judging. Under its own edited configuration that lane would have passed (a test proves
this).

## Quick start

Requires Python 3.12+, git 2.31+ and [uv](https://docs.astral.sh/uv/).

```bash
make install     # .venv with dev tools (locked)
make demo        # the run above, in a temporary repository
make check       # lint, strict types, 66 tests (what CI runs)
```

In your own repository (the steps below were run as written in a scratch repository):

```bash
alias agent-gates="uv run --project /path/to/agent-gates agent-gates"
agent-gates init                  # .agent-gates.toml, an example spec, the first decision
git add -A && git commit -m "Add agent-gates"
agent-gates spec check            # every spec has Problem, Acceptance criteria, Out of scope
agent-gates lane new export-csv   # branch lane/export-csv + worktree ../<repo>.lanes/export-csv
# ... the agent works and commits in that worktree ...
agent-gates queue add export-csv
agent-gates queue run             # gate the merge candidate; fast-forward main only on pass
agent-gates gate verify main      # exit 0 only if this exact commit passed
```

`.agent-gates.toml`:

```toml
main = "main"
specs_dir = "docs/specs"
decisions_dir = "docs/decisions"

[[gate.steps]]
name = "tests"
run = "python3 -m unittest -q"   # a string (split like a shell would, no shell) or an argv list
timeout_s = 600
```

MCP: `agent-gates -C /path/to/repo serve` speaks MCP on stdin/stdout. Tools: `check_specs`,
`gate_status`, `queue_status`, `list_lanes`, `list_decisions`, `add_decision`.

## How it works

```text
spec (docs/specs/NNN-name.md)          acceptance criteria as a checklist
   │
lane new ──► branch lane/<name> + its own worktree, started from main
   │
   │  agent commits in the lane
   ▼
queue add ─► queue run, one lane at a time:
   1. candidate = merge commit of main + lane (temporary worktree)   conflict → rejected
   2. gate on that exact candidate SHA, in a fresh detached worktree,
      with the steps from main's .agent-gates.toml                    fail → rejected
   3. fast-forward main to the candidate if main has not moved        moved → queued again
   ▼
main ──────► always a commit with a passing gate record
```

- **Exact commit.** A gate record is stored under the full 40-character SHA. A record for one
  commit never counts for another, so a rebase, an amend or a merge needs its own run.
  `gate verify` also refuses a record made under a different gate configuration.
- **Clean checkout.** The gate runs in a temporary detached worktree of the commit, so
  uncommitted edits and untracked files in someone's working copy cannot make it pass.
- **Configuration from main.** The steps are read from the committed `.agent-gates.toml` on the
  main branch; the branch name comes from the command line (`--main`), not from a file a lane
  could edit.
- **Compare-and-swap.** main is advanced only if it still points where the candidate was built
  from; otherwise the lane stays first in the queue and is rebuilt on the new main. After three
  such races in one run, the run stops and leaves the lane queued. If main is checked out, that
  working tree must be clean and is fast-forwarded.
- **Fail closed.** A configuration without steps never passes. A step that times out or cannot
  start fails. Steps run without a shell. A queue run holds a lock, so two runs cannot interleave.
- **Decisions** are files `NNNN-name.md` with `- Status: proposed|accepted|rejected|superseded|deprecated`;
  numbering must run from 0001 without gaps, and a superseded record must name an existing
  successor. `decision new` never overwrites a file.

## GitHub workflow template

`templates/github/gate.yml` is a template to copy into another repository's
`.github/workflows/`. It runs on `pull_request` and on `merge_group` (GitHub's own merge
queue), checks out the pull request's head commit or the queue's candidate commit, writes the
gated SHA to the job summary, and runs `make check`. It adds a gitleaks scan (release binary,
SHA-256 verified), dependency review and CodeQL. Permissions are `contents: read`, plus
`security-events: write` for the CodeQL job only. Checkouts do not keep credentials.

CodeQL and dependency review are free on public repositories; on private repositories they need
GitHub Advanced Security. The template is checked by the tests (YAML parses, every `uses:` is
pinned to a 40-character SHA with a version comment, least-privilege permissions), but it has
not been run on GitHub from this repository.

## Limits

- **Local and single-machine.** Lanes, the queue and gate records live in one clone's
  `.git/agent-gates/`. There is no server, no sharing between machines, and no signature on gate
  records: anyone who can write to `.git` can forge one. For a shared main branch, use the
  workflow template (or GitHub's merge queue) as the authority, and this tool locally.
- **The gate is as good as its steps.** It runs your commands; it does not know whether your
  tests are meaningful.
- **Queue runs are sequential.** One candidate is gated at a time; there is no batching or
  speculative parallel gating.
- **Spec and decision checks are about structure**, not content: they check that acceptance
  criteria exist and are a checklist, not that they are good.
- **No automatic lane clean-up.** Merged lanes keep their branch and worktree until removed with
  git (`git worktree remove`, `git branch -d`).
- Tested on Linux only (it uses `fcntl` file locks).

## License

Apache-2.0. See [LICENSE](LICENSE). Copyright 2026 Hossam Afifi.
