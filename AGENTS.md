# Pipeline development

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
