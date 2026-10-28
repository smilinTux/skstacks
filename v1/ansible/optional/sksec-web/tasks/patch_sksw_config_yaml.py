#!/usr/bin/env python3
"""Patch ONLY the handful of keys sksec-web's role owns in crowdsec-web-ui's
config.yaml (ui.readOnly, ui.timeZone, crowdsec.sync.lookback,
instances[0].lapi.url, instances[0].lapi.auth.username), round-tripping
through ruamel.yaml so comments and any other key (operator- or app-written)
survive untouched. Never writes the whole file from a template.

Env in: SKSW_CFG_B64 (the existing config.yaml, base64), SKSW_READ_ONLY
("true"/"false"), SKSW_TIMEZONE, SKSW_LOOKBACK, SKSW_LAPI_URL,
SKSW_LAPI_USER, SKSW_OUT_FILE (where to write the patched file, only if
something actually changed).

Prints "UNCHANGED" if every owned key already matches, or "CHANGED" (and
writes SKSW_OUT_FILE) if at least one did not. Exits non-zero -- fails the
whole play closed -- if the existing file cannot be parsed as YAML at all;
an empty file is valid YAML (parses to None) and is not a parse failure.
"""
import base64
import io
import os
import sys

from ruamel.yaml import YAML

raw = base64.b64decode(os.environ["SKSW_CFG_B64"])
yaml = YAML()
yaml.preserve_quotes = True
# Match block-sequence indent to what this app's own config.yaml already
# uses, so an untouched list (e.g. instances:) round-trips with the dash at
# the right column instead of ruamel's narrower default. Known, accepted
# limitation: ruamel still renders "- id: default" inline rather than this
# file's original "lone dash, then the key on its own indented line" style
# for a sequence item whose value is itself a mapping. That is a pure
# formatting choice with no data or key difference, and this role never
# writes a new value into that untouched list, so it is left as is rather
# than fighting ruamel's CommentedSeq formatting further.
yaml.indent(mapping=2, sequence=4, offset=2)
try:
    doc = yaml.load(raw)
except Exception as e:
    print(f"FAIL: config.yaml exists but could not be parsed: {e}", file=sys.stderr)
    sys.exit(1)
if doc is None:
    doc = {}

desired = [
    (["ui", "readOnly"], os.environ["SKSW_READ_ONLY"] == "true"),
    (["ui", "timeZone"], os.environ["SKSW_TIMEZONE"]),
    (["crowdsec", "sync", "lookback"], os.environ["SKSW_LOOKBACK"]),
    (["instances", 0, "lapi", "url"], os.environ["SKSW_LAPI_URL"]),
    (["instances", 0, "lapi", "auth", "username"], os.environ["SKSW_LAPI_USER"]),
]

changed = False
for path, want in desired:
    node = doc
    reachable = True
    for key in path[:-1]:
        if isinstance(key, int):
            if not isinstance(node, list) or len(node) <= key or node[key] is None:
                reachable = False
                break
            node = node[key]
        else:
            if not isinstance(node, dict):
                reachable = False
                break
            if key not in node or node[key] is None:
                node[key] = {}
            node = node[key]
    if not reachable:
        # e.g. instances[0] does not exist yet -- not this task's job to
        # invent a whole new instances array; first-run env-var seeding
        # already covers a genuinely fresh config.yaml.
        continue
    last = path[-1]
    current = node.get(last) if isinstance(node, dict) else None
    if current != want:
        node[last] = want
        changed = True

if changed:
    with open(os.environ["SKSW_OUT_FILE"], "wb") as fh:
        yaml.dump(doc, fh)
    print("CHANGED")
else:
    print("UNCHANGED")
