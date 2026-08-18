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
from collections import defaultdict
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

RF_CATEGORIES = ["amplifier", "mixer", "filter", "attenuator", "divider",
                 "coupler", "switch", "phase_shifter", "limiter", "multiplier",
                 "oscillator", "transformer", "termination", "equalizer",
                 "detector", "adapter", "cable", "dc_block", "bias_tee"]

# ==========================================================  table parser
_HDR_WORDS = re.compile(
    r"^(parameter|symbol|min|min\.|minimum|typ|typ\.|typical|max|max\.|maximum|"
    r"unit|units|conditions?|comments?|test|frequency|freq\.?)$", re.I)
_VALUE_HDR = re.compile(r"^(min|typ|max)\.?$", re.I)
_NUM = re.compile(r"^[-+\u2212]?\d+(?:\.\d+)?$")
_BAND = re.compile(
    r"^(DC|\d+(?:\.\d+)?)\s*(GHz|MHz|kHz)?\s*(?:-|\u2013|to)\s*"
    r"(\d+(?:\.\d+)?)\s*(GHz|MHz|kHz)?$", re.I)


def _num(s):
    s = str(s or "").strip().replace("\u2212", "-").replace(",", "")
    return float(s) if _NUM.match(s) else None


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
                    if any(c.strip() for c in cells):
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
            roles.setdefault("cond", i)     # a frequency column IS the key
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
        vcols = [i for i in range(len(headings))
                 if i not in (pcol, roles.get("cond"), roles.get("unit"))]

    def has_values(r):
        return any(_num(r[i]) is not None for i in vcols if i < len(r))

    recs, pending = [], []
    for r in rows:
        name = r[pcol].strip() if pcol < len(r) else ""
        if name:
            recs.append({"name": name, "lines": pending + [r]})
            pending = []
        elif recs:
            recs[-1]["lines"].append(r)
        else:
            pending.append(r)
    if pending:
        recs.append({"name": "", "lines": pending})

    for rec in recs:
        rec["values"] = [ln for ln in rec["lines"] if has_values(ln)]
    # A lone line with no numbers is a section band, not a spec.
    return [r for r in recs if r["values"] or len(r["lines"]) > 1]


# ==========================================================  classification
def classify(headings, rows):
    """(signature, facts) for one table, or None if it is not a spec table."""
    roles = col_roles(headings)
    recs = group_records(headings, rows)
    if len(recs) < 2:
        return None

    multi = sum(1 for r in recs if len(r["values"]) > 1)
    values = "multi-value" if multi >= 2 else "single-value"

    cond_i = roles.get("cond")
    banded = 0
    if cond_i is not None:
        for r in recs:
            for ln in r["values"]:
                if cond_i < len(ln) and _BAND.match(ln[cond_i].strip()):
                    banded += 1

    if values == "single-value":
        key = "conditions-present" if cond_i is not None else "no-conditions"
    elif banded >= 2:
        key = "keyed-by-frequency"
    elif cond_i is not None:
        key = "keyed-by-condition"
    else:
        key = "keyed-in-cell"

    # min/typ/max is ONE value set expressed three ways, not three sets.
    grid = ("min/typ/max" if {"min", "typ", "max"} <= set(roles)
            else "min/max" if {"min", "max"} <= set(roles)
            else "single-column")
    order = "".join(k[0].upper() for k in
                    ("param", "symbol", "cond", "min", "typ", "max", "unit")
                    if k in roles)
    return (f"{values:<12} | {key:<19} | {grid}",
            {"values": values, "key": key, "grid": grid, "roles": order,
             "specs": len(recs), "multi": multi, "cols": len(headings)})


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
  Layout string
    single-value / multi-value   how many value sets a spec carries
    keyed-by-frequency           the sets differ by a frequency band
    keyed-by-condition           the sets differ by a stated test condition
    keyed-in-cell                the discriminator sits inside a cell
    min/typ/max                  ONE value set expressed three ways, not three

  --show-table FILE.pdf prints the parsed grid, so the classification can be
  checked against the datasheet itself.""")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nstopped.")
        sys.exit(130)
