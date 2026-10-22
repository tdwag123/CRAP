# CRAP: Can't Reasonably Articulate Proofs

**Can't Reasonably Articulate Proofs** is a persistent supervisor around Codex CLI for sustained mathematical research and
Lean formalization. Each selected problem gets a 48-hour wall-clock deadline, a
natural-language argument, a dependency DAG, Git checkpoints, and independent
benchmark verification. Suitable lemmas can be delegated through Aristotle MCP.

**Implementation status:** the orchestration can be exercised locally without
external services. Live proof acceptance requires the Linux verifier preflight.
Live Codex and Aristotle runs require your configured accounts. A simulated test
result is never evidence of a mathematical proof.

## Workflow

1. Pin the benchmark commit and prepare its exact Lean/Mathlib dependencies.
2. Interpret the actual Lean statement and search primary mathematical sources.
3. Dissect a sourced proof, or develop an original argument if none was found.
4. Build `research/dag.json`, including missing definitions and library lemmas.
5. Codex works on difficult nodes and prepares self-contained Aristotle tasks.
6. The supervisor submits eligible tasks asynchronously and stores remote IDs.
7. Codex integrates returned candidates. The supervisor checks lemma types and
   axiom dependencies in a freshly reconstructed workspace.
8. A complete candidate is checked with the upstream `lake test`, including the
   comparator and independent nanoda kernel. Only this result marks a problem solved.
9. At expiry, preserve the argument, remaining gaps, source, job IDs, and history
   in a handoff, then continue with the next queued problem.

## Local development

Python 3.11+ and Git are required. The Python package has no runtime dependencies.
From this directory, all commands also work as `python -m crap`; installation
of the console entry point is optional.

```bash
python -m unittest discover -s tests -v
python scripts/demo.py --output .test-artifacts/demo
python -m crap init
python -m crap doctor
```

The demo runs real subprocess and MCP plumbing against fake services and a
synthetic benchmark, persists/reopens its database, and writes a clearly labeled
`SIMULATED-REPORT.json`. Use a new output directory for each demo.

On this Windows workstation, use the installed Python executable if `python` or
`py` resolves to a broken Windows Store alias:

```powershell
& "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe" -m unittest discover -s tests -v
```

## Linux/server setup

Install Python 3.11+, Git, Bash, curl, Codex CLI, elan/Lean, Go, Rust/Cargo, and the
native build tools required by those toolchains. Choose resource limits appropriate
to the server. Keep the SQLite state directory on a local filesystem with working
file locks; use a separate backup mechanism for durable server storage.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
source .venv/bin/activate
crap init
```

Edit `pipeline.local.json`. Paths are relative to the configuration file. Set a
model available to your Codex account in `codex.model`, or leave it null to use
your configured CLI default. Authenticate Codex interactively on the server using
its supported login flow; credentials do not belong in this repository.

```bash
crap bootstrap --install-tools
crap doctor
crap preflight
crap catalog
crap add two_plus_two
crap prepare two_plus_two
crap run --problem two_plus_two --once
crap status
```

`bootstrap` fetches the immutable benchmark commit. `--install-tools` builds the
four pinned verifier tools outside the solver workspace. Their source pins are
checked against the selected benchmark's `SECURITY.md`. It does not install OS
packages, Python, Codex, elan, Go, or Rust for you. Tool paths are activated by
the pipeline automatically. If a build is interrupted, inspect `.crap/tools/logs/`
and rerun after correcting the installation problem.

`preflight` runs the upstream comparator installation check, the upstream sandbox
engagement probe, rejects the unfinished `two_plus_two` starter, and accepts a
known valid proof. It records the benchmark pin and executable fingerprints.
`run` refuses to start without a matching preflight receipt. Changes to the
benchmark or verifier executables require another preflight. This is intentionally
a live Linux check, not something the offline test suite pretends to perform.

After the smoke problem, add selected substantive problems and run the queue:

```bash
crap add PROBLEM_ID
crap prepare PROBLEM_ID
crap run
```

The supervisor concentrates on one problem at a time. Codex has one active turn;
up to two remote Aristotle tasks may be outstanding. `deploy/` contains optional
systemd-user and Slurm examples. Adapt them when the amath server's environment
is known; neither is installed automatically.

## Aristotle MCP

The adapter uses a **stdio MCP server** with the `prove` and `check_proof`
contracts described in the [Aristotle MCP wrapper](https://github.com/septract/lean-aristotle-mcp).
It initializes MCP, discovers tool schemas, submits with `wait=false`, and polls
by `project_id`. Configure a working, version-pinned MCP server command; no
unverified wrapper version is downloaded or installed automatically.

```json
{
  "aristotle": {
    "enabled": true,
    "command": ["/absolute/path/to/aristotle-mcp"],
    "env_names": ["ARISTOTLE_API_KEY"],
    "max_pending": 2,
    "max_jobs_per_problem": 30,
    "max_attempts_per_node": 2,
    "poll_seconds": 120
  }
}
```

Merge those fields into your local configuration. The key is read from the named
environment variable; never put its value in configuration, prompts, or source.
`crap aristotle probe` validates the MCP handshake and required tool schemas.
It does **not** submit a proof or establish that the remote service accepts your
credentials or supports the benchmark's toolchain. Confirm those with a small
real delegation before committing to a long run. Service availability and wrapper
compatibility are deployment checks, not assumed capabilities.

Only nodes with checked prerequisites, exact Lean interfaces, and `delegate=true`
are submitted. Results are saved under `research/aristotle/JOB_ID/`; they are never
automatically substituted into source. Codex adapts them to the pinned toolchain.
The context includes the transitive DAG prerequisites, not arbitrary repository files.
Polling occurs between bounded Codex turns, no more frequently than `poll_seconds`.

If a submission response is lost, the job becomes `unknown`; the pipeline never
blindly retries it. Reconcile it with the service and then use:

```bash
crap aristotle jobs
crap aristotle attach LOCAL_JOB_ID REMOTE_PROJECT_ID
```

If you have established that it should not be tracked, use
`crap aristotle abandon LOCAL_JOB_ID`. This does not cancel a remote job.
The supported MCP contract has no cancellation method: at problem expiry the
pipeline stops submitting and polling, but an already-submitted remote task may
continue. Job and concurrency caps bound submissions; they are not a currency
spending cap. Set service-side limits appropriate to your account.

## DAG and research records

Each worker has `AGENTS.md` with the research protocol and DAG schema. Key files:

| File | Contents |
|---|---|
| `research/statement.md` | Exact mathematical interpretation and hypotheses |
| `research/search.md` | Search queries, inspected sources, and negative findings |
| `research/sources.json` | URLs, theorem/page references, and relevance |
| `research/proof.md` | Dissected or original argument, with explicit gaps |
| `research/dag.json` | Definitions, lemmas, dependencies, interfaces, delegation requests |
| `research/node-status.json` | Supervisor-generated verification states |
| `research/progress.md` | Current obstacle, failed approaches, and next steps |

Nodes may start with only mathematical statements. Before a check or delegation,
provide `lean_file`, fully qualified `declaration`, and its complete `lean_type`.
Use explicit parameters and globally qualified names; write universe metavariables
such as `Type _` when necessary. `check=true` requests a fresh type/axiom audit;
`owner="aristotle", delegate=true` requests remote work once dependencies are checked.

The DAG validator rejects duplicate IDs, absent dependencies, and cycles. Agent
supplied success labels are ignored. A changed interface invalidates descendants;
any changed submission source conservatively invalidates all lemma checks. This
may recheck more than necessary but cannot silently keep stale proof results.

A local lemma check uses a typed application of the named declaration and
`#print axioms`. It rejects unexpected axioms, including `sorryAx` and
`Lean.ofReduceBool`. These are useful development checks, **not** independent
benchmark certificates. A conditional or failed lemma stays unfinished. Only the
final comparator/nanoda run accepts the overall result.

## Budgets, recovery, and controls

Defaults are 48 hours per problem, at most two initial hours for literature search,
30 minutes per Codex turn, and a four-hour final integration reserve. Research can
finish early when a documented proof plan and DAG exist. The supervisor enforces
time boundaries; an agent's promise to stop is not the enforcement mechanism.

The clock begins immediately before the first research turn, after dependency
preparation. Queue waiting before this does not count. Once started, elapsed time
includes service waits, interruptions, machine downtime, and user pauses. Resuming,
restarting, or changing config never grants a new deadline. Expired problems are
archived; they cannot silently be requeued with a fresh budget.

```bash
crap status
crap stop
crap pause PROBLEM_ID
crap resume PROBLEM_ID
crap resume PROBLEM_ID --fresh-session
crap run
crap handoff PROBLEM_ID
```

Stop/pause requests are checked while Codex and local verification commands run.
An in-flight MCP request is bounded by its RPC timeout. `resume --fresh-session`
recovers from unavailable Codex session history using durable code and notes.
Three consecutive infrastructure/orchestration errors mark a problem
`needs_attention`; repair the cause and resume it within its original deadline.
Proof rejection and unresolved mathematics are not automatically infrastructure errors.

SQLite stores problems, deadlines, attempts, node verification, remote jobs, and
events. Git stores source/research checkpoints. A process lock prevents two
supervisors from writing the same state concurrently. Codex JSONL events, session
IDs, and reported token usage are retained per attempt. SIGTERM/Ctrl-C interrupts
the child process group and leaves recoverable state. A Linux process guard also
terminates the worker group if its supervisor dies abruptly. The production runtime is
Linux; Windows supports local orchestration tests and inspection.

## Verification and storage boundaries

Final verification extracts trusted files from the pinned Git object, ignoring
changes to the solver's challenge, bridge, build configuration, or dependencies.
Only `Submission.lean` and `.lean` files under `Submission/` are copied. Source
symlinks and paths escaping the workspace are rejected. Candidate hashes, source
snapshots, logs, tool provenance and verdicts remain under `.crap/checks/`.

Each verification uses fresh dependency resolution and a fresh build directory;
upstream downloaded artifact caches can be reused, but worker build products are
never accepted as verification evidence. The pinned TauCeti cache helper is run
for projects requiring its imported modules. Repeated setup has a performance and
disk cost; measure it before increasing concurrency.

Codex runs with workspace-write permissions and unattended approval policy;
unsupported actions fail rather than waiting for a prompt. This supervisor is
for cooperative research agents. Its state database, local Git repository, and
lemma audits are not an OS security boundary against a malicious same-user
process. The official final comparator supplies the benchmark's submission
sandbox; use account/container isolation appropriate to the shared server.

No results are pushed or submitted to the leaderboard. Accepted source and a
handoff are saved for inspection.

## References

- [Lean Eval solver workflow](https://github.com/leanprover/lean-eval#quick-start-for-solvers)
- [Lean Eval trust model and verifier pins](https://github.com/leanprover/lean-eval/blob/main/SECURITY.md)
- [Codex non-interactive execution](https://learn.chatgpt.com/docs/non-interactive-mode)
- [Aristotle MCP tool contracts](https://github.com/septract/lean-aristotle-mcp/blob/main/src/aristotle_mcp/server.py)
- [MCP stdio transport](https://modelcontextprotocol.io/specification/2025-03-26/basic/transports)
