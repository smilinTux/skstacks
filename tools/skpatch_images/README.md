# skpatch patched images

Most HIGH/CRITICAL findings in the estates' images are OS packages that
upstream has not rebuilt yet, often in images with no newer tag to move to.
Renovate cannot fix those. This pipeline does, and keeps doing it:

1. **Build (daily, GitHub Actions)** `.github/workflows/skpatch-images.yml`:
   for every image a v1 template pins (`plan.py`), `patch_one.sh` scans it with
   Trivy, and when it has fixable OS-package HIGH/CRITICAL CVEs, patches exactly
   those with Copacetic, checks the run config is unchanged and the patched image
   scans better, and pushes it as a tag of `ghcr.io/smilintux/skpatched`. The tag
   comes from the upstream digest and the fix list, so an unchanged day re-uses
   the same tag. `assemble.py` writes the `skpatch-patched-images` artifact.
2. **PR (timer on the ops host)** `update_overrides.py` renders that artifact into
   `v1/ansible/shared/vars/skpatch_patched_images.yml`, opens a PR when it
   changed, closes the PR it supersedes, and queues green skpatch PRs.
3. **Deploy** `v1/ansible/shared/tasks/skpatch_image_overrides.yml` (run at the
   top of the pre-pull) swaps each exact upstream reference in a rendered stack
   file for its patched build. A release carries the mapping; the estate's normal
   rollout applies it.

Limits: OS packages only (language libraries need an upstream release);
linux/amd64 only (the swap is skipped on a swarm with other architectures);
end-of-life bases (for example Debian 11) cannot be patched and show up as
`patch-failed` in the run summary, which is the list of images to replace.

Estate knobs: `skstacks_patched_images: false` deploys upstream images only;
`skstacks_patched_images_skip: [<repository>, ...]` keeps chosen images upstream.
