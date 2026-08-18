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


def _indent_levels(path, pages=2, tol=6.0):
    """Distinct left-edge x positions of first cells, as indentation levels.

    Indentation is the only reliable sign of hierarchy: ADI indent "Low-Side
    IP3" under "Input Third-Order Intercept" with no other marker. Column
    ORDER and header WORDING vary constantly within one house style and say
    nothing about how to read the table, so neither belongs in the archetype.
    """
    # Only rows whose first cell NAMES A SPEC. Measuring every row measured the
    # page -- headers, footers, side captions -- and reported three or four
    # levels for a perfectly flat table, so almost everything looked
    # hierarchical.
    xs = []
    for pno in range(1, pages + 1):
        try:
            rows = dsmine.cells_xy(path, pno)
        except Exception:
            continue
        for r in rows:
            cells = r.get("cells") or []
            if not cells:
                continue
            first = str(cells[0].get("t") or "").strip()
            if first and _SPECWORD.search(first):
                xs.append(round(float(cells[0]["x0"]), 1))
    if len(xs) < 4:
        return 1
    xs.sort()
    tiers, cur, counts = [xs[0]], xs[0], [1]
    for x in xs[1:]:
        if x - cur > tol:
            tiers.append(x)
            counts.append(1)
            cur = x
        else:
            counts[-1] += 1
    # A tier with only one or two rows is noise, not an indent level.
    real = [c for c in counts if c >= 3]
    return min(max(1, len(real)), 4)


def archetype(path, max_rows=400):
    """A COARSE table archetype: how specs sit relative to their conditions.

    Deliberately blind to header wording, column order and which specs appear.
    Those differ constantly inside one house style and are handled case by case
    in a reader; what decides WHICH reader is the geometry:

        matrix        spec names run across the header, rows are variants
        banded        one spec spans several rows, one per condition/band
        hierarchical  sub-rows indented beneath a parent label
        flat          one row per spec, values in columns
        keyvalue      label/value pairs with no column grid
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
    rows = rows[:max_rows]

    spec_rows = band_rows = wide_rows = 0
    multi_band_blocks = 0
    run = 0
    matrix_hdr = False
    for _p, cells in rows:
        toks = [str(c or "").strip() for c in cells]
        if not toks:
            continue
        if len(toks) >= 3:
            wide_rows += 1
        if sum(1 for x in toks if _SPECWORD.search(x)) >= 3:
            matrix_hdr = True
        has_band = any(dsmine.parse_band_cell(x) for x in toks[:3])
        if has_band:
            band_rows += 1
            run += 1
        else:
            if run >= 2:
                multi_band_blocks += 1
            run = 0
            if _SPECWORD.search(toks[0] or ""):
                spec_rows += 1
    if run >= 2:
        multi_band_blocks += 1

    levels = _indent_levels(path)

    if matrix_hdr and band_rows <= 2:
        kind = "matrix"
    elif multi_band_blocks >= 2:
        kind = "banded"
    elif levels >= 2 and spec_rows >= 6:
        kind = "hierarchical"
    elif wide_rows >= 4:
        kind = "flat"
    else:
        kind = "keyvalue"

    # Where the test conditions live -- the other thing a reader must know.
    cond = ("in-rows" if multi_band_blocks >= 2 or band_rows >= 3
            else "in-column" if _has_cond_column(rows)
            else "in-label" if _cond_in_labels(rows)
            else "none")

    return f"{kind}  (conditions {cond})", {
        "kind": kind, "cond": cond, "indent_levels": levels,
        "band_rows": band_rows, "blocks": multi_band_blocks,
        "spec_rows": spec_rows}


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
    ap.add_argument("--csv", default=None)
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args(argv)

    random.seed(args.seed)
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
