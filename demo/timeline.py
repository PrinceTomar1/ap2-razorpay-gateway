"""Render the audit chain as a self-contained HTML timeline.

`make demo` writes `demo/audit_chain.html`. The terminal log is the right format
for somebody at a prompt; this is the right format for somebody being *shown*
the run — on a video, in a review, on a phone.

Self-contained on purpose: no CDN, no fonts, no JavaScript beyond one filter
toggle. It has to open from a file:// URL on a machine with no network, because
that is where it will be opened.
"""

from __future__ import annotations

import html
import json
from typing import Any

from ap2_min.models import paise_to_inr_str
from gateway.audit import AuditRow, ChainVerification

#: Which actor gets which colour. Green is the deterministic money path — the
#: same colour the README's architecture diagram uses for it.
ACTOR_STYLE = {
    "verifier": ("#0b7a3d", "verifier"),
    "merchant_payment_processor": ("#1d4ed8", "processor"),
    "merchant": ("#7c3aed", "merchant"),
    "trusted_surface": ("#b45309", "human gate"),
    "shopping_agent": ("#0891b2", "agent"),
}

#: Events worth pulling out of the noise when someone is skimming.
PIVOTAL = {
    "verifier.decision",
    "mpp.payment_captured",
    "mpp.payment_declined",
    "recovery.succeeded",
    "recovery.exhausted",
    "recovery.not_retryable",
    "trusted_surface.gate_requested",
    "trusted_surface.gate_denied",
    "trusted_surface.gate_approved",
    "merchant.stock_recheck_failed",
    "merchant.checkout_unresolved_constraint",
    "verifier.mandate_rejected",
}

CSS = """
:root{--bg:#fbfbfa;--fg:#1a1a19;--muted:#6b6b68;--line:#e3e3e0;--card:#fff}
@media(prefers-color-scheme:dark){:root{--bg:#161615;--fg:#ebebe9;--muted:#9a9a96;
--line:#2e2e2c;--card:#1e1e1d}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.6 ui-sans-serif,-apple-system,"Segoe UI",system-ui,sans-serif}
.wrap{max-width:60rem;margin:0 auto;padding:2.5rem 1.25rem 5rem}
h1{font-size:1.6rem;margin:0 0 .3rem;letter-spacing:-.02em}
.sub{color:var(--muted);margin:0 0 2rem}
.result{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:1rem 1.25rem;margin:0 0 1.5rem;font-variant-numeric:tabular-nums}
.result b{font-size:1.05rem}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(9rem,1fr));gap:.75rem;
margin:0 0 2rem}
.stat{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:.8rem 1rem}
.stat .k{color:var(--muted);font-size:.78rem;text-transform:uppercase;letter-spacing:.04em}
.stat .v{font-size:1.15rem;font-weight:600;font-variant-numeric:tabular-nums;margin-top:.15rem}
.ok{color:#0b7a3d}.bad{color:#b91c1c}
.controls{margin:0 0 1.25rem;color:var(--muted);font-size:.9rem}
.controls label{margin-right:1rem;cursor:pointer;user-select:none}
ol{list-style:none;margin:0;padding:0;position:relative}
ol::before{content:"";position:absolute;left:8.5rem;top:0;bottom:0;width:1px;background:var(--line)}
li{display:grid;grid-template-columns:8.5rem 1fr;gap:1.25rem;padding:.45rem 0;position:relative}
li .when{color:var(--muted);font-size:.8rem;text-align:right;padding-top:.3rem;
font-variant-numeric:tabular-nums}
li .body{background:var(--card);border:1px solid var(--line);border-radius:9px;
padding:.6rem .85rem;position:relative}
li .body::before{content:"";position:absolute;left:-1.32rem;top:1rem;width:7px;height:7px;
border-radius:50%;background:var(--dot,var(--muted));box-shadow:0 0 0 3px var(--bg)}
li.pivotal .body{border-color:var(--dot)}
.ev{font:600 13px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--dot)}
.who{color:var(--muted);font-size:.75rem;margin-left:.5rem;font-weight:400}
.why{margin-top:.2rem}
details{margin-top:.4rem}
summary{cursor:pointer;color:var(--muted);font-size:.8rem}
pre{margin:.4rem 0 0;padding:.6rem .7rem;background:var(--bg);border:1px solid var(--line);
border-radius:6px;overflow-x:auto;font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace}
.hash{font:11px/1.4 ui-monospace,monospace;color:var(--muted);margin-top:.35rem;
word-break:break-all}
footer{margin-top:3rem;padding-top:1.25rem;border-top:1px solid var(--line);
color:var(--muted);font-size:.85rem}
a{color:inherit}
body.only-pivotal li:not(.pivotal){display:none}
"""

JS = """
document.getElementById('pivotal').addEventListener('change',function(e){
  document.body.classList.toggle('only-pivotal', e.target.checked);
});
"""


def render(
    rows: list[AuditRow],
    *,
    chain: ChainVerification,
    tip_hash: str,
    report_line: str,
    report: dict[str, Any],
    captured: int,
    rail: str,
) -> str:
    """One self-contained HTML page. No network, no build step."""
    items: list[str] = []
    for row in rows:
        colour, who = ACTOR_STYLE.get(row.actor, ("#6b6b68", row.actor))
        pivotal = " pivotal" if row.event in PIVOTAL else ""
        payload = html.escape(json.dumps(row.payload, indent=2, ensure_ascii=False))
        items.append(
            f'<li class="row{pivotal}" style="--dot:{colour}">'
            f'<div class="when">{html.escape(row.ts[11:19])}<br>#{row.id}</div>'
            f'<div class="body">'
            f'<span class="ev">{html.escape(row.event)}</span>'
            f'<span class="who">{html.escape(who)}</span>'
            f'<div class="why">{html.escape(row.human_reason or "")}</div>'
            f"<details><summary>payload &amp; chain link</summary>"
            f"<pre>{payload}</pre>"
            f'<div class="hash">hash {html.escape(row.hash)}<br>'
            f"prev {html.escape(row.prev_hash)}</div>"
            f"</details>"
            f"</div></li>"
        )

    chain_class = "ok" if chain.ok else "bad"
    chain_text = "intact" if chain.ok else f"BROKEN at row {chain.broken_at}"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Audit chain — AP2 × Razorpay</title>
<style>{CSS}</style></head><body><div class="wrap">
<h1>Audit chain</h1>
<p class="sub">Every money action from one run of <code>make demo</code>, in order,
each with the reason a person can read. Generated from the append-only SQLite
chain — not written by hand.</p>

<div class="result"><b>{html.escape(report_line)}</b></div>

<div class="stats">
  <div class="stat"><div class="k">Audit rows</div><div class="v">{len(rows)}</div></div>
  <div class="stat"><div class="k">Chain</div>
    <div class="v {chain_class}">{html.escape(chain_text)}</div></div>
  <div class="stat"><div class="k">Captured</div>
    <div class="v">₹{paise_to_inr_str(captured)}</div></div>
  <div class="stat"><div class="k">Unauthorised</div>
    <div class="v ok">₹{report.get("unauthorised_spend", 0)}</div></div>
  <div class="stat"><div class="k">Explained</div>
    <div class="v">{html.escape(str(report.get("actions_explained", "")))}</div></div>
  <div class="stat"><div class="k">Rail</div><div class="v">{html.escape(rail)}</div></div>
</div>

<div class="controls">
  <label><input type="checkbox" id="pivotal"> show only decisions, payments and gates</label>
</div>

<ol>{"".join(items)}</ol>

<footer>
<p><b>Chain tip</b> <code>{html.escape(tip_hash)}</code></p>
<p>Each row commits to its predecessor:
<code>sha256(prev_hash + canonical(actor, event, payload, ts, human_reason))</code>.
Editing any row invalidates every row after it, and the explanation is inside the
hash — an audit trail whose numbers are tamper-evident while the prose beside them
is freely editable would not be much of an audit trail.</p>
<p>Record this tip and you can detect truncation later, which is the one tamper a
self-contained hash chain cannot catch on its own. See <code>LIMITATIONS.md</code>.</p>
<p>Generated by <code>make demo</code> — do not edit by hand.</p>
</footer>
</div><script>{JS}</script></body></html>
"""
