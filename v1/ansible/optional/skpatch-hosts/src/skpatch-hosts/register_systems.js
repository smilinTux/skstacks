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
// Input (stdin, secrets included, so nothing is on a command line):
//   register_as    dashboard username of an existing admin (local or OIDC)
//   credential_id  an existing SSH credential's id, or 0 to use `credential`
//   credential     {name, username, private_key}: found by name, created if
//                  missing (never rewritten: rotate a key in the dashboard)
//   systems        [{name, hostname, port}], this run's targets
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
    }
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
  console.log("registration: ok");
}

main().catch((err) => fail(err && err.message ? err.message : String(err)));
