# skorch Workflow Files

This directory holds n8n workflow definitions (the JSON export format n8n
itself produces) for deployment to a skorch instance.

```
workflows/
├── shared/     # workflows deployed to every environment
├── dev/        # dev-only workflows
├── staging/    # staging-only workflows
├── prod/       # prod-only workflows
└── examples/   # a generic example, ships with the framework
```

Only `examples/` ships with the framework; `dev/`, `staging/`, `prod/` and
`shared/` are instance-specific and intentionally empty here (see
`.gitignore`). An instance keeps its own real workflow exports in its own
repository or vault, not in the public framework.

## Importing a workflow

n8n's own REST API imports a workflow export directly:

```bash
curl -X POST "https://<your-n8n-host>/api/v1/workflows" \
  -H "Content-Type: application/json" \
  -H "X-N8N-API-KEY: $N8N_API_KEY" \
  -d @workflows/examples/example-workflow.json
```

or via the n8n editor UI's Import from File.
