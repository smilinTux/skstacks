"""Generic ops notification to Nextcloud Talk (Windmill script f/ops/notify).

Posts as the FWE Bot. Default room is "HammerTime Ingest" (variable f/ops/hammertime_room_token). Optional `room`
argument selects another room by name (see ROOMS) or by raw Talk token; the bot must be enabled in that room
(`occ talk:bot:setup 2 <token>`). Callers: hammertime-intake (default room), skmesh jobs ("NAM Admin"). Routing rules
are in v1/scripts/windmill-ops/ALERT-POLICY.md. Summaries carry file names, counts and step names only. Nothing here returns a secret.
"""
import re

from f.fwe.talk_lib import WM_SECRET_BOT, bot_post, clean, wm_variable

ROOM_VAR = 'f/ops/hammertime_room_token'
MARK = {'info': 'INFO', 'warn': 'WARN', 'error': 'ERROR'}
# Room names are matched case-insensitively. Tokens are not secret, they are room addresses.
ROOMS = {'nam urgent': 'r6vbtg3w', 'nam admin': 'tourgejv'}


def resolve_room(room: str = '') -> str:
    room = (room or '').strip()
    if not room:
        return wm_variable(ROOM_VAR)
    if room.lower() in ROOMS:
        return ROOMS[room.lower()]
    if re.fullmatch(r'[a-z0-9]{6,20}', room):
        return room
    raise ValueError('unknown room %r, use a token or one of: %s' % (room, ', '.join(sorted(ROOMS))))


def main(title: str, summary: str = '', severity: str = 'info', links: list[str] = [], ref_id: str = '', silent: bool = False, room: str = ''):
    sev = severity if severity in MARK else 'info'
    lines = ['[%s] **%s**' % (MARK[sev], clean(title, 120))]
    if summary:
        lines.append(clean(summary, 1500))
    lines.extend(clean(x, 300) for x in (links or []))
    status = bot_post(resolve_room(room), wm_variable(WM_SECRET_BOT), '\n'.join(lines), ref_id=ref_id or None,
                      silent=silent or sev == 'info')
    return {'posted': status}
