# skbloom — AI-first sovereign installer

> *Kubefirst's day-1 platform + Coolify's one-click + a small self-hosted LLM concierge —
> fully open, air-gappable, adapter-swappable. `skbloom up` → a wired sovereign stack.*

## The spine: a resumable named-step state machine
The installer is an ordered list of **named, idempotent steps** (`steps.py`). Each writes
a completion flag to `~/.skbloom/steps.json`, so an interrupted/failed run **resumes
exactly where it stopped**, and already-satisfied steps (`check()`) are skipped. This is
Kubefirst's `provisionWatcher` pattern, but the steps are deterministic Python our engine
owns — the LLM narrates + repairs *by step name* and never touches the cluster directly.

## The flow (`flow.py`)
```
preflight → bootstrap-cluster (k3d) → install-eso → secret-backend → deploy:<svc>… → final-check
```
`deploy:<svc>` renders each `app.yaml` with **skrender** and applies it — so skbloom
inherits descriptor→deploy parity (the harness already proves these manifests run).

## Use it
```bash
skbloom plan                       # show the named-step plan
skbloom up                         # run it (resumable, idempotent, narrated)
skbloom resume                     # continue after a failure
skbloom status                     # done / pending
skbloom up --services cloud/skfence,core/sksso   # pick the service set
```

## Design & next increments
Full design: [`../docs/skbloom-design-proposal.md`](../docs/skbloom-design-proposal.md).
Shipped: the state-machine spine + sovereign flow + CLI (deterministic, tested).
Next: the **LLM concierge** (gather intent → emit a validated profile, reusing skwire's
model ladder), the **web app-store UI**, ArgoCD app-of-apps + GitOps repo generation,
and `skca`/mkcert local TLS. The model only ever produces the *what* (a profile); this
engine owns the *how*.
