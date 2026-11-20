# Contributing to SKStacks

SKStacks is the public framework: roles, playbooks and modules that private instance repos pin.
Anything in this repo is public, so commits, PRs, tests and docs carry no hostnames, IPs, estate
names or secrets ([AGENTS.md](./AGENTS.md) has the full rule set for humans and agents alike).

## Branches and commits

- Never commit to `main`. Branch from a fresh `origin/main` (`fix/...`, `feat/...`, `docs/...`,
  `ci/...`, `chore/...`), commit early and push before long test runs.
- Conventional commit subjects (`fix(skgit): ...`). A `Co-Authored-By` trailer names the agent that
  actually did the work, or is left out.
- No em or en dashes anywhere: commas, parentheses, colons or a new sentence instead.

## Tests

Every behavior change arrives with tests that fail without it ([docs/testing.md](./docs/testing.md)):

```bash
python -m pytest -q v1/tests v2/platform/swarm/tests v2/platform/k3d/tests   # framework tests
python v1/tests/render/render_playbooks.py                                    # render gate
python -m pytest -q tools                                                      # repo tooling
```

A full release is gated separately: a fresh full run of the skstack06 test cluster against the
exact commit that gets tagged (the release flow in [AGENTS.md](./AGENTS.md)).

## CHANGELOG

Every behavior change adds a bullet with an **Instance action** line to the topmost section of
[CHANGELOG.md](./CHANGELOG.md): `## Unreleased`, or the release being cut. Never add under a release
that is already tagged; after a tag, start a fresh `## Unreleased` on top. The `changelog
placement` check (`tools/changelog_placement.py`) fails a PR that adds lines anywhere else. Moving
or deleting old lines is allowed.

## Merging: the merge queue

`main` is protected by a merge queue, squash only.

1. Open the PR and wait for its checks. Read every check, not only the required one: `scan` is
   required; the framework tests are advisory but a red one is not merged.
2. Queue it: `gh pr merge <N> --squash` (or "Merge when ready" in the UI). Do not pass `--admin`;
   that bypasses the queue.
3. The queue builds a merge commit on top of `main` plus every PR ahead of it and runs `scan` and
   the framework tests on that commit (`merge_group` event). Green, it lands; red, the PR leaves
   the queue and the ones behind it are re-tested without it.

Why: with "require branches to be up to date", every merge put every other open PR behind `main`,
and each needed a fresh full CI run (25 to 60 minutes) before it could merge, often more than once.
The queue tests each combined state once.

## Review

A PR from an agent is reviewed by someone other than its author: check `git log origin/main..HEAD`,
the diff, and the CI result yourself, never the author's summary alone.
