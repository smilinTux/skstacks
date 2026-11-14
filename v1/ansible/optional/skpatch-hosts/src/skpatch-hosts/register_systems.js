// Register this run's provisioned nodes as linux-update-dashboard systems.
// Runs INSIDE the dashboard container (docker exec ... node -e), reading one
// JSON object on stdin, and talks to the app on 127.0.0.1 with a dashboard
// session it signs itself.
//
// Why a self-signed session and not an API token or a password:
// - linux-update-dashboard 2026.10.9 refuses every API-token request that
//   creates or edits a system or a credential (systems.post("/") and
//   systems.put("/:id") answer 403 "API tokens cannot configure SSH
//   connections"; /api/credentials is a session-only management path).
// - Password login is switched off in AUTH_MODE oidc
//   (LUDASH_PASSWORD_LOGIN_ENABLED=false), and in sso mode the dashboard is
//   behind Authentik forward auth, so a scripted login cannot reach it there.
// - The app signs its `ludash_session` cookie (HS256 JWT, server/auth/
//   session.ts) with LUDASH_SECRET_KEY or the key file next to the database
//   (server/config.ts getSecretKey). Whoever can `docker exec` into this
//   container can already read that key, the database and the encryption
//   key, so signing a short session here grants nothing new. The session is
//   for a NAMED existing admin account (`register_as`), checked against the
//   users table, carries that user's current session_version (so a "log out
//   everywhere" still revokes it) and expires after five minutes.
//
// Never deletes and never edits: a node already registered (same hostname
// and port) is left exactly as an operator configured it, because PUT
// /api/systems/:id replaces the whole connection config. Systems on the
// credential that this run's target list does not name are reported only.
//
// Optionally (configure() below) it also converges the dashboard's own
// configuration from the same input: APT upgrade mode per system, notification
// channels and schedules, all matched by name. That part edits, but only what
// the input names, and never deletes.
//
// Input (stdin, secrets included, so nothing is on a command line):
//   register_as    dashboard username of an existing admin (local or OIDC)
//   credential_id  an existing SSH credential's id, or 0 to use `credential`
//   credential     {name, username, private_key}: found by name, created if
//                  missing (never rewritten: rotate a key in the dashboard)
//   sync_credential  true: rewrite that credential's key from `credential`
//                  (key rotation); default false
//   systems        [{name, hostname, port, upgrade_mode?}], this run's targets
//   upgrade_mode, notifications, schedules: see configure() below
// Output: status lines only, never a secret; the last one is
// "registration: ok" on success. Exit non-zero on any error.
"use strict";
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

function fail(msg) {
  console.error("registration failed: " + msg);
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
    return db
      .prepare("SELECT id, username, role, session_version, auth_provider FROM users WHERE username = ?")
      .get(username);
  } finally {
    db.close();
  }
}

function b64url(buf) {
  return Buffer.from(buf).toString("base64url");
}

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

function makeApi(base, session) {
  const csrf = crypto.randomUUID();
  return async function call(method, p, body, ok = [200, 201]) {
    const headers = { Accept: "application/json", Cookie: `ludash_session=${session}; ludash_csrf=${csrf}` };
    if (method !== "GET") headers["x-csrf-token"] = csrf;
    if (body !== undefined) headers["Content-Type"] = "application/json";
    const res = await fetch(base + "/api" + p, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
    const text = await res.text();
    if (!ok.includes(res.status)) throw new Error(`${method} ${p}: HTTP ${res.status}: ${text.slice(0, 300)}`);
    return text ? JSON.parse(text) : null;
  };
}

async function main() {
  const cfg = JSON.parse(fs.readFileSync(0, "utf8"));
  if (!cfg.register_as) fail("register_as is not set");
  if (!Array.isArray(cfg.systems)) fail("systems is not a list");
  const credentialId = Number(cfg.credential_id || 0);
  const cred = cfg.credential || {};
  if (credentialId <= 0 && !(cred.name && cred.username && cred.private_key)) {
    fail("set credential_id, or credential {name, username, private_key}");
  }

  const user = lookupUser(cfg.register_as);
  if (!user) fail(`no dashboard user named ${JSON.stringify(cfg.register_as)} (sign in once, or create it, first)`);
  if (user.role === "read-only") fail(`dashboard user ${JSON.stringify(cfg.register_as)} is read-only, registration needs an admin`);

  const call = makeApi(`http://127.0.0.1:${process.env.LUDASH_PORT || 3001}`, signSession(user, secretKey()));
  const me = await call("GET", "/auth/me");
  if (!me || !me.user || me.user.username !== user.username) fail(`the signed session was not accepted: ${JSON.stringify(me)}`);
  console.log(`session: signed in as ${user.username} (${user.auth_provider}, expires in 5 min)`);

  let useId = credentialId;
  const creds = (await call("GET", "/credentials?kind=sshKey")).credentials || [];
  if (useId > 0) {
    if (!creds.some((c) => c.id === useId)) fail(`no sshKey credential with id ${useId}`);
    console.log(`credential: using id ${useId}`);
  } else {
    const found = creds.find((c) => c.name === cred.name);
    if (found) {
      useId = found.id;
      console.log(`credential ${cred.name}: unchanged (id ${useId})`);
    } else {
      const out = await call("POST", "/credentials", {
        name: cred.name,
        kind: "sshKey",
        payload: { username: cred.username, privateKey: cred.private_key, passphrase: "" },
      });
      useId = out.id;
      console.log(`credential ${cred.name}: created (id ${useId})`);
      cfg.sync_credential = false; // just written from the same key
    }
  }

  // Key rotation: overwrite the credential's private key from this run's
  // key (sync_credential, the vault's SYNC_CREDENTIAL). Off by default so a
  // run never silently replaces a key an operator changed in the dashboard.
  if (cfg.sync_credential) {
    if (!(cred.username && cred.private_key)) fail("sync_credential needs credential {username, private_key}");
    const current = creds.find((c) => c.id === useId) || { name: cred.name };
    await call("PUT", `/credentials/${useId}`, {
      name: current.name,
      payload: { username: cred.username, privateKey: cred.private_key, passphrase: "" },
    });
    console.log(`credential ${current.name}: key synced from this run (id ${useId})`);
  }

  const existing = (await call("GET", "/systems")).systems || [];
  const keyOf = (h, p) => `${h}:${Number(p || 22)}`;
  const byKey = new Map(existing.map((s) => [keyOf(s.hostname, s.port), s]));
  const seen = new Set();
  for (const w of cfg.systems) {
    const k = keyOf(w.hostname, w.port);
    seen.add(k);
    const cur = byKey.get(k);
    if (cur) {
      console.log(`${w.name}: unchanged (already registered as ${cur.name}, id ${cur.id})`);
      continue;
    }
    const out = await call("POST", "/systems", {
      name: w.name,
      hostname: w.hostname,
      port: Number(w.port || 22),
      credentialId: useId,
      hostKeyVerificationEnabled: false,
    });
    console.log(`${w.name}: created (id ${out.id})`);
  }

  const orphaned = existing.filter((s) => s.credentialId === useId && !seen.has(keyOf(s.hostname, s.port)));
  if (orphaned.length) {
    console.log("not in this run's target list (left in the dashboard, not removed):");
    for (const s of orphaned) console.log(`  ${s.name} (${s.hostname}:${s.port})`);
  }

  await configure(call, cfg);
  console.log("registration: ok");
}

// ---------------------------------------------------------------------------
// Dashboard configuration as code (all optional; nothing below runs unless the
// input names it). Everything is matched BY NAME and converged: a missing
// schedule or channel is created, one that differs is updated, and one this
// input does not name is reported and left alone, never deleted.
//   upgrade_mode    "upgrade" | "full-upgrade": APT default upgrade mode for
//                   this run's systems (per system: systems[].upgrade_mode).
//                   Uses the narrow PUT /api/systems/:id/upgrade-mode, never
//                   the whole-config PUT. A system whose package manager has
//                   not been detected yet (first check still pending) is
//                   reported and picked up by the next run.
//   scripts         [{name, description, type, operation, pkgManager,
//                     isDefault, steps}]: custom scripts matched by name
//                   (dashboard-scripts.json: the wrapper-backed "upgrade
//                   selected" defaults). Created when missing, updated when
//                   any of those fields differ.
//   settings        {key: value}: app settings, written only when a value
//                   differs (PUT /api/settings with just those keys).
//   notifications   [{name, type, enabled, notify_on, systems, config}]
//   schedules       [{name, type, enabled, cron, systems, only_if_required,
//                     cache_hours, notifications}]
// `systems` is a list of dashboard system NAMES (any system in the dashboard,
// not only this run's), or null/absent for all systems. Names it cannot find
// are reported; a schedule left with no system at all is skipped, never
// widened to "all".
// ---------------------------------------------------------------------------
const sortedJson = (v) => JSON.stringify(v, (k, x) => (x && typeof x === "object" && !Array.isArray(x)
  ? Object.keys(x).sort().reduce((o, key) => ((o[key] = x[key]), o), {}) : x));

function resolveNames(label, names, idByName) {
  if (names === null || names === undefined) return { ids: null, missing: [] };
  if (!Array.isArray(names)) fail(`${label}: systems must be a list of names or null`);
  const ids = [];
  const missing = [];
  for (const n of names) {
    if (idByName.has(n)) ids.push(idByName.get(n));
    else missing.push(n);
  }
  return { ids, missing };
}

function aptMode(system) {
  let configs = system.pkgManagerConfigs;
  if (typeof configs === "string") {
    try { configs = JSON.parse(configs); } catch { configs = null; }
  }
  return (configs && configs.apt && configs.apt.defaultUpgradeMode) || "upgrade";
}

async function configure(call, cfg) {
  const wantsModes = cfg.upgrade_mode || cfg.systems.some((w) => w.upgrade_mode);
  const notifs = Array.isArray(cfg.notifications) ? cfg.notifications : [];
  const scheds = Array.isArray(cfg.schedules) ? cfg.schedules : [];
  const scripts = Array.isArray(cfg.scripts) ? cfg.scripts : [];
  const settings = cfg.settings && typeof cfg.settings === "object" ? cfg.settings : {};
  if (!wantsModes && !notifs.length && !scheds.length && !scripts.length && !Object.keys(settings).length) return;

  if (Object.keys(settings).length) {
    const cur = (await call("GET", "/settings")).settings || {};
    const diff = {};
    for (const [k, v] of Object.entries(settings)) {
      if (String(cur[k]) !== String(v)) diff[k] = String(v);
    }
    if (Object.keys(diff).length) {
      await call("PUT", "/settings", diff);
      console.log(`settings: set ${Object.keys(diff).sort().join(", ")}`);
    } else {
      console.log(`settings: unchanged (${Object.keys(settings).sort().join(", ")})`);
    }
  }

  if (scripts.length) {
    const fields = ["description", "type", "operation", "pkgManager", "isDefault", "steps"];
    const shape = (s) => sortedJson(fields.reduce((o, f) => ((o[f] = f === "steps"
      ? (s.steps || []).map((x) => ({ label: x.label, command: x.command })) : s[f] ?? null), o), {}));
    const have = ((await call("GET", "/scripts")).scripts || []).filter((s) => !s.readonly);
    for (const want of scripts) {
      if (!want.name || !want.operation || !Array.isArray(want.steps)) fail("every script needs name, operation and steps");
      const cur = have.find((s) => s.name === want.name);
      if (!cur) {
        const out = await call("POST", "/scripts", want);
        console.log(`script ${want.name}: created (${out.script ? out.script.id : "?"})`);
      } else if (shape(cur) === shape(want)) {
        console.log(`script ${want.name}: unchanged (${cur.id})`);
      } else {
        await call("PUT", `/scripts/${encodeURIComponent(cur.id)}`, want);
        console.log(`script ${want.name}: updated (${cur.id})`);
      }
    }
  }

  const all = (await call("GET", "/systems")).systems || [];
  const byKey = new Map(all.map((s) => [`${s.hostname}:${Number(s.port || 22)}`, s]));
  const idByName = new Map(all.map((s) => [s.name, s.id]));

  if (wantsModes) {
    for (const w of cfg.systems) {
      const want = w.upgrade_mode || cfg.upgrade_mode;
      if (!want) continue;
      if (want !== "upgrade" && want !== "full-upgrade") fail(`${w.name}: upgrade_mode must be upgrade or full-upgrade`);
      const s = byKey.get(`${w.hostname}:${Number(w.port || 22)}`);
      if (!s) continue;
      const detected = s.pkgManager || (Array.isArray(s.detectedPkgManagers) && s.detectedPkgManagers.length);
      if (!detected) {
        console.log(`${s.name}: upgrade mode pending (package manager not detected yet, next run sets it)`);
        continue;
      }
      const managers = [s.pkgManager, ...(Array.isArray(s.detectedPkgManagers) ? s.detectedPkgManagers : [])];
      if (!managers.includes("apt") && !managers.includes("dnf")) {
        console.log(`${s.name}: upgrade mode n/a (${s.pkgManager || "unknown"} has none)`);
        continue;
      }
      if (aptMode(s) === want) {
        console.log(`${s.name}: upgrade mode ${want} unchanged`);
        continue;
      }
      try {
        await call("PUT", `/systems/${s.id}/upgrade-mode`, { fullUpgrade: want === "full-upgrade" });
        console.log(`${s.name}: upgrade mode set to ${want}`);
      } catch (err) {
        console.log(`${s.name}: upgrade mode not set (${err.message.slice(0, 160)})`);
      }
    }
  }

  const notifIdByName = new Map();
  if (notifs.length) {
    const have = (await call("GET", "/notifications")).notifications || [];
    for (const n of have) notifIdByName.set(n.name, n.id);
    for (const n of notifs) {
      if (!n.name || !n.type) fail("every notification needs name and type");
      const { ids, missing } = resolveNames(`notification ${n.name}`, n.systems, idByName);
      if (missing.length) console.log(`notification ${n.name}: not in the dashboard, ignored: ${missing.join(", ")}`);
      const body = {
        name: n.name,
        type: n.type,
        enabled: n.enabled !== false,
        notifyOn: n.notify_on || ["updates"],
        systemIds: ids,
        config: n.config || {},
      };
      // Channel config carries secrets that the API only ever returns masked,
      // so it cannot be compared: an existing channel is rewritten every run
      // (same values in, same channel out).
      if (notifIdByName.has(n.name)) {
        const id = notifIdByName.get(n.name);
        await call("PUT", `/notifications/${id}`, body);
        console.log(`notification ${n.name}: synced (id ${id})`);
      } else {
        const out = await call("POST", "/notifications", { ...body, schedule: null });
        notifIdByName.set(n.name, out.id);
        console.log(`notification ${n.name}: created (id ${out.id})`);
      }
    }
    for (const n of have) {
      if (!notifs.some((x) => x.name === n.name)) console.log(`notification ${n.name}: not declared here, left as is`);
    }
  }

  if (scheds.length) {
    if (scheds.length !== new Set(scheds.map((x) => x.name)).size) fail("schedule names must be unique");
    if (!notifIdByName.size && scheds.some((x) => x.type === "notification_digest")) {
      for (const n of (await call("GET", "/notifications")).notifications || []) notifIdByName.set(n.name, n.id);
    }
    const have = (await call("GET", "/schedules")).schedules || [];
    const haveByName = new Map(have.map((x) => [x.name, x]));
    for (const d of scheds) {
      if (!d.name || !d.type || !d.cron) fail("every schedule needs name, type and cron");
      const { ids, missing } = resolveNames(`schedule ${d.name}`, d.systems, idByName);
      if (missing.length) console.log(`schedule ${d.name}: not in the dashboard, left out: ${missing.join(", ")}`);
      if (ids !== null && ids.length === 0) {
        console.log(`schedule ${d.name}: skipped (none of its systems are in the dashboard yet)`);
        continue;
      }
      const config = { cron: String(d.cron).trim() };
      if (d.type === "refresh") config.cacheDurationHours = Number(d.cache_hours ?? 12);
      if (d.type === "reboot") config.onlyIfRequired = d.only_if_required !== false;
      if (d.type === "notification_digest") {
        config.notificationIds = (d.notifications || []).map((name) => {
          if (!notifIdByName.has(name)) fail(`schedule ${d.name}: no notification channel named ${name}`);
          return notifIdByName.get(name);
        });
      }
      const want = { type: d.type, enabled: d.enabled !== false, systemIds: ids, config };
      const cur = haveByName.get(d.name);
      if (!cur) {
        const out = await call("POST", "/schedules", { name: d.name, ...want });
        console.log(`schedule ${d.name}: created (id ${out.id}, ${d.type} "${config.cron}")`);
        continue;
      }
      const curShape = { type: cur.type, enabled: cur.enabled, systemIds: cur.systemIds, config: cur.config };
      if (sortedJson(curShape) === sortedJson(want)) {
        console.log(`schedule ${d.name}: unchanged (id ${cur.id})`);
        continue;
      }
      await call("PUT", `/schedules/${cur.id}`, want);
      console.log(`schedule ${d.name}: updated (id ${cur.id}, ${d.type} "${config.cron}")`);
    }
    for (const x of have) {
      if (!scheds.some((d) => d.name === x.name)) console.log(`schedule ${x.name}: not declared here, left as is (${x.enabled ? "enabled" : "disabled"})`);
    }
  }
}

main().catch((err) => fail(err && err.message ? err.message : String(err)));
