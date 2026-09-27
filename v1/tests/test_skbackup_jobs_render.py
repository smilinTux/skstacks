"""Each skbackup.jobs entry renders into Duplicati's own export/import JSON
schema. Sources are rewritten under /source (the container-side mount of
skbackup.source_path); every job wires in the shared exclude filter file;
and the resolved passphrase falls back from the per-job value to the
instance-wide skbackup.passphrase."""
import json
import pathlib

import jinja2

ANSIBLE = pathlib.Path(__file__).resolve().parents[1] / "ansible"
JOB_TPL = ANSIBLE / "optional/skbackup/src/config/skbackup/job.json.j2"


def render(job, skbackup=None, env_name="prod"):
    tpl_env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    out = tpl_env.from_string(JOB_TPL.read_text()).render(
        app="skbackup", env=env_name, job=job, skbackup=skbackup or {},
    )
    return json.loads(out)


def test_sources_are_rewritten_under_source_mount():
    doc = render({
        "name": "dumps",
        "sources": ["/skorch-prod/database-backup", "/skgallery-prod/database-backup"],
        "destination_url": "file:///backups/dumps",
        "passphrase": "correct-horse-battery-staple",
    })
    assert doc["Backup"]["Sources"] == [
        "/source/skorch-prod/database-backup",
        "/source/skgallery-prod/database-backup",
    ]


def test_destination_url_passed_through_verbatim():
    doc = render({
        "name": "dumps", "sources": ["/x"],
        "destination_url": "s3://bucket/prefix?auth-username=k&auth-password=s",
        "passphrase": "x",
    })
    assert doc["Backup"]["TargetURL"] == "s3://bucket/prefix?auth-username=k&auth-password=s"


def test_job_wires_in_the_shared_exclude_filter():
    doc = render({"name": "dumps", "sources": ["/x"], "destination_url": "file:///backups/x", "passphrase": "x"})
    settings = {s["Name"]: s["Value"] for s in doc["Backup"]["Settings"]}
    assert settings["filter-file"] == "/config/exclude.filter"


def test_per_job_passphrase_wins_over_instance_default():
    doc = render(
        {"name": "dumps", "sources": ["/x"], "destination_url": "file:///backups/x", "passphrase": "job-specific"},
        skbackup={"passphrase": "instance-default"},
    )
    settings = {s["Name"]: s["Value"] for s in doc["Backup"]["Settings"]}
    assert settings["passphrase"] == "job-specific"


def test_falls_back_to_instance_wide_passphrase():
    doc = render(
        {"name": "dumps", "sources": ["/x"], "destination_url": "file:///backups/x"},
        skbackup={"passphrase": "instance-default"},
    )
    settings = {s["Name"]: s["Value"] for s in doc["Backup"]["Settings"]}
    assert settings["passphrase"] == "instance-default"


def test_retention_defaults_to_30_days_and_is_overridable():
    default_doc = render({"name": "dumps", "sources": ["/x"], "destination_url": "file:///backups/x", "passphrase": "x"})
    settings = {s["Name"]: s["Value"] for s in default_doc["Backup"]["Settings"]}
    assert settings["keep-time"] == "30D"

    custom_doc = render({
        "name": "dumps", "sources": ["/x"], "destination_url": "file:///backups/x",
        "passphrase": "x", "retention": "90D",
    })
    settings = {s["Name"]: s["Value"] for s in custom_doc["Backup"]["Settings"]}
    assert settings["keep-time"] == "90D"


def test_no_passphrase_anywhere_renders_empty_string_not_a_crash():
    # The template itself must not silently invent a passphrase; the empty
    # string is the fail-closed sentinel the deploy playbook's assert task
    # checks for (see test_skbackup_secrets_fail_closed.py). Rendering must
    # not raise -- the *playbook* is what refuses to deploy on this value.
    doc = render({"name": "dumps", "sources": ["/x"], "destination_url": "file:///backups/x"})
    settings = {s["Name"]: s["Value"] for s in doc["Backup"]["Settings"]}
    assert settings["passphrase"] == ""
