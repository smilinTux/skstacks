# Attach the forward-auth catch-all Proxy Provider to the embedded outpost,
# inside Authentik, ADDITIVELY.
#
# Run with `ak shell` in an sksso worker container (tasks/forward_auth_attach.yml).
# Why not the blueprint: an outpost's `providers` is a many-to-many field and
# the blueprint importer saves it as a full replacement set, so a blueprint
# entry for the outpost would detach every other provider on it. The ORM
# `outpost.providers.add()` keeps what is already there and is a no-op when the
# provider is already attached.
#
# Input: env SKSSO_FORWARD_AUTH, JSON {"provider": "<provider name>",
# "outpost": "<outpost name>", "admin_group": "<group name>",
# "cookie_domain": "<domain>"}. No secret anywhere. Output: one status line
# starting `attach:`. The last line is exactly one of:
#   attach: ok          attached now, or already attached (additive)
#   attach: waiting ... the blueprint has not applied yet (retry)
#   attach: refused ... a state that must not be attached (do not retry)
#
# Fail closed: the provider is attached only when its application exists and
# is gated by the admin group ALONE (an application with no enabled binding
# admits every account, and a stray extra binding widens it), and when it is
# the only domain-wide provider for its cookie domain. Anything else is
# refused and nothing is attached.
import json
import os

from authentik.core.models import Application
from authentik.outposts.models import Outpost
from authentik.policies.models import PolicyBinding
from authentik.providers.proxy.models import ProxyProvider


def done(line):
    print(line)
    raise SystemExit(0)


cfg = json.loads(os.environ["SKSSO_FORWARD_AUTH"])
provider = ProxyProvider.objects.filter(name=cfg["provider"]).first()
if provider is None:
    done(f"attach: waiting (provider {cfg['provider']!r} is not in Authentik yet; the worker has not applied the blueprint)")
if provider.mode != "forward_domain":
    done(f"attach: waiting (provider {cfg['provider']!r} is mode {provider.mode!r}, not forward_domain yet)")
if (provider.cookie_domain or "").lower() != cfg["cookie_domain"].lower():
    done(f"attach: waiting (provider {cfg['provider']!r} has cookie domain {provider.cookie_domain!r}, "
         f"not {cfg['cookie_domain']!r} yet)")

app = Application.objects.filter(provider=provider).first()
if app is None:
    done(f"attach: waiting (provider {cfg['provider']!r} has no application yet)")

bindings = list(PolicyBinding.objects.filter(target=app))
live = [b for b in bindings if b.enabled]
admin = [b for b in live if b.group is not None and b.group.name == cfg["admin_group"] and not b.negate]
others = [b for b in live if b not in admin]
if not admin:
    done(f"attach: waiting (application {app.slug!r} is not bound to group {cfg['admin_group']!r} yet)")
if others:
    what = ", ".join(sorted(
        (f"group {b.group.name!r}" if b.group is not None
         else f"user {b.user.username!r}" if b.user is not None
         else f"policy {b.policy.name!r}" if b.policy is not None else "an empty binding")
        + (" (negated)" if b.negate else "")
        for b in others))
    done(f"attach: refused (application {app.slug!r} is bound to more than the admin group: {what}; "
         "remove those bindings in Authentik, then deploy again)")

twins = ProxyProvider.objects.filter(mode="forward_domain", cookie_domain__iexact=cfg["cookie_domain"]).exclude(pk=provider.pk)
if twins.exists():
    names = ", ".join(sorted(repr(p.name) for p in twins))
    done(f"attach: refused (another domain-wide provider already serves cookie domain {cfg['cookie_domain']!r}: {names}; "
         "two catch-alls for one domain are ambiguous. Set FORWARD_AUTH_PROVIDER_NAME and FORWARD_AUTH_APP_SLUG to "
         "the existing ones to adopt it, or set FORWARD_AUTH_ENABLED: false)")

outpost = Outpost.objects.filter(name=cfg["outpost"]).first()
if outpost is None:
    done(f"attach: refused (no outpost named {cfg['outpost']!r}; set FORWARD_AUTH_OUTPOST to its name)")
if outpost.type != "proxy":
    done(f"attach: refused (outpost {cfg['outpost']!r} is type {outpost.type!r}, not proxy)")

if outpost.providers.filter(pk=provider.pk).exists():
    done(f"attach: ok (already on outpost {outpost.name!r}, {outpost.providers.count()} provider(s) there)")
outpost.providers.add(provider)
# save() makes Authentik tell the running embedded outpost to reload its provider list
outpost.save()
done(f"attach: ok (attached to outpost {outpost.name!r}, which now has {outpost.providers.count()} provider(s))")
