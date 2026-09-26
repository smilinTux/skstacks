"""list_images.py must find every image a v1 service's compose template
references and resolve tags pulled through a Jinja default() filter, so the
image-availability canary knows exactly what to try to pull. A tag with no
default has nothing to resolve to and must come back as "<unresolved>" so
the canary knows to skip it instead of guessing."""
import pathlib
import subprocess
import sys

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "list_images.py"
REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def run_list_images(root):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(root)],
        capture_output=True, text=True, check=True,
    )
    rows = {}
    for line in result.stdout.splitlines():
        services, image = line.split("\t")
        rows[image] = services
    return rows


def write_template(root, service, contents):
    tpl_dir = root / "v1" / "ansible" / "optional" / service / "src" / "config" / service
    tpl_dir.mkdir(parents=True, exist_ok=True)
    (tpl_dir / f"{service}.yml.j2").write_text(contents)


def test_literal_image(tmp_path):
    write_template(tmp_path, "svcone", "services:\n  svcone:\n    image: example.com/svcone:1.0.0\n")
    rows = run_list_images(tmp_path)
    assert rows["example.com/svcone:1.0.0"] == "svcone"


def test_image_with_default_tag_filter(tmp_path):
    write_template(
        tmp_path, "svctwo",
        "services:\n  svctwo:\n"
        "    image: example.com/svctwo:{{ svctwo.VERSION | default('2.3') }}\n",
    )
    rows = run_list_images(tmp_path)
    assert rows["example.com/svctwo:2.3"] == "svctwo"


def test_image_with_no_default_is_unresolved(tmp_path):
    write_template(
        tmp_path, "svcthree",
        "services:\n  svcthree:\n"
        "    image: example.com/svcthree:{{ svcthree.VERSION }}\n",
    )
    rows = run_list_images(tmp_path)
    assert rows["example.com/svcthree:<unresolved>"] == "svcthree"


def test_duplicate_image_across_services_is_deduped(tmp_path):
    write_template(tmp_path, "svcfour", "services:\n  svcfour:\n    image: example.com/shared:9\n")
    write_template(tmp_path, "svcfive", "services:\n  svcfive:\n    image: example.com/shared:9\n")
    rows = run_list_images(tmp_path)
    assert set(rows["example.com/shared:9"].split(",")) == {"svcfour", "svcfive"}
    assert len(rows) == 1


def test_every_published_service_contributes_at_least_one_image():
    """Catches a template the image: regex misses: every service that ships
    a compose template must show up as the source of at least one image."""
    published_services = sorted(
        p.name for p in (REPO_ROOT / "v1" / "ansible" / "optional").iterdir() if p.is_dir()
    )
    assert published_services, "expected at least one published v1 service"
    rows = run_list_images(REPO_ROOT)
    all_services = set()
    for services in rows.values():
        all_services.update(services.split(","))
    missing = [s for s in published_services if s not in all_services]
    assert not missing, f"services with no discovered image: {missing}"
