"""dbg_datasheet_parse.py — show exactly what the miner reads from a datasheet.

    python dbg_datasheet_parse.py
    python dbg_datasheet_parse.py --examples 3 --random 3
    python dbg_datasheet_parse.py --file "C:\\...\\MAC-60LH+.pdf"
    python dbg_datasheet_parse.py --vendor Qorvo --random 5
    python dbg_datasheet_parse.py --seed 7          (repeatable sample)

By default: two datasheets from the EXAMPLE DATASHEETS folder and two picked at
random from the live library, so a spot check covers both the files that were
tuned against and files nobody has looked at.

For every value it shows WHICH extractor produced it and the text it matched, so
a wrong number can be judged without opening the PDF. Three extractors run, and
they disagree in useful ways:

    prose      PARAM_SPECS regexes   "Gain: 22 dB"
    table      label-unit-value      "Small Signal Gain (min) dB 8.5"
    range      the frequency band    "1 to 2500 MHz"

Where the part is in the database, the stored value is shown beside the mined one
so catalogue and datasheet can be compared directly -- that comparison is the
thing to check before letting datasheets take precedence.

Read-only. Nothing is written to the database or the library.
"""
from __future__ import annotations

import argparse
import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _opt(name):
    for pkg in ("pythonrfparts", "rfparts"):
        try:
            return __import__(f"{pkg}.{name}", fromlist=[name])
        except Exception:
            continue
    return None


dsmine = _opt("dsmine")
partdb = _opt("partdb")
registry = _opt("registry")
specmatch = _opt("specmatch")
if dsmine is None:
    sys.exit("Could not import dsmine. Run this from the folder that holds the "
             "package.")

EXAMPLE_DIRS = ("EXAMPLE DATASHEETS TO TUNE PARSING", "example datasheets",
                "examples")


def loose(s):
    return re.sub(r"[^A-Z0-9]", "", str(s or "").upper())


def find_example_dir(explicit=None):
    if explicit:
        p = Path(explicit)
        return p if p.is_dir() else None
    here = Path(__file__).resolve().parent
    for root in (here, here.parent, here.parent.parent):
        for name in EXAMPLE_DIRS:
            cand = root / name
            if cand.is_dir():
                return cand
        try:
            for d in root.iterdir():
                if d.is_dir() and "EXAMPLE DATASHEET" in d.name.upper():
                    return d
        except OSError:
            pass
    return None


def library_files(vendor=None):
    files = []
    for root in dsmine.default_roots():
        base = Path(root) / vendor if vendor else Path(root)
        if not base.is_dir():
            continue
        try:
            for f in base.rglob("*"):
                if f.is_file() and f.suffix.lower() in (".pdf", ".html", ".htm",
                                                        ".txt"):
                    files.append(f)
        except OSError:
            continue
    return files


# ---------------------------------------------------------------- provenance
def prose_hits(text):
    """{key: (value, unit, matched text)} from the PARAM_SPECS regexes."""
    out = {}
    if registry is None:
        return out
    for key, pats in dsmine._PATTERNS.items():
        meta = registry.PARAM_SPECS[key]
        ugroup = meta.get("unit_group")
        uscale = meta.get("unit_scale") or {}
        for pat in pats:
            m = pat.search(text or "")
            if not m:
                continue
            try:
                value = float(m.group(1))
            except (TypeError, ValueError, IndexError):
                continue
            if ugroup:
                try:
                    unit = (m.group(ugroup) or "").strip().lower()
                except (IndexError, re.error):
                    unit = ""
                factor = uscale.get(unit)
                if factor is None:
                    continue
                value = round(value * factor, 9)
            snippet = re.sub(r"\s+", " ", m.group(0))[:70]
            out[key] = (value, meta.get("unit", ""), snippet)
            break
    return out


def table_hits(text):
    out = {}
    if not hasattr(dsmine, "_TABLE_ROW"):
        return out
    for key, rx, unit in dsmine._TABLE_ROW:
        m = rx.search(text or "")
        if not m:
            continue
        try:
            out[key] = (float(m.group(1)), unit,
                        re.sub(r"\s+", " ", m.group(0))[:70])
        except (TypeError, ValueError):
            continue
    return out


def band_hit(text):
    if not hasattr(dsmine, "freq_range_ghz"):
        return None
    band = dsmine.freq_range_ghz(text)
    if not band:
        return None
    rx = getattr(dsmine, "_RANGE_RE", None)
    snippet = ""
    if rx is not None:
        for chunk in (text[:2500], text):
            m = rx.search(chunk or "")
            if m:
                snippet = re.sub(r"\s+", " ", m.group(0))[:70]
                break
    return band, snippet


def db_lookup(mpn):
    """What the database already holds for this part, for comparison."""
    if partdb is None or not mpn:
        return None, {}
    try:
        conn = partdb.db()
        rows = conn.execute(
            "SELECT id, mpn, vendor, category FROM parts").fetchall()
    except Exception:
        return None, {}
    target = loose(mpn)
    for r in rows:
        if loose(r["mpn"]) == target:
            specs = {}
            for s in conn.execute(
                    "SELECT key, value_min, value_typ, value_max, unit, method, "
                    "confidence FROM specs WHERE part_id=? "
                    "ORDER BY confidence DESC", (r["id"],)):
                # freq_ghz is stored as a BAND while the miner reports the two
                # edges, so split it -- otherwise the two sides of the comparison
                # are the same fact under different names and never line up.
                if s["key"] == "freq_ghz" and s["value_min"] is not None:
                    specs.setdefault("freq_min", (s["value_min"], "GHz",
                                                  s["method"], s["confidence"]))
                    specs.setdefault("freq_max", (s["value_max"], "GHz",
                                                  s["method"], s["confidence"]))
                    continue
                specs.setdefault(s["key"], (
                    s["value_typ"] if s["value_typ"] is not None
                    else s["value_min"] if s["value_min"] is not None
                    else s["value_max"], s["unit"], s["method"],
                    s["confidence"]))
            return r, specs
    return None, {}


def report(path, show_text=0):
    print("\n" + "=" * 78)
    print(f"  {path.name}")
    print("=" * 78)
    kind = dsmine._sniff(path)
    size = path.stat().st_size
    text = dsmine.datasheet_text(path)
    print(f"  file    : {path}")
    print(f"  type    : {kind}   {size:,} bytes on disk   "
          f"{len(text):,} characters extracted")
    if kind == "corrupt":
        print("  ! rejected as corrupt -- a text-mangled download. Re-fetch it "
              "in binary;\n    no amount of parsing recovers this.")
        return
    if not text.strip():
        print("  ! no text extracted. If this is a scanned PDF it needs OCR, "
              "which this\n    pipeline does not do.")
        return

    prose = prose_hits(text)
    table = table_hits(text)
    band = band_hit(text)
    final = dsmine.mine_text(text)

    print(f"\n  {'spec':<22}{'value':>14} {'unit':<6}{'from':<8} matched text")
    print("  " + "-" * 74)
    shown = set()
    if band:
        (lo, hi), snip = band
        print(f"  {'freq_min':<22}{lo:>14g} {'GHz':<6}{'range':<8} {snip}")
        print(f"  {'freq_max':<22}{hi:>14g} {'GHz':<6}{'range':<8}")
        shown |= {"freq_min", "freq_max"}
    for key in sorted(set(prose) | set(table)):
        if key in shown:
            continue
        if key in prose:
            v, u, snip = prose[key]
            src = "prose"
            if key in table and abs(table[key][0] - v) > 1e-9:
                # Both extractors fired and disagree. Worth seeing: usually the
                # prose figure is the headline and the table one a corner
                # condition, but it is exactly where a wrong value hides.
                src = "prose*"
        else:
            v, u, snip = table[key]
            src = "table"
        print(f"  {key:<22}{v:>14g} {u:<6}{src:<8} {snip}")
        if src == "prose*":
            tv, tu, tsnip = table[key]
            print(f"  {'':<22}{tv:>14g} {tu:<6}{'table':<8} {tsnip}")
        shown.add(key)
    for key in sorted(final):
        if key in shown or key in ("space_score_pct", "space_evidence"):
            continue
        v = final[key][0]
        print(f"  {key:<22}{str(v):>14} {'':<6}{'other':<8}")

    pct = final.get("space_score_pct")
    if pct:
        why = final.get("space_evidence", ("",))[0]
        print(f"\n  space score : {pct[0]:.0f}%   {why[:90]}")
    else:
        print("\n  space score : (no qualification wording found)")

    mpn = path.stem
    row, specs = db_lookup(mpn)
    if row:
        print(f"\n  in the database as {row['mpn']} ({row['vendor']}, "
              f"{row['category'] or 'uncategorized'}):")
        keys = sorted(set(list(final) + list(specs)))
        print(f"    {'spec':<22}{'datasheet':>14}{'database':>16}  source")
        for k in keys:
            if k in ("space_evidence",):
                continue
            mined = final.get(k, (None,))[0]
            stored = specs.get(k, (None, "", "", 0))
            flag = ""
            if (mined is not None and stored[0] is not None
                    and isinstance(mined, (int, float))
                    and isinstance(stored[0], (int, float))
                    and abs(float(mined) - float(stored[0])) > 1e-6):
                flag = "   <-- DIFFER"
            print(f"    {k:<22}{str(mined)[:13]:>14}{str(stored[0])[:15]:>16}"
                  f"  {stored[2] or '-'}{flag}")
    else:
        print(f"\n  not in the database under {mpn!r} "
              f"(filename may not match the part number)")

    if show_text:
        print(f"\n  first {show_text} characters of extracted text:")
        print("  " + re.sub(r"\s+", " ", text[:show_text]))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--file", nargs="*", default=(),
                    help="specific datasheet(s) to parse")
    ap.add_argument("--examples", type=int, default=2,
                    help="how many from the EXAMPLE DATASHEETS folder")
    ap.add_argument("--random", type=int, default=2,
                    help="how many picked at random from the live library")
    ap.add_argument("--vendor", default=None,
                    help="restrict the random pick to one vendor folder")
    ap.add_argument("--example-dir", default=None)
    ap.add_argument("--seed", type=int, default=None,
                    help="fix the random pick so a run can be repeated")
    ap.add_argument("--show-text", type=int, default=0,
                    help="also print this many characters of extracted text")
    args = ap.parse_args(argv)

    if args.seed is not None:
        random.seed(args.seed)

    picked = []
    if args.file:
        for f in args.file:
            p = Path(f)
            if p.is_file():
                picked.append(p)
            else:
                print(f"! not a file: {f}")
    else:
        ex_dir = find_example_dir(args.example_dir)
        if ex_dir and args.examples:
            ex = [f for f in sorted(ex_dir.rglob("*"))
                  if f.is_file() and f.name.lower() != "desktop.ini"]
            print(f"examples : {ex_dir}  ({len(ex)} file(s))")
            picked += random.sample(ex, min(args.examples, len(ex)))
        elif args.examples:
            print("! could not find the EXAMPLE DATASHEETS folder "
                  "(--example-dir to point at it)")
        if args.random:
            lib = library_files(args.vendor)
            print(f"library  : {len(lib)} file(s)"
                  + (f" under {args.vendor}" if args.vendor else ""))
            if lib:
                picked += random.sample(lib, min(args.random, len(lib)))

    if not picked:
        print("\nNothing to parse.")
        return 1
    for p in picked:
        report(p, args.show_text)

    print("\n" + "=" * 78)
    print("""  What to check
    - the frequency band matches the one on the front page of the datasheet
    - values marked 'prose*' -- two extractors disagreed, one of them is wrong
    - rows marked DIFFER -- the datasheet and the catalogue do not agree, which
      is the case that decides whether datasheets should take precedence
    - anything missing that the datasheet clearly states""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
