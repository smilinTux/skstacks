# v2/apps/ — SUPERSEDED

This directory is a pre-4C-reorg artifact. The canonical capability stubs live in
the 4C layout:

  v2/cloud/<port>/    — edge, routing, naming, deploy, infra, dweb
  v2/comms/<port>/    — chat, voice, transport, bus
  v2/compute/<port>/  — data, cache, object, files, models, automation, obs, backup
  v2/core/<port>/     — identity, defense, WAF, PKI, secrets

The `skfence/docker-compose.yml.j2` here was an early draft. The authoritative
skfence descriptor and compose template are in `v2/cloud/skfence/`.

Do not add new ports here. Kept for git history only.
