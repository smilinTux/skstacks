# GPG Signer Service

A small REST API for detached PGP document signing, verification and key
listing. It runs as an opt-in sidecar container in the skorch (n8n) stack,
so a workflow can ask a trusted signer to countersign a document with one
of several pre-provisioned keys ("trustees") without the workflow ever
handling private key material itself.

`skorch.enable_gpg_signer` is `false` by default: the service needs real
PGP keys provisioned before it does anything useful, so an instance turns
it on deliberately once those keys exist.

## Architecture

```
n8n (workflow) --> gpg-signer:8080 --> /keys volume (PGP private keys)
```

The sidecar is reachable only on the skorch stack's internal Docker
network; it is never routed through Traefik.

## API Endpoints

### Health Check
```
GET /health
```
Returns service health status and GPG version. Unauthenticated (it reveals
no more than a healthy/unhealthy status and the GPG version).

### List Keys
```
GET /keys
Authorization: Bearer <API_SECRET>
```
Lists available signing keys and the trustee mapping.

### Sign Document
```
POST /sign
Authorization: Bearer <API_SECRET>
Content-Type: multipart/form-data

Parameters:
- file: Document to sign (binary)
- trustee: trustee-a | trustee-b | trustee-c
- passphrase: (optional) Key passphrase

Returns: Detached ASCII-armored signature (.asc)
```

### Verify Signature
```
POST /verify
Authorization: Bearer <API_SECRET>
Content-Type: multipart/form-data

Parameters:
- file: Original document
- signature: Detached signature (.asc)

Returns: Verification result
```

### Import Key
```
POST /import-key
Authorization: Bearer <API_SECRET>
Content-Type: multipart/form-data

Parameters:
- key: ASCII-armored private key
- trustee: trustee-a | trustee-b | trustee-c
```
Disabled unless `GPG_SIGNER_ALLOW_KEY_IMPORT=true` is set on the
container (see Key Setup below). This is provisioning-only surface: leave
it off for the container's normal lifetime, since it is guarded by the
same shared secret used for day-to-day signing.

## Configuration

Environment variables:
- `GNUPGHOME`: GPG home directory (default: `/keys`)
- `GPG_SIGNER_API_SECRET`: API authentication token. Required: the service
  refuses every authenticated request (503) until this is set, it has no
  usable default.
- `GPG_SIGNER_ALLOW_KEY_IMPORT`: set to `true` to enable `/import-key`
  (default `false`)
- `GPG_SIGNER_MAX_UPLOAD_BYTES`: max request body size for `/sign` and
  `/verify` (default `26214400`, 25 MiB)
- `TRUSTEE_A_KEY_ID` / `TRUSTEE_B_KEY_ID` / `TRUSTEE_C_KEY_ID`: GPG key ID
  or fingerprint for each trustee
- `LOG_LEVEL`: Logging level (default: `INFO`)

## Key Setup

Key provisioning happens once, out of band, and never touches the public
framework: generate the keys, import them into the sidecar's `/keys`
volume, then turn key import back off.

```bash
# Generate a key for each trustee (do this somewhere trusted, not on the
# sidecar host)
gpg --full-generate-key
# Choose: RSA and RSA, 4096 bits, an expiry your policy requires
# Name / email: whatever identifies this trustee to your organization

# Export the private key
gpg --export-secret-keys --armor <trustee-key-id-or-email> > trustee-a.key

# Get the key ID
gpg --list-secret-keys --keyid-format long
```

Import it into the running sidecar (with `GPG_SIGNER_ALLOW_KEY_IMPORT=true`
set for this window only):

```bash
curl -X POST http://gpg-signer:8080/import-key \
  -H "Authorization: Bearer $API_SECRET" \
  -F "key=@trustee-a.key" \
  -F "trustee=trustee-a"
```

Then set `TRUSTEE_A_KEY_ID` (etc.) to the imported key's ID and unset
`GPG_SIGNER_ALLOW_KEY_IMPORT` (or set it back to `false`) before the next
deploy.

## Security Considerations

1. **Network isolation**: only reachable from the skorch stack's internal
   Docker network, never routed through Traefik.
2. **Key protection**: keys live in a volume mounted `0700`, owned by the
   container's non-root user.
3. **API authentication**: a bearer token is required for every operation
   except `/health`; the comparison is constant-time and the service fails
   closed (refuses everything) if the token is unset.
4. **No key export**: the API has no endpoint that returns private key
   material.
5. **Audit logging**: every sign/verify/import call is logged with a
   document hash (never the document or key contents).
6. **Single shared secret, multiple trustees**: `GPG_SIGNER_API_SECRET` is
   one credential that can sign as *any* configured trustee. This service
   does not implement per-trustee authorization; if the multi-party
   guarantee that separate trustees imply matters for your use case, put a
   layer in front of this sidecar (e.g. per-trustee tokens or an
   authorizing workflow step) rather than relying on the sidecar alone.

## Building the Image

CI builds and publishes this image to
`ghcr.io/smilintux/skstacks-gpg-signer` on every `skstacks-v*` release tag
(see `.github/workflows/gpg-signer-image.yml`). To build locally:

```bash
cd v1/ansible/optional/skorch/src/gpg-signer
docker build -t gpg-signer:local .
```

## Tests

```bash
cd v1/ansible/optional/skorch/src/gpg-signer
python3 -m pip install -r requirements.txt pytest
python3 -m pytest tests/ -v
```

## License

Copyright (C) 2026 S&K Holding QT (Quantum Technologies)
GNU AGPL v3
