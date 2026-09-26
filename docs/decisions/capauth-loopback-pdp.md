# Decision: capauth is a per-node loopback PDP; the standalone verification-service frontend is deprecated

**Status: DECIDED by Chef (operator), 2026-09-26.**

## Problem

PR #39 removed a fabricated `deploy:` block from `v2/core/capauth/app.yaml`
(wrong port, an image nothing in the fleet runs) and left an open question:
does `capauth` in the v2 service catalog mean the loopback authz PDP that
actually runs in production today, or the separate standalone
verification-service (challenge/verify/callback endpoints for passwordless
PGP OIDC login) that CapAuth's own SOP also documents? Those are two
different services sharing one descriptor name, and the catalog could not
honestly describe HA for either shape until this was settled.

## Decision

Chef: "capauth should just be run on localhost like it's currently set up,
deprecate the frontend service."

- `capauth` in the v2 catalog means the loopback-only authz PDP:
  `capauth-service --host 127.0.0.1 --port 8420`, one instance per node
  running a PEP (skgateway's authz enforce), token-gated
  `POST /v1/authz/decide`. It is a per-node systemd service, not a
  Swarm/K8s stack. There is no `deploy:` block by design, and none should be
  added for this shape.
- HA for this descriptor means "every node that needs a PDP runs its own
  instance", not "N replicas of one exposed, load-balanced service" (the
  shape `min_replicas` implies for, say, skvault's Raft cluster or a Swarm
  service). There is no shared ingress and no clustering between PDP
  instances by design: a PDP instance is a same-node dependency of its own
  gateway, not a fleet-shared resource.
- The standalone public verification-service mode (challenge/verify/callback
  endpoints for passwordless PGP OIDC login, the `ghcr.io/smilintux/capauth`
  image, previously run as the now-decommissioned `capauth-prod` stack) is
  **DEPRECATED as of 2026-09-26**. It carried zero real traffic in the 7 days
  before decommission and no Forgejo/Nextcloud/Immich integration was ever
  wired up to it. PGP passwordless login for apps is now covered by
  `ghcr.io/smilintux/authentik-capauth` (an Authentik stage, via sksso),
  which is actively used and published.

## What changes in this repo

- `v2/core/capauth/app.yaml`: comments state the decision plainly; still no
  `deploy:` block (descriptor-only, status stays 📋). `ha: true` and
  `min_replicas: 3` are kept, reinterpreted as "at least 3 independent
  per-node PDP instances across the fleet" rather than clustered replicas.
  The healthcheck path is corrected to the service's real self-report route
  (`/capauth/v1/status`) instead of a nonexistent `/healthz`.
- `v2/docs/services/capauth.md`: the "Open design question" section is
  replaced with a "Decision" section pointing here; the standalone
  verification-service shape (Scenario B) is marked DEPRECATED with the
  reason and the replacement.
- `v2/tests/test_redundancy_descriptors.py`: **unchanged**. Its assertion
  (`ha is True`, `min_replicas >= 2`) is a generic floor ("if you need one,
  get two") that does not itself assume a clustered or exposed-replica
  shape; it holds equally under the new per-node-instance reading of
  `min_replicas`. The old fabricated-deploy-block assumption lived only in
  the removed `deploy:` block and the port/image it named, not in this test,
  so there is nothing in the test to correct.

## Consequences

- Anyone filling in a `deploy:` block for `capauth` in the future must build
  it as a per-node or host-scoped unit (an Ansible role or a host-plugin
  pattern), not a Swarm/K8s service definition with `replicas:`. Those two
  shapes render incompatible topologies for this descriptor.
- The CapAuth repo's own README/SOP separately marks the standalone
  verification-service deployment path as deprecated, with the same reason
  and replacement recorded here.
