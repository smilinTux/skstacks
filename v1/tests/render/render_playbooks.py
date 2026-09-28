#!/usr/bin/env python3
"""Render every v1 service's deploy playbook through REAL Ansible, with the
public example vars in this directory, and validate what comes out.

Why this exists: the pytest suite renders templates with plain jinja2 and
ChainableUndefined, which forgives things Ansible does not. Bugs that only
Ansible raises (an ``is mapping`` test on an absent key, a task reading a
vault key under a different name than the README documents, a filter that
only Ansible ships) used to surface only on a VM run. This gate catches them
on every pull request, without a VM, a vault, or a cluster.

How: each ``v1/ansible/*/*/deploy_<svc>-<env>.yml`` is rewritten into a
render-only playbook that runs against localhost:

* kept: set_fact, include_vars, assert, fail, debug, stat, setup, meta, and
  the shared select_vault_file.yml, so vars are resolved exactly as a deploy
  resolves them;
* every template task becomes a set_fact that renders the same src through
  Ansible's own template lookup (the same Templar the template action uses),
  with the task's own loop/when/vars, and records {dest: text}; copy tasks
  record their content or source file the same way (under ``_copy/``). One
  copy task at the end of the play writes the lot as JSON, which the driver
  unpacks into an output dir. That is one module run per playbook instead of
  a template+file module run per file, which is what makes the gate fast
  enough for every PR;
* shell/command task BODIES are rendered too (into ``_shell/``) and then
  syntax-checked with ``bash -n``: a Jinja or undefined-var error in a shell
  task is just as fatal on a real deploy;
* everything that would touch a host (apt, docker_*, ufw, file, uri,
  cloudflare_dns, the shell/command itself, ...) is dropped. When a dropped
  task registers a result, a canned stand-in result is set instead (see
  FAKE_STDOUT) so later tasks that read it still render;
* included task files (include_tasks/import_tasks) are rewritten the same
  way, except select_vault_file.yml, which runs as-is.

The vault a deploy would read is replaced by ``vars/<svc>.example.yml``
(obviously fake, example.test values only). A second pass merges
``vars/<svc>.set.yml`` on top where it exists, to exercise the optional
hooks (parity hooks, placement, router hooks) on their "set" path.

Then every rendered file is checked: YAML/JSON/TOML must parse, compose
files must pass the Compose Specification schema plus the swarm checks from
test_compose_templates_schema.py, no Jinja may be left unrendered, no env
file may carry a ``None`` value, rendered shell must pass ``bash -n``, and
no ``php -r`` body may put ``\\n`` inside a single-quoted PHP string (a
literal backslash-n; see the skhub config.php fix in v2.19.1). Where a
service README documents its vault keys, every key the example file needs
must appear in that README, so the documented contract and the keys the
tasks read cannot drift apart silently.

Usage: python v1/tests/render/render_playbooks.py [--service NAME ...]
       [--env dev|staging|prod ...] [--pass base|set] [--keep DIR] [-j N]
Exit status is non-zero if any render or check fails.
"""
from __future__ import annotations

import argparse
import base64
import concurrent.futures
import copy
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
V1 = HERE.parents[1]
ANSIBLE = V1 / "ansible"
VARS = HERE / "vars"
FIXTURES = HERE / "fixtures"

sys.path.insert(0, str(V1 / "tests"))
from test_compose_templates_schema import VALIDATOR, check_swarm  # noqa: E402

DOMAIN = "example.test"
CLUSTER = "render"

# Task keys that are not the module itself.
META_KEYS = {
    "name", "tags", "when", "register", "loop", "loop_control", "vars",
    "become", "become_user", "become_method", "ignore_errors", "no_log",
    "run_once", "delegate_to", "delegate_facts", "changed_when",
    "failed_when", "notify", "until", "retries", "delay", "check_mode",
    "environment", "args", "listen", "any_errors_fatal", "throttle",
    "async", "poll", "diff", "with_items", "with_dict", "with_list",
    "block", "rescue", "always", "timeout", "ignore_unreachable",
}
# Keys a render-only task must not carry: they target another host, a real
# user, or real privileges this unprivileged local run does not have.
STRIP_KEYS = ("become", "become_user", "become_method", "delegate_to",
              "delegate_facts", "notify", "environment", "async", "poll",
              "until", "retries", "delay", "throttle")
KEEP_MODULES = {"set_fact", "include_vars", "assert", "fail", "debug",
                "stat", "meta", "setup"}
TEMPLATE_MODULES = {"template"}
COPY_MODULES = {"copy"}
SHELL_MODULES = {"shell", "command"}
INCLUDE_MODULES = {"include_tasks", "import_tasks"}
PREFIXES = ("ansible.builtin.", "ansible.legacy.")

# Canned stdout for a dropped task's registered result, by register name.
# These are TEST DOUBLES for live-cluster answers (docker service ls, node
# hostnames, cert lookups), never real state. Anything not listed gets "".
FAKE_STDOUT = {
    "fence_service_detection": "skfenceha",
    "traefik_detection": "skfenceha",
    "socket_proxy_detection": "skfenceha",
    "actual_cert_dir": "/var/data/runtime/skfenceha-dev/certs/mail.example.test",
    "skfenceha_logrotate_nodes": "render-node-1",
    "skfence_logrotate_nodes": "render-node-1",
    "garage_node": "render-node-1",
    "garage_replicas": "1/1",
}


def module_of(task: dict) -> str | None:
    for key in task:
        if key not in META_KEYS and not key.startswith("with_"):
            return key
    return None


def short(module: str) -> str:
    for prefix in PREFIXES:
        if module.startswith(prefix):
            return module[len(prefix):]
    return module


def jinja_literal(value) -> str:
    """A Python value as a Jinja literal (Jinja has no `null`)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        return "[" + ", ".join(jinja_literal(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{json.dumps(k)}: {jinja_literal(v)}" for k, v in value.items()) + "}"
    raise TypeError(value)


# Canned slurp content (base64), by register name. Same caveat as above.
FAKE_CONTENT = {
    "persisted_bouncer_key": base64.b64encode(b"example-bouncer-key-not-a-secret").decode(),
}


def fake_result(register: str) -> dict:
    out = FAKE_STDOUT.get(register, "")
    return {
        "changed": False, "failed": False, "skipped": False, "rc": 0,
        "stdout": out, "stdout_lines": [out] if out else [],
        "stderr": "", "stderr_lines": [], "msg": "", "content": FAKE_CONTENT.get(register, ""),
        "exists": False, "stat": {"exists": False}, "results": [],
    }


def loop_keys(task: dict) -> list[str]:
    return [k for k in task if k == "loop" or k.startswith("with_")]


def loop_of(task: dict):
    keys = loop_keys(task)
    return task[keys[0]] if keys else None


class Rewriter:
    """Turns deploy tasks into render-only tasks (see module docstring)."""

    def __init__(self, prefix: str = "main"):
        self.prefix = prefix
        self.counter = 0

    def _collect(self, task: dict, dest: str, value_expr: str, extra_vars: dict, label: str,
                 key_expr: str = "_render_dest") -> dict:
        """A set_fact that adds {dest: rendered text} to _render_files, with
        the task's own loop/when/vars, so the text is rendered in exactly the
        variable context the real task would have had. One module call at
        the end of the play writes them all (see build_render_playbook):
        far faster than a template/file module round trip per file."""
        new = {
            "name": f"render {label}: {task.get('name', '')}",
            "set_fact": {"_render_files": "{{ _render_files | default({}) | combine({" + key_expr + ": "
                         + value_expr + "}) }}"},
            "vars": {**(task.get("vars") or {}), "_render_dest": dest, **extra_vars},
        }
        for carry in loop_keys(task) + ["loop_control", "when"]:
            if carry in task:
                new[carry] = task[carry]
        return new

    def _template(self, task: dict, module: str) -> list[dict]:
        spec = task[module]
        if isinstance(spec, str) or not isinstance(spec.get("dest"), str) or not isinstance(spec.get("src"), str):
            return self._stub(task)
        opts = "convert_data=False"
        for opt in ("trim_blocks", "lstrip_blocks"):
            if opt in spec:
                opts += f", {opt}=" + ("True" if str(spec[opt]).lower() in ("true", "yes", "1") else "False")
        value = "lookup('ansible.builtin.template', _render_src, " + opts + ")"
        return [self._collect(task, spec["dest"], value, {"_render_src": spec["src"]}, "template")] \
            + self._stub(task)

    def _shell(self, task: dict, module: str) -> list[dict]:
        spec = task[module]
        body = spec if isinstance(spec, str) else (spec.get("cmd") or spec.get("_raw_params"))
        out: list[dict] = []
        if isinstance(body, str):
            self.counter += 1
            name = f"{self.prefix}-{self.counter:03d}"
            dest = ("_shell/" + name + ("-{{ ansible_loop.index }}" if loop_of(task) is not None else "")
                    + (".sh" if short(module) == "shell" else ".cmd"))
            copy_task = self._collect(task, dest, "_render_body", {"_render_body": body + "\n"}, "shell body")
            lc = dict(task.get("loop_control") or {})
            if loop_of(task) is not None:
                lc["extended"] = True
                copy_task["loop_control"] = lc
            # `when` may read the register this very task sets; guard it
            if "when" in copy_task and task.get("register"):
                reg = task["register"]
                if reg in json.dumps(copy_task["when"]):
                    copy_task.pop("when")
            out.append(copy_task)
        out += self._stub(task)
        return out

    def _stub(self, task: dict) -> list[dict]:
        reg = task.get("register")
        if not reg:
            return []
        result = fake_result(reg)
        loop = loop_of(task)
        if loop is None:
            return [{"name": f"render stub result: {reg}", "set_fact": {reg: result}}]
        loop_var = (task.get("loop_control") or {}).get("loop_var", "item")
        acc = f"_render_acc_{reg}"
        per_item = dict(result)
        per_item.pop("results")
        item_expr = jinja_literal(per_item)[:-1] + ', "item": ' + loop_var + "}"
        acc_task = {
            "name": f"render stub results: {reg}",
            "set_fact": {acc: "{{ (" + acc + " | default([])) + [" + item_expr + "] }}"},
        }
        for key in loop_keys(task):
            acc_task[key] = task[key]
        if "vars" in task:
            acc_task["vars"] = task["vars"]
        if "loop_control" in task:
            acc_task["loop_control"] = {k: v for k, v in task["loop_control"].items()
                                        if k in ("loop_var", "label")}
        result_expr = jinja_literal({k: v for k, v in result.items() if k != "results"})[:-1] \
            + ', "results": ' + acc + " | default([])}"
        return [acc_task, {"name": f"render stub result: {reg}",
                           "set_fact": {reg: "{{ " + result_expr + " }}"}}]

    def tasks(self, tasks) -> list[dict]:
        out: list[dict] = []
        for task in tasks or []:
            if not isinstance(task, dict):
                continue
            if "block" in task:
                new = {k: v for k, v in task.items() if k not in STRIP_KEYS}
                for part in ("block", "rescue", "always"):
                    if part in task:
                        new[part] = self.tasks(task[part])
                if new.get("block"):
                    out.append(new)
                continue
            module = module_of(task)
            if module is None:
                continue
            base = short(module)
            if base in INCLUDE_MODULES:
                new = {k: v for k, v in task.items() if k not in STRIP_KEYS and k != module}
                new[base] = task[module]
                out.append(new)
            elif base in KEEP_MODULES:
                new = {k: v for k, v in task.items() if k not in STRIP_KEYS and k != module}
                new[base] = task[module]
                out.append(new)
            elif base in TEMPLATE_MODULES:
                out += self._template(task, module)
            elif base in COPY_MODULES:
                spec = task[module]
                if isinstance(spec, dict) and "content" in spec and isinstance(spec.get("dest"), str):
                    out += [self._collect(task, "_copy/" + spec["dest"].lstrip("/"), "_render_body",
                                          {"_render_body": spec["content"]}, "copy content")] + self._stub(task)
                elif isinstance(spec, dict) and isinstance(spec.get("src"), str) \
                        and isinstance(spec.get("dest"), str) and not spec.get("remote_src"):
                    # a file or directory copy: verbatim by design, so it lands
                    # under _copy/ and the unrendered-Jinja check skips it. A
                    # directory is only marked here; unpack() copies the tree.
                    value = ("(lookup('ansible.builtin.file', _render_src, rstrip=False) if _render_src is file"
                             " else '" + DIR_MARK + "' ~ (_render_src | realpath)"
                             " ~ ('/' if _render_src.endswith('/') else ''))")
                    key = ("(_render_dest ~ ((_render_src | basename) if (_render_dest.endswith('/')"
                           " and _render_src is file) else ''))")
                    out += [self._collect(task, "_copy/" + spec["dest"].lstrip("/"), value,
                                          {"_render_src": spec["src"]}, "copy", key)] + self._stub(task)
                else:
                    out += self._stub(task)
            elif base in SHELL_MODULES:
                out += self._shell(task, module)
            else:
                out += self._stub(task)
        return out


def rewrite_task_file(path: Path) -> None:
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, list):
        return
    rewriter = Rewriter(prefix=f"{path.parent.parent.name}-{path.stem}")
    path.write_text(yaml.safe_dump(rewriter.tasks(data), sort_keys=False, width=100000))


DIR_MARK = "__render_copy_dir__:"
WRITE_TASK = {
    "name": "render: write every collected file in one go",
    "copy": {"content": "{{ _render_files | default({}) | to_json }}",
             "dest": "{{ render_outdir }}/" + "_render_files.json"},
}


def unpack(outdir: Path) -> None:
    """Split _render_files.json (dest -> text) into files under outdir."""
    bundle = outdir / "_render_files.json"
    if not bundle.exists():
        return
    for dest, text in json.loads(bundle.read_text()).items():
        target = outdir / dest.lstrip("/")
        if outdir.resolve() not in target.resolve().parents:
            raise ValueError(f"rendered dest escapes the output dir: {dest}")
        if text.startswith(DIR_MARK):
            # copy module semantics: "src/" copies the contents, "src" the dir
            src = text[len(DIR_MARK):]
            into = target if src.endswith("/") else target / Path(src.rstrip("/")).name
            shutil.copytree(src.rstrip("/"), into, dirs_exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    bundle.unlink()


def build_render_playbook(playbook: Path, dest: Path) -> int:
    """Write the render-only version of `playbook` to `dest`; returns the
    number of plays kept."""
    plays = yaml.safe_load(playbook.read_text())
    rewriter = Rewriter()
    out = []
    for play in plays:
        if not isinstance(play, dict) or "import_playbook" in play:
            continue
        if play.get("hosts") in ("all", "localhost"):
            continue  # the select-manager play; the inventory stands in for it
        out.append({
            "name": f"render {playbook.name}",
            "hosts": "localhost",
            "connection": "local",
            "become": False,
            # facts only where the playbook gathers them itself (skha's setup task)
            "gather_facts": False,
            "vars": play.get("vars") or {},
            "pre_tasks": rewriter.tasks(play.get("pre_tasks")),
            "tasks": rewriter.tasks(play.get("tasks")),
            "post_tasks": rewriter.tasks(play.get("post_tasks")) + [WRITE_TASK],
        })
    dest.write_text(yaml.safe_dump(out, sort_keys=False, width=100000))
    return len(out)


# ---------------------------------------------------------------------------
# Checks on the rendered output
# ---------------------------------------------------------------------------

# Jinja left in a TEMPLATE's output: `{{ name }}`/`{{ a.b | f }}` (spaces
# inside the braces, which Go/Docker/Grafana templates like `{{.Name}}` or
# `{{range ...}}` do not use) or a `{% if/for/set %}` statement.
LEFTOVER_JINJA = re.compile(r"\{\{-?\s+[A-Za-z_][\w.]*(\s*[|(\[][^}]*)?\s+-?\}\}|\{%-?\s+(if|for|set|endif|endfor|else)\b")
ENV_LINE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
PHP_R = re.compile(r"php\s+-r\s+\"((?:[^\"\\]|\\.)*)\"", re.S)
PHP_SQ_NEWLINE = re.compile(r"'(?:[^'\\\n]|\\.)*\\n(?:[^'\\\n]|\\.)*'")


def check_file(path: Path, rel: str) -> list[str]:
    findings: list[str] = []
    try:
        text = path.read_text()
    except (UnicodeDecodeError, OSError):
        return findings  # binary copy (images, ...): nothing to check
    ext = path.suffix.lower()
    # shell bodies legitimately carry Go/Docker templates like {{.Name}}
    if not rel.startswith(("_shell/", "_copy/")):
        m = LEFTOVER_JINJA.search(text)
        if m:
            line = text.count("\n", 0, m.start()) + 1
            findings.append(f"{rel}:{line}: unrendered Jinja {m.group(0)[:40]!r}")
    doc = None
    try:
        if ext in (".yml", ".yaml"):
            docs = list(yaml.safe_load_all(text))
            doc = docs[0] if len(docs) == 1 else None
        elif ext == ".json":
            doc = json.loads(text)
        elif ext == ".toml":
            tomllib.loads(text)
    except (yaml.YAMLError, json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
        findings.append(f"{rel}: does not parse: {str(exc).splitlines()[0]}")
        return findings
    if isinstance(doc, dict) and isinstance(doc.get("services"), dict):
        for err in sorted(VALIDATOR.iter_errors(doc), key=lambda e: list(map(str, e.path))):
            findings.append(f"{rel}: compose schema: {'/'.join(map(str, err.path)) or '<root>'}: {err.message}")
        errors: list[str] = []
        check_swarm(doc, errors)
        findings += [f"{rel}: swarm: {e}" for e in errors]
    if path.name.endswith(".env") or ".env." in path.name:
        for n, line in enumerate(text.splitlines(), 1):
            m = ENV_LINE.match(line.strip())
            if m and m.group(2).strip() in ("None", "none", "null"):
                findings.append(f"{rel}:{n}: env value {m.group(1)}={m.group(2).strip()}")
    if ext == ".j2":
        pass  # a template copied verbatim; its rendered twin is checked
    elif ext == ".sh" or path.name == "deploy" or text.startswith("#!/bin/bash") or text.startswith("#!/usr/bin/env bash"):
        proc = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
        if proc.returncode:
            findings.append(f"{rel}: bash -n: {proc.stderr.strip().splitlines()[0]}")
    for body in PHP_R.findall(text):
        for m in PHP_SQ_NEWLINE.finditer(body):
            findings.append(f"{rel}: php -r: '\\n' inside a single-quoted PHP string is a literal backslash-n: {m.group(0)[:50]}")
    return findings


def documented_keys(readme: Path) -> set[str] | None:
    """Every identifier in the README's vault-contract sections (headings
    naming the vault or instance vars), or None if it has no such section:
    then there is no documented contract to hold the example to."""
    if not readme.exists():
        return None
    words: set[str] = set()
    found = False
    level = 0
    fenced = False
    for line in readme.read_text().splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
        m = None if fenced else re.match(r"^(#+)\s+(.*)", line)
        if m:
            if found and level and len(m.group(1)) <= level:
                level = 0
            if re.search(r"vault|instance var", m.group(2), re.I):
                found, level = True, len(m.group(1))
            continue
        if level:
            words |= set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", line))
    return words if found else None


def contract_findings(service: str, stype: str, vault: dict) -> list[str]:
    words = documented_keys(ANSIBLE / stype / service / "README.md")
    ns = vault.get(service)
    if words is None or not isinstance(ns, dict):
        return []
    return [f"README.md: vault key {service}.{k} is read by the deploy but not documented"
            for k in ns if k not in words]


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

class Unsafe(str):
    """A vault value tagged `!unsafe` (Ansible writes it without templating)."""


class VarsLoader(yaml.SafeLoader):
    pass


class VarsDumper(yaml.SafeDumper):
    pass


VarsLoader.add_constructor("!unsafe", lambda loader, node: Unsafe(loader.construct_scalar(node)))
VarsDumper.add_representer(Unsafe, lambda dumper, value: dumper.represent_scalar(
    "!unsafe", str(value), style="|" if "\n" in value else None))


def load_vars(path: Path) -> dict:
    return yaml.load(path.read_text(), Loader=VarsLoader) or {}


def deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def discover(services: list[str] | None, envs: list[str]):
    for pb in sorted(ANSIBLE.glob("*/*/deploy_*-*.yml")):
        m = re.match(r"deploy_(.+)-(dev|staging|prod)\.yml$", pb.name)
        if not m:
            continue
        svc, env = m.groups()
        if services and svc not in services:
            continue
        if env in envs:
            yield pb.parent.parent.name, svc, env, pb


def write_inventory(path: Path, target_group: str) -> None:
    host = {
        "ansible_connection": "local",
        "ansible_python_interpreter": sys.executable,
        "domain": DOMAIN,
        "cluster_name": CLUSTER,
        "selected_manager": "localhost",
    }
    groups = {g: {"hosts": {"localhost": None}}
              for g in {"swarm_managers", "selected_manager_group", target_group}}
    path.write_text(yaml.safe_dump({"all": {"hosts": {"localhost": host}, "children": groups}}))


def run_one(work: Path, stype: str, svc: str, env: str, pb: Path, pass_name: str) -> dict:
    t0 = time.monotonic()
    res = {"service": svc, "env": env, "pass": pass_name, "status": "PASS",
           "files": 0, "findings": []}
    base_vars = VARS / f"{svc}.example.yml"
    set_vars = VARS / f"{svc}.set.yml"
    if not base_vars.exists():
        res.update(status="FAIL", findings=[f"missing {base_vars.relative_to(V1.parent)}"])
        return res
    vault = load_vars(base_vars)
    res["findings"] += contract_findings(svc, stype, vault)
    if pass_name == "set":
        vault = deep_merge(vault, load_vars(set_vars))

    run_dir = work / f"{svc}-{env}-{pass_name}"
    vault_dir = run_dir / "vaults" / stype / "group_vars" / env
    vault_dir.mkdir(parents=True)
    (vault_dir / f"{svc}-{env}_vault.yml").write_text(yaml.dump(vault, Dumper=VarsDumper))
    inv_dir = run_dir / "inventory"
    shutil.copytree(FIXTURES, inv_dir)
    target_group = "skfenceha_managers" if svc == "skfenceha" else "swarm_managers"
    write_inventory(inv_dir / "hosts.yml", target_group)
    outdir = run_dir / "out"
    (outdir / "_shell").mkdir(parents=True)

    render_pb = work / "ansible" / stype / svc / f"_render_{svc}-{env}-{pass_name}.yml"
    build_render_playbook(pb, render_pb)
    env_vars = dict(os.environ,
                    ANSIBLE_LOCAL_TEMP=str(run_dir / ".ansible_tmp"),
                    ANSIBLE_HOST_KEY_CHECKING="False",
                    ANSIBLE_RETRY_FILES_ENABLED="False",
                    ANSIBLE_DEPRECATION_WARNINGS="False",
                    ANSIBLE_NOCOLOR="1",
                    ANSIBLE_GATHERING="smart",
                    ANSIBLE_CACHE_PLUGIN="jsonfile",
                    ANSIBLE_CACHE_PLUGIN_CONNECTION=str(work / ".facts"),
                    ANSIBLE_CACHE_PLUGIN_TIMEOUT="86400")
    cmd = ["ansible-playbook", "-i", str(inv_dir / "hosts.yml"),
           "-e", f"render_outdir={outdir}",
           "-e", f"skstacks_vault_dir={run_dir / 'vaults'}",
           "-e", f"target_manager_group={target_group}",
           str(render_pb)]
    proc = subprocess.run(cmd, cwd=render_pb.parent, env=env_vars,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    (run_dir / "ansible.log").write_text(proc.stdout)
    if proc.returncode != 0:
        res["status"] = "FAIL"
        res["findings"].append("ansible-playbook failed: " + summarize(proc.stdout))
    else:
        unpack(outdir)
        for f in sorted(p for p in outdir.rglob("*") if p.is_file()):
            res["files"] += 1
            res["findings"] += check_file(f, str(f.relative_to(outdir)))
        if res["files"] == 0:
            res["findings"].append("rendered nothing")
    if res["findings"]:
        res["status"] = "FAIL"
    res["seconds"] = round(time.monotonic() - t0, 1)
    return res


def summarize(log: str) -> str:
    """The failing task's name and error message, not the full result dump."""
    lines = log.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("[ERROR]") or line.startswith("ERROR!"):
            task = next((lines[j] for j in range(i, -1, -1) if lines[j].startswith("TASK [")), "")
            task = task.split(" *")[0]
            msg = " ".join(l.strip() for l in lines[i:i + 6] if l.strip() and not l.startswith("TASK ["))
            msg = msg.split(" Origin: ")[0]
            return f"{task} {msg}"[:700]
    return " ".join(lines[-8:])[:700]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--service", action="append")
    ap.add_argument("--env", action="append", choices=["dev", "staging", "prod"])
    ap.add_argument("--pass", dest="passes", action="append", choices=["base", "set"])
    ap.add_argument("--keep", help="render into this dir and keep it")
    ap.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 2)
    args = ap.parse_args()
    envs = args.env or ["dev", "staging", "prod"]
    passes = args.passes or ["base", "set"]

    if shutil.which("ansible-playbook") is None:
        print("ansible-playbook not found (pip install ansible-core)", file=sys.stderr)
        return 2
    work = Path(args.keep).resolve() if args.keep else Path(tempfile.mkdtemp(prefix="skstacks-render-"))
    if args.keep and work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True, exist_ok=True)
    try:
        # a private copy of the playbook tree: render playbooks and rewritten
        # task files live beside the originals (playbook_dir-relative paths)
        shutil.copytree(ANSIBLE, work / "ansible")
        for task_file in (work / "ansible").glob("**/tasks/*.yml"):
            if task_file.name != "select_vault_file.yml":
                rewrite_task_file(task_file)
        jobs = []
        for stype, svc, env, pb in discover(args.service, envs):
            for p in passes:
                if p == "set" and not (VARS / f"{svc}.set.yml").exists():
                    continue
                jobs.append((stype, svc, env, work / "ansible" / pb.relative_to(ANSIBLE), p))
        if not jobs:
            print("no playbooks matched", file=sys.stderr)
            return 2
        t0 = time.monotonic()
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
            results = list(pool.map(lambda j: run_one(work, *j), jobs))
        print(f"{'SERVICE':<11} {'ENV':<8} {'PASS':<5} {'RESULT':<6} {'FILES':>5} {'SECS':>5}")
        for r in results:
            print(f"{r['service']:<11} {r['env']:<8} {r['pass']:<5} {r['status']:<6} {r['files']:>5} {r.get('seconds', 0):>5}")
            for f in r["findings"]:
                print(f"    - {f}")
        failed = [r for r in results if r["status"] != "PASS"]
        print(f"\n{len(results) - len(failed)}/{len(results)} renders passed in {time.monotonic() - t0:.0f}s")
        return 1 if failed else 0
    finally:
        if not args.keep:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
