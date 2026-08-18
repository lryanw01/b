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
_ALL_GROUPS = {}

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
    # A conditions column is mostly EMPTY -- ADI state a condition on some rows
    # and leave the rest blank, so "mostly prose" never fires and the column
    # read as "other". A few long non-numeric entries and no numbers is a
    # conditions/comments column, however sparse.
    longish = sum(1 for v in filled
                  if dsmine._cell_num(v) is None and len(v) >= 10)
    if longish >= 2 and num <= 0.2 * n:
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


_SYMBOLIC = re.compile(
    r"^(?:[A-Za-z]{1,6}\d{0,3}|[A-Za-z]{1,3}[-_]?[A-Za-z0-9]{1,4}|"
    r"P\s?1\s?dB|I?IP\s?[23]|V?I?[A-Z]{2,4})$")


def _looks_symbolic(vals):
    """Is this column of short symbols a parameter column?

    "f", "P1dB", "IP3", "IDD", "VDD" name specs as compactly as prose does; a
    column of them is the parameter column even though nothing in it is long
    enough to read as text.
    """
    flat = [x.strip() for v in vals for x in str(v or "").split("\n")
            if x.strip()]
    if len(flat) < 3:
        return False
    sym = sum(1 for v in flat
              if dsmine._cell_num(v) is None and _SYMBOLIC.match(v))
    return sym >= 0.6 * len(flat)


def _span_header_rows(rows, width):
    """Indices of rows that LABEL the columns beneath rather than hold data.

    Two shapes, both common:
      * a section band -- one text cell across an otherwise empty row
        ("BASEBAND AMPLIFIER", "Frequency Range")
      * a group header -- a cell spanning the min/typ/max columns, with the
        second tier ("Min Typ Max") on the row below

    Counting either as a data row makes the table look like it has a spec with
    no values, and hides the two-tier header that says which columns the
    numbers underneath belong to.
    """
    out = set()
    for i, r in enumerate(rows):
        filled = [c for c in r if str(c or "").strip()]
        if not filled:
            continue
        numeric = sum(1 for c in filled
                      if dsmine._cell_num(str(c)) is not None)
        if len(filled) <= max(1, width // 3) and numeric == 0 and width >= 3:
            out.add(i)
    return out


def classify_table(tbl, ruling="ruled"):
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
    # Drop columns that are blank everywhere. Text-aligned extraction leaves a
    # lot of them -- a header split across two columns, a gutter read as a
    # column -- and they carried no information while tripping the "mostly
    # blank" rejection, so ADI's newer tables were being thrown away as
    # non-spec tables when they are the most structured tables in the set.
    keep = [i for i in range(width)
            if any(str(r[i] or "").strip() for r in rows)]
    if len(keep) >= 2:
        rows = [[r[i] for i in keep] for r in rows]
        width = len(keep)
    rows = [r for r in rows if any(str(c or "").strip() for c in r)]
    if len(rows) < 3:
        return None

    span_rows = _span_header_rows(rows, width)
    data_rows = [r for i, r in enumerate(rows) if i not in span_rows]
    if len(data_rows) < 2:
        data_rows = rows
    cols = list(zip(*data_rows))
    kinds = [_col_kind(c) for c in cols]

    # The parameter column is the leftmost text column -- UNLESS its heading
    # says otherwise. ADRF6520 extracts with only two columns and the leftmost
    # is headed "Test Conditions/Comments": taking it as the parameter consumed
    # the conditions column and left the table reporting no conditions at all.
    hdr_txt = [str(c or "").strip() for c in rows[0]]
    cond_named = {i for i, h in enumerate(hdr_txt)
                  if re.search(r"test\s*cond|condition|comment|remark",
                               h, re.I)}
    text_cols = [i for i, k in enumerate(kinds) if k == "text"]
    pcol = next((i for i in text_cols if i not in cond_named),
                text_cols[0] if text_cols else 0)
    cond_cols = sorted(set(
        [i for i, k in enumerate(kinds)
         if k == "cond" or (k == "text" and i != pcol)]) | cond_named)
    val_cols = [i for i, k in enumerate(kinds) if k == "value"]
    # A spec table names its parameters. A grid of bare numbers is a plot's
    # axis labels or a pin-out; those were producing most of the "distinct
    # layouts" while being nothing anyone would write a reader for.
    # A parameter column need not be prose. ADI's newer style carries a SYMBOL
    # column -- f, P1dB, IP3, IDD -- and those tokens are too short to type as
    # text, so no parameter column was found and the whole table was discarded.
    # That is the difference between the two ADI families, not a reason to
    # throw one of them away.
    if "text" not in kinds:
        sym = [i for i, c in enumerate(cols) if _looks_symbolic(c)]
        if not sym:
            return None
        kinds[sym[0]] = "text"
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
    grouped = len(span_rows) >= 1 and len(rows) - len(span_rows) >= 2
    band_hdr = sum(1 for c in header if dsmine.parse_band_cell(c))
    if band_hdr >= 2 and not dsmine.parse_band_cell(header[pcol] or ""):
        shape = "band-columns"
    else:
        # A continuation row: no parameter, but a condition and a value.
        # Multi-row shows up three ways, all meaning the same thing: one spec,
        # several values keyed by condition.
        # A stacked VALUE cell is multi-row on its own. Requiring a separate
        # conditions column missed the common case where the condition is text
        # in an ordinary column:
        #     Slew Rate | max gain | 1100
        #               | min gain | 1500
        # Two values for one spec is multi-row whether or not the thing telling
        # them apart sits in a column this code recognised as "conditions".
        stacked = 0
        for r in data_rows[1:]:
            if _stacked(r[pcol]) > 1:
                continue        # a stacked PARAMETER cell is several specs
            if max([_stacked(r[i]) for i in val_cols] or [1]) > 1:
                stacked += 1
        conts = 0
        for r in data_rows[1:]:
            if r[pcol].strip():
                continue
            if any(r[i].strip() for i in val_cols):
                conts += 1
        # A blank parameter with a value beneath a named one is the same shape
        # even when it is only ONE extra row.
        if conts == 1 and any(r[pcol].strip() for r in data_rows[1:]):
            conts = 2
        from collections import Counter as _C
        names = _C(r[pcol].strip().lower() for r in data_rows[1:]
                   if r[pcol].strip())
        repeated = bool(names) and max(names.values()) >= 2
        shape = ("multi-row" if (stacked >= 1 or conts >= 2 or repeated)
                 else "single-row")

    # min/typ/max is ONE value set, not several: a spec quoted min/typ/max has a
    # single answer expressed three ways. What matters is how many SETS a spec
    # carries -- one, several keyed by a condition, or several spread across
    # separate tables. Grouping on the column structure instead put a table with
    # frequency-keyed values in with one that merely has a conditions column.
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
    values = "multi-value" if shape == "multi-row" else "single-value"
    # What distinguishes the sets, when there is more than one. A frequency
    # range and a test condition are different enough to need different
    # handling: one is a band the spec is valid over, the other a setting it
    # was measured at.
    band_like = any(k == "cond" for k in kinds) or any(
        dsmine.parse_band_cell(c) for r in data_rows for c in r)
    if values == "single-value":
        conds = "conditions-present" if cond_cols else "no-conditions"
    elif band_like:
        conds = "keyed-by-frequency"
    elif cond_cols:
        conds = "keyed-by-condition"
    else:
        conds = "keyed-in-cell"
    if grouped:
        conds += " | grouped-header"
    # Ruled vs banded is the first thing a reader must know: it decides which
    # extraction strategy recovers the cells at all.
    symbolic = any(_looks_symbolic(c) for c in cols)
    if symbolic:
        conds += " | symbol-col"
    return (f"{ruling:<7} | {values:<12} | {conds}",
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
                    out.append((cur, "html"))
                cur, w = [list(cells)], len(cells)
        if len(cur) >= 2:
            out.append((cur, "html"))
        return out
    # Two extraction strategies, because ADI publish two kinds of table and the
    # difference is whether the table is RULED:
    #
    #   ruled   -- cell borders drawn as lines. pdfplumber's default,
    #              line-based strategy finds these.
    #   banded  -- no borders at all, structure conveyed by alternating fill
    #              colour. The line strategy returns NOTHING for these, which
    #              is why an entire family of ADI datasheets produced no tables
    #              and no layout -- they were invisible, not unclassifiable.
    #
    # Columns in a banded table have to be inferred from text alignment.
    TEXT_SETTINGS = {"vertical_strategy": "text",
                     "horizontal_strategy": "text",
                     "intersection_tolerance": 5,
                     "text_x_tolerance": 2}
    try:
        import pdfplumber
        with pdfplumber.open(str(path)) as pdf:
            for page in pdf.pages[:pages]:
                found = [tb for tb in (page.extract_tables() or [])
                         if tb and len(tb) >= 2]
                if found:
                    for tb in found:
                        out.append((tb, "ruled"))
                    continue
                try:
                    alt = page.extract_tables(TEXT_SETTINGS) or []
                except Exception:
                    alt = []
                for tb in alt:
                    if tb and len(tb) >= 3:
                        out.append((tb, "banded"))
    except Exception:
        pass
    return out


RF_CATEGORIES = ["amplifier", "mixer", "filter", "attenuator", "divider",
                 "coupler", "switch", "phase_shifter", "limiter", "multiplier",
                 "oscillator", "transformer", "termination", "equalizer",
                 "detector", "adapter", "cable", "dc_block", "bias_tee"]


def category_index():
    """{loose part number: category} from the database."""
    out = {}
    partdb = _opt("partdb")
    if partdb is None:
        return out
    try:
        for r in partdb.db().execute(
                "SELECT mpn, category FROM parts WHERE category IS NOT NULL "
                "AND category != ''"):
            out.setdefault(re.sub(r"[^A-Z0-9]", "", r["mpn"].upper()),
                           r["category"])
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
    ap.add_argument("--categories", nargs="*", default=None,
                    help="categories to survey (default: the RF component "
                         "types; transistors, eval boards and the like are "
                         "left out)")
    ap.add_argument("--any-category", action="store_true",
                    help="do not filter by category at all")
    ap.add_argument("--dump", type=int, default=0,
                    help="print this many raw table grids per layout, so the "
                         "classification can be checked against the table")
    ap.add_argument("--style", action="store_true",
                    help="fold the table shading into the signature (it varies "
                         "page to page on a small sample, so it is reported "
                         "but not grouped on by default)")
    ap.add_argument("--zip", default=None,
                    help="bundle a few datasheets per layout into this .zip, "
                         "so the classification can be checked against the "
                         "real files")
    ap.add_argument("--per-format", type=int, default=3,
                    help="datasheets to bundle per layout (default 3)")
    ap.add_argument("--max-zip-mb", type=float, default=20.0)
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
    index = category_index() if not args.any_category else {}
    allowed = {c.lower() for c in (args.categories or RF_CATEGORIES)}
    for name, dset in dirs:
        files = files_in(dset, 0)
        if index:
            # A transistor or an evaluation board tells us nothing about how RF
            # spec tables are laid out, and there are enough of them to skew the
            # shares. Parts outside the working categories are left out.
            kept = [f for f in files
                    if index.get(re.sub(r"[^A-Z0-9]", "", f.stem.upper()), "")
                    .lower() in allowed]
            dropped = len(files) - len(kept)
            files = kept
        else:
            dropped = 0
        if args.sample and len(files) > args.sample:
            files = random.sample(files, args.sample)
            files.sort()
        if not files:
            continue
        print(f"\n{'=' * 76}")
        print(f"  {name}   {len(files)} datasheet(s) sampled"
              + (f", {dropped} outside the working categories" if dropped
                 else ""))
        print(f"{'=' * 76}")
        groups = defaultdict(list)
        n_tables = skipped = 0
        for i, f in enumerate(files, 1):
            for tb, ruling in tables_in(f):
                res = classify_table(tb, ruling)
                if res is None:
                    skipped += 1
                    continue
                sig, _facts = res
                groups[sig].append(f)
                _EXAMPLES.setdefault(sig, []).append((f.name, tb))
                _ALL_GROUPS.setdefault((name, sig), []).append(f)
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

    if args.zip and _ALL_GROUPS:
        import zipfile, io as _io
        out = Path(args.zip)
        budget = args.max_zip_mb * 1024 * 1024
        used, added, left_out = 0, 0, 0
        manifest = _io.StringIO()
        w = csv.writer(manifest)
        w.writerow(["vendor", "layout", "file"])
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            for (vend, sig), paths in _ALL_GROUPS.items():
                # Distinct FILES: the same datasheet often contributes several
                # tables of one layout, and three copies of it teaches nothing.
                uniq, seen = [], set()
                for pth in paths:
                    if pth.name in seen:
                        continue
                    seen.add(pth.name)
                    uniq.append(pth)
                    if len(uniq) >= args.per_format:
                        break
                safe = re.sub(r"[^A-Za-z0-9]+", "_", sig).strip("_")[:60]
                for pth in uniq:
                    try:
                        size = pth.stat().st_size
                    except OSError:
                        continue
                    w.writerow([vend, sig, pth.name])
                    if used + size > budget:
                        left_out += 1
                        continue
                    z.write(pth, arcname=f"{vend}/{safe}/{pth.name}")
                    used += size
                    added += 1
            z.writestr("MANIFEST.csv", manifest.getvalue())
            z.writestr("README.txt",
                       "Datasheets grouped by the table layout dbg_formats.py "
                       "assigned them.\n\nEach folder is one layout. Check "
                       "whether the files inside really do\nshare a table "
                       "structure -- if two obviously different tables sit in "
                       "one\nfolder, the signature is still too coarse; if one "
                       "layout is split across\nfolders, it is too fine.\n\n"
                       "MANIFEST.csv lists every file, including any left out "
                       "for size.\n")
        print(f"\n  wrote {out}  ({used / 1e6:.1f} MB, {added} file(s)"
              + (f", {left_out} left out for size" if left_out else "") + ")")

    if args.csv and rows_out:
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows_out[0]))
            w.writeheader()
            w.writerows(rows_out)
        print(f"\nwritten to {args.csv}")

    print("""
  Reading a layout string
    single-value        one value (or one min/typ/max set) per spec
    multi-value         several values per spec, and what keys them:
                          keyed-by-frequency   a band the spec is valid over
                          keyed-by-condition   a stated test condition
                          keyed-in-cell        the discriminator is inside a
                                               cell, not a column of its own
    conditions-present  a conditions column exists but each spec still has one
                        value -- worth reading, but it does not change the shape
    grouped-header      a heading spans several columns and labels the numbers
                        beneath it

  min/typ/max is ONE value set, not several: a spec quoted three ways still has
  a single answer. What decides the reader is how many SETS a spec carries.

  --zip bundles a few datasheets per layout so the grouping can be checked
  against the real files.
""")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nstopped.")
        sys.exit(130)
