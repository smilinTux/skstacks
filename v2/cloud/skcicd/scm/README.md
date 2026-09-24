# skcicd / scm — pluggable Git + CI provider (where the GitOps repo lives)

Kubefirst-style: the installer **generates a GitOps repo** and wires CI on the Git
provider you choose. SKStacks makes that provider swappable — sovereign by default,
but meet teams where they are.

| Provider | What | Hosting | When |
|---|---|---|---|
| **skgit** ([`skgit/`](skgit/)) | **Forgejo** (Gitea fork) + Forgejo Actions | **self-host (sovereign default)** | Full sovereignty; lightweight; our default |
| **gitlab** ([`gitlab/`](gitlab/)) | **GitLab** — Git + CI/CD pipelines + registry + the works | self-host (enterprise) | The "big" all-in-one DevOps platform; teams that want enterprise CI/CD self-hosted |
| **github** | GitHub + Actions | managed (SaaS) | Public projects / existing GitHub orgs |
| **gitea** | Gitea + Actions | self-host | Lighter alt to Forgejo |

## How the installer uses it
1. You pick a provider (the AI installer asks; default = **skgit**).
2. It creates the **GitOps repo** there (Kustomize overlays + ArgoCD app-of-apps),
   provisions a deploy token/app (stored in the secret backend), and registers the
   repo as an ArgoCD source.
3. CI runs on that provider (Forgejo Actions / GitLab CI / GitHub Actions / Gitea
   Actions) — the same pipeline rendered to the provider's workflow syntax
   (`v2/cicd/{forgejo,github}/` today; GitLab `.gitlab-ci.yml` added under `gitlab/`).
4. ArgoCD (K8s) + Ansible (Swarm/nodes) drive CD from the repo.

## Sovereignty ladder (mirrors the mesh/tunnel ladders)
**GitHub (ease) → GitLab (enterprise self-host) → skgit/Forgejo (sovereign).**
Start where your team is; graduate to fully sovereign without changing the GitOps
model — only the provider adapter changes.
