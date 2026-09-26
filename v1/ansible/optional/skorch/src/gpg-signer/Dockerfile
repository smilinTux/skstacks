# GPG Signer Service
# Lightweight Alpine-based container with GPG and Flask
#
# Copyright (C) 2026 S&K Holding QT (Quantum Technologies)
# License: GNU AGPL v3

FROM python:3.12-alpine

# Install GPG and required system dependencies
RUN apk add --no-cache \
    gnupg \
    gnupg-gpgconf \
    && rm -rf /var/cache/apk/*

# Create app directory and user
RUN adduser -D -u 1000 signer
WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY signer.py .

# Create keys directory with proper permissions
RUN mkdir -p /keys && chown signer:signer /keys

# Switch to non-root user
USER signer

# Environment defaults
ENV GNUPGHOME=/keys
ENV LOG_LEVEL=INFO
ENV PYTHONUNBUFFERED=1

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD wget --no-verbose --tries=1 --spider http://127.0.0.1:8080/health || exit 1

# Expose port
EXPOSE 8080

# Run with gunicorn
CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:8080", "--access-logfile", "-", "--error-logfile", "-", "signer:app"]
