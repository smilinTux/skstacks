#!/bin/sh
# skpatch-hosts DNS-host check. READ-ONLY: it lists port 53 listeners and reads
# NetBird's config, nothing else. Run by provision_skpatch_hosts_nodes.yml (also
# under --check), so the provisioning run warns and the compliance check raises
# `dns-conflict`.
#
# Why: on a host that serves DNS (Pi-hole and the like), the NetBird client's
# own DNS can bind <netbird-ip>:53 at boot before the DNS server starts, and the
# DNS server then fails to bind 0.0.0.0:53. The estate loses DNS and every
# package run on that host fails afterwards. Fix on the host:
# `netbird up --disable-dns`.
#
# Usage: skpatch-dns-check.sh [true|false]   (true: this host serves DNS, even
# if no DNS server is running right now; the host var skpatch_hosts_dns_server)
# Output: "dns-host: yes|no", then one "conflict: ..." line per problem.
# Always exits 0. SKP_DNS_ROOT prefixes the config paths (tests only).
set -u
force=${1:-false}
root=${SKP_DNS_ROOT:-}
servers=" pihole-FTL dnsmasq unbound named coredns AdGuardHome kresd knotd pdns_server pdns_recursor docker-proxy "

listeners=$(ss -Hlnptu 2>/dev/null | awk '$5 ~ /:53$/ {print $5 "\t" $7}')

# loopback-only listeners (systemd-resolved's stub, a local cache) never
# compete for the address clients use
nonloop=$(printf '%s\n' "$listeners" | awk -F '\t' '$1 != "" && $1 !~ /^(127\.|\[::1\]|::1)/')

proc_of() {
  printf '%s' "$1" | sed -n 's/.*users:(("\([^"]*\)".*/\1/p'
}

is_server() {
  case "$servers" in *" $1 "*) return 0 ;; esac
  return 1
}

dns_host=no
[ "$force" = "true" ] && dns_host=yes
for bin in /usr/bin/pihole-FTL /usr/local/bin/pihole /usr/bin/pihole; do
  [ -x "$root$bin" ] && dns_host=yes
done
server_seen=no
others=""
IFS='
'
for line in $nonloop; do
  addr=${line%%	*}
  name=$(proc_of "${line#*	}")
  if is_server "$name"; then
    dns_host=yes
    server_seen=yes
  else
    others="$others$addr $name
"
  fi
done
unset IFS

echo "dns-host: $dns_host"
[ "$dns_host" = yes ] || exit 0

printf '%s' "$others" | sort -u | while read -r addr name; do
  [ -n "$addr" ] || continue
  case "$name" in
    netbird*) echo "conflict: netbird is bound to $addr on a DNS server (run: netbird up --disable-dns)" ;;
    "") echo "conflict: an unknown process holds $addr on a DNS server" ;;
    *) echo "conflict: $name holds $addr on a DNS server" ;;
  esac
done

[ "$server_seen" = yes ] || echo "conflict: no DNS server is listening on a non-loopback port 53"

# NetBird keeps DNS on unless the profile says "DisableDNS": true (the key is
# omitted or false otherwise). Older clients use /etc/netbird/config.json,
# newer ones /var/lib/netbird/<profile>.json and per-user profile dirs.
for f in "$root"/etc/netbird/config.json "$root"/var/lib/netbird/*.json "$root"/var/lib/netbird/*/*.json; do
  [ -f "$f" ] || continue
  grep -q '"ManagementURL"' "$f" 2>/dev/null || continue
  if ! grep -Eq '"DisableDNS"[[:space:]]*:[[:space:]]*true' "$f" 2>/dev/null; then
    echo "conflict: NetBird DNS is enabled in ${f#"$root"}: at boot it can take port 53 before the DNS server (run: netbird up --disable-dns)"
  fi
done
exit 0
