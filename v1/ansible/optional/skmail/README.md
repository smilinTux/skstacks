# skmail (docker-mailserver)

A self-hosted SMTP/IMAP mail server (`docker-mailserver`) on Docker Swarm,
with Rspamd, ClamAV and Fail2ban built in, plus a small `mail-web` sidecar
that exists only to make Traefik issue a TLS certificate for the mail
hostname (docker-mailserver then reads that certificate directly from
Traefik's `acme.json`).

## Why host-mode ports

SMTP/IMAP are not HTTP protocols, so they cannot go through Traefik's normal
HTTP routing. The `skmail` service publishes its mail ports directly on the
host network (Swarm `mode: host`) instead of through an overlay network:

| Port | Protocol | Purpose |
|---|---|---|
| 25 | SMTP | inbound mail transfer |
| 587 | SMTP submission | STARTTLS, auth required |
| 465 | SMTPS | implicit TLS, auth required |
| 143 | IMAP | STARTTLS |
| 993 | IMAPS | implicit TLS |

Because ports are host-published, `skmail` also carries a
`node.labels.mail-vip == true` placement constraint: exactly one manager
should hold that label at a time (e.g. moved by a keepalived VIP notify
script), so mail traffic always lands on the node currently reachable at the
public IP. Deploying this service assumes something in the instance already
manages that label; the playbook only checks that one manager has it.

## Required vault keys

None of the following has a built-in default; the playbook fails closed
(undefined-variable error) if they are missing, rather than silently using a
placeholder domain.

```yaml
skmail:
  CLUSTERNAME: "<this instance's cluster name>"
  DOMAIN: "example.com"          # the Cloudflare zone / mail domain
  # HOSTNAME defaults to "mail.<DOMAIN>" if not set explicitly
```

## Optional vault keys

```yaml
skmail:
  DMS_VERSION: "17.0.0"                  # default: 17.0.0 - see "Upgrading" below
  HOSTNAME: "mail.example.com"          # default: mail.<DOMAIN>
  INSTANCE: "primary"                    # default: primary
  APP_ENV: "prod"                        # default: prod
  ENABLE_RSPAMD: "1"                     # default: 1
  ENABLE_CLAMAV: "1"                     # default: 1
  ENABLE_FAIL2BAN: "1"                   # default: 1
  HEALTHCHECK_START_PERIOD: "1800s"      # default: 1800s - see "Healthcheck and slow starts" below
  MAIL_WEB_ENABLED: true                 # default: true - see "The mail-web sidecar" below

  # SSL_TYPE: default: letsencrypt - see "Certificates" below for manual/self-signed
  SSL_TYPE: "letsencrypt"
  SSL_CERT_PATH: "/srv/tls/mail.example.com/fullchain.pem"  # only for SSL_TYPE: manual
  SSL_KEY_PATH: "/srv/tls/mail.example.com/privkey.pem"     # only for SSL_TYPE: manual

  # Outbound relay (skip this block to send directly from this host's IP;
  # most residential/dynamic IPs get blocked or spam-foldered by major
  # providers without a relay with established sender reputation)
  RELAY_HOST: "smtp.your-relay.example"
  RELAY_PORT: "587"                      # default: 587
  RELAY_USER: "postmaster@example.com"
  RELAY_PASSWORD: "<relay password>"

  # Optional: let the playbook manage this zone's mail DNS records via the
  # Cloudflare API instead of creating them by hand (see below)
  CLOUDFLARE_DNS_API_TOKEN: "<scoped Cloudflare API token>"
  DNS_RECORDS:
    SPF:    { name: "@",                type: TXT, value: "v=spf1 ip4:<your WAN IP>/32 -all" }
    DMARC:  { name: "_dmarc",           type: TXT, value: "v=DMARC1; p=quarantine; rua=mailto:postmaster@example.com" }
    DKIM:   { name: "mail._domainkey",  type: TXT, value: "" }   # leave empty; the playbook fills it in after DKIM key generation
    A:      { name: "mail",             type: A,   value: "<your WAN IP>" }
    MX:     { name: "@",                type: MX,  value: "mail.example.com", priority: 10 }
```

## Healthcheck and slow starts

At every container start docker-mailserver runs `chown -R` over each
account's mailbox (`helpers/accounts.sh:_create_accounts`) and fixes
ownership under `/var/mail` (`_chown_var_mail`) before postfix listens on
port 25. On NFS every file costs a SETATTR round trip: one production start
with 22 mailboxes (~2.5k files) took about 17 minutes while the NFS server
was busy with an unrelated bulk copy. The old healthcheck (60s
`start_period`, then 3 failed probes 30s apart) gave up after ~2.5 minutes,
Swarm killed the task as unhealthy, and the replacement started the same
chown from the top: a kill loop, and no mail, until `start_period` was
raised by hand.

`skmail.HEALTHCHECK_START_PERIOD` (default `1800s`) sets that window. The
default is the observed worst case (~17 min) with about 75% headroom, and
still bounded: a container that never becomes healthy is replaced after
roughly 32 minutes (`start_period` plus 3 x 30s). A long value does not
slow a normal start: failed probes inside the window are not counted, and
the first passing probe marks the task healthy immediately (the mail ports
are published in host mode, so nothing waits on the health state to route
traffic). An instance whose NFS is slower, or which has many more
mailboxes, should raise it; time a restart
(`docker service logs skmail-<env>_skmail` from "Welcome to
docker-mailserver" to "is up and running") and leave generous headroom.

DMS 17.0.0 (and 16.0.1 before it) has no environment option that skips or limits this ownership
fix: the per-mailbox `chown -R` in `_create_accounts` is unconditional for
the default FILE account provisioner, and `_chown_var_mail` runs on every
start. So there is no knob here to turn it off; the durable fixes are
keeping heavy I/O off the NFS server while mail restarts, and keeping
mailbox counts and file counts in mind when sizing this window.

## First mail account (fresh install)

docker-mailserver refuses to start without at least one mail account ("You
need at least one mail account to start Dovecot"), and `add_user` needs a
running container, so a brand-new instance must be given its first account
by the deploy itself. Set both keys, first install only:

```yaml
skmail:
  BOOTSTRAP_ACCOUNT: "postmaster@example.com"
  BOOTSTRAP_PASSWORD: "<first account password>"
```

When both are set and `/var/data/skmail-<env>/docker-data/dms/config/postfix-accounts.cf`
does not exist yet, the playbook hashes the password on the target with
`openssl passwd -6 -stdin` (the password goes over stdin, never argv; the
tasks are `no_log`) and writes that file with the single line
`<account>|{SHA512-CRYPT}<hash>` (root:root, 0640; DMS reads it as root at
startup). If the file already exists it is never read or overwritten, so the
keys are inert on existing instances and can be removed from the vault once
the instance is up. Change the password afterwards with DMS's own
`setup email update`.

Further accounts: run `/var/data/skmail-<env>/add_user <email>` **on the node
hosting the running `skmail-<env>_skmail` task** (`docker service ps
skmail-<env>_skmail`); `docker exec` only reaches local containers. The
password is read from stdin (prompted on a terminal).

## Upgrading `docker-mailserver`

`skmail.DMS_VERSION` pins the exact image tag; the framework's default
(`17.0.0`) is only what a brand-new instance gets if it never sets this key
- it is **not** a signal that every running instance should be on it. An
existing instance keeps whatever `DMS_VERSION` its own vault already has (or
inherits the framework default the first time it's added, which may be a
major-version jump from what it's actually running). Always check the
current running image first (`docker service inspect --format
'{{.Spec.TaskTemplate.ContainerSpec.Image}}' <stack>_skmail`) and set
`DMS_VERSION` explicitly before ever re-running this playbook, rather than
relying on the framework default. Read the
[docker-mailserver release notes](https://github.com/docker-mailserver/docker-mailserver/releases)
for every major version between what you run and what you're moving to
before bumping across a major boundary; see `docs/runbooks/skmail.md` in an
instance repo (or your own change log) for a per-instance upgrade record.

### Upgrading to docker-mailserver 17 (from 15 or 16)

Checked against the upstream changelog (v15.1.0, v16.0.0, v16.0.1, v17.0.0) and
the 17.0.0 image itself. What matters for a skmail instance:

- **No intermediate version is required.** DMS does not gate on sequential
  majors. A 15.x instance can go straight to 17.0.0, and that is the better
  path for Rspamd state (see Valkey below). Only the order of reading matters:
  16.0.0 is the big one (Debian 12 to 13, Dovecot 2.3 to 2.4), 17.0.0 is
  smaller.
- **Dovecot 2.3 to 2.4 (16.0.0).** A custom `dovecot.cf` or `dovecot-*.cf`
  override in the config volume written for 2.3 will not load. The framework
  templates set none. Mailbox data (maildir) is unchanged; Dovecot rebuilds
  its indexes on first access, so the first IMAP logins after the upgrade are
  slower.
- **`SA_SPAM_SUBJECT` removed (16.0.0).** Use `SPAM_SUBJECT`. The skmail env
  template sets neither.
- **DH parameters removed (16.0.0).** `/tmp/docker-mailserver/dhparams.pem` is
  ignored and DHE ciphers are no longer offered on 587/465/143/993/110/995.
  Modern clients use ECDHE, so this is only visible to very old clients.
- **Redis replaced by Valkey (17.0.0).** `ENABLE_RSPAMD_REDIS` is now
  `ENABLE_RSPAMD_VALKEY` (default on with Rspamd, the framework does not set
  either). State from DMS 15 or older (`/var/mail-state/lib-redis`) moves to
  `lib-valkey` automatically. State written by DMS 16 (Redis 8) cannot be
  loaded: it is left untouched in `lib-redis`, Valkey starts empty, and Rspamd
  data such as Bayes must be relearned. If you are on 16.x, expect that reset.
- **Xapian removed (17.0.0).** Only matters with a custom FTS config. The
  skmail templates do not enable one.
- **LDAP rework (17.0.0).** `LDAP_QUERY_FILTER_*` became
  `POSTFIX_<KIND>_QUERY_FILTER` and there are no default filters. skmail uses
  the FILE account provisioner, so this does not apply.
- **`setup config dkim` key layout (17.0.0)** only changes Rspamd-managed
  DKIM. With the skmail env as shipped (`ENABLE_RSPAMD=1` and `ENABLE_OPENDKIM`
  left at its default 1) DMS logs "OpenDKIM will manage DKIM keys", so the
  keys stay in `config/opendkim/keys/` and the playbook's DKIM read and DNS
  update tasks are unaffected (verified against the 17.0.0 image).
- **Amavis, Postgrey, SpamAssassin:** 16.0.0 swapped Postfix
  `disable_dns_lookups` for `smtp_dns_support_level` in the Amavis config
  (internal) and dropped Pyzor and Razor. Neither needs operator action.
- **Fail2Ban 1.1.1 (16.0.0)** pins the Postfix and Dovecot jails to the file
  backend. The `postfix-sasl` jail went away in 15.1.0 (covered by the
  `postfix[mode=extra]` jail).
- **Dovecot stats writer re-enabled (17.0.0).** Only a `user-patches.sh` that
  deletes `60-stats.conf` is affected (use `rm -f`). The skmail one does not.
- **`user-patches.sh` with `SSL_TYPE=manual`** still carries two `sed` lines
  that rewrite the Dovecot 2.3 keys `ssl_cert =` and `ssl_key =`. Dovecot 2.4
  uses `ssl_server_cert_file` / `ssl_server_key_file`, so those lines match
  nothing and DMS's own manual-TLS handling applies. They are harmless and are
  left alone here.

Operator steps: back up `docker-data/dms/{config,mailstate}` (and take an NFS
snapshot of `maildata` if your storage supports it), set
`DMS_VERSION: "17.0.0"` in the instance vault, redeploy, and allow the
healthcheck window (the per-mailbox ownership fix still runs at start).
Rollback: set `DMS_VERSION` back to the previous tag and redeploy. Maildir
data is forward and backward compatible; restore `config` and `mailstate` from
the backup if you had moved to 17 and Rspamd or Dovecot state was rewritten.
Expect a few minutes of SMTP/IMAP downtime (single replica, stop-first).
Senders retry for days, so inbound mail queues at the sender.

## DNS records an instance must create

Whether managed by hand or via `CLOUDFLARE_DNS_API_TOKEN` above, a working
mail deployment needs, in the `DOMAIN` zone:

| Record | Type | Purpose |
|---|---|---|
| `@` | MX | points mail for the domain at the `HOSTNAME` |
| `mail` (or your `HOSTNAME`) | A | resolves the mail hostname to the instance's public IP |
| `@` | TXT (SPF) | authorizes this IP (or your relay) to send for the domain |
| `_dmarc` | TXT (DMARC) | delivery/reporting policy |
| `mail._domainkey` | TXT (DKIM) | published after the playbook generates the DKIM keypair inside the container (`setup config dkim`); re-run with `--tags dkim,dns` after the first deploy to publish it |

A reverse-DNS (PTR) record matching `HOSTNAME` on the instance's public IP
is also strongly recommended for inbound deliverability; that is set with
your ISP/hosting provider, not Cloudflare, and is outside what this
playbook manages.

## Certificates

`skmail.SSL_TYPE` (default `letsencrypt`) selects how `docker-mailserver`
gets its SMTP/IMAP TLS certificate; see the upstream
[SSL_TYPE docs](https://docker-mailserver.github.io/docker-mailserver/latest/config/security/ssl/)
for the full reference on every value below.

- **`letsencrypt`** (default, unchanged behaviour): `skmail` does not run its
  own ACME client. It expects a TLS certificate for `HOSTNAME` (or a wildcard
  covering it) to already be extracted from Traefik's `acme.json` onto shared
  storage at `/var/data/runtime/<fence-service>-<env>/certs/`, where
  `<fence-service>` is whichever of `skfence` / `skfenceha` / `holofence` this
  instance runs (the playbook auto-detects it and preflight-checks the dumped
  files before deploying). The `mail-web` sidecar's only job is to give
  Traefik an HTTP route to `HOSTNAME` so it issues that certificate in the
  first place.
- **`manual`**: for an instance bringing its own certificate. Set
  `skmail.SSL_CERT_PATH` / `skmail.SSL_KEY_PATH` to the certificate/key's host
  paths (PEM encoded); the playbook mounts them read-only into the container.
  The Traefik-acme preflight above is skipped entirely for this type.
- **`self-signed`**: for an instance without ACME at all (e.g. a test
  instance). The deploy playbook generates a self-signed CA and a leaf
  certificate for `HOSTNAME` with `openssl`, idempotently, directly into the
  already-mounted `docker-data/dms/config/ssl/` directory (skipped once the
  files exist) - no vault keys needed beyond `SSL_TYPE: self-signed` itself.
  The Traefik-acme preflight above is skipped for this type too.

### The mail-web sidecar

`mail-web` (a `traefik/whoami` container behind a Traefik router for
`HOSTNAME` with `certresolver=main`) exists only so that Traefik's ACME
resolver OBTAINS a certificate for the mail hostname on an instance that does
not hold one yet. It is not what renews it: Traefik's ACME provider renews
every certificate stored in `acme.json` on its own timer
(`renewCertificates` in `pkg/provider/acme/provider.go`, Traefik v3.6.2),
whether or not any router still references it. A router whose domain is
already covered by a stored certificate, a wildcard included, never obtains a
new one (`getUncheckedDomains`), so on an instance whose skmail uses a
wildcard (`SSL_DOMAIN: "*.example.com"`) mail-web does nothing for TLS at all.
It does, however, publish each visitor's request headers and client IP at
`https://HOSTNAME`, because that is what whoami does.

Set `skmail.MAIL_WEB_ENABLED: false` to drop it: the compose file then has no
mail-web service and no Traefik router, and the deploy script removes a
mail-web service left over from an earlier deploy (`docker stack deploy`
never removes a service that disappeared from the file). `https://HOSTNAME`
then falls through to the fence's catch-all. Before you disable it, check
that the certificate docker-mailserver reads is already in `acme.json`.
Neither setting covers a lost `acme.json`: only a router that names the
certificate's exact domain (for a wildcard, `tls.domains[n].main=*.example.com`
on some router using a DNS-01 resolver) makes Traefik obtain it again, and
mail-web asks for `HOSTNAME`, not a wildcard, unless `TLS_DOMAINS` says
otherwise.

## Estate data

All of the above (`DOMAIN`, `HOSTNAME`, relay credentials, Cloudflare
token, DNS record values, and every mail account/alias created with
`setup email add` / `setup alias add`) is instance data. None of it lives in
this framework; it is supplied entirely through the instance's own vault and
the running container's own mail account database.
