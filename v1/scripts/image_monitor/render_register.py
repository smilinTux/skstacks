"""Render the vuln register's matched findings into a static HTML page.

This is the operator-facing "generated human-readable page" - written
outside the repo (see check_fleet_vulns.py's --register-html default),
since it necessarily contains real cluster/service/version data. Plain,
functional HTML: a totals summary, a severity x exposure matrix, and the
full per-row table, grouped by vulnerability.
"""
from __future__ import annotations

import html
from collections import defaultdict
from datetime import datetime, timezone

from vuln_register import VulnMatch, risk_key, summarize

_STATUS_LABELS = {
    "patched": "Patched",
    "patch-available": "Patch available",
    "no-patch-yet": "No patch yet",
    "not-affected": "Not affected",
    "needs-manual-verification": "Needs manual verification",
    "unknown": "Unknown",
    "unreviewed": "Unreviewed",
}


def _e(text) -> str:
    return html.escape(str(text if text is not None else ""))


def render_html(matches: list[VulnMatch], *, generated_at: datetime | None = None) -> str:
    generated_at = generated_at or datetime.now(timezone.utc)
    totals = summarize(matches)

    by_vuln: dict[str, list[VulnMatch]] = defaultdict(list)
    for m in matches:
        by_vuln[m.vuln_id].append(m)
    ordered_vuln_ids = sorted(by_vuln, key=lambda vid: risk_key(by_vuln[vid][0]))

    sev_exp_rows = "\n".join(
        f"<tr><td>{_e(k)}</td><td>{_e(v)}</td></tr>"
        for k, v in sorted(totals["by_severity_exposure"].items())
    )
    status_rows = "\n".join(
        f"<tr><td>{_e(_STATUS_LABELS.get(k, k))}</td><td>{_e(v)}</td></tr>"
        for k, v in sorted(totals["by_status"].items())
    )

    vuln_sections = []
    for vid in ordered_vuln_ids:
        rows = sorted(by_vuln[vid], key=lambda m: (m.cluster, m.service))
        head = rows[0]
        cve_str = ", ".join(head.cve_ids) if head.cve_ids else vid
        aka = f" ({_e(head.aka)})" if head.aka else ""
        sources_html = " &middot; ".join(
            f'<a href="{_e(s)}">{_e(s)}</a>' for s in head.sources
        ) or "-"

        row_html = "\n".join(
            f"<tr class=\"status-{_e(m.status)}\">"
            f"<td>{_e(m.cluster)}</td><td>{_e(m.service)}</td><td class=\"mono\">{_e(m.image)}</td>"
            f"<td>{_e(m.exposure)}<br><span class=\"muted\">{_e(m.exposure_mechanism)}</span></td>"
            f"<td>{_e(m.probed_version)}</td><td>{_e(m.fixed_version)}</td>"
            f"<td>{_e(_STATUS_LABELS.get(m.status, m.status))}</td><td>{_e(m.action)}</td>"
            f"</tr>"
            for m in rows
        )

        vuln_sections.append(f"""
<section class="vuln">
  <h3>{_e(head.title)}{aka}</h3>
  <p class="meta">{_e(cve_str)} &middot; severity: <strong>{_e(head.severity)}</strong>
     {f"&middot; CVSS {_e(head.cvss)}" if head.cvss is not None else ""}</p>
  <table class="rows">
    <thead><tr><th>Cluster</th><th>Service</th><th>Image</th><th>Exposure</th>
      <th>Version seen</th><th>Fixed version</th><th>Status</th><th>Action</th></tr></thead>
    <tbody>{row_html}</tbody>
  </table>
  <p class="sources">Sources: {sources_html}</p>
</section>""")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>SKStacks Fleet Vulnerability Register</title>
<style>
  body {{ font-family: -apple-system, system-ui, sans-serif; margin: 2rem auto; max-width: 1100px; padding: 0 1rem;
         color: #1b1f24; background: #fff; }}
  h1 {{ margin-bottom: 0.2rem; }}
  .generated {{ color: #666; font-size: 0.9rem; margin-bottom: 1.5rem; }}
  table {{ border-collapse: collapse; width: 100%; margin: 0.5rem 0 1.5rem; }}
  th, td {{ border: 1px solid #d8dee4; padding: 0.4rem 0.6rem; text-align: left; font-size: 0.92rem; vertical-align: top; }}
  th {{ background: #f6f8fa; }}
  .mono {{ font-family: SFMono-Regular, Consolas, monospace; font-size: 0.82rem; word-break: break-all; }}
  .muted {{ color: #777; font-size: 0.82rem; }}
  .totals-grid {{ display: flex; gap: 2rem; flex-wrap: wrap; margin-bottom: 1.5rem; }}
  .vuln {{ border-top: 3px solid #d8dee4; padding-top: 1rem; margin-top: 1.5rem; }}
  .vuln h3 {{ margin-bottom: 0.1rem; }}
  .meta {{ color: #555; margin-top: 0; }}
  .sources {{ font-size: 0.85rem; color: #555; }}
  tr.status-patched, tr.status-not-affected {{ background: #f0fdf4; }}
  tr.status-patch-available {{ background: #fff8e1; }}
  tr.status-no-patch-yet {{ background: #fff0f0; }}
  tr.status-needs-manual-verification, tr.status-unknown, tr.status-unreviewed {{ background: #f6f6f6; }}
</style>
</head>
<body>
<h1>SKStacks Fleet Vulnerability Register</h1>
<p class="generated">Generated {_e(generated_at.strftime('%Y-%m-%d %H:%M UTC'))} by check_fleet_vulns.py. Private
operator page: real cluster/service/version data, not part of the public skstacks repo.</p>

<div class="totals-grid">
  <div>
    <h2>Totals</h2>
    <table class="rows">
      <tr><td>Total rows tracked</td><td>{_e(totals['total_rows'])}</td></tr>
      <tr><td>Open / needs action</td><td>{_e(totals['open_count'])}</td></tr>
      <tr><td>Unreviewed (no register entry)</td><td>{_e(totals['unreviewed_count'])}</td></tr>
    </table>
  </div>
  <div>
    <h2>By status</h2>
    <table class="rows">{status_rows}</table>
  </div>
  <div>
    <h2>By severity &times; exposure</h2>
    <table class="rows">{sev_exp_rows}</table>
  </div>
</div>

<h2>Vulnerabilities</h2>
{''.join(vuln_sections)}
</body>
</html>"""
