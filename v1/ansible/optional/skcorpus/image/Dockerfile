# skcorpus runtime: Python plus fastapi, uvicorn, httpx and psycopg, nothing else.
# The app itself (app/skcorpus.py in this role) is not in the image: the
# deploy checks its sha256 and bind-mounts it read-only (README.md).
#
# The same base and package pins as the published skpayment runtime image,
# plus psycopg, so the published skpayment runtime image runs Qdrant-only
# skcorpus unchanged; that is the default until a dedicated tag is published.
# pgvector corpora need psycopg, so they need an image built from here.
# To publish one (amd64), then pin the dated tag by index digest in skc_default_image and in
# src/config/skcorpus/skcorpus.yml.j2:
#   docker build -t smilintux/skcorpus-runtime:py3.11-<yyyymmdd> image/
#   docker push smilintux/skcorpus-runtime:py3.11-<yyyymmdd>
FROM python:3.11-slim-bookworm@sha256:a83ba18aef0f865b1a4a611c2cf460fe446f2795673bbb9fe43be570439e3b39
RUN apt-get update && apt-get -y upgrade && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --no-deps -r /tmp/requirements.txt && pip check && rm /tmp/requirements.txt \
 && pip uninstall -y setuptools wheel   # build-time only; nothing imports them at runtime
WORKDIR /
