# Vendored upstream files

`backup.sh`, `env.sh`, `init.sh` and `hooks/` are copied unchanged from
https://github.com/prodrigestivill/docker-postgres-backup-local at commit `d257e5d`
(2025-09-26), the commit the previous default image
(`prodrigestivill/postgres-backup-local:*-debian-d257e5d`) was built from. MIT licence,
Copyright (c) Pau Rodriguez-Estivill. The Dockerfile follows upstream's `debian.Dockerfile`
except for the base (a pinned `postgres:<major>-trixie`), `apt-get -y upgrade`, and go-cron
being built from source (`prodrigestivill/go-cron` v0.0.11) with a current Go toolchain.

To take a newer upstream: copy the four items from the new commit, diff them, update the
commit above, rebuild and re-pin.
