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
//               sudo_denied: [{action, pkgManager, at, error}],
//               failed: [{action, at, error}]}],
//    schedules: [{name, type, enabled, last_run_at, status, message}]}
// failed is the same "latest row per action" view for every action except
// check: a failed upgrade, reboot or autoremove whose latest run did not hit
// sudo (that is sudo_denied, its own class). A later success clears it.
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

// The most telling line of a failed run's error text: apt/dpkg "E:" lines and
// the like first; when nothing in it explains the failure (the app keeps only
// the first 2000 characters, often just download progress) say so rather
// than quote a random progress line. At most 300 characters.
const REASON = [/^E: /, /^(dpkg|error|fatal|failed)\b/i, /\b(error|failed|timed out|timeout|lost|refused|denied)\b/i];
function reason(text) {
  const lines = String(text || "").split("\n").map((l) => l.trim()).filter(Boolean);
  for (const re of REASON) {
    const hit = lines.filter((l) => re.test(l)).pop();
    if (hit) return hit.slice(0, 300);
  }
  return "no error line recorded (see the run in the dashboard history)";
}

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
    const latest = new Map();
    for (const h of history) {
      const at = parseAt(h.startedAt ?? h.started_at);
      if (!(at >= since)) continue;
      const key = h.action;
      const cur = latest.get(key);
      if (!cur || at > cur.at) latest.set(key, { at, h });
    }
    const denied = [];
    const failed = [];
    for (const { at, h } of latest.values()) {
      const text = `${h.error || ""}\n${h.output || ""}`;
      const sudo = SUDO_DENIED.some((re) => re.test(text));
      if (h.status === "failed" && h.action !== "check" && !sudo) {
        failed.push({
          action: h.action,
          at: new Date(at).toISOString(),
          error: reason(h.error),
        });
      }
      if (h.status === "success" || !sudo) continue;
      denied.push({
        action: h.action,
        pkgManager: h.pkgManager ?? h.pkg_manager ?? null,
        at: new Date(at).toISOString(),
        error: String(h.error || "").split("\n").find((l) => SUDO_DENIED.some((re) => re.test(l))) || "sudo denied",
      });
    }
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
