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
_EXAMPLES = {}

# ---------------------------------------------------------------- per table
# The unit of analysis is a TABLE, not a datasheet. A single datasheet routinely
# carries an electrical table, an absolute-maximum table and an ordering table,
# and they have nothing structurally in common -- classifying the file as a
# whole averaged three different layouts into one meaningless answer.
_UNIT_TOK = re.compile(
    r"^\(?\s*(dB|dBm|dBc|GHz|MHz|kHz|Hz|ns|ps|us|ms|mA|uA|A|mW|W|kW|mV|V|"
    r"Ohms?|\u03a9|deg|degrees|\u00b0|%|:1|dBc/Hz|\u00b0C)\s*\)?$", re.I)
_MINMAXTYP = re.compile(r"^(min|typ|max|nom)(imum|ical)?\.?$", re.I)


def _col_kind(vals):
    """What a column holds: param, cond, unit, value or blank.

    Stacked cells are split first. "0.34\n0.61\n0.78" is three values, but as
    one string it is not numeric and not a unit, so the column read as prose --
    and with no value column at all the whole table was discarded. Every real
    multi-band electrical table was being thrown away on exactly this.
    """
    flat = []
    for v in vals:
        for part in str(v or "").split("\n"):
            part = part.strip()
            if part:
                flat.append(part)
    vals = flat or [str(v or "").strip() for v in vals]
    filled = [v for v in vals if v]
    if not filled:
        return "blank"
    unit = sum(1 for v in filled if _UNIT_TOK.match(v))
    num = sum(1 for v in filled if dsmine._cell_num(v) is not None)
    band = sum(1 for v in filled if dsmine.parse_band_cell(v))
    prose = sum(1 for v in filled if dsmine._cell_num(v) is None and len(v) > 3)
    n = len(filled)
    if unit >= 0.5 * n:
        return "unit"
    if band >= 0.4 * n:
        return "cond"          # a frequency-range column IS the condition
    if num >= 0.6 * n:
        return "value"
    if prose >= 0.5 * n:
        return "text"
    return "other"


def _stacked(cell):
    """How many values are stacked inside one cell.

    pdfplumber merges a vertically-spanned group into a SINGLE cell whose text
    is newline-separated:

        Insertion Loss | "DC - 2\n2 - 6\n6 - 10" | "0.34\n0.61\n0.78" | dB

    So a multi-row spec never appears as several rows at all -- it is one row
    with stacked cells. Looking for empty continuation rows could never have
    found it, which is why every table classified as single-row.
    """
    s = str(cell or "")
    parts = [x.strip() for x in s.split("\n") if x.strip()]
    return len(parts)


def classify_table(tbl):
    """(signature, facts) for ONE table.

    multi-row means: the same parameter has SEVERAL value rows, distinguished
    by a frequency range or a test condition. That is the property worth
    grouping on -- those rows are one answer in pieces, and being able to
    collect them is the whole point.
    """
    rows = [[("" if c is None else str(c).strip()) for c in r] for r in tbl
            if r and any(c for c in r)]
    if len(rows) < 2:
        return None
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    cols = list(zip(*rows))
    kinds = [_col_kind(c) for c in cols]

    # The parameter column is the leftmost text column.
    try:
        pcol = next(i for i, k in enumerate(kinds) if k == "text")
    except StopIteration:
        pcol = 0
    cond_cols = [i for i, k in enumerate(kinds)
                 if k == "cond" or (k == "text" and i != pcol)]
    val_cols = [i for i, k in enumerate(kinds) if k == "value"]
    # A spec table names its parameters. A grid of bare numbers is a plot's
    # axis labels or a pin-out; those were producing most of the "distinct
    # layouts" while being nothing anyone would write a reader for.
    if "text" not in kinds:
        return None
    if sum(1 for k in kinds if k == "blank") > len(kinds) / 2:
        return None
    if len(rows) < 3:
        return None
    if not val_cols:
        # A table of pure prose (ordering info, pin descriptions) is not a spec
        # table. Anything with numbers is, even if the columns are ragged --
        # requiring a clean text column skipped most real electrical tables.
        return None

    # Bands as COLUMN HEADINGS: the header row holds two or more ranges.
    header = rows[0]
    band_hdr = sum(1 for c in header if dsmine.parse_band_cell(c))
    if band_hdr >= 2 and not dsmine.parse_band_cell(header[pcol] or ""):
        shape = "band-columns"
    else:
        # A continuation row: no parameter, but a condition and a value.
        # Multi-row shows up three ways, all meaning the same thing: one spec,
        # several values keyed by condition.
        stacked = 0
        for r in rows[1:]:
            if _stacked(r[pcol]) > 1:
                continue        # a stacked PARAMETER cell is several specs
            depth = max([_stacked(r[i]) for i in val_cols] or [1])
            cdepth = max([_stacked(r[i]) for i in cond_cols] or [1])
            if depth > 1 and (cdepth > 1 or cond_cols):
                stacked += 1
        conts = 0
        for r in rows[1:]:
            if r[pcol].strip():
                continue
            if any(r[i].strip() for i in val_cols):
                conts += 1
        from collections import Counter as _C
        names = _C(r[pcol].strip().lower() for r in rows[1:] if r[pcol].strip())
        repeated = bool(names) and max(names.values()) >= 2
        shape = ("multi-row" if (stacked >= 1 or conts >= 2 or repeated)
                 else "single-row")

    # min/typ/max triple vs a single value column
    hdr_mtm = sum(1 for c in header if _MINMAXTYP.match(c))
    grid = ("min/typ/max" if hdr_mtm >= 2
            else f"{len(val_cols)}-value" if len(val_cols) <= 2
            else f"{len(val_cols)}-cols")

    # The ROLE ORDER is the stable key: names and wording vary constantly
    # inside one house style, the sequence of column roles does not.
    roles = "".join({"text": "P", "cond": "C", "unit": "U", "value": "V",
                     "blank": ".", "other": "?"}[k] for k in kinds)
    # The SIGNATURE is deliberately coarse. Column count, column order and the
    # exact role sequence vary table to table inside one house style -- keying
    # on them produced 32 "formats" from 52 tables, which is a list of tables,
    # not a list of formats. Only two things change how a row is read: whether
    # a spec carries several condition-keyed values, and whether the values are
    # a min/typ/max triple or a single column. The rest is reported as detail.
    values = "min/typ/max" if grid == "min/typ/max" else "single-value"
    conds = "with-conditions" if cond_cols else "no-conditions"
    return (f"{shape:<11} | {values:<12} | {conds}",
            {"shape": shape, "grid": grid, "roles": roles,
             "rows": len(rows), "width": width, "cond_cols": len(cond_cols)})


def tables_in(path, pages=4):
    """Every table on the first few pages, as row lists."""
    out = []
    if path.suffix.lower() != ".pdf":
        try:
            rows = dsmine.rows_for(path)
        except Exception:
            return out
        # HTML rows arrive already split; treat a run of equal width as a table.
        cur, w = [], None
        for _p, cells in rows:
            if not cells:
                continue
            if w is None or len(cells) == w:
                cur.append(list(cells)); w = len(cells)
            else:
                if len(cur) >= 2:
                    out.append(cur)
                cur, w = [list(cells)], len(cells)
        if len(cur) >= 2:
            out.append(cur)
        return out
    try:
        import pdfplumber
        with pdfplumber.open(str(path)) as pdf:
            for page in pdf.pages[:pages]:
                for tb in page.extract_tables() or []:
                    if tb and len(tb) >= 2:
                        out.append(tb)
    except Exception:
        pass
    return out


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
    ap.add_argument("--dump", type=int, default=0,
                    help="print this many raw table grids per layout, so the "
                         "classification can be checked against the table")
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
        n_tables = skipped = 0
        for i, f in enumerate(files, 1):
            for tb in tables_in(f):
                res = classify_table(tb)
                if res is None:
                    skipped += 1
                    continue
                sig, _facts = res
                groups[sig].append(f)
                _EXAMPLES.setdefault(sig, []).append((f.name, tb))
                n_tables += 1
            if i % 100 == 0:
                print(f"    ...{i}/{len(files)} files, {n_tables} tables")
        total = max(1, n_tables)
        print(f"  {n_tables} table(s), {len(groups)} distinct layout(s)"
              + (f", {skipped} not spec tables" if skipped else "") + "\n")
        print(f"  {'n':>5} {'share':>7}  layout")
        print("  " + "-" * 72)
        cumulative = 0
        for sig, hits in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            share = 100 * len(hits) / total
            if share < args.min_share:
                continue
            cumulative += share
            print(f"  {len(hits):>5} {share:>6.1f}%  {sig}")
            if args.dump:
                for tb in _EXAMPLES.get(sig, [])[:args.dump]:
                    print(f"        ---- {tb[0]} ----")
                    for row in tb[1][:8]:
                        cells = ["" if c is None else str(c).strip()[:16]
                                 for c in row]
                        print("        | " + " | ".join(cells)[:96])
            if args.show:
                seen, ex = set(), []
                for pth in hits:
                    if pth.name not in seen:
                        seen.add(pth.name); ex.append(pth.name)
                    if len(ex) >= args.show:
                        break
                print(f"        e.g. " + ", ".join(ex))
            rows_out.append({"vendor": name, "n": len(hits),
                             "share_pct": f"{share:.1f}", "layout": sig,
                             "examples": ", ".join(
                                 sorted({p.name for p in hits})[:3])})
        print(f"\n  the listed layouts cover {cumulative:.0f}% of the sample")
        top3 = sorted(groups.items(), key=lambda kv: -len(kv[1]))[:3]
        cov3 = 100 * sum(len(v) for _s, v in top3) / total
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
