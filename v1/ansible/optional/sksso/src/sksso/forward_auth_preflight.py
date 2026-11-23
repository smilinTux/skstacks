# Preflight for the forward-auth catch-all, run BEFORE the blueprint is
# rendered: refuse when Authentik already has a domain-wide (forward_domain)
# Proxy Provider under another name than the configured one.
#
# Why: the blueprint matches the provider by NAME. On an estate that made its
# catch-all by hand and redeploys without setting FORWARD_AUTH_PROVIDER_NAME and
# FORWARD_AUTH_APP_SLUG, Authentik's worker would apply the rendered blueprint
# on its own and create a SECOND provider and application under the default
# names; the attach step only refuses afterwards. This runs first, so the
# blueprint file never reaches the mount.
#
# Run with `ak shell` in a running sksso server container
# (tasks/forward_auth_blueprint.yml). READ ONLY: it never edits or deletes
# anything. No API token, no secret.
#
# Input: env SKSSO_FORWARD_AUTH, JSON {"provider": "<configured provider
# name>", "cookie_domain": "<domain>"}. Output: one status line starting
# `preflight:`, exactly one of:
#   preflight: ok ...       nothing in the way (none, or only the configured one)
#   preflight: refused ...  another forward_domain provider overlaps the cookie
#                           domain (same domain, a parent or a subdomain of it)
#
# A forward_domain provider for an unrelated cookie domain is not a duplicate
# and passes. One whose cookie domain is empty cannot be told apart from a
# duplicate, so it counts as overlapping.
import json
import os

from authentik.core.models import Application
from authentik.providers.proxy.models import ProxyProvider


def done(line):
    print(line)
    raise SystemExit(0)


def overlaps(theirs, ours):
    a = (theirs or "").strip().strip(".").lower()
    b = ours.strip().strip(".").lower()
    return not a or a == b or a.endswith("." + b) or b.endswith("." + a)


cfg = json.loads(os.environ["SKSSO_FORWARD_AUTH"])
others = [p for p in ProxyProvider.objects.filter(mode="forward_domain").exclude(name=cfg["provider"])
          if overlaps(p.cookie_domain, cfg["cookie_domain"])]
if not others:
    done(f"preflight: ok (no other domain-wide provider overlaps cookie domain {cfg['cookie_domain']!r})")

parts = []
for p in sorted(others, key=lambda o: o.name):
    app = Application.objects.filter(provider=p).first()
    parts.append(f"{p.name!r} (cookie domain {p.cookie_domain!r}, "
                 + (f"application slug {app.slug!r}" if app is not None else "no application") + ")")
done(f"preflight: refused (Authentik already has a domain-wide provider covering cookie domain "
     f"{cfg['cookie_domain']!r} under another name than the configured {cfg['provider']!r}: {', '.join(parts)}. "
     "Deploying now would create a second catch-all beside it. Set FORWARD_AUTH_PROVIDER_NAME and "
     "FORWARD_AUTH_APP_SLUG to that provider's name and its application's slug to adopt it (and "
     "FORWARD_AUTH_COOKIE_DOMAIN / FORWARD_AUTH_SSO_HOST if they differ), or set FORWARD_AUTH_ENABLED: false. "
     "Nothing was rendered or changed.)")
