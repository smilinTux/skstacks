# AGENTS.md: rules for AI agents working on the SKStacks framework

This repository is PUBLIC. These rules apply to every agent (Claude Code, Codex, pi, Hermes and others).

## Public-repo hygiene

- Never add site hostnames, domains, private or tailnet IPs, home-directory paths, people's names or
  machines, or live site config. Use placeholders (`example.com`, `site-a`, `192.0.2.x`). Site values live in
  each operator's private config repo (see `skstacks-config-template`).
- Never commit secrets or ansible-vault ciphertext. Secrets are read by single-key extraction, never printed.
- Check compound tokens too: a site name inside a longer identifier is still a leak.

## Branches and releases

- Work lands on `integration/vX.Y.Z` release lines, then a `release/vX.Y.Z` branch PRs into `main`.
- A PR's base branch MUST be the release line it targets. If the PR title or plan says `[target vX.Y.Z]`, the
  base is `integration/vX.Y.Z`; if that branch does not exist yet, leave the PR open and retarget later.
  Never merge into a different line than the stated target.
- A release is cut only after a full end-to-end harness run passes for the release pin. A stage that fails may
  be resumed only when it then passes with no code change (a glitch); otherwise fix it and rerun in full.

## Changes

- Tests first (pytest under `v1/tests`, render gate under `v1/tests/render`), at least happy path, edge case
  and failure per behavior. Never skip, weaken or delete an assertion to get a pass.
- Every behavior change gets a CHANGELOG bullet under the target release.
- Commit and push as soon as a fast check passes; run long suites after. Never end a turn waiting on a
  background job with uncommitted work.
- Writing: no em or en dashes anywhere (use commas, colons, parentheses or a period).
