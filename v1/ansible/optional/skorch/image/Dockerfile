# skorch Postgres dump sidecar: a rebuild of prodrigestivill/postgres-backup-local
# on a current Debian base, so the OS packages, the Postgres client tools and the
# scheduler binary carry today's fixes. The upstream image was last built
# 2025-09-26 and its base and go-cron have fixable HIGH and CRITICAL findings.
#
# Same contract as the upstream image: the same env vars (POSTGRES_*, SCHEDULE,
# BACKUP_KEEP_*, HEALTHCHECK_PORT, WEBHOOK_*), the same /init.sh entrypoint and
# the same /backups layout, so the role's postgres-backup.env and compose service
# need no change. backup.sh, env.sh, init.sh and hooks/ are vendored unchanged
# from upstream (MIT, see UPSTREAM.md), pinned at the commit named there.
#
# The Postgres client major MUST equal the server major skorch runs (the role
# default is 18, an instance that kept Postgres 16 builds with PG_MAJOR=16).
# Build both as published tags:
#   docker build --build-arg PG_BASE=postgres:16-trixie@sha256:<digest> \
#     -t smilintux/postgres-backup-local:16-<yyyymmdd> image/
# See the README "Image" section for the pinned digests and the publish steps.

# The server image the client tools come from. Pin by digest; default is major 16.
ARG PG_BASE=postgres:16-trixie@sha256:ce81150802644ddfac1db4da9e63be6f2a3818aa6ee98a7a28838aeea65dc37a

# go-cron is built from the pinned source with a current Go toolchain, instead of
# downloading upstream's prebuilt release binary (Go standard library findings
# travel with the toolchain a binary was built with).
FROM golang:1.25-trixie@sha256:2c4c60ef415fbfa5e90300722293bef36c5e63fae17570ce18f580af933dbd73 AS gocron
ARG GOCRONVER=v0.0.11
ENV CGO_ENABLED=0 GOTOOLCHAIN=local GOFLAGS=-trimpath
RUN go install -ldflags='-s -w' github.com/prodrigestivill/go-cron@${GOCRONVER} \
 && install -m 0755 "$(go env GOPATH)/bin/go-cron" /go-cron

FROM ${PG_BASE}

ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get -y upgrade \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && apt-get clean && rm -rf /var/lib/apt/lists/*

COPY --from=gocron /go-cron /usr/local/bin/go-cron

ENV POSTGRES_DB="**None**" \
    POSTGRES_DB_FILE="**None**" \
    POSTGRES_HOST="**None**" \
    POSTGRES_PORT=5432 \
    POSTGRES_USER="**None**" \
    POSTGRES_USER_FILE="**None**" \
    POSTGRES_PASSWORD="**None**" \
    POSTGRES_PASSWORD_FILE="**None**" \
    POSTGRES_PASSFILE_STORE="**None**" \
    POSTGRES_EXTRA_OPTS="-Z1" \
    POSTGRES_CLUSTER="FALSE" \
    SCHEDULE="@daily" \
    VALIDATE_ON_START="TRUE" \
    BACKUP_ON_START="FALSE" \
    BACKUP_DIR="/backups" \
    BACKUP_SUFFIX=".sql.gz" \
    BACKUP_LATEST_TYPE="symlink" \
    BACKUP_KEEP_DAYS=7 \
    BACKUP_KEEP_WEEKS=4 \
    BACKUP_KEEP_MONTHS=6 \
    BACKUP_KEEP_MINS=1440 \
    HEALTHCHECK_PORT=8080 \
    WEBHOOK_URL="**None**" \
    WEBHOOK_ERROR_URL="**None**" \
    WEBHOOK_PRE_BACKUP_URL="**None**" \
    WEBHOOK_POST_BACKUP_URL="**None**" \
    WEBHOOK_EXTRA_ARGS=""

COPY hooks /hooks
COPY backup.sh env.sh init.sh /

VOLUME /backups

ENTRYPOINT []
CMD ["/init.sh"]

HEALTHCHECK --interval=5m --timeout=3s \
  CMD curl -f "http://localhost:$HEALTHCHECK_PORT/" || exit 1
