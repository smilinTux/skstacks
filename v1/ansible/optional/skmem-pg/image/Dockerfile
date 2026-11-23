# Base pinned by digest, on Debian 12 (bookworm) and PG 17 like every image
# before it: never let a rebuild move the OS (glibc collation) or the PG
# major under an existing data directory. Bump the tag and digest together.
FROM pgvector/pgvector:0.8.7-pg17-bookworm@sha256:b513c48566e4777cd3370451400000c78eda6a08131d68f4c31d4a76f27bd8fd
USER root
# Debian security updates published since the base image was built (Trivy
# found fixed-but-not-installed CRITICALs in libgnutls30, perl and libbrotli1).
RUN apt-get update && apt-get -y upgrade && rm -rf /var/lib/apt/lists/*
# --- ParadeDB pg_search (real BM25, @@@ operator) — PG17 bookworm deb ---
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
 && curl -fL -o /tmp/pg_search.deb https://github.com/paradedb/paradedb/releases/download/v0.24.0/postgresql-17-pg-search_0.24.0-1PARADEDB-bookworm_amd64.deb \
 && apt-get install -y /tmp/pg_search.deb && rm -f /tmp/pg_search.deb
# --- Apache AGE (graph in postgres) — build from PG17 branch ---
RUN apt-get install -y --no-install-recommends build-essential git postgresql-server-dev-17 flex bison \
 && git clone --depth 1 -b PG17/v1.7.0-rc0 https://github.com/apache/age.git /tmp/age \
 && cd /tmp/age && make PG_CONFIG=/usr/lib/postgresql/17/bin/pg_config \
 && make PG_CONFIG=/usr/lib/postgresql/17/bin/pg_config install \
 && rm -rf /tmp/age \
 && apt-get purge -y build-essential git postgresql-server-dev-17 flex bison && apt-get autoremove -y \
 && apt-get clean && rm -rf /var/lib/apt/lists/*
USER postgres
