# Changelog

All notable changes to the SKStacks framework. Tags: `skstacks-vMAJOR.MINOR.PATCH`.
Each entry says what an instance must do, if anything.

## skstacks-v2.10.0 - 2026-09-24

- skred.denylist: a non-UTF-8 denylist exits 2; unreadable files are
  findings; FIFOs and sockets are skipped instead of hanging; UTF-16 text is
  searched.
- skred gate: exits 2 when every target is out of scope (nothing scanned)
  instead of passing.
- skmesh: cloudflared, netbird and newt Deployments run with a read-only root
  filesystem, no privilege escalation, minimal capabilities and the
  RuntimeDefault seccomp profile.
- INSTANCE-MODEL guide: status section brought up to date; clone steps
  re-disable framework push.

Instance action: netbird and newt now mount emptyDirs for their state
paths; if you patched these manifests, re-apply your changes on top.

## skstacks-v2.9.0 - 2026-09-24

First public release of the v2 framework.

- Framework + instance model: see `v2/docs/INSTANCE-MODEL.md`.
- skred scope defaults are loopback-only; declare yours with
  `SKRED_SCOPE_DOMAINS` / `SKRED_SCOPE_CIDRS`.
- `skred.denylist` estate gate in CI.

Instance action: none (first release). Pin `framework/` to this tag.
