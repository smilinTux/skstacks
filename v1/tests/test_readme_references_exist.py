"""Every README.md path mentioned in a v1/ansible comment must actually
exist. PR #40 fixed one dangling reference; skfenceha carried a second:
"Set the skfenceha vault vars (see README.md for the full reference)" in
all three deploy playbooks, but skfenceha has no README.md of its own (it
shares vault-var documentation with core/skfence, which the same file's
header comment already points to for its other two README references). A
dangling doc pointer sends the next operator looking for a file that isn't
there, right when they're trying to find the vault vars they're missing.
"""
import pathlib
import re

ANSIBLE = pathlib.Path(__file__).resolve().parents[1] / "ansible"
REPO_ROOT = ANSIBLE.parents[1]

_README_TOKEN_RE = re.compile(r"(?<![\w./-])([\w./-]*README\.md)")
_SCAN_SUFFIXES = (".yml", ".yaml", ".j2", ".sh")


def _iter_readme_refs():
    """Yield (path, lineno, token) for every README.md path mentioned in a
    comment line under v1/ansible."""
    for path in sorted(ANSIBLE.rglob("*")):
        if not path.is_file() or path.suffix not in _SCAN_SUFFIXES:
            continue
        text = path.read_text(errors="replace")
        for lineno, line in enumerate(text.splitlines(), 1):
            if "#" not in line:
                continue
            comment = line.split("#", 1)[1]
            for m in _README_TOKEN_RE.finditer(comment):
                yield path, lineno, m.group(1)


def _service_root(path):
    """core/<service>/ or optional/<service>/ under v1/ansible - the
    directory every service-relative README.md reference is rooted at,
    regardless of how deeply nested the referencing file itself is (a
    template three directories down still says "see README.md" meaning
    its own service's top-level README, not a README beside itself)."""
    parts = path.relative_to(ANSIBLE).parts
    return ANSIBLE / parts[0] / parts[1]


def _resolve(path, token):
    """v1/... tokens are repo-root-relative (the convention already used in
    several header comments); anything else - a bare README.md or a
    subdirectory-relative path like src/error-pages/README.md or
    src/gpg-signer/README.md - is relative to the referencing file's own
    service root, not the file's own directory."""
    if token.startswith("v1/"):
        return REPO_ROOT / token
    return _service_root(path) / token


README_REFS = list(_iter_readme_refs())


def test_finds_at_least_one_readme_reference():
    assert README_REFS, "expected at least one README.md reference under v1/ansible"


def test_every_readme_reference_resolves_to_a_real_file():
    dangling = sorted(
        f"{path.relative_to(ANSIBLE)}:{lineno} references {token!r} "
        f"-> {_resolve(path, token).relative_to(REPO_ROOT)} (does not exist)"
        for path, lineno, token in README_REFS
        if not _resolve(path, token).is_file()
    )
    assert not dangling, "\n".join(dangling)


def test_resolve_handles_repo_root_relative_token():
    fake_path = ANSIBLE / "core" / "skfenceha" / "deploy_skfenceha-prod.yml"
    assert _resolve(fake_path, "v1/README.md") == REPO_ROOT / "v1" / "README.md"


def test_resolve_handles_service_relative_token_from_the_service_root():
    fake_path = ANSIBLE / "core" / "skfenceha" / "deploy_skfenceha-prod.yml"
    assert _resolve(fake_path, "src/error-pages/README.md") == (
        ANSIBLE / "core" / "skfenceha" / "src" / "error-pages" / "README.md"
    )


def test_resolve_handles_bare_readme_token():
    fake_path = ANSIBLE / "core" / "skfenceha" / "deploy_skfenceha-prod.yml"
    assert _resolve(fake_path, "README.md") == ANSIBLE / "core" / "skfenceha" / "README.md"


def test_resolve_roots_a_deeply_nested_file_at_its_service_root():
    """A template several directories under the service root still means
    its own service's top-level README, not a README beside itself."""
    fake_path = ANSIBLE / "optional" / "skorch" / "src" / "config" / "skorch" / "skorch.yml.j2"
    assert _resolve(fake_path, "src/gpg-signer/README.md") == (
        ANSIBLE / "optional" / "skorch" / "src" / "gpg-signer" / "README.md"
    )
    assert _resolve(fake_path, "README.md") == ANSIBLE / "optional" / "skorch" / "README.md"
