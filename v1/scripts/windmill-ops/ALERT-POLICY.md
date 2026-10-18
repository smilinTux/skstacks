# NAM alert policy (Nextcloud Talk)

Goal: few messages, each one worth reading. If a room gets noisy, people mute it and the next real alert is missed.

## Rooms

| Room | Token | Members | Purpose | Phone push |
|---|---|---|---|---|
| NAM Urgent | r6vbtg3w | Chef (chefboyrdave2.1), Casey (mrarch) | Critical items only | All messages |
| NAM Admin | tourgejv | Chef, Casey | Operational admin: skmesh pending approvals, new user signups, job failures | All messages |
| FWE Matters | (existing) | FWE team | Matter events from the FWE flows | unchanged |
| HammerTime Ingest | (existing) | existing | Ingest notices from hammertime-intake | unchanged |

Both new rooms use notification level "all messages" for both members. Anything else gets a different room, not a
louder one.

## NAM Urgent: what qualifies

Post only if at least one is true:
- Court or legal exposure (orders, citations, hearings, filings).
- A deadline under 7 days.
- A security issue (exposed credential, suspected intrusion, unexpected admin change).
- A service is down and it blocks work.

Everything else goes to NAM Admin or a digest. When unsure, it is not urgent.

## Message shape (Urgent)

Three parts, plain words: what happened, why it matters, the one action needed and who owns it. Name the person
addressed. Include where the files or evidence are. End with how to reply ("Reply here when done").

## NAM Admin: what goes there

- skmesh users waiting for approval (f/ops/skmesh_pending_approvals, every 5 minutes, one post per user).
- New user signups.
- Scheduled job failures (f/ops/skmesh_on_failure, at most one per hour per job).

## Severity

- error: needs a human to act now. In Urgent only if it meets the list above, otherwise Admin.
- warn: needs a human soon. Admin.
- info: no action. Posted silent, or better, put it in a digest.

## Quiet rules

1. No repeats: one message per event, keyed by a stable ref_id so retries do not duplicate.
2. One reminder at most (the skmesh job reminds once after 24 hours, then stops).
3. Rate limit failures: one failure post per job per hour.
4. Digests for non-urgent items: batch into one daily summary instead of a stream.
5. Resolved means silent. Clear state when the item is approved, fixed or deleted. No "all clear" chatter.
6. No secrets, tokens or full file contents in any message. File names, counts and step names only.

## Sending

Windmill script f/ops/notify takes an optional `room` argument: a name from its ROOMS table ("NAM Urgent",
"NAM Admin") or a raw Talk token. With no `room` it posts to HammerTime Ingest as before. The FWE Bot must be enabled
in the room first: `occ talk:bot:setup 2 <token>`.
