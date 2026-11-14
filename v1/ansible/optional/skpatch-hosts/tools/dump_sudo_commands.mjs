// Runs INSIDE the pinned dashboard image (see extract_sudo_commands.py).
// It is appended to a copy of the image's own server bundle, cut just before
// the server starts, so buildCommandReference() is the app's real code, not a
// reimplementation. Output: one JSON document on stdout.
const __skpManagers = listPackageManagerDefinitions().map((d) => d.name).sort();
const __skpOut = { managers: {}, common: [], warnings: [] };
const __skpKey = (e) => `${e.category}\t${e.command}`;
const __skpCommon = new Map();
for (const manager of __skpManagers) {
  const ref = buildCommandReference({
    id: 0,
    pkgManager: manager,
    detectedPkgManagers: JSON.stringify([manager]),
    disabledPkgManagers: null,
    pkgManagerConfigs: null
  });
  const rows = [];
  for (const e of ref.sudoers) {
    const row = { category: e.category, command: e.command, safety: e.sudoersSafety };
    if (e.pkgManager === manager) rows.push(row);
    else __skpCommon.set(__skpKey(row), row);
  }
  for (const w of ref.warnings) {
    __skpOut.warnings.push({ pkgManager: w.pkgManager ?? null, category: w.category, command: w.command });
  }
  rows.sort((a, b) => __skpKey(a).localeCompare(__skpKey(b)));
  __skpOut.managers[manager] = { rows, envKeep: [...(ref.envKeep ?? [])].sort() };
}
__skpOut.common = [...__skpCommon.values()].sort((a, b) => __skpKey(a).localeCompare(__skpKey(b)));
const __skpSeen = new Set();
__skpOut.warnings = __skpOut.warnings.filter((w) => {
  const k = JSON.stringify(w);
  if (__skpSeen.has(k)) return false;
  __skpSeen.add(k);
  return true;
}).sort((a, b) => JSON.stringify(a).localeCompare(JSON.stringify(b)));
process.stdout.write("@@SKP-JSON@@" + JSON.stringify(__skpOut) + "\n");
process.exit(0);
