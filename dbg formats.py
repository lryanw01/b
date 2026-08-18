"""dbg_formats.py — how many different table layouts does each vendor use?

    python dbg_formats.py
    python dbg_formats.py --vendor Mini-Circuits --sample 400
    python dbg_formats.py --vendor MACOM --show 3        (example files per format)
    python dbg_formats.py --csv formats.csv

Fingerprints the STRUCTURE of each datasheet's spec table -- never the values --
and groups the files by fingerprint. The output says how many distinct layouts a
vendor actually uses and what share of its library each one covers.

WHY
    A generalised reader has to be right about every layout at once, and each
    new rule risks the ones already working. If a vendor turns out to use four
    layouts covering 95% of its files, four exact readers are simpler to write,
    easier to verify, and cannot interfere with each other. This is the survey
    that says whether that is true, and which four.

    The fingerprint deliberately ignores which specs are present. Two amplifier
    datasheets and a filter datasheet in the same house style are ONE format;
    the same spec in two house styles is two.

Read-only, and it does not parse specs -- only enough structure to classify.
"""
from __future__ import annotations

import argparse
import csv
import random
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _opt(name):
    for pkg in ("rfparts", "pythonrfparts"):
        try:
            return __import__(f"{pkg}.{name}", fromlist=[name])
        except Exception:
            continue
    return None


dsmine = _opt("dsmine")
if dsmine is None:
    sys.exit("Could not import dsmine. Run this from the folder holding "
             "rfparts/.")

EXTS = (".pdf", ".html", ".htm")

# ---- structural probes -----------------------------------------------------
_MIN = re.compile(r"^min(?:imum)?\.?$", re.I)
_TYP = re.compile(r"^typ(?:ical)?\.?$", re.I)
_MAX = re.compile(r"^max(?:imum)?\.?$", re.I)
_UNITS = re.compile(r"^units?$", re.I)
_COND = re.compile(r"^(test\s*)?conditions?$|^notes?$", re.I)
_PARAM = re.compile(r"^(parameter|specification|spec|description|item)s?$",
                    re.I)
_TEMP = re.compile(r"^[-+]?\d{1,3}\s*\u00b0?\s*[oCc]?C?\s*(?:to|-)\s*"
                   r"[-+]?\d{1,3}\s*\u00b0?\s*[oCc]?C?\*?$|"
                   r"^[-+]?\d{1,3}\s*\u00b0?\s*[oCc]?C\*?$", re.I)
_SYMBOL = re.compile(r"^F\s*c$|^F\s*1\s*-\s*F\s*2$|^F\s*[3-9]$", re.I)
_SUBPORT = re.compile(r"^(?:[SP]\d(?:\s*[,&/]\s*[SP]\d)*|RF\s?\d|LO|IF|"
                      r"low[\s-]?side|high[\s-]?side|in[\s-]?band)$", re.I)
_SPECWORD = re.compile(
    r"gain|loss|isolation|vswr|return|noise|power|current|voltage|"
    r"frequency|coupling|directivity|attenuation|phase|delay|rejection",
    re.I)


_WITH_STYLE = [False]


def table_fills(path, pages=2):
    """Distinct fill colours used behind table cells, and how they repeat.

    Vendors reuse a house table style, and the shading is the most stable part
    of it: a shaded header band, alternating row stripes, or nothing. Two
    datasheets with the same shading almost always want the same reader, which
    makes it a better grouping key than anything in the text.
    """
    try:
        import pdfplumber
    except ImportError:
        return "?", 0
    fills, striped = [], False
    try:
        with pdfplumber.open(str(path)) as pdf:
            for page in pdf.pages[:pages]:
                ys = []
                for r in page.rects:
                    col = r.get("non_stroking_color")
                    if col is None:
                        continue
                    if isinstance(col, (int, float)):
                        col = (col,)
                    key = tuple(round(float(c), 2) for c in col)
                    if key in ((1.0,), (1.0, 1.0, 1.0), (0.0, 0.0, 0.0, 0.0)):
                        continue        # white / unset
                    if float(r.get("height") or 0) > 2:
                        fills.append(key)
                        ys.append(round(float(r["top"]), 0))
                ys.sort()
                gaps = [b - a for a, b in zip(ys, ys[1:]) if 4 < b - a < 40]
                if len(gaps) >= 4:
                    striped = True
    except Exception:
        return "?", 0
    n = len(set(fills))
    if not fills:
        return "unshaded", 0
    if striped:
        return "striped", n
    return ("banded-header" if n <= 2 else "multi-colour"), n


def column_profile(path, pages=2, tol=6.0):
    """Column x-positions and what each column mostly holds.

    Everything below is decided positionally. Word-matching a header failed
    repeatedly -- a conditions column headed "Comments", or a header that never
    made it into the extracted rows at all, both read as "no conditions" when
    the column was plainly there. What a column CONTAINS does not lie.
    """
    cols = defaultdict(list)
    for pno in range(1, pages + 1):
        try:
            rows = dsmine.cells_xy(path, pno)
        except Exception:
            continue
        for r in rows:
            for c in (r.get("cells") or []):
                txt = str(c.get("t") or "").strip()
                if not txt:
                    continue
                x = round(float(c.get("x0", 0)) / tol) * tol
                cols[x].append(txt)
    out = []
    for x in sorted(cols):
        vals = cols[x]
        if len(vals) < 3:
            continue
        numeric = sum(1 for v in vals if dsmine._cell_num(v) is not None)
        unitish = sum(1 for v in vals
                      if re.fullmatch(r"\(?\s*(dB|dBm|GHz|MHz|kHz|ns|ps|mA|"
                                      r"uA|V|W|mW|deg|\u00b0|%|:1|Ohms?)\s*\)?",
                                      v, re.I))
        prose = sum(1 for v in vals
                    if dsmine._cell_num(v) is None and len(v) > 4)
        kind = ("unit" if unitish >= 0.5 * len(vals)
                else "value" if numeric >= 0.6 * len(vals)
                else "text")
        out.append({"x": x, "n": len(vals), "kind": kind,
                    "prose": prose / len(vals), "vals": vals})
    return out


def has_conditions(rows_or_path, path=None):
    """Is there a conditions/comments column?

    Positional: a column of mostly prose that is NOT the leftmost (parameter)
    column is a conditions or comments column, whatever its heading says.
    """
    p = path or (rows_or_path if isinstance(rows_or_path, Path) else None)
    if p is None:
        return False
    prof = column_profile(p)
    text_cols = [c for c in prof if c["kind"] == "text" and c["prose"] > 0.5]
    return len(text_cols) >= 2          # parameter column, plus one more


def band_columns(path, pages=2):
    """True when the frequency bands are COLUMNS, not rows.

    A header row of two or more band cells means each spec row carries one
    value per band:

        Frequency      | DC - 2 | 2 - 6 | 6 - 10 | 10 - 18
        Insertion Loss |  0.34  | 0.61  |  0.78  |  1.22

    That is piecewise ACROSS the row, and a reader that takes the first number
    reports only the easiest band.
    """
    for pno in range(1, pages + 1):
        try:
            rows = dsmine.cells_xy(path, pno)
        except Exception:
            continue
        for r in rows:
            toks = [str(c.get("t") or "").strip() for c in (r.get("cells") or [])]
            if not toks:
                continue
            # A band HEADER names no spec: its first cell is blank or a column
            # title. SMA88's "Frequency MHz 2-500 5-500 5-500" is a spec ROW
            # measured at three temperatures, and reading it as a band header
            # inverted the whole table.
            if _SPECWORD.search(toks[0] or ""):
                continue
            bands = [x for x in toks if dsmine.parse_band_cell(x)]
            if len(bands) >= 2 and len(set(bands)) >= 2:
                return True
    return False


def is_multirow(path, pages=2, tol=6.0):
    """Does any spec span more than one row?

    Judged the way you read it: a CONTINUATION row has nothing at the parameter
    column's x position, but still carries a unit or a value on the same line.
    Counting rows per spec was the wrong measure -- the exact number never
    mattered, and averaging it over a whole document buried the multi-row specs
    among the single-row ones.
    """
    prof = column_profile(path, pages, tol)
    if not prof:
        return False
    param_x = prof[0]["x"]
    value_xs = {c["x"] for c in prof if c["kind"] in ("value", "unit")}
    if not value_xs:
        return False
    conts = 0
    for pno in range(1, pages + 1):
        try:
            rows = dsmine.cells_xy(path, pno)
        except Exception:
            continue
        for r in rows:
            cells = r.get("cells") or []
            if not cells:
                continue
            xs = {round(float(c.get("x0", 0)) / tol) * tol: str(c.get("t") or "")
                  for c in cells}
            at_param = str(xs.get(param_x, "")).strip()
            on_line = any(x in value_xs and str(v).strip() for x, v in xs.items())
            if on_line and not at_param:
                conts += 1
                if conts >= 3:
                    return True
    return False


def repeats_across_tables(rows, max_rows=400):
    """Does the same spec name appear in two or more separate tables?

    Some vendors publish one table per condition -- 25 C, then -55 C, then
    +125 C -- so a spec is piecewise ACROSS tables rather than down rows. A
    reader that stops at the first table silently reports the best case.
    """
    seen, blocks, gap = defaultdict(set), 0, 0
    for _p, cells in rows[:max_rows]:
        toks = [str(c or "").strip() for c in (cells or [])]
        if not toks or not any(toks):
            gap += 1
            if gap == 2:
                blocks += 1
            continue
        gap = 0
        first = (toks[0] or "").lower()
        if _SPECWORD.search(first):
            seen[re.sub(r"[^a-z]", "", first)[:18]].add(blocks)
    return sum(1 for v in seen.values() if len(v) >= 2) >= 3


def archetype(path, max_rows=400):
    """A COARSE archetype: how a spec's values are laid out relative to its row.

    Three things decide which reader a datasheet needs, and nothing else does:
      * are the frequency bands COLUMNS (piecewise across the row)?
      * does a spec span more than one ROW (piecewise down the table)?
      * does the same spec continue into another TABLE?
    Column order, header wording and where the conditions sit are case-by-case
    details inside a reader, not different formats.
    """
    try:
        rows = dsmine.rows_for(path)
    except Exception as e:
        return "ERROR", {"note": type(e).__name__}
    if not rows:
        try:
            txt = dsmine.datasheet_text(path)
        except Exception:
            txt = ""
        return ("NO-TEXT" if not txt.strip() else "NO-TABLE"), {}

    pdf = path.suffix.lower() == ".pdf"
    bandcols = band_columns(path) if pdf else False
    multirow = is_multirow(path) if pdf else False
    across = repeats_across_tables(rows, max_rows)
    conds = has_conditions(rows, path) if pdf else True
    style, ncol = table_fills(path) if pdf else ("html", 0)

    if bandcols:
        shape = "band-columns"
    elif multirow:
        shape = "multi-row"
    else:
        shape = "single-row"

    sig = shape
    if across:
        sig += " | repeats-across-tables"
    if not conds:
        sig += " | no-conditions-col"
    if _WITH_STYLE[0]:
        sig += f" | style:{style}"
    return sig, {"shape": shape, "band_columns": bandcols,
                 "multirow": multirow, "across": across, "conditions": conds,
                 "style": style, "fills": ncol}


def _has_cond_column(rows):
    for _p, cells in rows[:60]:
        for c in cells[:4]:
            if _COND.match(str(c or "").strip()):
                return True
    return False


def _cond_in_labels(rows):
    """Conditions written into the label itself: 'Gain @ 2 GHz', 'IL, 25C'."""
    hits = 0
    for _p, cells in rows:
        first = str((cells or [""])[0] or "")
        if _SPECWORD.search(first) and re.search(
                r"@|\bat\b|,\s*\d|\d\s*(GHz|MHz|\u00b0?C)\b", first, re.I):
            hits += 1
            if hits >= 3:
                return True
    return False


def vendor_dirs():
    """One entry per vendor, with every physical folder that holds its files.

    default_roots() returns several roots, and on a case-insensitive filesystem
    "data\\datasheets" and "Data\\datasheets" are the SAME directory reached by
    two different strings -- so every vendor was surveyed two or three times.
    Deduped on the resolved, case-folded path, then merged by vendor name so a
    vendor split across roots is still counted once.
    """
    byname, seen = {}, set()
    for root in dsmine.default_roots():
        r = Path(root)
        if not r.is_dir():
            continue
        try:
            key = str(r.resolve()).casefold()
        except OSError:
            key = str(r).casefold()
        if key in seen:
            continue
        seen.add(key)
        for d in sorted(r.iterdir()):
            if not d.is_dir():
                continue
            try:
                dk = str(d.resolve()).casefold()
            except OSError:
                dk = str(d).casefold()
            if dk in seen:
                continue
            seen.add(dk)
            byname.setdefault(d.name, []).append(d)
    return [(name, dirs) for name, dirs in sorted(byname.items())]


def files_in(dirs, limit=0):
    fs, seen = [], set()
    for d in dirs:
        for f in d.rglob("*"):
            if not (f.is_file() and f.suffix.lower() in EXTS):
                continue
            k = f.name.casefold()
            if k in seen:          # the same file reached through two roots
                continue
            seen.add(k)
            fs.append(f)
    if limit and len(fs) > limit:
        fs = random.sample(fs, limit)
    return sorted(fs)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--vendor", default=None)
    ap.add_argument("--sample", type=int, default=150,
                    help="datasheets per vendor (default 150; 0 = all)")
    ap.add_argument("--show", type=int, default=2,
                    help="example files per format")
    ap.add_argument("--min-share", type=float, default=1.0,
                    help="hide formats below this %% of the vendor")
    ap.add_argument("--style", action="store_true",
                    help="fold the table shading into the signature (it varies "
                         "page to page on a small sample, so it is reported "
                         "but not grouped on by default)")
    ap.add_argument("--csv", default=None)
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args(argv)

    random.seed(args.seed)
    _WITH_STYLE[0] = args.style
    dirs = vendor_dirs()
    if args.vendor:
        dirs = [(n, ds) for n, ds in dirs if args.vendor.lower() in n.lower()]
    if not dirs:
        print("No vendor folders found under the datasheet library.")
        return 1

    rows_out = []
    for name, dset in dirs:
        files = files_in(dset, args.sample)
        if not files:
            continue
        print(f"\n{'=' * 76}")
        print(f"  {name}   {len(files)} datasheet(s) sampled")
        print(f"{'=' * 76}")
        groups = defaultdict(list)
        for i, f in enumerate(files, 1):
            sig, _feats = archetype(f)
            groups[sig].append(f)
            if i % 200 == 0:
                print(f"    ...{i}/{len(files)}")
        total = sum(len(v) for v in groups.values())
        print(f"  {len(groups)} distinct layout(s)\n")
        print(f"  {'n':>5} {'share':>7}  layout")
        print("  " + "-" * 72)
        cumulative = 0
        for sig, hits in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            share = 100 * len(hits) / max(1, total)
            if share < args.min_share:
                continue
            cumulative += share
            print(f"  {len(hits):>5} {share:>6.1f}%  {sig}")
            if args.show:
                print(f"        e.g. " + ", ".join(p.name for p in
                                                   hits[:args.show]))
            rows_out.append({"vendor": name, "n": len(hits),
                             "share_pct": f"{share:.1f}", "layout": sig,
                             "examples": ", ".join(p.name
                                                   for p in hits[:3])})
        print(f"\n  the listed layouts cover {cumulative:.0f}% of the sample")
        top3 = sorted(groups.items(), key=lambda kv: -len(kv[1]))[:3]
        cov3 = 100 * sum(len(v) for _s, v in top3) / max(1, total)
        print(f"  the top 3 alone cover {cov3:.0f}%"
              + ("  <- worth hardcoding" if cov3 >= 70 else
                 "  <- too fragmented to hardcode cheaply"))

    if args.csv and rows_out:
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows_out[0]))
            w.writeheader()
            w.writerows(rows_out)
        print(f"\nwritten to {args.csv}")

    print("""
  Reading a layout string
    header     what the column headings look like: param/min/typ/max,
               temperature-columns, symbol-row, spec-names-as-columns
    label      where a spec's name sits relative to its numbers: col0,
               heading-above, in-row (centred in a merged cell), or mixed
    cond       what the condition column holds: a frequency band, free text,
               or nothing
    order      "Gain 22 dB" (value-first) vs "Gain dB 22" (unit-first)
    subports   the table subdivides a spec by port (S1, P1/P2, RF1)

  A vendor whose top three layouts cover most of its library is a good
  candidate for exact readers. One spread across a dozen is not.""")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nstopped.")
        sys.exit(130)
