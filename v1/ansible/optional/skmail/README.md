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
  HOSTNAME: "mail.example.com"          # default: mail.<DOMAIN>
  INSTANCE: "primary"                    # default: primary
  APP_ENV: "prod"                        # default: prod
  ENABLE_RSPAMD: "1"                     # default: 1
  ENABLE_CLAMAV: "1"                     # default: 1
  ENABLE_FAIL2BAN: "1"                   # default: 1

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

`skmail` does not run its own ACME client. It expects a TLS certificate for
`HOSTNAME` (or a wildcard covering it) to already be extracted from
Traefik's `acme.json` onto shared storage at
`/var/data/runtime/<fence-service>-<env>/certs/`, where `<fence-service>` is
whichever of `skfence` / `skfenceha` / `holofence` this instance runs (the
playbook auto-detects it). The `mail-web` sidecar's only job is to give
Traefik an HTTP route to `HOSTNAME` so it issues that certificate in the
first place.

## Estate data

All of the above (`DOMAIN`, `HOSTNAME`, relay credentials, Cloudflare
token, DNS record values, and every mail account/alias created with
`setup email add` / `setup alias add`) is instance data. None of it lives in
this framework; it is supplied entirely through the instance's own vault and
the running container's own mail account database.
