"""Human-readable and self-contained reports. Never interpolate unescaped HTML."""

import html
import json


def interaction_witnesses(report):
    """Joint failures whose immediate predecessor removals all pass."""
    experiments = report["experiments"]
    by_removed = {frozenset(e["removed"]): e for e in experiments}
    witnesses = []
    for removed, experiment in by_removed.items():
        if len(removed) < 2 or experiment["feasible"]:
            continue
        if all(by_removed.get(removed - {name}, {}).get("feasible", False) for name in removed):
            witnesses.append({"removed": sorted(removed),
                              "failed_queries": [q["name"] for q in experiment["queries"]
                                                 if q["status"] != "ok" or not q["result_match"]
                                                 or q["callback_ratio"] is None
                                                 or q["callback_ratio"] > next(
                                                     w["max_step_ratio"] for w in report["workload"]["queries"]
                                                     if w["name"] == q["name"])]})
    return sorted(witnesses, key=lambda w: (len(w["removed"]), w["removed"]))


def _ratio(value):
    return "n/a" if value is None else f"{value:.3f}x"


def _count(value):
    return "n/a" if value is None else f"{value:g}"


def _md(value):
    return str(value).replace("\\", "\\\\").replace("|", "\\|").replace("`", "'").replace("\n", " ").replace("\r", " ").replace("<", "&lt;").replace(">", "&gt;")


def render_markdown(report):
    rec = report["recommendation"]
    lines = ["# IndexWitness report", "", f"Source: {_md(report['source']['name'])}",
             f"SQLite: {_md(report['runtime']['sqlite'])}; Python: {_md(report['runtime']['python'])}", "",
             f"Complete search: {len(report['experiments'])} subsets of {len(report['candidates'])} candidate indexes.", "",
             f"Suggested retained set: {', '.join(_md(x) for x in rec['retained']) or '(none)' }.",
             f"Removal candidates: {', '.join(_md(x) for x in rec['removed']) or '(none)' }.", "",
             "Suggestion applies only to this snapshot, bindings, result contract and callback budgets.",
             "Callback counts (interval 1) are an approximate SQLite work proxy including preparation; they are not time or exact VM-step counters.", "",
             "| Retained candidates | Feasible | Weighted callbacks | Worst ratio |",
             "|---|---:|---:|---:|"]
    for e in report["experiments"]:
        lines.append(f"| {_md(', '.join(e['retained']) or '(none)')} | {'yes' if e['feasible'] else 'no'} | {_count(e['weighted_callbacks'])} | {_ratio(e['worst_callback_ratio'])} |")
    witnesses = interaction_witnesses(report)
    if witnesses:
        lines.extend(["", "## Joint-removal witnesses", ""])
        for witness in witnesses:
            lines.append(f"- Removing {_md(', '.join(witness['removed']))} together fails ({_md(', '.join(witness['failed_queries']))}); each one-index-smaller removal passes.")
    lines.extend(["", "## Query evidence", ""])
    for e in report["experiments"]:
        lines.extend([f"### Retain {_md(', '.join(e['retained']) or '(none)')}", ""])
        for q in e["queries"]:
            lines.append(f"- {_md(q['name'])}: {_md(q['status'])}, {q['progress_callbacks']} callbacks, {_ratio(q['callback_ratio'])}, result match: {q['result_match']}.")
            if q.get("error"):
                lines.append(f"  {_md(q['error'])}")
    return "\n".join(lines) + "\n"


def render_text(report):
    rec = report["recommendation"]
    lines = [f"IndexWitness | {report['source']['name']} | SQLite {report['runtime']['sqlite']}",
             f"Complete: {len(report['experiments'])} subsets; {len(report['candidates'])} candidate indexes",
             f"Retain: {', '.join(rec['retained']) or '(none)'}",
             f"Removal candidates: {', '.join(rec['removed']) or '(none)'}",
             f"Worst callback ratio: {_ratio(rec['worst_callback_ratio'])}",
             "", "Retained candidates | feasible | weighted callbacks | worst ratio"]
    for e in report["experiments"]:
        lines.append(f"{', '.join(e['retained']) or '(none)'} | {e['feasible']} | {_count(e['weighted_callbacks'])} | {_ratio(e['worst_callback_ratio'])}")
    for w in interaction_witnesses(report):
        lines.append(f"Joint-removal trap: {', '.join(w['removed'])}; smaller removals pass, joint removal fails: {', '.join(w['failed_queries'])}")
    lines.extend(["", "Evidence is specific to this snapshot and supplied queries/parameters.",
                  "Callbacks at interval 1 are an approximate work proxy, not elapsed time or exact VM steps."])
    return "\n".join(lines) + "\n"


def render_html(report):
    esc = lambda value: html.escape(str(value), quote=True)
    rec = report["recommendation"]
    frontier = {tuple(x) for x in report["frontier"]}
    rows, details = [], []
    for number, experiment in enumerate(report["experiments"]):
        e = experiment
        label = ", ".join(e["retained"]) or "(none)"
        mark = " · Pareto" if tuple(e["retained"]) in frontier else ""
        rows.append(f'<tr class="{"pass" if e["feasible"] else "fail"}"><td><a href="#e{number}">{esc(label)}</a>{mark}</td><td>{"PASS" if e["feasible"] else "FAIL"}</td><td>{_count(e["weighted_callbacks"])}</td><td>{_ratio(e["worst_callback_ratio"])}</td></tr>')
        queries = []
        for q in e["queries"]:
            plan = "\n".join(" | ".join(map(str, row)) for row in q["plan"])
            queries.append(f'<article><h4>{esc(q["name"])}</h4><p>{esc(q["status"])} · {q["progress_callbacks"]} callbacks · {_ratio(q["callback_ratio"])} · result match: {q["result_match"]}</p><p>{esc(q.get("error", ""))}</p><pre>{esc(plan)}</pre></article>')
        details.append(f'<details id="e{number}"><summary>Retain {esc(label)}</summary>{"".join(queries)}</details>')
    witnesses = "".join(f'<li><strong>{esc(", ".join(w["removed"]))}</strong>: joint removal fails; every one-index-smaller removal passes. Affected queries: {esc(", ".join(w["failed_queries"]))}.</li>' for w in interaction_witnesses(report))
    return f'''<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>IndexWitness experiment report</title>
<style>:root{{font-family:system-ui,sans-serif;color:#dce7f5;background:#0c1423;color-scheme:dark}}body{{max-width:1100px;margin:48px auto;padding:0 24px;line-height:1.65}}h1{{font-size:3rem;letter-spacing:-.06em;margin-bottom:0}}h2{{margin-top:2.5rem}}.eyebrow{{color:#63e3bf;font-size:.8rem;letter-spacing:.18em}}.lead{{color:#a9bdd5;max-width:850px}}.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:16px;margin:32px 0}}.card,details{{background:#152135;border:1px solid #293a53;border-radius:12px;padding:20px}}.card strong{{display:block;font-size:1.6rem;color:#63e3bf}}table{{width:100%;border-collapse:collapse;text-align:left}}th,td{{border-bottom:1px solid #293a53;padding:12px;overflow-wrap:anywhere}}.fail td:nth-child(2){{color:#ffab97}}.pass td:nth-child(2){{color:#63e3bf}}a{{color:#9ecbff}}details{{margin:12px 0}}summary{{cursor:pointer}}pre{{overflow:auto;background:#0c1423;padding:14px}}small{{color:#a9bdd5}}@media(max-width:650px){{h1{{font-size:2.2rem}}body{{padding:0 14px;margin:24px auto}}th,td{{padding:6px}}}}</style>
<main><div class="eyebrow">SQLITE INDEX RETIREMENT LAB</div><h1>IndexWitness</h1>
<p class="lead">An experiment with every retained-index subset. Evidence for this snapshot and workload, with explicit result contracts and query budgets.</p>
<p><small>Source: {esc(report['source']['name'])} · SQLite {esc(report['runtime']['sqlite'])} · Python {esc(report['runtime']['python'])}</small></p>
<div class="cards"><div class="card"><strong>{len(report['experiments'])}</strong>subsets tested · complete</div><div class="card"><strong>{len(rec['removed'])}</strong>removal candidates</div><div class="card"><strong>{_ratio(rec['worst_callback_ratio'])}</strong>worst suggested-set ratio</div></div>
<h2>Suggested set</h2><p>Retain: <strong>{esc(', '.join(rec['retained']) or '(none)')}</strong></p><p>Removal candidates: {esc(', '.join(rec['removed']) or '(none)')}. Unique and unselected indexes remain pinned.</p>
<p class="lead">Progress callbacks at interval 1 measure an approximate work proxy, including query preparation. They are not wall time, I/O, saved bytes, or exact VM-step counters. Validate other queries, bindings, write workloads and future data before changing a database.</p>
<h2>Complete search</h2><table><thead><tr><th>Retained candidates</th><th>Budget + results</th><th>Weighted callbacks</th><th>Worst ratio</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
<h2>Joint-removal witnesses</h2><ul>{witnesses or '<li>No joint-removal trap found in this experiment.</li>'}</ul>
<h2>Query plans and evidence</h2>{''.join(details)}<p><small>Plans are raw EXPLAIN QUERY PLAN observations, never parsed as a stable protocol. Report contains schema identifiers and result hashes, but no result rows or bound parameters.</small></p></main></html>'''


def render(report, format):
    if format == "json":
        result = dict(report)
        result["interaction_witnesses"] = interaction_witnesses(report)
        return json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    return {"text": render_text, "markdown": render_markdown, "html": render_html}[format](report)
