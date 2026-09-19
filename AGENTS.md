# AGENTS.md

NON-NEGOTIABLE ENGINEERING RULES:

- Never modify main directly — always work on a branch or worktree.
- Always inspect existing code/architecture before changing it.
- Never delete functionality without explicit approval.
- Run tests after every meaningful change.
- Never expose secrets; never commit .env files.
- Prefer existing project patterns over introducing new ones.
- Keep changes focused — one task, one branch, one reviewable diff.
- Explain destructive operations before performing them.
- Do not claim a task is complete without running verification (tests/build).
- Respect the approval tiers in approval.py — SAFE tools auto-run, MODERATE tools need approval unless the session is explicitly trusted, DANGEROUS tools always stop and ask.
