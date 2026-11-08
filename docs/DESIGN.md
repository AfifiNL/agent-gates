# Design notes

## Goals, in priority order

1. The main branch only ever points at a commit that passed the gate, as that exact commit.
2. A change cannot make its own gate weaker.
3. Agents (and people) can see what is expected (spec), what was decided (decision log) and
   where a change stands (lane, gate, queue) without asking.
4. No server, no database, no new service: plain git plus files.

## Decisions

**Gate records are keyed by the full commit SHA.** A record says "this tree, with this history,
passed these steps". Anything else (a branch name, a short SHA, a pull request number) can point
at different content tomorrow. A rebase or an amend that changes nothing visible still produces
a new SHA and needs a new run; that is the point.

**The gate runs on a clean, detached worktree of the commit.** Running in the developer's or the
agent's working copy would let uncommitted edits, untracked files or build output make a commit
pass that would fail on its own. A temporary worktree costs one checkout and is removed
afterwards (also on failure).

**The queue gates the merge, not the lane.** A lane can pass on its own and still break main
when combined with what merged before it. The queue builds the merge commit of main + lane, gates
that SHA, and fast-forwards main to it. Because main moves only by fast-forward to a gated
commit, every commit main ever points to has a record.

**The gate configuration comes from main, and changing it needs an operator.** If the gate read
`.agent-gates.toml` from the commit under test, a lane could replace the test step with `true`
and pass. Reading it from main is not enough on its own: a lane that changes only the
configuration (its tests still pass) would merge and weaken the gate for every later lane. So
the queue rejects any lane whose merge changes the file, unless the operator names that lane
with `queue run --allow-config-change <lane>`. That flag exists only on the command line, not
over MCP. Even with it, the new configuration must parse, keep at least one step and the same
main branch, and the candidate must pass under the old and the new configuration. The name of
the main branch comes from the command line, not from a file.

**Records are per commit and per configuration.** A record is stored under the commit's SHA and
the SHA-256 of the configuration it ran under, with the commit the configuration came from.
`gate verify` accepts a passing record made under main's current configuration or under the
configuration committed in the commit itself (the one in force from that commit on, which the
queue also gates a configuration change under). After a legitimate configuration change, main
still verifies; a record made under some other configuration does not count.

**Branches by full name.** In git a short name such as `main` resolves to a tag before a
branch. A tag called `main` pointing at a gated commit would make an ungated branch look gated,
and a tag called `lane/x` would be merged instead of the branch. Internally every branch is
`refs/heads/...`; names typed by a person or an agent try the branch first. Refs that could be
read as git options are refused.

**Built-in checks on the candidate.** The spec format and the decision-log numbering are checked
on the commit's own files in every gate run, before the configured steps. Two lanes that each add
decision 0002 pass on their own; the second candidate (main already has the first 0002) fails.
The directories come from the configuration and must be relative paths inside the repository;
symlinks that lead outside are refused.

**Compare-and-swap on main.** Gating takes time, and someone may push in between. The queue
updates main only if it still points at the commit the candidate was built on (`git update-ref
<ref> <new> <old>`, or a fast-forward-only merge in the worktree that has main checked out).
Otherwise the lane goes back to the front and is rebuilt; after three races in one run the run
stops rather than spin.

**Gate steps run untrusted code; limit what they inherit.** A step runs the change's own tests
and build files. Steps get a minimal environment (`PATH`, locale, a temporary `HOME`, and the
variables the configuration lists), never the caller's full environment with its tokens. All
refs are snapshotted before the steps and compared after each one; a step that moves a branch or
a tag fails the gate. The starter command runs Python with `-I`, so a file in the change cannot
replace the test runner module. None of this is a sandbox: a step can still read what the
operator's user can read, write into `.git`, or report success without testing anything. The
queue should run in a disposable environment, and CI on protected infrastructure stays the
authority. Before moving main, the queue checks that main's tip is the commit it built on and
that this tip has a passing record (or is the first commit); otherwise the run stops.

**Lane errors and operator errors are different.** A lane that conflicts, fails its gate or
causes an unexpected error is rejected with the reason and removed from the queue, so one bad
lane cannot wedge it (step output is decoded leniently for the same reason). A problem on the
operator's side (main's tip not gated, main's working tree with uncommitted or untracked files)
stops the run and keeps the lane queued. Candidate merges are made with `commit.gpgsign=false`:
they are the tool's commits, and a signing setup that needs a person would fail every merge.

**Fail closed everywhere.** No steps configured: fail. Timeout: fail (output and errors kept).
Program not found: fail (exit 127). Steps run as argv lists without a shell.

**State in `.git/agent-gates/`, not in the tree.** Gate records, lanes and the queue are local
facts about this clone. Putting them in the tree would make every gate run a commit, and a
commit cannot contain a record about itself. The shared `.git` directory is visible to every
worktree. Writes are atomic (temp file + rename) and serialised with a lock; the queue runner
holds its own lock so two runs cannot interleave.

**The MCP server is read-mostly.** Agents can read specs, gate status, the queue, lanes and
decisions, and propose a decision (written as `proposed`; a person accepts it). They cannot run
the gate, add to the queue or move branches through MCP: those are actions with side effects
that a person or a pipeline triggers. The repository is fixed at server start and no tool takes
a path.

**Specs and decisions are checked for structure only.** A checklist of acceptance criteria can
be ticked off and reviewed; prose cannot. Whether the criteria are good is a human judgement.
Decision numbers run without gaps and are never reused, and accepted records are superseded
rather than edited, so links to them stay valid.

**The CI template gates the exact commit too.** On `merge_group` it checks out the queue's
candidate (the commit main will move to); on `pull_request` the head SHA for early feedback, not
GitHub's synthetic merge ref. It writes the SHA it tested to the job summary. A pull request can
edit its own workflow and Makefile, so the checks only protect main when they are required by
protected configuration (branch protection, rulesets, required workflows) together with the
merge queue. Third-party code runs only from SHA-pinned actions or from a release binary whose
SHA-256 is checked. Permissions default to read-only.

**Removing a lane is safe by default.** `lane done` deletes a lane's branch only when
`refs/heads/main` contains its tip (a tag of the same name does not count), its worktree has no
uncommitted or untracked files, and it is not waiting in the queue. The branch is deleted with a
compare-and-swap on the tip it checked, and removal runs from the main worktree, so it also
works when called from inside the lane. `--force` exists on the command line only, for lanes
that are abandoned on purpose. `queue run --cleanup` applies the same rules right after a merge
and records "lane kept: <reason>" instead of failing the run.

**One status view, deterministic.** `status` collects main, specs, lanes, the queue and the
decision log into one structure; the CLI renders it as Markdown (or JSON) and the MCP tool
`repo_status` returns the structure and the same Markdown. No timestamps, no absolute paths,
sorted rows and fixed-length SHAs: the same repository state always prints the same text, so
the output can be diffed, pasted into a review, or compared in a test.

## Known gaps

- Gate records are not signed, and gate steps can write into `.git`. Anyone (or any step) with
  write access to `.git` can forge a record; on a shared repository, CI on protected
  infrastructure is the authority and this tool is the local mirror of it.
- SHA-256 object-format repositories are refused with a clear message (not supported yet).
- One candidate at a time. Batching or speculative parallel candidates would cut queue time for
  busy repositories.
