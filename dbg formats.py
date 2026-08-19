"""dbg_formats.py — survey spec-table formats using a custom table parser.

    python dbg_formats.py --sample 300
    python dbg_formats.py --vendor Analog-Devices --zip formats.zip
    python dbg_formats.py FILE.pdf --show-table

The table parser lives in THIS FILE rather than calling dsmine, so it can be
proven here and then moved into dsmine once it is right.

WHY A CUSTOM PARSER
    pdfplumber's extract_tables() infers a table from ruling LINES. ADI rule
    some edges and not others, so it merges or drops columns -- ADRF5040's
    seven-column table came back as two, with Parameter and Symbol gone and
    Min/Typ/Max fused into one cell. No settings fix that: the lines it needs
    are not on the page.

    This locates the HEADER ROW, takes the column boundaries from where its
    headings sit, and assigns every word below by x-overlap. The header is
    drawn on every one of these tables and its positions are exact, so the
    columns come from what the vendor laid out rather than from what happens to
    be ruled. pdfplumber is used only as a source of positioned words; none of
    its table logic is involved.
"""
from __future__ import annotations

import argparse
import csv
import io
import random
import re
import sys
import zipfile
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
partdb = _opt("partdb")

_TRAITS = {}
_LOWCONF = {}

RF_CATEGORIES = ["amplifier", "mixer", "filter", "attenuator", "divider",
                 "coupler", "switch", "phase_shifter", "limiter", "multiplier",
                 "oscillator", "transformer", "termination", "equalizer",
                 "detector", "adapter", "cable", "dc_block", "bias_tee"]

# ==========================================================  table parser
_HDR_WORDS = re.compile(
    r"^(parameter|symbol|min|min\.|minimum|typ|typ\.|typical|max|max\.|maximum|"
    r"unit|units|conditions?|comments?|test|frequency|freq\.?)$", re.I)
_VALUE_HDR = re.compile(r"^(min|typ|max)\.?$", re.I)
# "+-1" is a tolerance and a perfectly good value; rejecting it made every
# gain-flatness and balance row look like text in a value column.
_NUM = re.compile(r"^[-+\u2212\u00b1<>~]?\s*\d+(?:\.\d+)?$")
# A table ends. Without a stop rule the parser kept appending every line to the
# last table until the next header -- footnotes, the copyright block and the
# page footer included -- and their words landed in whichever column they
# happened to sit under. That, not vendor overflow, was most of what
# "text-in-value-cols" was reporting.
_END_OF_TABLE = re.compile(
    r"^\s*(?:\[\d+\]|\d+\s*Unless|Rev\.|Page\s+\d|Information\s+furnished|"
    r"One\s+Technology|P\.O\.\s*Box|Fax:|Phone:|www\.|\u00a9|Trademarks|"
    r"Specifications\s+subject|All\s+rights)", re.I)


def _looks_like_footer(cells):
    joined = " ".join(c for c in cells if c.strip())
    if _END_OF_TABLE.match(joined):
        return True
    words = joined.split()
    if not words:
        return False
    # Footnote prose starts lower-case and carries no numbers. A section
    # heading also has no numbers, but it is capitalised -- that is the whole
    # difference, and it is reliable across every vendor seen so far.
    numeric = any(_num(w) is not None for w in words)
    if not numeric and len(words) >= 3 and words[0][:1].islower():
        return True
    if len(words) >= 10 and not numeric:
        return True
    return False


_BAND_IN_TEXT = re.compile(
    r"(?:DC|\d+(?:\.\d+)?)\s*(?:GHz|MHz|kHz)?\s*(?:-|\u2013|to)\s*"
    r"\d+(?:\.\d+)?\s*(?:GHz|MHz|kHz)", re.I)
_BAND = re.compile(
    r"^(DC|\d+(?:\.\d+)?)\s*(GHz|MHz|kHz)?\s*(?:-|\u2013|to)\s*"
    r"(\d+(?:\.\d+)?)\s*(GHz|MHz|kHz)?$", re.I)


def _num(s):
    s = str(s or "").strip().replace("\u2212", "-").replace(",", "")
    if not _NUM.match(s):
        return None
    return float(re.sub(r"^[\u00b1<>~+]\s*", "", s) or 0)


def page_words(page):
    try:
        return page.extract_words(x_tolerance=1.5, y_tolerance=2,
                                  keep_blank_chars=False) or []
    except Exception:
        return []


def group_lines(words, ytol=2.5):
    """Words clustered into visual lines by vertical position."""
    lines, cur, top = [], [], None
    for w in sorted(words, key=lambda w: (round(float(w["top"]), 1),
                                          float(w["x0"]))):
        y = float(w["top"])
        if top is None:
            cur, top = [w], y
        elif abs(y - top) <= ytol:
            cur.append(w)
        else:
            lines.append(cur)
            cur, top = [w], y
    if cur:
        lines.append(cur)
    return lines


def header_columns(line, gap=6.0):
    """Column boundaries taken from a header line, or None.

    Adjacent headings are merged when they nearly touch ("Test" +
    "Conditions/Comments"), then each owns the span from midway to its left
    neighbour to midway to its right.
    """
    hits = sum(1 for w in line if _HDR_WORDS.match(w["text"].strip(" :.")))
    if hits < 2 or not any(_VALUE_HDR.match(w["text"].strip(" .:"))
                           for w in line):
        return None
    heads, cur = [], None
    for w in sorted(line, key=lambda w: float(w["x0"])):
        if cur and float(w["x0"]) - cur["x1"] <= gap:
            cur["text"] += " " + w["text"]
            cur["x1"] = float(w["x1"])
        else:
            if cur:
                heads.append(cur)
            cur = {"text": w["text"], "x0": float(w["x0"]),
                   "x1": float(w["x1"])}
    if cur:
        heads.append(cur)
    if len(heads) < 3:
        return None
    out = []
    for i, h in enumerate(heads):
        left = -1e6 if i == 0 else (heads[i - 1]["x1"] + h["x0"]) / 2
        right = 1e6 if i == len(heads) - 1 else (h["x1"] + heads[i + 1]["x0"]) / 2
        out.append((left, right, h["text"]))
    return out


def assign(line, bounds):
    """One grid row: each word placed in the column its centre falls in."""
    cells = [""] * len(bounds)
    for w in sorted(line, key=lambda w: float(w["x0"])):
        mid = (float(w["x0"]) + float(w["x1"])) / 2
        for i, (lo, hi, _t) in enumerate(bounds):
            if lo <= mid < hi:
                cells[i] = (cells[i] + " " + w["text"]).strip()
                break
    return cells


def parse_tables(path, pages=8):
    """[(headings, rows)] for every header-anchored table in the file."""
    try:
        import pdfplumber
    except ImportError:
        return []
    out = []
    try:
        with pdfplumber.open(str(path)) as pdf:
            for page in pdf.pages[:pages]:
                bounds, rows = None, []
                for line in group_lines(page_words(page)):
                    b = header_columns(line)
                    if b:
                        if bounds and len(rows) >= 2:
                            out.append(([t for _l, _r, t in bounds], rows))
                        bounds, rows = b, []
                        continue
                    if not bounds:
                        continue
                    cells = assign(line, bounds)
                    if not any(c.strip() for c in cells):
                        continue
                    if _looks_like_footer(cells):
                        # Everything after the footnote block belongs to the
                        # page, not the table.
                        if len(rows) >= 2:
                            out.append(([t2 for _l, _r, t2 in bounds], rows))
                        bounds, rows = None, []
                        continue
                    rows.append(cells)
                if bounds and len(rows) >= 2:
                    out.append(([t for _l, _r, t in bounds], rows))
    except Exception:
        pass
    return out


def col_roles(headings):
    """{role: column index}, read from the heading text.

    Reliable now that the headings are real text taken from the page, rather
    than whatever a line-based extractor happened to leave behind.
    """
    roles = {}
    for i, h in enumerate(headings):
        s = h.strip().lower()
        if re.search(r"parameter|description|item", s):
            roles.setdefault("param", i)
        elif re.search(r"symbol", s):
            roles.setdefault("symbol", i)
        elif re.search(r"cond|comment|remark|note", s):
            roles.setdefault("cond", i)
        elif re.fullmatch(r"min\.?|minimum", s):
            roles.setdefault("min", i)
        elif re.fullmatch(r"typ\.?|typical", s):
            roles.setdefault("typ", i)
        elif re.fullmatch(r"max\.?|maximum", s):
            roles.setdefault("max", i)
        elif re.search(r"unit", s):
            roles.setdefault("unit", i)
        elif re.search(r"freq", s):
            # A FREQUENCY column is not a conditions column. Folding them into
            # one role made every yellow table report a conditions column it
            # does not have -- the yellow style has a Frequency column and no
            # Test Conditions/Comments at all, and that absence is part of what
            # identifies it.
            roles.setdefault("freq", i)
    if "param" not in roles:
        roles["param"] = roles.get("symbol", 0)
    return roles


def group_records(headings, rows):
    """[{name, lines, values}] -- one record per spec, continuations folded in.

    Two shapes have to be right or the grouping is wrong:

      * a SECTION heading ("INSERTION LOSS") names the specs beneath it and
        carries no values of its own;
      * a CENTRED label sits on the middle line of its own block, so lines
        ABOVE it are its own continuations, not orphans of the record before.
    """
    roles = col_roles(headings)
    pcol = roles["param"]
    vcols = [roles[k] for k in ("min", "typ", "max") if k in roles]
    if not vcols:
        skip = {pcol, roles.get("cond"), roles.get("freq"), roles.get("unit")}
        vcols = [i for i in range(len(headings)) if i not in skip]

    def has_values(r):
        return any(_num(r[i]) is not None for i in vcols if i < len(r))

    # A BAND ROW states a frequency range and no values, and every spec below
    # it is measured over that range until the next one:
    #
    #     Frequency Range: 2 - 4 GHz
    #     Gain            | 10 | 12 | 14 | dB
    #     Noise Figure    |    | 3.5|    | dB
    #     Frequency Range: 4 - 8 GHz
    #     Gain            |  9 | 11 | 13 | dB
    #
    # So Gain has two value sets keyed by frequency, with no frequency COLUMN
    # anywhere. Treated as an ordinary label the band row became a spec with no
    # values and was discarded, taking the grouping with it -- the two Gains
    # then looked like one spec measured twice for no stated reason.
    recs, pending, band = [], [], ""
    for r in rows:
        name = r[pcol].strip() if pcol < len(r) else ""
        joined = " ".join(c for c in r if c.strip())
        no_values = not has_values(r)
        bm = _BAND_IN_TEXT.search(joined)
        if bm and no_values:
            band = bm.group(0).strip()
            continue
        if name:
            recs.append({"name": name, "lines": pending + [r], "band": band})
            pending = []
        elif recs:
            recs[-1]["lines"].append(r)
        else:
            pending.append(r)
    if pending:
        recs.append({"name": "", "lines": pending, "band": band})

    ucol = roles.get("unit")
    for rec in recs:
        rec["values"] = [ln for ln in rec["lines"] if has_values(ln)]
        # The unit is stated PER ROW -- a supply current in uA two rows above
        # one in mA. Assuming one unit per spec key from the registry is a
        # silent 1000x error of exactly the kind the MHz/GHz bugs were.
        rec["units"] = [ln[ucol].strip() if ucol is not None and ucol < len(ln)
                        else "" for ln in rec["values"]]
    # A lone line with no numbers is a section band, not a spec.
    return [r for r in recs if r["values"] or len(r["lines"]) > 1]


# ==========================================================  classification
# Formats are named for what they ARE, not for whose datasheet they came from
# or what colour it was printed in. "ADI-bluegrey" told you nothing about how
# to read the table and stopped meaning anything the moment a second vendor
# used the same layout; "cond-col+sym | min-typ-max" says exactly what a reader
# has to handle, and a Qorvo table with that structure is the same format.
#
# The name is built from the three facts that decide how a row is read:
#   1. what carries the condition -- a conditions column, a frequency column,
#      band rows, or nothing
#   2. whether there is a separate symbol column
#   3. what the value grid is -- min/typ/max, min/max, or a single column
#
# Punctuation ("Min" vs "Min.", "Unit" vs "Units") is still recorded, because
# it identifies the house style reliably and is worth knowing -- but as a
# reported dialect, not as the format's identity.
DIALECTS = {
    "terse": {"value_hdr": r"^(min|typ|max)$", "unit_hdr": r"^unit$"},
    "dotted": {"value_hdr": r"^(min|typ|max)\.$", "unit_hdr": r"^units$"},
}


def dialect_of(headings):
    """Header punctuation style, which tracks the vendor's house template."""
    hs = [h.strip().lower() for h in headings]
    for name, tpl in DIALECTS.items():
        vals = sum(1 for h in hs if re.fullmatch(tpl["value_hdr"], h))
        unit = any(re.fullmatch(tpl["unit_hdr"], h) for h in hs)
        if vals >= 2 and unit:
            return name
    return "mixed"


def format_name(roles, traits):
    """A structural name for the table's layout."""
    if "cond" in roles:
        carrier = "cond-col"
    elif "freq" in roles:
        carrier = "freq-col"
    elif any(x.startswith("band-") for x in traits):
        carrier = "band-rows"
    else:
        carrier = "no-cond"
    if "symbol" in roles:
        carrier += "+sym"
    grid = ("min-typ-max" if {"min", "typ", "max"} <= set(roles)
            else "min-max" if {"min", "max"} <= set(roles)
            else "single-col")
    return f"{carrier} | {grid}"


def classify(headings, rows):
    """(signature, facts) for one table, or None if it is not a spec table."""
    roles = col_roles(headings)
    recs = group_records(headings, rows)
    if len(recs) < 2:
        return None

    order = "".join({"param": "P", "symbol": "S", "cond": "C", "freq": "F",
                     "min": "M", "typ": "T", "max": "X", "unit": "U"}[k]
                    for k in ("param", "symbol", "cond", "freq",
                              "min", "typ", "max", "unit") if k in roles)

    # Reported as DETAIL, never as the family key.
    multi = sum(1 for r in recs if len(r["values"]) > 1)
    # The same spec under two different band rows is two value sets, even
    # though each of its own rows carries only one.
    by_name = defaultdict(set)
    for r in recs:
        if r["name"]:
            by_name[r["name"].strip().lower()].add(r.get("band", ""))
    band_repeats = sum(1 for v in by_name.values() if len(v) > 1)
    band_rows = any(r.get("band") for r in recs)
    # Count where band text actually appears.
    cond_i = roles.get("cond")
    vcols = [roles[k] for k in ("min", "typ", "max") if k in roles]
    in_cond = shifted = 0
    for r in recs:
        for ln in r["lines"]:
            if cond_i is not None and cond_i < len(ln) \
                    and _BAND.match(ln[cond_i].strip()):
                in_cond += 1
            for vc in vcols:
                if vc < len(ln) and _BAND_IN_TEXT.fullmatch(ln[vc].strip()):
                    shifted += 1
    band_in_cond = in_cond >= 2
    band_shift = shifted >= 2

    # A section row: a name, no values, but a condition that applies downward.
    inherit_cond = 0
    for r in recs:
        first = r["lines"][0]
        if not r["values"] and cond_i is not None and cond_i < len(first) \
                and len(first[cond_i].strip()) > 3:
            inherit_cond += 1

    # A continuation line that carries only a short fragment and no value is a
    # wrapped subscript, not a value set.
    sym_i = roles.get("symbol")
    subscript_wrap = 0
    if sym_i is not None:
        for r in recs:
            for ln in r["lines"][1:]:
                frag = ln[sym_i].strip() if sym_i < len(ln) else ""
                others = [c for i, c in enumerate(ln)
                          if i != sym_i and c.strip()]
                if frag and not others and len(frag) <= 5:
                    subscript_wrap += 1

    # Non-numeric text sitting in a value column.
    text_in_values = 0
    for r in recs:
        for ln in r["lines"]:
            for vc in vcols:
                cell = ln[vc].strip() if vc < len(ln) else ""
                if cell and _num(cell) is None \
                        and not re.fullmatch(r"[-\u2014\u2013\u2212+*]+", cell):
                    text_in_values += 1

    # How many label tiers: section rows, then named specs, then unnamed
    # sub-rows that still carry their own values.
    sections = sum(1 for r in recs if not r["values"] and r["name"])
    subrows = sum(1 for r in recs if len(r["values"]) > 1)
    depth = 1 + (1 if sections else 0) + (1 if subrows else 0)
    values = ("multi-value" if multi >= 2 or band_repeats >= 2
              else "single-value")
    key_i = roles.get("freq", cond_i)
    banded = 0
    if key_i is not None:
        for r in recs:
            for ln in r["values"]:
                if key_i < len(ln) and _BAND.match(ln[key_i].strip()):
                    banded += 1
    key = ("keyed-by-frequency" if banded >= 2 or band_repeats >= 2
           or roles.get("freq") is not None
           else "keyed-by-condition" if cond_i is not None
           else "keyed-in-cell")

    # WHERE the band sits is the difference between the two styles, and it
    # decides whether a reader may trust the value columns:
    #
    #   grey   | INSERTION LOSS | | 0.1 GHz to 12 GHz | | 2.1 | 2.4 | dB
    #          band in the conditions column; Min/Typ/Max mean what they say.
    #
    #   yellow | Input Return Loss |             |  8 |  | dB
    #          |                   | 20 - 30 GHz | 15 |  | dB
    #          no conditions column, so the band occupies the Min slot and
    #          shifts every value one column right. Reading Min positionally
    #          there returns the band, and Typ returns what is actually Min.
    # ---- traits ---------------------------------------------------------
    # Binary flags could not express what these tables actually do. Each trait
    # below is a separate thing a reader must handle, and they co-occur freely,
    # so they are measured independently rather than collapsed into one label.
    traits = []

    # 1. A SECTION row that carries a condition and no values. The condition
    #    governs every spec beneath it:
    #       INPUT LINEARITY | | 200 MHz to 40 GHz | | | |
    #       1 dB Power Compression | P1dB | | | 27.5 | | dBm
    #    P1dB is measured over 200 MHz to 40 GHz, and nothing in its own row
    #    says so.
    if inherit_cond >= 1:
        traits.append("inherited-cond")

    # 2. Where the band sits, which decides whether the value columns can be
    #    trusted positionally.
    if band_shift:
        traits.append("band-shifts-values")
    elif band_in_cond:
        traits.append("band-in-cond-col")
    elif band_rows:
        traits.append("band-rows")

    # 3. A subscript wrapped onto its own line: "I" then "DD" is I_DD, "V"
    #    then "INL" is V_INL. Read as separate rows they are two meaningless
    #    fragments; the second line has no value of its own.
    if subscript_wrap >= 2:
        traits.append("wrapped-subscripts")

    # 4. Condition text overflowing INTO a value column:
    #       Third-Order Intercept | IP3 | Two tone input | Δf = 1 MHz | 50 |
    #    "Δf = 1 MHz" is in the Min slot. Positionally, Min reads as text and
    #    the real value has moved right -- the same damage as band-shift, from
    #    a different cause.
    if text_in_values >= 2:
        traits.append("text-in-value-cols")

    # 5. Label depth: a flat table names each spec once; a nested one has a
    #    section, then a spec, then a sub-condition under it.
    if depth >= 3:
        traits.append("3-level-labels")

    # Traits are NOT part of the grouping key. They co-occur freely -- one
    # ADRF6650 table has three of them -- so folding them into the signature
    # turned seven tables into seven "formats". The format is the family and
    # the column roles; the traits are what a reader for that format must cope
    # with, and they are summarised per format instead.
    # How cleanly the value columns parse. Low means the column boundaries are
    # probably wrong, and everything derived from them is suspect -- worth
    # knowing per table rather than discovering later as bad data.
    cells = tot = 0
    for r in recs:
        for ln in r["values"]:
            for vc in vcols:
                c = ln[vc].strip() if vc < len(ln) else ""
                if c:
                    tot += 1
                    cells += 1 if _num(c) is not None else 0
    confidence = round(cells / tot, 2) if tot else 0.0

    name = format_name(roles, traits)
    dialect = dialect_of(headings)
    return (f"{name:<28} | {dialect}",
            {"format": name, "dialect": dialect, "roles": order,
             "traits": traits, "confidence": confidence,
             "values": values,
             "key": key, "specs": len(recs), "multi": multi,
             "cols": len(headings), "headings": list(headings)})


# ==========================================================  survey
def category_index():
    out = {}
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
    """One entry per vendor. Roots can name the same folder twice on a
    case-insensitive filesystem, which surveyed every vendor two or three
    times."""
    byname, seen = {}, set()
    for root in (dsmine.default_roots() if dsmine else []):
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
    return [(n, ds) for n, ds in sorted(byname.items())]


def files_in(dirs):
    fs, seen = [], set()
    for d in dirs:
        for f in d.rglob("*.pdf"):
            if f.name.casefold() in seen:
                continue
            seen.add(f.name.casefold())
            fs.append(f)
    return sorted(fs)


def show_one(path, limit=18):
    print("\n" + "=" * 78)
    print(f"  {path.name}")
    print("=" * 78)
    tables = parse_tables(path)
    if not tables:
        print("  no header-anchored table found")
        return
    for n, (heads, rows) in enumerate(tables):
        res = classify(heads, rows)
        recs = group_records(heads, rows)
        print(f"\n  --- table {n}: {len(heads)} cols, {len(recs)} spec(s) --- "
              f"{res[0] if res else 'unclassified'}")
        print("    " + " | ".join(h[:16] for h in heads))
        print("    " + "-" * 70)
        for rec in recs[:limit]:
            print("    " + " | ".join(c[:16] for c in rec["lines"][0]))
            for ln in rec["lines"][1:]:
                print("      \u21b3 " + " | ".join(c[:16] for c in ln))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("files", nargs="*")
    ap.add_argument("--vendor", default=None)
    ap.add_argument("--sample", type=int, default=150)
    ap.add_argument("--show", type=int, default=2)
    ap.add_argument("--show-table", action="store_true",
                    help="print the parsed grid for the files given")
    ap.add_argument("--categories", nargs="*", default=None)
    ap.add_argument("--any-category", action="store_true")
    ap.add_argument("--min-share", type=float, default=0.0)
    ap.add_argument("--zip", default=None)
    ap.add_argument("--per-format", type=int, default=3)
    ap.add_argument("--max-zip-mb", type=float, default=20.0)
    ap.add_argument("--csv", default=None)
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args(argv)
    random.seed(a.seed)

    given = [Path(f) for f in a.files if Path(f).is_file()]
    if given and a.show_table:
        for p in given:
            show_one(p)
        return 0

    dirs = [("(given)", [])] if given else vendor_dirs()
    if a.vendor and not given:
        dirs = [(n, ds) for n, ds in dirs if a.vendor.lower() in n.lower()]
    if not dirs:
        print("No vendor folders found under the datasheet library.")
        return 1

    index = {} if (a.any_category or given) else category_index()
    allowed = {c.lower() for c in (a.categories or RF_CATEGORIES)}
    rows_out, groups_all = [], {}

    for name, dset in dirs:
        files = given if given else files_in(dset)
        dropped = 0
        if index:
            kept = [f for f in files
                    if index.get(re.sub(r"[^A-Z0-9]", "", f.stem.upper()), "")
                    .lower() in allowed]
            dropped = len(files) - len(kept)
            files = kept
        if a.sample and len(files) > a.sample:
            files = sorted(random.sample(files, a.sample))
        if not files:
            continue

        print(f"\n{'=' * 76}")
        print(f"  {name}   {len(files)} datasheet(s)"
              + (f", {dropped} outside the working categories" if dropped
                 else ""))
        print(f"{'=' * 76}")
        groups, n_tables, no_table = defaultdict(list), 0, 0
        for i, f in enumerate(files, 1):
            tabs = parse_tables(f)
            if not tabs:
                no_table += 1
            for heads, rws in tabs:
                res = classify(heads, rws)
                if res is None:
                    continue
                groups[res[0]].append(f)
                # Value structure varies WITHIN a family, so it is summarised
                # rather than used to split it.
                if res[1].get("confidence", 1) < 0.8:
                    _LOWCONF[res[0]] = _LOWCONF.get(res[0], 0) + 1
                _TRAITS.setdefault(res[0], Counter()).update(
                    res[1].get("traits") or [])
                _TRAITS.setdefault(res[0], Counter())[res[1]["values"]] += 1
                groups_all.setdefault((name, res[0]), []).append(f)
                n_tables += 1
            if i % 100 == 0:
                print(f"    ...{i}/{len(files)}, {n_tables} tables")
        total = max(1, n_tables)
        print(f"  {n_tables} spec table(s), {len(groups)} layout(s)"
              + (f", {no_table} file(s) with no table found" if no_table
                 else "") + "\n")
        print(f"  {'n':>5} {'share':>7}  layout")
        print("  " + "-" * 70)
        for sig, hits in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            share = 100 * len(hits) / total
            if share < a.min_share:
                continue
            print(f"  {len(hits):>5} {share:>6.1f}%  {sig}")
            lc = _LOWCONF.get(sig)
            if lc:
                print(f"        ! {lc} table(s) with poor column alignment "
                      f"(<80% of value cells numeric)")
            tr = _TRAITS.get(sig)
            if tr:
                print("        traits: " + ", ".join(
                    f"{k} x{v}" for k, v in tr.most_common()))
            if a.show:
                ex, seen = [], set()
                for p in hits:
                    if p.name not in seen:
                        seen.add(p.name)
                        ex.append(p.name)
                    if len(ex) >= a.show:
                        break
                print("        e.g. " + ", ".join(ex))
            rows_out.append({"vendor": name, "n": len(hits),
                             "share_pct": f"{share:.1f}", "layout": sig,
                             "examples": ", ".join(
                                 sorted({p.name for p in hits})[:3])})
        top3 = sorted(groups.values(), key=len, reverse=True)[:3]
        cov = 100 * sum(len(v) for v in top3) / total
        print(f"\n  top 3 cover {cov:.0f}%"
              + ("  <- worth hardcoding" if cov >= 70 else
                 "  <- still fragmented"))

    if a.zip and groups_all:
        out = Path(a.zip)
        budget, used, added, left = a.max_zip_mb * 1024 * 1024, 0, 0, 0
        man = io.StringIO()
        w = csv.writer(man)
        w.writerow(["vendor", "layout", "file"])
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            for (vend, sig), paths in groups_all.items():
                uniq, seen = [], set()
                for p in paths:
                    if p.name in seen:
                        continue
                    seen.add(p.name)
                    uniq.append(p)
                    if len(uniq) >= a.per_format:
                        break
                safe = re.sub(r"[^A-Za-z0-9]+", "_", sig).strip("_")[:60]
                for p in uniq:
                    try:
                        size = p.stat().st_size
                    except OSError:
                        continue
                    w.writerow([vend, sig, p.name])
                    if used + size > budget:
                        left += 1
                        continue
                    z.write(p, arcname=f"{vend}/{safe}/{p.name}")
                    used += size
                    added += 1
            z.writestr("MANIFEST.csv", man.getvalue())
            z.writestr("README.txt",
                       "Datasheets grouped by the table layout assigned to "
                       "them.\n\nEach folder is one layout. If two obviously "
                       "different tables sit in one\nfolder the signature is "
                       "too coarse; if one layout is split across folders it "
                       "is\ntoo fine.\n")
        print(f"\n  wrote {out}  ({used / 1e6:.1f} MB, {added} file(s)"
              + (f", {left} left out for size" if left else "") + ")")

    if a.csv and rows_out:
        with open(a.csv, "w", newline="", encoding="utf-8") as fh:
            wr = csv.DictWriter(fh, fieldnames=list(rows_out[0]))
            wr.writeheader()
            wr.writerows(rows_out)
        print(f"\n  written to {a.csv}")

    print("""
  Format name -- what the table IS, not whose it is
    cond-col        a Test Conditions/Comments column carries the condition
    freq-col        a Frequency column carries it instead
    band-rows       neither; a band row states it for the specs beneath
    no-cond         no condition anywhere
    +sym            a separate Symbol column
    min-typ-max     the value grid (or min-max / single-col)

  dialect  header punctuation, which tracks the vendor's house template:
    terse   "Min" "Typ" "Max" "Unit"        dotted  "Min." "Typ." "Max." "Units"
  Recorded because it identifies the template reliably -- but a format is
  named for its structure, so the same layout from another vendor is the same
  format.

  TRAITS are per-table and co-occur freely, so they are summarised under each
  format rather than folded into its name:
    inherited-cond       a section row's condition governs the specs beneath it
    band-in-cond-col     band in the conditions column; values read positionally
    band-shifts-values   band takes the Min slot and pushes values right
    text-in-value-cols   condition text overflowed into a value column
    wrapped-subscripts   a subscript on its own line ("I" then "DD" = I_DD)
    3-level-labels       section, then spec, then sub-rows with their own values
""")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nstopped.")
        sys.exit(130)
