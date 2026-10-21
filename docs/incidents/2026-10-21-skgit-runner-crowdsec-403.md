# 2026-10-21: skgit runners and API clients get HTTP 403 from the CrowdSec bouncer

Instance: Nor estate, `skgit.skstack01.douno.it` (stack `skgit-prod`, security stack `sksec-prod`).
Tracking card: `5c0a000b` (chi coordination board). Live fix by Jarvis, recorded here by Lumina.

## Symptom

- Forgejo Actions runners (sklegal-ziowk01, sklegal-chiap01, sklegal-chiap03) logged
  `permission_denied: 403 Forbidden` on FetchTask, ReportState/UpdateTask and ReportLog/UpdateLog.
  On ziowk01, 55 such lines after the 2026-10-20 19:08 CDT reboot; before the fix, 54 in one hour
  (32 fetch, 14 ReportState, 8 ReportLog) while only 10 tasks were fetched.
- Token-authenticated REST calls from the chi cluster (`/api/v1/...`) failed the same way at random,
  and every API call took about 4.5 s. Fresh `git clone` over HTTPS dropped with
  "unexpected disconnect while reading sideband packet". SSO static files were affected too.
- The runner registration and tokens were valid throughout. HTTP/1.1 and HTTP/2 both reproduced.

## Cause

The general Forgejo router `skgit` runs `crowdsec-bouncer@file,default-no-crowdsec@file`
(set through `skgit.TRAEFIK_MIDDLEWARES` in the instance vars, the per-router knob from #160).
`crowdsec-bouncer@file` is a Traefik forwardAuth to `sksec-prod_bouncer-traefik`
(fbonalair/traefik-crowdsec-bouncer). Both bouncer replicas (norap1001, norap1021) return 403 after
about 5000 ms when their request to `http://sksec-prod_crowdsec:8080/v1/decisions` times out, so a
slow CrowdSec LAPI makes the bouncer fail closed for clean, authenticated clients.
CrowdSec logs show decision requests sometimes taking several seconds. **Why the LAPI is slow is
not established**; that stays open.

## Live mitigation (2026-10-21 11:36:20 CDT, Jarvis)

Through Portainer endpoint 6 (skstack01.douno.it), service-level labels on `skgit-prod_forgejo`
(service id `5k9slgihwqvqod993lqa93jw3`, resulting version 4209566). Only these eight labels were
added; the general router, TaskTemplate, environment and credentials were unchanged, and Forgejo was
not restarted:

```
traefik.http.routers.skgit-runner-rpc.rule=Host(`skgit.skstack01.douno.it`) && PathPrefix(`/api/actions/runner.v1.RunnerService/`)
traefik.http.routers.skgit-runner-rpc.entrypoints=websecure
traefik.http.routers.skgit-runner-rpc.service=skgit
traefik.http.routers.skgit-runner-rpc.priority=200
traefik.http.routers.skgit-runner-rpc.middlewares=default-no-crowdsec@file
traefik.http.routers.skgit-runner-rpc.tls=true
traefik.http.routers.skgit-runner-rpc.tls.certresolver=main
traefik.http.routers.skgit-runner-rpc.tls.options=intermediate@file
```

Only runner RPC skips the CrowdSec forward-auth. Forgejo still authenticates every runner call with
the runner token; an unauthenticated FetchTask still returns 401.

## Verification

- After the change, 60/60 authenticated read-only UpdateLog requests from ziowk01 returned 200
  (expected ackIndex 3273 for task 6023), maximum 1808 ms, finished 11:38:03 CDT.
- Unauthenticated FetchTask: 401. The runner journal recorded zero new 403s after the change.
- Real job after the fix: SKLegal workflow run 2380, static job, task 6246, runner sklegal-ziowk01 v11.1.2.

## Permanent fix (this change)

`v1/ansible/optional/skgit/src/config/skgit/skgit.yml.j2` now declares the same router for every
instance (`skgit[-env]-runner-rpc`, priority 200, beside the existing registry router). Its middleware
is `skgit.RUNNER_RPC_MIDDLEWARES`, default `default-no-crowdsec@file`. Rendered for prod with
`skgit.skstack01.douno.it` it is byte-identical to the eight live labels above, so the next
source-driven redeploy keeps the fix instead of overwriting it. On an instance whose general router
does not use CrowdSec the new router is a no-op.

## Rollback

Live: re-read the current service spec and version, remove only the eight
`traefik.http.routers.skgit-runner-rpc.*` labels, submit a version-checked service update. Source:
revert this commit, or set `skgit.RUNNER_RPC_MIDDLEWARES` to the general router's chain. Either
restores the bouncer on runner RPC and may bring the runner 403s back.

## Still open

1. **CrowdSec LAPI latency** (root cause). Until it is fixed, every other route behind
   `crowdsec-bouncer@file` keeps failing closed at random, including skgit's token-authenticated
   REST API and HTTPS clones used by the chi agents.
2. **Fail-closed on timeout.** The bouncer turns an LAPI timeout into a 403. Options to evaluate:
   a bouncer that keeps a local decision cache (stream mode), a longer bouncer timeout, or more LAPI
   capacity. Each trades availability against how fresh ban decisions are.
3. **Instance vars.** Where `skgit.TRAEFIK_MIDDLEWARES` adds `crowdsec-bouncer@file` for prod is in
   the encrypted vault vars, not visible in plain source; record it when the vault is next opened.
