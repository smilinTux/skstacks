#!/usr/bin/env python3
"""
GPG Signer Service.

A small REST API for detached PGP signing, verification and key listing.
It runs as an opt-in sidecar container in the skorch (n8n) stack, so an
n8n workflow can ask a trusted signer to countersign a document with one
of several pre-provisioned keys ('trustees') without ever handling the
private key material itself.

Copyright (C) 2026 S&K Holding QT (Quantum Technologies)
License: GNU AGPL v3
"""

import hmac
import logging
import os
import subprocess
import tempfile
import hashlib
from datetime import datetime
from functools import wraps

from flask import Flask, request, jsonify, Response

app = Flask(__name__)

# Configuration
GNUPGHOME = os.environ.get('GNUPGHOME', '/keys')
LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO')

# Fail closed: there is no usable default for the API secret. An instance
# that forgets to set GPG_SIGNER_API_SECRET gets a service that refuses
# every authenticated request instead of one that accepts a well-known
# value (the previous default was the literal string
# 'change-me-in-production', which is a public secret).
API_SECRET = os.environ.get('GPG_SIGNER_API_SECRET') or None

# Key import is destructive setup surface (it adds key material to the
# keyring under whatever authority holds the one shared API secret) and is
# only ever needed once, during provisioning. It is off unless an instance
# explicitly turns it on for that window.
ALLOW_KEY_IMPORT = os.environ.get('GPG_SIGNER_ALLOW_KEY_IMPORT', 'false').strip().lower() in ('1', 'true', 'yes', 'on')

# Bound upload size so a large multipart body cannot exhaust memory/disk
# on the sidecar (no limit was enforced before).
app.config['MAX_CONTENT_LENGTH'] = int(os.environ.get('GPG_SIGNER_MAX_UPLOAD_BYTES', 25 * 1024 * 1024))

# Setup logging
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('gpg-signer')

if not API_SECRET:
    logger.warning('GPG_SIGNER_API_SECRET is not set: all authenticated endpoints will refuse requests until it is configured.')

# Trustee key mapping (trustee name -> key ID or fingerprint)
# These will be populated when keys are imported
TRUSTEE_KEYS = {
    'trustee-a': os.environ.get('TRUSTEE_A_KEY_ID', ''),
    'trustee-b': os.environ.get('TRUSTEE_B_KEY_ID', ''),
    'trustee-c': os.environ.get('TRUSTEE_C_KEY_ID', ''),
}


def require_auth(f):
    """Decorator to require API authentication.

    Fails closed: if no API secret is configured, every request is
    rejected (503) rather than being compared against an empty/default
    value. The comparison itself is constant-time to avoid leaking the
    secret's prefix through response-time differences.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        if not API_SECRET:
            return jsonify({'error': 'Service not configured: GPG_SIGNER_API_SECRET is unset'}), 503

        auth_header = request.headers.get('Authorization')
        if not auth_header:
            return jsonify({'error': 'Missing Authorization header'}), 401

        # Expect: Bearer <token>
        parts = auth_header.split()
        if len(parts) != 2 or parts[0].lower() != 'bearer':
            return jsonify({'error': 'Invalid Authorization header format'}), 401

        if not hmac.compare_digest(parts[1], API_SECRET):
            logger.warning(f"Invalid API secret attempt from {request.remote_addr}")
            return jsonify({'error': 'Invalid API secret'}), 403

        return f(*args, **kwargs)
    return decorated


def run_gpg_command(args: list, input_data: bytes = None, extra_fds: tuple = ()) -> tuple:
    """
    Run a GPG command with the configured GNUPGHOME.

    Args:
        args: List of GPG command arguments
        input_data: Optional bytes to pass to stdin
        extra_fds: Additional file descriptors to keep open and inherited
            in the child (e.g. a pipe read-end used for --passphrase-fd),
            so a passphrase never has to be passed as a command-line
            argument, where it would be visible to any other process on
            the host via /proc/<pid>/cmdline.

    Returns:
        Tuple of (stdout, stderr, return_code)
    """
    env = os.environ.copy()
    env['GNUPGHOME'] = GNUPGHOME

    cmd = ['gpg', '--batch', '--yes', '--no-tty'] + args
    logger.debug(f"Running GPG command: {' '.join(cmd)}")

    try:
        result = subprocess.run(
            cmd,
            input=input_data,
            capture_output=True,
            env=env,
            timeout=60,
            pass_fds=extra_fds,
        )
        return result.stdout, result.stderr, result.returncode
    except subprocess.TimeoutExpired:
        logger.error("GPG command timed out")
        return b'', b'Command timed out', 1
    except Exception as e:
        logger.error(f"GPG command failed: {e}")
        return b'', str(e).encode(), 1


def sign_with_key(key_id: str, document: bytes, passphrase: str) -> tuple:
    """Detach-sign document with key_id, passing any passphrase via a pipe
    file descriptor rather than argv (see run_gpg_command docstring)."""
    gpg_args = ['--detach-sign', '--armor', '--local-user', key_id]

    if not passphrase:
        return run_gpg_command(gpg_args, input_data=document)

    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, passphrase.encode('utf-8') + b'\n')
    finally:
        os.close(write_fd)

    try:
        gpg_args = ['--pinentry-mode', 'loopback', '--passphrase-fd', str(read_fd)] + gpg_args
        return run_gpg_command(gpg_args, input_data=document, extra_fds=(read_fd,))
    finally:
        os.close(read_fd)


@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint."""
    # Check if GPG is available
    stdout, stderr, rc = run_gpg_command(['--version'])
    if rc != 0:
        return jsonify({
            'status': 'unhealthy',
            'error': 'GPG not available',
            'details': stderr.decode()
        }), 500

    # Check if keys directory exists
    if not os.path.isdir(GNUPGHOME):
        return jsonify({
            'status': 'unhealthy',
            'error': f'Keys directory not found: {GNUPGHOME}'
        }), 500

    return jsonify({
        'status': 'healthy',
        'timestamp': datetime.utcnow().isoformat(),
        'gnupghome': GNUPGHOME,
        'gpg_version': stdout.decode().split('\n')[0] if stdout else 'unknown'
    })


@app.route('/keys', methods=['GET'])
@require_auth
def list_keys():
    """List available signing keys."""
    stdout, stderr, rc = run_gpg_command(['--list-secret-keys', '--keyid-format', 'long'])

    if rc != 0:
        return jsonify({
            'error': 'Failed to list keys',
            'details': stderr.decode()
        }), 500

    return jsonify({
        'keys': stdout.decode(),
        'trustee_mapping': TRUSTEE_KEYS
    })


@app.route('/sign', methods=['POST'])
@require_auth
def sign_document():
    """
    Sign a document with a trustee's PGP key.

    Expects multipart/form-data with:
    - file: The document to sign (binary)
    - trustee: Which trustee's key to use (trustee-a, trustee-b, trustee-c)
    - passphrase: (optional) Key passphrase if protected

    Returns:
    - The detached ASCII-armored signature (.asc)
    """
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400

    trustee = request.form.get('trustee', '').lower()
    if trustee not in TRUSTEE_KEYS:
        return jsonify({
            'error': f'Invalid trustee: {trustee}',
            'valid_trustees': list(TRUSTEE_KEYS.keys())
        }), 400

    key_id = TRUSTEE_KEYS.get(trustee)
    if not key_id:
        return jsonify({
            'error': f'No key configured for trustee: {trustee}'
        }), 400

    passphrase = request.form.get('passphrase', '')
    file = request.files['file']
    file_content = file.read()

    # Calculate file hash for audit
    file_hash = hashlib.sha256(file_content).hexdigest()
    logger.info(f"Signing document for {trustee}, hash: {file_hash[:16]}...")

    stdout, stderr, rc = sign_with_key(key_id, file_content, passphrase)

    if rc != 0:
        logger.error(f"Signing failed for {trustee}: {stderr.decode()}")
        return jsonify({
            'error': 'Signing failed',
            'details': stderr.decode()
        }), 500

    logger.info(f"Successfully signed document for {trustee}")

    return Response(
        stdout,
        mimetype='application/pgp-signature',
        headers={
            'Content-Disposition': f'attachment; filename="{file.filename}.asc"',
            'X-Document-Hash': file_hash,
            'X-Signed-By': trustee,
            'X-Signed-At': datetime.utcnow().isoformat()
        }
    )


@app.route('/verify', methods=['POST'])
@require_auth
def verify_signature():
    """
    Verify a PGP signature.

    Expects multipart/form-data with:
    - file: The original document
    - signature: The detached signature (.asc)

    Returns:
    - Verification result with signer info
    """
    if 'file' not in request.files or 'signature' not in request.files:
        return jsonify({'error': 'Both file and signature are required'}), 400

    file_content = request.files['file'].read()
    signature_content = request.files['signature'].read()

    # Write to temp files for GPG verification
    with tempfile.NamedTemporaryFile(delete=False) as doc_file:
        doc_file.write(file_content)
        doc_path = doc_file.name

    with tempfile.NamedTemporaryFile(delete=False, suffix='.asc') as sig_file:
        sig_file.write(signature_content)
        sig_path = sig_file.name

    try:
        stdout, stderr, rc = run_gpg_command(['--verify', sig_path, doc_path])

        # GPG outputs verification info to stderr
        output = stderr.decode()

        if rc == 0:
            return jsonify({
                'valid': True,
                'details': output
            })
        else:
            return jsonify({
                'valid': False,
                'details': output
            })
    finally:
        # Cleanup temp files
        os.unlink(doc_path)
        os.unlink(sig_path)


@app.route('/import-key', methods=['POST'])
@require_auth
def import_key():
    """
    Import a PGP private key.

    Expects multipart/form-data with:
    - key: The private key file (ASCII-armored)
    - trustee: Which trustee this key belongs to

    Disabled by default (GPG_SIGNER_ALLOW_KEY_IMPORT=false): this endpoint
    is provisioning-only surface, not something that should stay reachable
    for the lifetime of the container behind the same shared secret used
    for day-to-day signing.
    """
    if not ALLOW_KEY_IMPORT:
        return jsonify({
            'error': 'Key import is disabled. Set GPG_SIGNER_ALLOW_KEY_IMPORT=true '
                     'for the duration of key provisioning only, then unset it.'
        }), 403

    if 'key' not in request.files:
        return jsonify({'error': 'No key file provided'}), 400

    trustee = request.form.get('trustee', '').lower()
    if trustee not in TRUSTEE_KEYS:
        return jsonify({
            'error': f'Invalid trustee: {trustee}',
            'valid_trustees': list(TRUSTEE_KEYS.keys())
        }), 400

    key_content = request.files['key'].read()

    # Import the key
    stdout, stderr, rc = run_gpg_command(['--import'], input_data=key_content)

    if rc != 0:
        return jsonify({
            'error': 'Key import failed',
            'details': stderr.decode()
        }), 500

    # Get the key ID from the import output
    output = stderr.decode()
    logger.info(f"Imported key for {trustee}: {output}")

    return jsonify({
        'success': True,
        'trustee': trustee,
        'details': output
    })


if __name__ == '__main__':
    # For development only - use gunicorn in production (see Dockerfile CMD).
    # debug=True enables the Werkzeug debugger, which is remote code
    # execution if this ever runs reachable from anything but a
    # developer's own machine, so it is opt-in and off by default here too.
    debug = os.environ.get('FLASK_DEBUG', 'false').strip().lower() in ('1', 'true', 'yes', 'on')
    app.run(host='0.0.0.0', port=8080, debug=debug)
