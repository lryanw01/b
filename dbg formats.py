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


def rows_per_spec(rows, max_rows=400):
    """(mode, mean rows per spec) -- the thing that decides how to read a row.

    A table with one row per spec is read straight; a table where a spec owns
    four rows is piecewise and every one of those rows is part of the same
    answer. That difference changes the reader; where the condition column sits
    does not.
    """
    rows = rows[:max_rows]
    owners, cur, counts = [], None, []
    for _p, cells in rows:
        toks = [str(c or "").strip() for c in (cells or [])]
        if not toks:
            continue
        named = bool(_SPECWORD.search(toks[0] or ""))
        has_band = any(dsmine.parse_band_cell(x) for x in toks[:3])
        numeric = sum(1 for x in toks if dsmine._cell_num(x) is not None)
        if named:
            if cur is not None:
                counts.append(cur)
            cur = 1
        elif cur is not None and (has_band or numeric >= 2):
            cur += 1                    # a continuation row of the same spec
        elif cur is not None:
            counts.append(cur)
            cur = None
    if cur is not None:
        counts.append(cur)
    if not counts:
        return "unknown", 0.0
    mean = sum(counts) / len(counts)
    multi = sum(1 for c in counts if c >= 2)
    share = multi / len(counts)
    if share >= 0.4:
        return "multi-row", mean
    if share >= 0.12:
        return "mixed", mean
    return "single-row", mean


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
    """A COARSE archetype: how many rows a spec occupies, and the table style.

    Blind to header wording, column order and where the conditions sit -- those
    are case-by-case details inside a reader. What selects the reader is whether
    a spec is one row or several, whether it continues into another table, and
    which house table style it is drawn in.
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

    shape, mean = rows_per_spec(rows, max_rows)
    style, ncol = table_fills(path) if path.suffix.lower() == ".pdf" \
        else ("html", 0)
    across = repeats_across_tables(rows, max_rows)
    conds = has_conditions(rows)

    # Shading is reported but NOT part of the signature by default: measured
    # across these files it varies page to page inside one vendor, so it split
    # groups instead of clustering them. --style folds it in if it turns out to
    # be stable on a larger sample.
    band = ("1" if mean < 1.3 else "2-3" if mean < 3.5 else "4+")
    sig = f"{shape:<10} rows/spec~{band}"
    if across:
        sig += " | repeats-across-tables"
    if not conds:
        sig += " | no-condition-column"
    if _WITH_STYLE[0]:
        sig += f" | style:{style}"
    return sig, {"shape": shape, "mean_rows_per_spec": round(mean, 2),
                 "style": style, "fills": ncol, "across": across,
                 "conditions": conds}


def has_conditions(rows):
    """Is there a conditions column at all, by any of its names?

    "Comments", "Test Conditions", "Conditions", "Notes" and "Test Level" all
    mean the same column. Matching only the exact phrase reported "no
    conditions" for tables that plainly had one.
    """
    pat = re.compile(r"test\s*cond|condition|comment|remark|note|test\s*level",
                     re.I)
    for _p, cells in rows[:80]:
        for c in (cells or [])[:6]:
            if pat.search(str(c or "")):
                return True
    return False


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
