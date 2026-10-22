# Pipeline development

## Publication boundary

CRAP's pipeline source is public. All LeanEval problem work is private: full or
partial proofs, helper lemmas, problem-specific research, DAGs, notes, logs,
results, and handoff artifacts. Never push, post, attach, or submit that work to
GitHub, the leaderboard, public issues, releases, or any other public destination.
User authorization to publish pipeline changes does not authorize publishing
problem work. Only a new explicit user instruction can change this boundary.

Aristotle delegation is authorized only for the minimum lemma and prerequisite
context needed for that task. Do not send an entire campaign or unrelated notes.
Public tests and demos must use synthetic fixtures. Run
`python scripts/check_public.py` before publishing pipeline changes. Keep the
pre-push hook enabled; do not bypass it to publish restricted artifacts.

This repository orchestrates research; a successful mock test is not a Lean proof.
Keep simulated artifacts explicitly labeled. Never weaken the official acceptance
gate or promote an agent/remote-service success message to a proof verdict.

Preserve per-problem deadlines across restarts. Do not automatically resubmit a
remote request whose outcome is unknown. Keep benchmark and tool pins immutable.

Use `python -m unittest discover -s tests -v` after changing orchestration,
verification, persistence, or process handling. Use Python 3.11 or newer.
Server preflight is a separate live Linux check and must be reported separately.

Do not start paid or long-running proof campaigns merely to test pipeline code.
The user will choose when to begin problem solving on the server.
