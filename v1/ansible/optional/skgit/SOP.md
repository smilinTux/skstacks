# skgit: Standard Operating Procedures

Forgejo on the swarm with an Anubis crawler gate. What the role deploys and
every knob: [README.md](README.md). This file is the how-to.

## 1. Deploy or upgrade

1. Back up first (a major Forgejo upgrade migrates the database and
   repositories in place):
   ```sh
   # on the node running skgit-<env>_postgres
   docker exec $(docker ps -q -f name=skgit-<env>_postgres.) pg_dumpall -U forgejo | gzip > /var/data/backups/skgit-<env>-$(date +%Y%m%d%H%M).sql.gz
   tar -C /var/data/skgit-<env> -czf /var/data/backups/skgit-<env>-config-$(date +%Y%m%d%H%M).tgz config data/gitea
   ```
2. If anything was patched by hand in the live `app.ini`, compare it with
   the rendered template first (section 4). The deploy rewrites `app.ini`
   from `app.ini.j2` on every run; a hand patch the template does not carry
   is lost.
3. Run `deploy_skgit-<env>.yml` the standard way.
4. Verify (section 2). A major upgrade may stay `starting` for a long time
   while it migrates; the healthcheck allows `HEALTH_START_PERIOD` (60m).
   Watch `docker service logs -f skgit-<env>_forgejo`, do not restart it.

## 2. Verify after a deploy

| Check | How | Expect |
|---|---|---|
| web UI | open `https://skgit.<cluster>.<domain>/` in a browser | one Anubis check, then Forgejo |
| crawler gate | `curl -s -A 'Mozilla/5.0' $H/<owner>/<repo>/blame/branch/main/README.md \| grep -ci anubis` | non-zero |
| non-browser pass | `curl -s -o /dev/null -w '%{http_code}' $H/<owner>/<repo>/commits/branch/main` | `200` (or the login redirect for a private repo) |
| HTTPS clone | `git clone $H/<owner>/<repo>.git` | clones, never sees Anubis |
| SSH clone | `git clone ssh://git@skgit.<cluster>.<domain>:222/<owner>/<repo>.git` | clones |
| API | `curl -s $H/api/v1/version` | JSON version |
| registry | `curl -s -o /dev/null -w '%{http_code}' $H/v2/` | `401` (auth challenge) |
| runners | Site Administration, Actions, Runners: last seen within a minute | online |
| SSO | sign in with the sksso button | lands signed in |
| robots.txt | `curl -s $H/robots.txt \| head -3` | the role's file |
| git.config | `docker exec <forgejo> grep -A3 '\[pack\]' /data/forgejo/home/.gitconfig` | `threads = 1`, `windowmemory = 256m` |
| real client IPs | `docker service logs skgit-<env>_forgejo --since 5m \| grep -c 'from 172\.16\.'` | near zero |

## 3. The crawler gate

**Turn it off in an emergency** (Anubis broken, a client wrongly
challenged): set `skgit.ANUBIS_ENABLED: false` in the vault and redeploy.
The web UI routes straight to Forgejo again and the anubis service is
removed. Faster, without a deploy: point the main router back at Forgejo by
hand with `docker service update --label-add
'traefik.http.routers.skgit.service=skgit' skgit-prod_forgejo` (the next
deploy restores the configured state).

**See what it does.** `docker service logs skgit-<env>_anubis` logs every
challenge and deny with the rule that matched. Anubis also exports
Prometheus metrics on `:9090/metrics` inside the overlay network.

**Measure the crawler load** from the Traefik access log (JSON): count
requests per 30 minutes for the skgit router and its top client hosts and
user agents, before and after.
```sh
LOG=<traefik access log>
since=$(date -u -d '-30 min' +%Y-%m-%dT%H:%M)
jq -r --arg s "$since" 'select(.StartUTC >= $s and (.RouterName // "" | startswith("skgit"))) | .RouterName' "$LOG" | sort | uniq -c
jq -r --arg s "$since" 'select(.StartUTC >= $s and (.RouterName // "" | startswith("skgit"))) | .ClientHost' "$LOG" | sort | uniq -c | sort -rn | head
```
Requests answered by Anubis show up on the main router with a small body;
what reaches Forgejo shows up in Forgejo's router log with the real IP.

**A legitimate client is challenged** (a browser-UA integration, a feed
reader): give it a non-browser user agent, or move its path into the direct
router in `skgit.yml.j2` (with a test in `v1/tests/test_skgit_crawler_gate.py`).

## 4. Compare the live app.ini with the template (no values printed)

Forgejo reads only `app.ini`; the deploy renders it from `app.ini.j2`. To
prove a deploy changes nothing but what you intend, compare per-key hashes:

1. Render the templates with the instance vault into a private dir on the
   controller (a localhost play that `include_vars` the vault and runs
   `template` for `app.ini.j2`, with `no_log: true`).
2. Hash every `section.KEY` of both files (sha256 of the value, first 16 hex
   characters) and compare the key lists and hashes. Report key names only.
3. Expect: every existing key unchanged, only the keys the release notes
   list as added. A secret key (`SECRET_KEY`, `INTERNAL_TOKEN`, database
   `PASSWD`, `LFS_JWT_SECRET`, `JWT_SECRET`, metrics `TOKEN`) that differs
   means the vault and the live instance disagree: stop and fix the vault
   before deploying, or the deploy breaks sessions, tokens or the database
   login.
4. Delete the rendered files.

## 5. Roll back

- Gate only: `ANUBIS_ENABLED: false` (section 3).
- Config: restore the `config` tarball from section 1 and redeploy the
  previous framework tag (the template would otherwise rewrite it).
- Forgejo major upgrade: Forgejo cannot downgrade a migrated database.
  Restore the database dump and the data directory from before the upgrade,
  then deploy with the old `FORGEJO_VERSION`.
