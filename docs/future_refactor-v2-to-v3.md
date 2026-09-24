# 1. Freeze v2
git tag v2-final

# 2. Promote v3
mv v3 v2-new
git mv v2 v2-legacy
git mv v2-new v2

# 3. Update CI
mv .forgejo/workflows/v3-test.yaml .forgejo/workflows/v2-deploy.yaml
