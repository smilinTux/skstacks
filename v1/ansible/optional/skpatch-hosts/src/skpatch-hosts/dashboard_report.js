// Read-only dashboard report for tools/skpatch_compliance.py. Runs INSIDE the
// dashboard container like register_systems.js (same signed five-minute
// session for an existing admin, REGISTER_AS), and only ever sends GET.
//
// Input (stdin): {register_as, history_days}
// Output: one line "@@SKP-REPORT@@<json>":
//   {systems: [{id, name, hostname, port, pkgManager, detected, disabled,
//               reachable, needs_reboot, boot_id, package_issues, updates,
//               security, lifecycle: {status, label, eol, days_to_eol,
//               support_end, days_to_support_end},
//               package_issue_list: [{pkgManager, key, title, message, detected_at}],
//               last_ok: {action: at}, last_clean_check,
//               sudo_denied: [{action, pkgManager, at, history_id, error, command}],
//               failed: [{action, at, history_id, error, tail, tail_is_raw}]}],
//    schedules: [{name, type, enabled, last_run_at, status, message}]}
// failed is the same "latest row per action" view for every action except
// check: a failed upgrade, reboot or autoremove whose latest run did not hit
// sudo (that is sudo_denied, its own class). A later success of the same
// action clears it, and for an upgrade so does a later success of any
// upgrade or a later clean check (history older than the last good run is
// never reported). tail holds the run's last meaningful lines.
// sudo_denied holds, per action, the LATEST history row within history_days
// when that row failed on sudo asking for a password. Keyed by action alone:
// one check covers several managers and the app records them as a joined
// list ("pacman,flatpak,snap") that changes between runs. A later run of the
// same action that did not hit sudo clears it, so the issue resolves itself
// once the host is fixed.
"use strict";
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const SUDO_DENIED = [
  /sudo: a password is required/,
  /We trust you have received the usual lecture/,
  /is not in the sudoers file/,
  /Sorry, user \S+ is not allowed to execute/,
  /sudo: a terminal is required/,
];

// A run whose whole output is sudo talking (an insult when `Defaults insults`
// is on, e.g. "I'm afraid I can't do that", or any other refusal) failed on
// sudo too, whatever the wording.
function isSudoDenied(text) {
  if (SUDO_DENIED.some((re) => re.test(text))) return true;
  const lines = String(text).split("\n").map((l) => l.trim()).filter(Boolean)
    .filter((l) => !/^sudo: unable to resolve host/.test(l));
  return lines.length > 0 && lines.every((l) => /^sudo: /.test(l));
}

// The last meaningful lines of a run: apt/dpkg progress, package lists and
// summary chatter are dropped, so what is left is what went wrong. When the
// stored text is ONLY progress (the app keeps the first 5000 characters of
// output, so a long run is cut mid-download) the raw tail is returned and
// flagged, never an empty "no error line".
const NOISE = [
  /^(Get|Hit|Ign):\d*\s/, /^Reading (package lists|state information|database)/, /^Building dependency tree/,
  /^Calculating upgrade/, /^\(Reading database/, /^(Preparing to unpack|Unpacking|Setting up|Processing triggers for|Selecting previously unselected)\b/,
  /^(Fetched|Need to get|After this operation)\b/, /^The following (packages|NEW packages|additional packages)/,
  /^\d+ upgraded, \d+ newly installed/, /^\d+ not fully installed or removed/, /^Use '.*apt autoremove' to remove/,
  /^\s+(?!.*\b(error|errors|returned|failed|status|warning|denied|broken|unmet)\b)[a-z0-9][a-z0-9.+:~-]*(\s+[a-z0-9][a-z0-9.+:~-]*)*\s*$/i, /^\d+% \[/, /^Progress: \[/, /^Scanning (processes|candidates|linux images)/, /^Running kernel seems/,
  /^No (services|containers|user sessions|VM guests) need to be restarted/, /^Restarting services/,
];
function tail(error, output, n = 10) {
  const raw = `${output || ""}\n${error || ""}`.split("\n").map((l) => l.replace(/\r/g, "").trimEnd()).filter((l) => l.trim());
  const seen = new Set();
  const lines = raw.filter((l) => !NOISE.some((re) => re.test(l))).filter((l) => (seen.has(l) ? false : seen.add(l)));
  if (lines.length) return { lines: lines.slice(-n).map((l) => l.slice(0, 240)), truncated: false };
  return { lines: raw.slice(-Math.min(n, 4)).map((l) => l.slice(0, 240)), truncated: true };
}

// Actions that bring packages up to date: a later success of any of them, or
// a later clean check, supersedes an older failure of any of them.
const UPGRADES = new Set(["upgrade_all", "full_upgrade", "upgrade_package", "upgrade_packages", "upgrade_selected"]);

function fail(msg) {
  console.error("report failed: " + msg);
  process.exit(1);
}

function secretKey() {
  const env = process.env;
  if (env.LUDASH_SECRET_KEY) return env.LUDASH_SECRET_KEY;
  if (env.LUDASH_SECRET_KEY_FILE) return fs.readFileSync(env.LUDASH_SECRET_KEY_FILE, "utf8").replace(/[\r\n]+$/g, "");
  const dbPath = env.LUDASH_DB_PATH || "./data/dashboard.db";
  return fs.readFileSync(path.join(path.dirname(dbPath), ".secret_key"), "utf8").trim();
}

function lookupUser(username) {
  const { DatabaseSync } = require("node:sqlite");
  const db = new DatabaseSync(process.env.LUDASH_DB_PATH || "./data/dashboard.db", { readOnly: true });
  try {
    return db.prepare("SELECT id, username, role, session_version, auth_provider FROM users WHERE username = ?").get(username);
  } finally {
    db.close();
  }
}

const b64url = (buf) => Buffer.from(buf).toString("base64url");

function signSession(user, key) {
  const now = Math.floor(Date.now() / 1000);
  const header = b64url(JSON.stringify({ alg: "HS256" }));
  const payload = b64url(JSON.stringify({
    userId: user.id,
    username: user.username,
    authMethod: user.auth_provider === "oidc" ? "oidc" : "password",
    sessionVersion: user.session_version,
    iat: now,
    exp: now + 300,
  }));
  const sig = b64url(crypto.createHmac("sha256", Buffer.from(key, "utf8")).update(header + "." + payload).digest());
  return header + "." + payload + "." + sig;
}

async function get(base, session, p) {
  const res = await fetch(base + "/api" + p, { headers: { Accept: "application/json", Cookie: `ludash_session=${session}` } });
  const text = await res.text();
  if (res.status !== 200) throw new Error(`GET ${p}: HTTP ${res.status}: ${text.slice(0, 300)}`);
  return JSON.parse(text);
}

const asList = (v) => {
  if (Array.isArray(v)) return v;
  if (typeof v === "string" && v) {
    try { const x = JSON.parse(v); return Array.isArray(x) ? x : []; } catch { return []; }
  }
  return [];
};

// The app stores UTC as "YYYY-MM-DD HH:MM:SS(.sss)" without a zone.
const parseAt = (s) => Date.parse(String(s || "").replace(" ", "T") + (/[zZ]|[+-]\d\d:?\d\d$/.test(String(s)) ? "" : "Z"));

async function main() {
  const cfg = JSON.parse(fs.readFileSync(0, "utf8"));
  if (!cfg.register_as) fail("register_as is not set");
  const user = lookupUser(cfg.register_as);
  if (!user) fail(`no dashboard user named ${JSON.stringify(cfg.register_as)}`);
  const base = `http://127.0.0.1:${process.env.LUDASH_PORT || 3001}`;
  const session = signSession(user, secretKey());
  const since = Date.now() - Number(cfg.history_days || 14) * 86400e3;

  const systems = (await get(base, session, "/systems")).systems || [];
  const out = [];
  for (const s of systems) {
    const history = (await get(base, session, `/systems/${s.id}/history`)).history || [];
    const rows = [];
    const lastOk = {};
    let lastCleanCheck = 0;
    for (const h of history) {
      const at = parseAt(h.startedAt ?? h.started_at);
      if (!Number.isFinite(at)) continue;
      if (h.status === "success") {
        lastOk[h.action] = Math.max(lastOk[h.action] || 0, at);
        if (h.action === "check") lastCleanCheck = Math.max(lastCleanCheck, at);
      }
      if (at >= since) rows.push({ at, h });
    }
    const lastUpgradeOk = Math.max(0, ...[...UPGRADES].map((a) => lastOk[a] || 0));
    const latest = new Map();
    for (const r of rows) {
      const cur = latest.get(r.h.action);
      if (!cur || r.at > cur.at) latest.set(r.h.action, r);
    }
    const denied = [];
    const failed = [];
    for (const { at, h } of latest.values()) {
      if (h.status === "success") continue;
      const text = `${h.error || ""}\n${h.output || ""}`;
      const sudo = isSudoDenied(text);
      // stale: the same action, or for upgrades any upgrade or a clean
      // check, has succeeded since
      const superseded = (lastOk[h.action] || 0) > at
        || (!sudo && UPGRADES.has(h.action) && (lastUpgradeOk > at || lastCleanCheck > at));
      if (superseded) continue;
      const t = tail(h.error, h.output);
      if (h.status === "failed" && h.action !== "check" && !sudo) {
        failed.push({ action: h.action, at: new Date(at).toISOString(), history_id: h.id ?? null,
                      error: t.lines[t.lines.length - 1], tail: t.lines, tail_is_raw: t.truncated });
      }
      if (!sudo) continue;
      denied.push({
        action: h.action,
        pkgManager: h.pkgManager ?? h.pkg_manager ?? null,
        at: new Date(at).toISOString(),
        history_id: h.id ?? null,
        error: String(text).split("\n").map((l) => l.trim()).find((l) => /^sudo: /.test(l) && !/unable to resolve host/.test(l)) || "sudo denied",
        command: (String(h.command || "").match(/Sudoers-relevant command: (.*)/) || [])[1] || null,
      });
    }
    let issues = [];
    if (Number(s.packageIssueCount || 0) > 0) {
      const detail = await get(base, session, `/systems/${s.id}`);
      issues = (detail.packageIssues || []).filter((i) => i.active !== 0 && i.active !== false).map((i) => ({
        pkgManager: i.pkgManager || null, key: i.issueKey || null, title: i.title || null,
        message: i.message || null, detected_at: i.detectedAt ? new Date(parseAt(i.detectedAt)).toISOString() : null,
      }));
    }
    const iso = (t) => (t ? new Date(t).toISOString() : null);
    out.push({
      id: s.id,
      name: s.name,
      hostname: s.hostname,
      port: Number(s.port || 22),
      pkgManager: s.pkgManager || null,
      detected: asList(s.detectedPkgManagers),
      disabled: asList(s.disabledPkgManagers),
      reachable: s.isReachable === undefined ? null : Boolean(Number(s.isReachable)),
      needs_reboot: Boolean(Number(s.needsReboot || 0)),
      boot_id: s.bootId || null,
      package_issues: Number(s.packageIssueCount || 0),
      package_issue_list: issues,
      last_ok: Object.fromEntries(Object.entries(lastOk).map(([k, v]) => [k, iso(v)])),
      last_clean_check: iso(lastCleanCheck),
      updates: Number(s.updateCount || 0),
      security: Number(s.securityCount || 0),
      lifecycle: {
        status: s.osLifecycleStatus || null,
        label: s.osLifecycleLabel || null,
        eol: s.osLifecycleEolDate || null,
        days_to_eol: s.osLifecycleDaysUntilEol ?? null,
        support_end: s.osLifecycleSupportEndDate || null,
        days_to_support_end: s.osLifecycleDaysUntilSupportEnd ?? null,
      },
      sudo_denied: denied.sort((a, b) => a.action.localeCompare(b.action)),
      failed: failed.sort((a, b) => a.action.localeCompare(b.action)),
    });
  }
  const schedules = ((await get(base, session, "/schedules")).schedules || []).map((d) => ({
    name: d.name,
    type: d.type,
    enabled: Boolean(d.enabled),
    last_run_at: d.lastRunAt ? new Date(parseAt(d.lastRunAt)).toISOString() : null,
    status: d.lastRunStatus || null,
    message: d.lastRunMessage || null,
  }));
  process.stdout.write("@@SKP-REPORT@@" + JSON.stringify({ systems: out, schedules }) + "\n");
}

main().catch((err) => fail(err && err.message ? err.message : String(err)));
