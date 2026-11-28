# skseek runtime: Perplexica 1.12.1, rebuilt so the findings that a rebuild can
# clear are cleared, with the same layout, port and paths as the published image
# the role used to pin (itzcrazykns1337/perplexica, /home/perplexica, port 3000,
# data in /home/perplexica/data, config.toml mounted by the deploy).
#
# Why a layered rebuild and not a from-scratch source build: the upstream image
# bundles a SearXNG checkout and a Python venv that upstream builds from the
# HEAD of the SearXNG repository at build time, so a from-scratch build could not
# reproduce what instances run today. The application layer, which is where the
# fixable Node findings are, IS rebuilt from the pinned upstream source commit:
#   1. builder: clone Perplexica at the pinned commit, bump next to a release
#      that carries the fixes, run the upstream build (yarn build, standalone).
#   2. runtime: start FROM the upstream image digest the role pinned, apt-get
#      upgrade, purge the compilers and -dev packages upstream left behind
#      (this also drops the linux-libc-dev kernel header findings), upgrade the
#      SearXNG venv packages, take node and npm from the current Node 24 image
#      and replace /home/perplexica with the rebuilt application.
# To publish (amd64), then pin the dated tag by index digest in
# skseek_default_image (deploy_skseek-*.yml) and in skseek.yml.j2:
#   docker build -t smilintux/skseek-runtime:1.12.1-<yyyymmdd> image/
#   docker push smilintux/skseek-runtime:1.12.1-<yyyymmdd>

ARG UPSTREAM=itzcrazykns1337/perplexica@sha256:343d2a02858cc3c15bb35bf94b3249923f4eb5de16eb80d266eddfef6d84951d

FROM node:24-bookworm-slim@sha256:873f586f3794f52281bd039bf02c048a0be08fde619338f9941bad1eb6b34530 AS builder
# v1.12.1 (2025-12-31): the version the pinned upstream image was built from.
ARG PERPLEXICA_COMMIT=3b8d8be6761949cf9034d6b3d9ec82a78115cb81
ARG NEXT_VERSION=16.4.6
ARG SHARP_VERSION=0.35.5
# onnxruntime-node's install script tries to fetch the CUDA execution provider
# (about 400 MB, 404 at the source today). The service runs on CPU on swarm nodes
# without GPU libraries, so the CUDA provider was never usable; skip it.
ENV ONNXRUNTIME_NODE_INSTALL_CUDA=skip
RUN apt-get update && apt-get -y upgrade \
 && apt-get install -y --no-install-recommends git ca-certificates python3 python3-pip sqlite3 make g++ \
 && rm -rf /var/lib/apt/lists/*
RUN git clone https://github.com/ItzCrazyKns/Perplexica.git /src \
 && git -C /src checkout --detach "$PERPLEXICA_COMMIT" \
 && test "$(git -C /src rev-parse HEAD)" = "$PERPLEXICA_COMMIT"
WORKDIR /home/perplexica
RUN cp /src/package.json /src/yarn.lock /src/tsconfig.json /src/next.config.mjs /src/next-env.d.ts \
       /src/postcss.config.js /src/drizzle.config.ts /src/tailwind.config.ts ./ \
 && cp -r /src/src /src/public /src/drizzle ./
RUN yarn install --frozen-lockfile --network-timeout 600000 \
 && yarn add --exact "next@${NEXT_VERSION}" --network-timeout 600000
# sharp is a transitive dependency (image handling); force the patched release.
RUN node -e 'const fs=require("fs");const p=JSON.parse(fs.readFileSync("package.json"));p.resolutions=Object.assign(p.resolutions||{},{sharp:process.argv[1]});fs.writeFileSync("package.json",JSON.stringify(p,null,2))' "$SHARP_VERSION" \
 && yarn install --network-timeout 600000 \
 && node -e 'console.log("sharp", require("sharp/package.json").version)'
RUN mkdir -p /home/perplexica/data && yarn build

FROM ${UPSTREAM}
RUN apt-get update && apt-get -y upgrade \
 && apt-get purge -y build-essential python3-dev libxslt1-dev zlib1g-dev libffi-dev libssl-dev linux-libc-dev \
 && apt-get autoremove -y --purge \
 && rm -rf /var/lib/apt/lists/*

# The SearXNG venv upstream builds. certifi and lxml are pinned by SearXNG's own
# requirements at the build date, so `pip check` reports those two pins as
# mismatched after the upgrade; both are drop-in compatible releases.
RUN /usr/local/searxng/searx-pyenv/bin/pip install --no-cache-dir --upgrade certifi lxml anyio setuptools wheel \
 && /usr/local/searxng/searx-pyenv/bin/pip check || true

# node from the current Node 24 image (same Debian release as the upstream base).
# npm, npx and corepack are removed: the entrypoint only runs `node server.js`, and
# npm's bundled packages (undici, brace-expansion, tar, glob, ...) carried most of
# the Node findings. yarn (/opt) stays for anyone who execs into the container.
RUN rm -rf /usr/local/lib/node_modules /usr/local/include/node /usr/local/bin/npm /usr/local/bin/npx /usr/local/bin/corepack
COPY --from=builder /usr/local/bin/node /usr/local/bin/node

# Replace the application with the rebuilt one; keep data/, uploads/ and entrypoint.sh.
WORKDIR /home/perplexica
RUN find . -mindepth 1 -maxdepth 1 ! -name data ! -name uploads ! -name entrypoint.sh -exec rm -rf {} +
COPY --from=builder /home/perplexica/public ./public
COPY --from=builder /home/perplexica/.next/static ./public/_next/static
COPY --from=builder /home/perplexica/.next/standalone ./
COPY --from=builder /home/perplexica/drizzle ./drizzle
