# Link sksso.CAPAUTH_USERS to their PGP fingerprints, inside Authentik.
#
# Run with `ak shell` in an sksso server or worker container (the role does
# this on Authentik 2026.2+; capauth_provision.py still links over the API on
# older versions). Authentik 2026.8 made UserSourceConnection.user read-only
# in the API, so POST /sources/user_connections/oauth/ can no longer create a
# link for a given user (it answers HTTP 500 on a NULL user_id).
#
# Input: env SKSSO_CAPAUTH_LINKS, JSON {"source_slug": "...", "users":
# [{"username": "...", "fingerprint": "..."}]}. Usernames and fingerprints
# are not secrets. Output: one status line per user, then "links: ok".
# Same rules as the API path: ONE connection per (user, source), a new
# fingerprint for a linked user is a key rotation (updated in place), and a
# fingerprint already linked to another user is refused, never moved.
import json
import os

from authentik.core.models import User
from authentik.sources.oauth.models import OAuthSource, UserOAuthSourceConnection

cfg = json.loads(os.environ["SKSSO_CAPAUTH_LINKS"])
source = OAuthSource.objects.get(slug=cfg["source_slug"])
for entry in cfg["users"]:
    username = entry["username"]
    fp = str(entry["fingerprint"]).replace(" ", "").upper()
    if len(fp) not in (40, 64) or any(ch not in "0123456789ABCDEF" for ch in fp):
        raise SystemExit(f"users: '{username}' has no 40/64 hex digit PGP fingerprint")
    user = User.objects.filter(username=username).first()
    if user is None:
        raise SystemExit(f"users: Authentik user '{username}' does not exist (create it first)")
    other = UserOAuthSourceConnection.objects.filter(source=source, identifier=fp).exclude(user=user).first()
    if other is not None:
        raise SystemExit(f"users: fingerprint ...{fp[-16:]} is already linked to another Authentik user "
                         f"(pk {other.user_id}); unlink it there first")
    conn = UserOAuthSourceConnection.objects.filter(source=source, user=user).first()
    label = f"link {username} <- {fp[-16:]}"
    if conn is None:
        UserOAuthSourceConnection.objects.create(source=source, user=user, identifier=fp)
        print(f"{label}: created")
    elif conn.identifier != fp:
        conn.identifier = fp
        conn.save()
        print(f"{label}: updated (identifier)")
    else:
        print(f"{label}: unchanged")
print("links: ok")
