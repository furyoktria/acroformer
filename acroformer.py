#!/usr/bin/env python3
"""acroformer — turn flat (non-fillable) PDF forms into fillable AcroForms.

Detects drawn form geometry (character boxes, checkboxes, dotted leaders,
write-on lines, empty table cells) and overlays real AcroForm widgets on the
original pages, so the output looks 100% identical to the source PDF.

Field naming: each widget is named from the printed label next to it,
e.g. `p02_nama_depan`, `p03_pekerjaan_2` — page prefix guarantees uniqueness.

Usage:
    python acroformer.py input.pdf [-o output.pdf] [--debug-overlay overlay.pdf]

Requires: PyMuPDF  (pip install pymupdf)
"""
import argparse
import json
import re
import sys
import unicodedata

import fitz


# ---------------------------------------------------------------- helpers

def cluster_rows(items, ykey, tol=2.5):
    """Group items whose y-centers lie within `tol` points."""
    rows = []
    for it in sorted(items, key=ykey):
        for row in rows:
            if abs(ykey(it) - row["y"]) <= tol:
                row["items"].append(it)
                break
        else:
            rows.append({"y": ykey(it), "items": [it]})
    return rows


def slugify(text, maxlen=40):
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    text = re.sub(r"_+", "_", text)
    return text[:maxlen].rstrip("_")


def label_for(kind, rect, words):
    """Derive a human label for a field from nearby printed text.

    Checkboxes are labeled by the words to their right ("[x] Saham / Stock"),
    text fields by the words to their left ("Nama Depan / First Name : [___]"),
    falling back to the closest label line up-left of the field. Bilingual
    labels are cut at "/" keeping the first (usually Indonesian) part.
    """
    yc = (rect.y0 + rect.y1) / 2

    def same_line(w):
        return w[1] - 3 < yc < w[3] + 3

    def firstpart(label):
        label = label.strip(" :.")
        return label.split("/")[0] if re.search(r"[a-zA-Z]", label) else ""

    if kind == "checkbox":
        right = sorted([w for w in words if same_line(w) and w[0] >= rect.x1 - 1
                        and w[0] - rect.x1 <= 80], key=lambda w: w[0])
        parts, prev = [], rect.x1
        for w in right:
            if w[0] - prev > 12 or w[4].startswith("/"):
                break
            if "/" in w[4]:
                parts.append(w[4].split("/")[0])
                break
            parts.append(w[4])
            prev = w[2]
        return " ".join(parts)

    # 1) words on the same line, left of the field ("label : [___]").
    # Cluster them by horizontal gaps and take the nearest cluster that
    # actually contains letters (skipping lone ":" or "/" separators).
    left = sorted([w for w in words if same_line(w) and w[2] <= rect.x0 + 2
                   and rect.x0 - w[2] <= 320], key=lambda w: w[0])
    clusters, cur = [], []
    for w in left:
        if cur and w[0] - cur[-1][2] > 14:
            clusters.append(cur)
            cur = []
        cur.append(w)
    if cur:
        clusters.append(cur)
    for cl in reversed(clusters):
        label = firstpart(" ".join(w[4] for w in cl))
        if label:
            return label

    # 2) label block up-left of the field (multi-line labels)
    band = [w for w in words if w[2] <= rect.x0 + 2 and rect.x0 - w[2] <= 320
            and w[3] > rect.y0 - 6 and w[1] < rect.y1 + 2]
    if band:
        top = min(w[1] for w in band)
        line = sorted([w for w in band if w[1] < top + 6], key=lambda w: w[0])
        label = firstpart(" ".join(w[4] for w in line))
        if label:
            return label

    # 3) label line directly above, starting near the field's left edge
    above = [w for w in words if rect.y0 - 20 < w[3] <= rect.y0 + 2
             and w[0] < rect.x1 and w[2] > rect.x0 - 30]
    if above and min(w[0] for w in above) > rect.x0 - 50:
        line_y = max(w[3] for w in above)
        line = sorted([w for w in above if w[3] > line_y - 6], key=lambda w: w[0])
        return firstpart(" ".join(w[4] for w in line))
    return ""


# ---------------------------------------------------------------- detection

def detect_fields(page):
    """Return [(kind, rect)] for one page. kind: 'text' | 'checkbox' | 'combN'."""
    ticks, boxes = [], []
    for d in page.get_drawings():
        for it in d["items"]:
            if it[0] == "re":
                r = it[1]
                w, h = r.width, r.height
                if w <= 2.2 and 5 <= h <= 24:
                    ticks.append(fitz.Rect(r))
                elif 5 <= w <= 22 and 5 <= h <= 20:
                    boxes.append(fitz.Rect(r))
            elif it[0] == "l":
                p1, p2 = it[1], it[2]
                if abs(p1.x - p2.x) < 1.2 and 5 <= abs(p1.y - p2.y) <= 24:
                    ticks.append(fitz.Rect(min(p1.x, p2.x), min(p1.y, p2.y),
                                           max(p1.x, p2.x) + 0.7, max(p1.y, p2.y)))

    # dedupe ticks drawn twice (as 're' and as 'l')
    uniq = []
    for t in ticks:
        if not any(abs(t.x0 - u.x0) < 3.0 and abs(t.y0 - u.y0) < 2.5
                   and abs(t.y1 - u.y1) < 2.5 for u in uniq):
            uniq.append(t)
    ticks = uniq

    fields = []
    words = page.get_text("words")
    glyphs = []
    for blk in page.get_text("rawdict")["blocks"]:
        if blk["type"] != 0:
            continue
        for line in blk["lines"]:
            for span in line["spans"]:
                for c in span["chars"]:
                    if c["c"].strip():
                        glyphs.append((c["c"], c["bbox"]))

    def classify_small(box, y0, y1):
        """Checkbox-size box: '/' or '-' neighbor => date cell (text);
        a letter/digit label immediately right => checkbox."""
        sep = lab = False
        for gc, (gx0, gy0, gx1, gy1) in glyphs:
            gcy = (gy0 + gy1) / 2
            if not (y0 - 2 < gcy < y1 + 2):
                continue
            near_l = 0 <= box.x0 - gx1 <= 10
            near_r = 0 <= gx0 - box.x1 <= 10
            if gc in "/-" and (near_l or near_r):
                sep = True
            if gc.isalnum() and near_r:
                lab = True
        return "text" if (sep or not lab) else "checkbox"

    # --- runs of vertical ticks: comb rows, single cells, checkboxes ---
    for row in cluster_rows(ticks, lambda r: (r.y0 + r.y1) / 2):
        xs = sorted(row["items"], key=lambda r: r.x0)
        yb0 = min(t.y0 for t in xs)
        yb1 = max(t.y1 for t in xs)

        def glyph_between(a, b):
            for _gc, (gx0, gy0, gx1, gy1) in glyphs:
                cx = (gx0 + gx1) / 2
                cy = (gy0 + gy1) / 2
                if a.x1 + 0.5 < cx < b.x0 - 0.5 and yb0 - 1 < cy < yb1 + 1:
                    return True
            return False

        runs, run = [], [xs[0]]
        for a, b in zip(xs, xs[1:]):
            gap = b.x0 - a.x0
            h = max(a.y1 - a.y0, b.y1 - b.y0)
            limit = max(2.4 * h, 30) if len(run) >= 2 else 1.8 * h + 6
            if gap > limit or glyph_between(a, b):
                runs.append(run)
                run = [b]
            else:
                run.append(b)
        runs.append(run)

        for rn in runs:
            y0 = min(t.y0 for t in rn)
            y1 = max(t.y1 for t in rn)
            h = y1 - y0
            if len(rn) == 2:
                gap = rn[1].x0 - rn[0].x1
                if gap < 5:
                    continue
                box = fitz.Rect(rn[0].x0, y0, rn[1].x1, y1)
                if gap <= 20 and gap <= 1.7 * h and classify_small(box, y0, y1) == "checkbox":
                    fields.append(("checkbox", box))
                elif gap <= 260:
                    fields.append(("text", fitz.Rect(rn[0].x1 + 0.8, y0 + 0.5,
                                                     rn[1].x0 - 0.8, y1 - 0.5)))
            elif len(rn) >= 3:
                cells = len(rn) - 1
                wtot = rn[-1].x1 - rn[0].x0
                cws = [rn[i + 1].x0 - rn[i].x0 for i in range(len(rn) - 1)]
                uniform = max(cws) / max(min(cws), 0.1) <= 1.9
                if wtot < 24:
                    box = fitz.Rect(rn[0].x0, y0, rn[-1].x1, y1)
                    if classify_small(box, y0, y1) == "checkbox":
                        fields.append(("checkbox", box))
                    else:
                        fields.append(("text", fitz.Rect(box.x0 + 1, y0 + 0.5,
                                                         box.x1 - 1, y1 - 0.5)))
                elif not uniform:
                    # mixed run: close tick pairs = checkboxes, drop strays
                    i = 0
                    while i < len(rn) - 1:
                        gap = rn[i + 1].x0 - rn[i].x1
                        if 5 <= gap <= 1.8 * (y1 - y0):
                            fields.append(("checkbox", fitz.Rect(rn[i].x0, y0,
                                                                 rn[i + 1].x1, y1)))
                            i += 2
                        else:
                            i += 1
                elif cells <= 12:
                    fields.append(("comb%d" % cells,
                                   fitz.Rect(rn[0].x0, y0 + 0.4, rn[-1].x1, y1 - 0.4)))
                else:
                    rect = fitz.Rect(rn[0].x0 + 1.2, y0 + 0.6, rn[-1].x1 - 1.2, y1 - 0.6)
                    if rect.width >= 15:
                        fields.append(("text", rect))

    # --- standalone small rects: contiguous runs => comb/text, isolated => checkbox ---
    for row in cluster_rows(boxes, lambda r: (r.y0 + r.y1) / 2):
        xs = sorted(row["items"], key=lambda r: r.x0)
        runs, run = [], [xs[0]]
        for a, b in zip(xs, xs[1:]):
            if (b.x0 - a.x1) <= 2.0:
                run.append(b)
            else:
                runs.append(run)
                run = [b]
        runs.append(run)
        for rn in runs:
            y0 = min(t.y0 for t in rn)
            y1 = max(t.y1 for t in rn)
            ws = [t.width for t in rn]
            uniform_r = max(ws) / max(min(ws), 0.1) <= 1.6
            if len(rn) >= 3 and uniform_r and (rn[-1].x1 - rn[0].x0) >= 24:
                if len(rn) <= 12:
                    fields.append(("comb%d" % len(rn),
                                   fitz.Rect(rn[0].x0, y0 + 0.4, rn[-1].x1, y1 - 0.4)))
                else:
                    fields.append(("text", fitz.Rect(rn[0].x0 + 1, y0 + 0.5,
                                                     rn[-1].x1 - 1, y1 - 0.5)))
            elif len(rn) >= 3 and (rn[-1].x1 - rn[0].x0) < 24:
                box = fitz.Rect(rn[0].x0, y0, rn[-1].x1, y1)
                if classify_small(box, y0, y1) == "checkbox":
                    fields.append(("checkbox", box))
                else:
                    fields.append(("text", fitz.Rect(box.x0 + 1, y0 + 0.5,
                                                     box.x1 - 1, y1 - 0.5)))
            else:
                for t in rn:
                    if t.width >= 5:
                        fields.append(("checkbox", t))

    # --- dotted-leader / underscore runs (char-level, handles mixed spans) ---
    for blk in page.get_text("rawdict")["blocks"]:
        if blk["type"] != 0:
            continue
        for line in blk["lines"]:
            for span in line["spans"]:
                run = []

                def flush(run):
                    if len(run) >= 4:
                        x0 = run[0]["bbox"][0]
                        x1 = run[-1]["bbox"][2]
                        ry1 = max(c["bbox"][3] for c in run)
                        ry0 = min(c["bbox"][1] for c in run)
                        hgt = max(ry1 - ry0, 11)
                        if x1 - x0 >= 25:
                            fields.append(("text", fitz.Rect(x0, ry1 - hgt, x1, ry1)))

                for c in span["chars"]:
                    if c["c"] in "._…":
                        run.append(c)
                    elif c["c"] != " ":
                        flush(run)
                        run = []
                flush(run)

    # --- empty bordered table cells ---
    for d in page.get_drawings():
        if d.get("fill") and sum(d["fill"]) < 2.0:
            continue
        col = d.get("color")
        if col is None or sum(col) > 1.5:
            continue
        for it in d["items"]:
            if it[0] != "re":
                continue
            r = it[1]
            if 60 <= r.width <= 420 and 13 <= r.height <= 32:
                inner = fitz.Rect(r.x0 + 2, r.y0 + 2, r.x1 - 2, r.y1 - 2)
                if any(fitz.Rect(w[:4]).intersects(inner) for w in words):
                    continue
                fields.append(("text", fitz.Rect(r.x0 + 2, r.y0 + 1.5, r.x1 - 2, r.y1 - 1.5)))

    # --- drawn write-on lines (e.g. "Lainnya/Others ____") ---
    for d in page.get_drawings():
        for it in d["items"]:
            r = None
            if it[0] == "re" and it[1].height <= 1.5 and 28 <= it[1].width <= 260:
                r = it[1]
            elif it[0] == "l":
                p1, p2 = it[1], it[2]
                if abs(p1.y - p2.y) < 1 and 28 <= abs(p1.x - p2.x) <= 260:
                    r = fitz.Rect(min(p1.x, p2.x), min(p1.y, p2.y),
                                  max(p1.x, p2.x), max(p1.y, p2.y) + 0.5)
            if r is None:
                continue
            above = fitz.Rect(r.x0 + 6, r.y0 - 9.0, r.x1 - 2, r.y0 - 0.5)
            if any(fitz.Rect(w[:4]).intersects(above) for w in words):
                continue  # it's an underline, not a write-on line
            hasl = any(abs(w[3] - r.y1) < 6 and 0 <= r.x0 - w[2] < 45 for w in words)
            hasr = any(abs(w[3] - r.y1) < 6 and 0 <= w[0] - r.x1 < 45 for w in words)
            if not (hasl or hasr):
                continue
            fields.append(("text", fitz.Rect(r.x0, r.y0 - 10.5, r.x1, r.y0 + 0.5)))

    # --- stacked horizontal rules => table-row text fields ---
    hrules = []
    for d in page.get_drawings():
        for it in d["items"]:
            r = None
            if it[0] == "re" and it[1].height <= 1.2 and it[1].width >= 60:
                r = it[1]
            elif it[0] == "l":
                p1, p2 = it[1], it[2]
                if abs(p1.y - p2.y) < 1 and abs(p1.x - p2.x) >= 60:
                    r = fitz.Rect(min(p1.x, p2.x), p1.y, max(p1.x, p2.x), p1.y + 0.5)
            if r is not None:
                hrules.append(r)
    cols = {}
    for r in hrules:
        cols.setdefault((round(r.x0 / 3), round(r.width / 3)), []).append(r)
    for rs in cols.values():
        rs = sorted(rs, key=lambda r: r.y0)
        ys = []
        for r in rs:
            if not ys or r.y0 - ys[-1].y0 > 3:
                ys.append(r)
        if len(ys) < 3:
            continue
        for a, b in zip(ys, ys[1:]):
            gap = b.y0 - a.y1
            if not (9 <= gap <= 26):
                continue
            cell = fitz.Rect(a.x0 + 2, a.y1 + 1, a.x1 - 2, b.y0 - 1)
            inner = fitz.Rect(cell.x0 + 2, cell.y0 + 2, cell.x1 - 2, cell.y1 - 2)
            if any(fitz.Rect(w[:4]).intersects(inner) for w in words):
                continue
            fields.append(("text", cell))

    # --- suppress fields inside shaded (gray) header cells ---
    shaded = []
    for d in page.get_drawings():
        f = d.get("fill")
        if f and 0.2 < sum(f) < 2.5:
            r = d["rect"]
            if r.width > 30 and r.height > 8:
                shaded.append(r)

    def in_shade(r):
        c = fitz.Point((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2)
        return any(s.contains(c) for s in shaded)

    fields = [(k, r) for k, r in fields if not in_shade(r)]

    # --- checkboxes must be empty inside (no pre-printed glyphs) ---
    def has_glyph(r):
        inner = fitz.Rect(r.x0 + 1.5, r.y0 + 1.5, r.x1 - 1.5, r.y1 - 1.5)
        return any(fitz.Rect(g[1]).intersects(inner) for g in glyphs)

    fields = [(k, r) for k, r in fields if not (k == "checkbox" and has_glyph(r))]

    # --- dedupe overlapping candidates (bigger first) ---
    kept = []
    for kind, r in sorted(fields, key=lambda f: -abs(f[1])):
        if any(abs(r & r2) > 0.4 * min(abs(r), abs(r2)) for _, r2 in kept):
            continue
        kept.append((kind, r))
    # reading order, so duplicate labels number top-down
    kept.sort(key=lambda f: (round(f[1].y0), f[1].x0))
    return kept, words, glyphs


# ---------------------------------------------------------------- build

def build(src, out, debug_overlay=None, field_map=None):
    doc = fitz.open(src)
    used = set()
    stats = {"text": 0, "comb": 0, "checkbox": 0}
    mapping = []

    for pno in range(len(doc)):
        page = doc[pno]
        kept, words, glyphs = detect_fields(page)

        # pass 1: raw labels
        bases = [slugify(label_for(kind, r, words)) for kind, r in kept]

        # pass 2a: continuation lines (e.g. address line 2) inherit the label
        # of the text field directly above them
        for i, (kind, r) in enumerate(kept):
            if bases[i] or kind == "checkbox":
                continue
            for j, (kj, rj) in enumerate(kept):
                if kj == "checkbox" or not bases[j]:
                    continue
                if abs(rj.x0 - r.x0) < 6 and 0 < r.y0 - rj.y0 <= 20:
                    bases[i] = bases[j]
                    break

        # pass 2b: date rows — narrow boxes on one line separated by printed
        # "/" or "-" get dd/mm/yyyy suffixes
        by_row = {}
        for i, (kind, r) in enumerate(kept):
            if kind != "checkbox" and r.width < 75:
                by_row.setdefault(round((r.y0 + r.y1) / 2 / 4), []).append(i)
        for idxs in by_row.values():
            if len(idxs) < 2:
                continue
            idxs = sorted(idxs, key=lambda i: kept[i][1].x0)
            seps = 0
            for a, b in zip(idxs, idxs[1:]):
                ra, rb = kept[a][1], kept[b][1]
                if any(gc in "/-" and ra.x1 - 2 < (g[0] + g[2]) / 2 < rb.x0 + 2
                       and ra.y0 - 3 < (g[1] + g[3]) / 2 < ra.y1 + 3
                       for gc, g in glyphs):
                    seps += 1
            if seps == len(idxs) - 1 and len(idxs) == 3:
                root = next((bases[i] for i in idxs if bases[i]), "tanggal")
                for i, suf in zip(idxs, ("dd", "mm", "yyyy")):
                    bases[i] = f"{root}_{suf}"

        for (kind, r), base in zip(kept, bases):
            base = base or "field"
            name = f"p{pno + 1:02d}_{base}"
            n = 1
            while name in used:
                n += 1
                name = f"p{pno + 1:02d}_{base}_{n}"
            used.add(name)

            w = fitz.Widget()
            w.rect = r
            w.field_name = name
            if kind == "checkbox":
                w.field_type = fitz.PDF_WIDGET_TYPE_CHECKBOX
                stats["checkbox"] += 1
            elif kind.startswith("comb"):
                w.field_type = fitz.PDF_WIDGET_TYPE_TEXT
                w.text_maxlen = int(kind[4:])
                w.field_flags = 1 << 24  # comb: one character per box
                w.text_fontsize = min(9, max(6, r.height * 0.62))
                stats["comb"] += 1
            else:
                w.field_type = fitz.PDF_WIDGET_TYPE_TEXT
                w.text_fontsize = min(9, max(6, r.height * 0.62))
                stats["text"] += 1
            w.border_width = 0
            w.fill_color = None
            w.text_color = (0, 0, 0.6)
            page.add_widget(w)
            mapping.append({"name": name, "page": pno + 1,
                            "type": "checkbox" if kind == "checkbox" else "text",
                            "maxlen": int(kind[4:]) if kind.startswith("comb") else None,
                            "rect": [round(v, 1) for v in r]})

    doc.save(out)
    print(f"{out}: {stats['text']} text + {stats['comb']} comb + "
          f"{stats['checkbox']} checkbox = {sum(stats.values())} fields")

    if field_map:
        with open(field_map, "w") as fh:
            json.dump(mapping, fh, indent=2, ensure_ascii=False)
        print(f"{field_map}: field map written ({len(mapping)} fields)")

    if debug_overlay:
        dbg = fitz.open(out)
        for page in dbg:
            shape = page.new_shape()
            for w in page.widgets():
                if w.field_type == fitz.PDF_WIDGET_TYPE_CHECKBOX:
                    col = (0, 0.7, 0)
                elif w.text_maxlen:
                    col = (0, 0, 1)
                else:
                    col = (1, 0, 0)
                shape.draw_rect(w.rect)
                shape.finish(color=col, width=1.2)
            shape.commit()
        dbg.save(debug_overlay)
        print(f"{debug_overlay}: overlay written (red=text, blue=comb, green=checkbox)")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("input", help="flat PDF form")
    ap.add_argument("-o", "--output", help="output path (default: <input>_acroform.pdf)")
    ap.add_argument("--debug-overlay", help="also write a copy with color-coded field outlines")
    ap.add_argument("--map", help="also write a JSON field map (name, page, type, rect)")
    args = ap.parse_args()
    out = args.output or re.sub(r"\.pdf$", "", args.input, flags=re.I) + "_acroform.pdf"
    build(args.input, out, args.debug_overlay, args.map)


if __name__ == "__main__":
    sys.exit(main())
