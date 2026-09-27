# Testing SKStacks

This describes how the framework itself is tested before a release, and
how an instance (a private repo that pins `framework/` as a submodule and
supplies real `group_vars`/vaults for its own cluster) should validate that
a release actually works before adopting it.

## Framework test gates (run on every change)

From the repo root:

```
python3 -m pytest -q v1/tests v2/platform/swarm/tests v2/platform/k3d/tests v2/tests
```

This renders every service's Ansible templates and compose files through
real Jinja with representative variables, with no VM and no live cluster,
and asserts on the result. It includes, among others:

- **Compose-schema lint** - every rendered compose file must parse as valid
  YAML matching the Compose spec.
- **No floating image tags** - every image reference must be a pinned tag
  or digest, never `:latest` or another moving target. A floating tag means
  a redeploy months later can pull a different image than the one that was
  actually tested.
- **No secrets in argv** - no compose `command:`/`entrypoint:` may carry a
  password, token, or key as a literal argument. Anything with swarm/API
  read access can see a container's command line
  (`docker service inspect`, `docker top`); secrets belong in a mounted
  config file, an environment variable read at process start, or a value
  piped over stdin at deploy time, never in the argument list itself.
- **Runtime templates copied, not templated** - a handful of services
  template a config file themselves at runtime (a Python server doing its
  own string substitution on an HTML file, for example). Deploying that
  file with Ansible's `template:` module instead of `copy:` double-renders
  Jinja delimiters the application expects to see literally, and breaks it
  silently. Any file like that must be deployed with `copy:`.
- Structural checks that catch a whole bug class at once: unique subnets
  across every optional stack's overlay network (a subnet collision
  silently breaks both services), every declared bind mount actually
  getting created before the container starts, `null` where a boolean
  toggle turned off an optional block (Jinja can render `null` for an
  empty conditional list/mapping when the intent was an empty list/mapping,
  and that difference matters to YAML consumers downstream).

Add a new regression test in the matching `tests/` directory whenever a
real bug is found and fixed; a bug that reappears without a test to catch
it wastes the debugging effort spent finding it the first time.

## Security gate (run on every push)

The framework ships a self-hardening security scanner, `skred`
(`v2/skred/`), combining OSS scanners (gitleaks, trivy, semgrep) behind one
scope-locked orchestrator that fails CI on HIGH+ findings:

```
cd v2 && PYTHONPATH=. python -m skred.cli gate . --fail-on high
```

An instance repo (which holds real, estate-specific secrets and identifiers
that must never reach the public framework) additionally runs a denylist
gate against its own instance-specific value list before anything from
that repo is exported, published, or merged toward this framework:

```
PYTHONPATH=<framework>/v2 python3 -m skred.denylist --denylist <your-denylist-file> <export-root>
```

Exit 0 means clean, exit 1 means findings, exit 2 means a usage or
environment error. Keep the denylist file itself outside of any repo that
gets published; only the gate's presence (and its 0/1/2 exit convention) is
part of the framework contract, never the list of values it checks.

Both gates run on a nightly schedule as well as on every push, because a
history scan can surface a finding from a commit made long before the gate
existed.

## How an instance should validate a release

The framework's own tests prove the *templates* are correct in isolation.
They cannot prove a real cluster with a real vault actually converges,
because that depends on values only the instance has. Before adopting a
new framework release (or even before merging a change that touches a
service you run), an instance should run its own validation in three
layers, cheapest first:

### 1. Render against your real vault, no deploy

Render every service's templates through Ansible with your instance's
actual `group_vars`/vault values, without provisioning anything or
touching a live target. This catches the class of bug the framework's own
tests cannot: a value that is empty, a dict key that collides with a
Jinja/Python attribute, a conditional that only misbehaves with your
specific combination of settings. Keep this render outside of any
long-lived path (a `mktemp -d`, cleaned up after) and make sure it never
prints secret values, only file paths and key names, so its output is safe
to share when reporting a finding.

### 2. A second, disposable instance (VM/cluster) test

Stand up a small, throwaway cluster (a handful of VMs is enough: a few
managers plus one worker reproduces swarm's manager/worker split without
needing production-scale hardware) that runs the framework end to end:
provision, bootstrap the swarm, deploy each service you care about with a
disposable dev vault, and check each one live (the service actually
converges, and a real protocol-level check succeeds - an HTTP health
endpoint, a database round trip, a login - not just "the container
started"). Tear it down automatically on both success and failure, unless
you are actively debugging a failure and want to keep it up for
inspection.

This is the only layer that catches a bug that only appears with a real
Docker Swarm scheduler making real placement decisions, a real service
converging at real startup speed, or a real interaction between two
services on the same overlay network. Run it against a candidate branch,
and again against the exact tagged commit before you actually adopt the
tag: what gets tagged can differ from the last thing you tested (a
changelog edit, a fix folded in right before release), so testing the
branch is not the same as testing the tag.

### 3. Compare against what is actually running live

Before rolling a new release into a production cluster, diff what the new
release would render (image versions, mount paths, feature flags, auth
settings) against what your production service is actually running right
now - not against another private copy of your own templates, which can
itself have already drifted from what is live. A render that only ever
gets compared to another private template can look "verified" while
missing a real difference: a newer image pin than what is actually
deployed, a mount layout that does not match your real data directories, a
default that assumes a feature is off when your live service has it on.
Confirm parity against the live, running artifact, then roll the change.

## Failure triage

When a live cluster test fails, `docker service ps --no-trunc <service>`
is the first thing to check, and its `ERROR` column tells you which kind
of failure you have:

- **`Rejected: ...`** means the scheduler never got the task running at
  all: a bind-mount source that does not exist on the target node, an
  image that failed to pull, or a placement constraint no node satisfies.
  Check `docker node ls --format '{{.Hostname}} {{.Labels}}'` against the
  compose file's constraints, and watch specifically for a constraint list
  that rendered `null` because whatever toggle controls it was off.
- **`Failed: task: non-zero exit (N)`** means the container started and
  then died; the scheduler will keep retrying and this line alone will not
  tell you why. Go read the actual logs
  (`docker service logs <service>`, or `docker logs <container>` on
  whichever node the task landed on).
- **A node that is simply unreachable** (ssh times out, tasks never
  schedule there) usually means the VM/host itself is the problem, not the
  framework: check the hypervisor's own view of that node's state before
  assuming a template bug. A pure infrastructure flake (a node dropping
  off the network for a few seconds mid-run) can look identical to a real
  bug until you check that layer.

## Agent/automation notes

If you dispatch automated agents against this pipeline:

- Commit as soon as a fix passes the fast static gates, before kicking off
  a long VM test; do not let a turn end with work sitting uncommitted while
  a long-running verification is still in flight.
- Give every parallel task its own disposable environment (its own git
  worktree, its own subnet range if it deploys network-attached services)
  so two agents working at once cannot collide.
- Never let a check print a secret value to prove a match; compare a hash
  or a boolean pass/fail instead.
- Treat "the render matches" claims skeptically until verified against the
  actual live artifact, not just another rendered copy.
