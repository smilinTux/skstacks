# Vendored file: compose-spec.json

Source: https://github.com/compose-spec/compose-spec/blob/main/schema/compose-spec.json
Pinned commit: 914ec15d1fa498969c0df5c1d672306db3256089
Fetched: 2026-09-26
Fetch command:
    curl -sL "https://raw.githubusercontent.com/compose-spec/compose-spec/914ec15d1fa498969c0df5c1d672306db3256089/schema/compose-spec.json" -o compose-spec.json

Used by v1/tests/test_compose_templates_schema.py to validate every rendered
compose/stack template against the official Compose Specification JSON
Schema. Re-vendor by bumping the pinned commit above and re-running the
curl command; do not hand-edit the JSON file.
