"""
Build a single scrollable audit HTML that renders every insert's content with
KEEP / STRUCK / EDIT / INSERT markers. Reads outputs/inserts/Insert_*.html
(which generate_inserts.py already saved alongside each PDF) and the
insert_plan. Emits outputs/audit.html.

Purpose: replace the O(488)-PDF manual review with a single page you can
skim. Anomalies are flagged at the top with jump-links; each insert shows
summary + line-by-line diff-style markup.

Run AFTER generate_inserts.py (so Insert_*.html files exist).

Usage:
    python src/audit.py
"""

from __future__ import annotations

import html as htmllib
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Tuple

from bs4 import BeautifulSoup

from patterns import (
    LINE_NUM_RE,
    is_chapter_year_header,
    is_fund_top,
    line_num_of,
    RE_AMOUNT_RE,
)


ROOT = Path(__file__).resolve().parent.parent
OUTPUTS = ROOT / "outputs"
INSERTS_DIR = OUTPUTS / "inserts"


# ──────────────────────────────────────────────────────────────────────────
# Line classification for a single <p> in an insert HTML
# ──────────────────────────────────────────────────────────────────────────

def classify_p(p) -> Tuple[str, str]:
    """Return (class, rendered_text).
    class: keep | struck | insert | edit | page_header | blank
    rendered_text: HTML-safe text with <del>/<ins> boundaries marked inline.
    """
    text = p.get_text()
    if not text.strip():
        return "blank", ""
    dels = p.find_all("del")
    inss = p.find_all("ins")
    visible_ln = line_num_of(text)
    is_page_header = visible_ln is None

    # A line we tracked-inserted for the label: "Insert NNNA"
    if p.get("class") and "new-line" in p.get("class"):
        return "insert", htmllib.escape(text)

    if not dels and not inss:
        return ("page_header" if is_page_header else "keep"), htmllib.escape(text)

    # Fully struck (all text wrapped by del, no ins)
    if dels and not inss:
        del_text = "".join(d.get_text() for d in dels)
        if del_text.strip() == text.strip():
            return "struck", htmllib.escape(text)

    # Mixed: render inline with markers
    parts: List[str] = []
    for child in p.children:
        if getattr(child, "name", None) == "del":
            parts.append(
                f'<span class="d">{htmllib.escape(child.get_text())}</span>'
            )
        elif getattr(child, "name", None) == "ins":
            parts.append(
                f'<span class="i">{htmllib.escape(child.get_text())}</span>'
            )
        elif hasattr(child, "get_text"):
            parts.append(htmllib.escape(child.get_text()))
        else:
            parts.append(htmllib.escape(str(child)))
    return "edit", "".join(parts)


# ──────────────────────────────────────────────────────────────────────────
# Anomaly detectors run per-insert
# ──────────────────────────────────────────────────────────────────────────

_SUBSCHEDULE_MARKERS = (
    # Only the tail/closure marker — the plain word "subschedule" also
    # appears in legit survivor body text like "according to the following
    # subschedule:" (e.g. DOL 34215), so matching it creates false positives.
    "Total of sub-schedule",
)
_SEPARATOR_RE = re.compile(r"^-{5,}|^={5,}")


def detect_anomalies(insert: dict, lines: List[Tuple[str, str, str]]) -> List[str]:
    """lines: list of (kind, raw_text, rendered_html) for every <p> in the insert.
    Returns a list of short anomaly tags. Empty if clean."""
    tags: List[str] = []

    # (A) [removed] survivor@line1 — used to proxy sub-schedule tail
    #     attribution. Now that the extractor detects sub-schedule blocks
    #     and doesn't start buffering inside them, this fires only on
    #     legitimate page-break reapprop continuations. Not useful.

    # (B) Sub-schedule markers in KEPT lines (parent reapprop attribution bug)
    kept_texts = [raw for (k, raw, _h) in lines if k in ("keep", "edit")]
    for raw in kept_texts:
        low = raw.lower()
        if any(m.lower() in low for m in _SUBSCHEDULE_MARKERS):
            tags.append("subschedule-in-kept")
            break
    # Separator "---------" in kept
    for raw in kept_texts:
        body = raw.strip()
        if _SEPARATOR_RE.match(body):
            tags.append("separator-in-kept")
            break

    # (C) Struck content bisecting kept BODY content: a STRUCK line between
    #     two KEPT reapprop bodies means planner should have split here.
    #     Careful: preserved CHYR / FUND headers (kept by needs_chapter_header
    #     / needs_fund_header logic) may sit on an earlier page with struck
    #     non-survivor reapprops between them and the actual survivor body —
    #     that's expected, not a bug. Only flag when struck content sits
    #     between two kept SURVIVOR-body lines.
    #
    #     A "body keep" is a kept/edited line that (a) has a reapprop
    #     terminator "(re. $X)" OR (b) carries an approp_id "(XXXXX)" OR
    #     (c) continues such a line. We approximate with a looser rule:
    #     kept/edited line containing "(re." or an "(NNNNN)" approp_id
    #     pattern. Chyr headers ("By chapter ...") and fund headers never
    #     match those.
    body_kinds_rich = [(k, raw) for (k, raw, _h) in lines
                       if k in ("keep", "struck", "edit")]
    def is_body_keep(k, raw):
        if k not in ("keep", "edit"):
            return False
        if "(re." in raw:
            return True
        if re.search(r"\(\d{5}\)", raw):
            return True
        # Continuation of a survivor body line — hard to detect in isolation.
        # Treat as body keep if preceded by a body-keep within 3 lines (done
        # separately below).
        return False
    # First pass: explicit body keeps
    body_keep_idx = [
        i for i, (k, raw) in enumerate(body_kinds_rich) if is_body_keep(k, raw)
    ]
    # Expand backward: any keep/edit immediately preceding a body-keep (with
    # no struck line in between) is ALSO a body keep (survivor's intro lines).
    expanded = set(body_keep_idx)
    for idx in body_keep_idx:
        j = idx - 1
        while j >= 0:
            kj, rawj = body_kinds_rich[j]
            if kj in ("keep", "edit"):
                expanded.add(j)
                j -= 1
            else:
                break
    if expanded:
        first_bk = min(expanded)
        last_bk = max(expanded)
        middle = [body_kinds_rich[i] for i in range(first_bk + 1, last_bk)
                  if i not in expanded]
        if any(k == "struck" for (k, _r) in middle):
            tags.append("struck-between-kept")

    # (D) No kept body lines at all — insert would render empty
    if not any(k in ("keep", "edit") for (k, _r, _h) in lines):
        tags.append("empty-insert")

    # (E) Zero struck lines — no deletions at all (usually fine, but flag
    #     for cases where survivors were ALL the lines on the source page).
    if not any(k == "struck" for (k, _r, _h) in lines):
        tags.append("no-strikes")

    # (F) Chapter-year header flagged `needs_chapter_header` but no chyr
    #     header ends up kept in the rendered HTML.
    any_needs_ch = any(s["needs_chapter_header"] for s in insert["survivors"])
    if any_needs_ch:
        has_kept_chyr = any(
            k in ("keep",) and is_chapter_year_header(raw) is not None
            for (k, raw, _h) in lines
        )
        if not has_kept_chyr:
            tags.append("missing-chyr-header")

    # (G) Fund header needed but not kept
    any_needs_fd = any(s["needs_fund_header"] for s in insert["survivors"])
    if any_needs_fd:
        has_kept_fund = any(
            k == "keep" and is_fund_top(raw)
            for (k, raw, _h) in lines
        )
        if not has_kept_fund:
            tags.append("missing-fund-header")

    return tags


# ──────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────

CSS = """
body { font: 12px/1.4 ui-monospace, monospace; max-width: 1200px; margin: 0 auto; padding: 24px; background: #fafafa; color: #222; }
h1 { font-size: 22px; margin: 0 0 8px; }
.meta { color: #666; margin-bottom: 24px; }
details { background: white; border: 1px solid #ddd; border-radius: 4px; margin-bottom: 8px; padding: 8px 12px; }
details[open] { padding-bottom: 12px; }
summary { cursor: pointer; font-weight: 600; }
.tags { color: #b00; margin-left: 8px; font-weight: 400; }
.toc { background: white; border: 1px solid #ddd; border-radius: 4px; padding: 12px 16px; margin-bottom: 16px; }
.toc a { color: #06c; text-decoration: none; margin-right: 12px; display: inline-block; padding: 2px 4px; }
.toc a:hover { background: #eef; }
.toc h3 { font-size: 13px; margin: 6px 0; }
pre { white-space: pre-wrap; margin: 0; font-size: 11px; }
.line { padding: 0 6px; border-left: 3px solid transparent; }
.line.keep { border-color: #5aa; }
.line.struck { color: #888; text-decoration: line-through; border-color: #bbb; background: #f3f3f3; }
.line.insert { border-color: #5c5; background: #efe; color: #030; }
.line.edit { border-color: #a5c; background: #fef4ff; }
.line.page_header { color: #999; border-color: #eee; font-style: italic; }
.line.blank { height: 8px; }
.d { text-decoration: line-through; color: #a00; }
.i { text-decoration: underline; color: #060; }
.stats { color: #666; font-size: 11px; margin: 4px 0 8px; }
"""


def main():
    plan = json.loads((OUTPUTS / "insert_plan.json").read_text())

    # Build per-insert rendering
    rendered: List[dict] = []  # {label, tags, lines, stats, survivor_brief}
    flagged_by_tag: Dict[str, List[str]] = defaultdict(list)

    for ins in plan:
        label = ins["label"]
        html_path = INSERTS_DIR / f"Insert_{label}.html"
        if not html_path.exists():
            rendered.append({
                "label": label,
                "tags": ["no-html"],
                "lines": [],
                "stats": "",
                "survivor_brief": "",
                "exec_page": ins["label_pdf_page"],
            })
            flagged_by_tag["no-html"].append(label)
            continue

        soup = BeautifulSoup(html_path.read_text(), "lxml")
        page_divs = soup.find_all("div", class_="page")
        lines: List[Tuple[str, str, str]] = []
        for pd in page_divs:
            for p in pd.find_all("p"):
                kind, rendered_html = classify_p(p)
                lines.append((kind, p.get_text(), rendered_html))
            # Thin separator between source pages
            lines.append(("blank", "", ""))

        tags = detect_anomalies(ins, lines)
        for t in tags:
            flagged_by_tag[t].append(label)

        counts = defaultdict(int)
        for k, _r, _h in lines:
            counts[k] += 1
        stats = (f"keep={counts['keep']} struck={counts['struck']} "
                 f"edit={counts['edit']} insert={counts['insert']}")

        s0 = ins["survivors"][0]
        survivor_brief = (
            f"{len(ins['survivors'])} survivor(s) · "
            f"${sum(s['new_reapprop_amount'] for s in ins['survivors']):,} · "
            f"{s0.get('agency','')} · {s0['program'][:40]} · chyr "
            + ",".join(sorted({str(s['chapter_year']) for s in ins['survivors']}))
            + f" · src={s0.get('source', 'reapprop')}"
        )

        rendered.append({
            "label": label,
            "tags": tags,
            "lines": lines,
            "stats": stats,
            "survivor_brief": survivor_brief,
            "exec_page": ins["label_pdf_page"],
        })

    # Sort by exec_page then label
    rendered.sort(key=lambda r: (r["exec_page"], r["label"]))

    # Emit HTML
    out = OUTPUTS / "audit.html"
    parts: List[str] = []
    parts.append(f"<!doctype html><html><head><meta charset='utf-8'>"
                 f"<title>Insert audit — {len(plan)} inserts</title>"
                 f"<style>{CSS}</style></head><body>")
    parts.append(f"<h1>Insert audit</h1>")
    parts.append(f"<div class='meta'>{len(plan)} inserts · "
                 f"{sum(1 for r in rendered if r['tags'])} flagged · "
                 f"{sum(len(ins['survivors']) for ins in plan)} survivors · "
                 f"${sum(s['new_reapprop_amount'] for ins in plan for s in ins['survivors']):,} total</div>")

    # TOC of anomaly groups
    parts.append("<div class='toc'><h3>Flags</h3>")
    for tag in sorted(flagged_by_tag.keys()):
        labels = flagged_by_tag[tag]
        parts.append(f"<details><summary><b>{tag}</b> "
                     f"<span class='tags'>({len(labels)})</span></summary><div>")
        for lbl in labels:
            parts.append(f"<a href='#ins-{lbl}'>{lbl}</a>")
        parts.append("</div></details>")
    parts.append("</div>")

    # Each insert as collapsible
    for r in rendered:
        tag_html = ""
        if r["tags"]:
            tag_html = " <span class='tags'>[" + ", ".join(r["tags"]) + "]</span>"
        parts.append(
            f"<details id='ins-{r['label']}'><summary>{r['label']}"
            f"{tag_html}</summary>"
        )
        parts.append(f"<div class='stats'>{r['survivor_brief']}<br>{r['stats']}</div>")
        parts.append("<pre>")
        for k, _raw, rendered_line in r["lines"]:
            if k == "blank":
                parts.append(f"<div class='line blank'> </div>")
            else:
                parts.append(f"<div class='line {k}'>{rendered_line or '&nbsp;'}</div>")
        parts.append("</pre></details>")

    parts.append("</body></html>")
    out.write_text("".join(parts), encoding="utf-8")

    # Summary on stdout
    print(f"\n{'='*72}\nAUDIT\n{'='*72}")
    print(f"  Inserts rendered:         {len(rendered)}")
    print(f"  Flagged (any anomaly):    {sum(1 for r in rendered if r['tags'])}")
    for tag in sorted(flagged_by_tag.keys()):
        print(f"    {tag:30s} {len(flagged_by_tag[tag])}")
    print(f"\n  Saved: {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
