# skpayment runtime: Python plus fastapi, uvicorn and httpx, nothing else.
# The app itself (skpayment.py) is not in the image: the deploy verifies it
# by sha256 and bind-mounts it read-only (README.md).
#
# Replaces the general-purpose web UI image this role used to borrow as a
# runtime only because it happened to ship these packages: that image
# carried hundreds of unused packages (11 critical CVEs with fixes) onto the
# one service Stripe reaches from the internet.
#
# Build and push (amd64), then pin the dated tag by index digest in
# skp_default_image and in src/config/skpayment/skpayment.yml.j2:
#   docker build -t smilintux/skpayment-runtime:py3.11-<yyyymmdd> image/
#   docker push smilintux/skpayment-runtime:py3.11-<yyyymmdd>
FROM python:3.11-slim-bookworm@sha256:a83ba18aef0f865b1a4a611c2cf460fe446f2795673bbb9fe43be570439e3b39
RUN apt-get update && apt-get -y upgrade && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --no-deps -r /tmp/requirements.txt && pip check && rm /tmp/requirements.txt \
 && pip uninstall -y setuptools wheel   # build-time only; nothing imports them at runtime
WORKDIR /
