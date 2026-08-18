"""dbg_vendor_matrix.py — does parsing work for every VENDOR, not just overall?

    python dbg_vendor_matrix.py
    python dbg_vendor_matrix.py --per-cell 5 --zip problems.zip
    python dbg_vendor_matrix.py --vendors MACOM Qorvo --per-cell 8
    python dbg_vendor_matrix.py --only-problems --zip problems.zip --max-zip-mb 20

Samples X datasheets from every vendor x category cell, parses each, and reports
a matrix of how well extraction works per cell. Datasheets that parse badly are
collected into a .zip with a manifest explaining why each was flagged, so the
failures can be looked at rather than guessed at.

WHY BY VENDOR
    Every check so far samples by CATEGORY, which averages a vendor's failures
    into whatever categories it lands in. A vendor whose PDFs do not extract at
    all is invisible that way -- its parts simply look like parts with no specs,
    indistinguishable from parts whose datasheets genuinely say little. Sampling
    the vendor x category grid separates "this vendor's layout defeats the
    parser" from "this category has little to state".

PROBLEM CLASSES
    These need different fixes and so are counted separately:
      CORRUPT    no text at all -- a mangled download. Re-fetch, do not re-parse.
      THIN       text extracted but almost none of it: scanned image, or a
                 wrapper page rather than the datasheet.
      NO-SPECS   plenty of text, zero specs. The layout is not being read.
      FEW-SPECS  below the expected count for that category.
      CONFLICT   specs found, but they contradict the catalogue values.
    OK is everything else.

Read-only: nothing is written except the zip you ask for.
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
    for pkg in ("pythonrfparts", "rfparts"):
        try:
            return __import__(f"{pkg}.{name}", fromlist=[name])
        except Exception:
            continue
    return None


dsmine = _opt("dsmine")
partdb = _opt("partdb")
registry = _opt("registry")
if dsmine is None or partdb is None:
    sys.exit("Could not import dsmine/partdb. Run this from the folder that "
             "holds the package.")

CATEGORIES = ["amplifier", "mixer", "filter", "attenuator", "divider",
              "coupler", "switch", "phase_shifter"]

# Specs that say nothing about whether the electrical table was read. Counting
# them makes a datasheet that yielded only a frequency and a space score look
# like a success.
SOFT_KEYS = getattr(dsmine, "_SOFT_KEYS",
                    {"freq_min", "freq_max", "space_score_pct",
                     "space_evidence", "throw_config"})

THIN_CHARS = 400          # below this, text extraction effectively failed
FEW_SPECS = 3             # hard specs below which a datasheet is under-read


def loose(s):
    return re.sub(r"[^A-Z0-9]", "", str(s or "").upper())


def hard_count(mined):
    fn = getattr(dsmine, "hard_spec_count", None)
    if fn:
        try:
            return fn(mined)
        except Exception:
            pass
    return sum(1 for k in mined if k not in SOFT_KEYS)


def part_table():
    """{loose mpn: (mpn, vendor, category)} for everything with a category."""
    out = {}
    try:
        for r in partdb.db().execute(
                "SELECT mpn, vendor, category FROM parts "
                "WHERE category IS NOT NULL AND category != ''"):
            out.setdefault(loose(r["mpn"]),
                           (r["mpn"], r["vendor"] or "?", r["category"]))
    except Exception as e:
        print(f"  ! could not read parts: {e}")
    return out


def canon_vendor(name):
    fn = getattr(partdb, "canonical_vendor", None)
    if fn:
        try:
            return fn(name)
        except Exception:
            pass
    return str(name or "?").strip()


def library_files():
    files = []
    for root in dsmine.default_roots():
        try:
            for f in Path(root).rglob("*"):
                if f.is_file() and f.suffix.lower() in (".pdf", ".html",
                                                        ".htm", ".txt"):
                    files.append(f)
        except OSError:
            continue
    return files


def db_specs(mpn):
    """{key: value} the catalogue already holds, for the conflict check."""
    out = {}
    try:
        conn = partdb.db()
        row = conn.execute("SELECT id FROM parts WHERE upper(mpn)=?",
                           (str(mpn).upper(),)).fetchone()
        if not row:
            return out
        for s in conn.execute(
                "SELECT key, value_min, value_typ, value_max, method, "
                "confidence FROM specs WHERE part_id=? ORDER BY confidence DESC",
                (row["id"],)):
            if s["method"] == "datasheet":
                continue          # comparing mined values to mined values
            if s["key"] == "freq_ghz" and s["value_min"] is not None:
                out.setdefault("freq_min", s["value_min"])
                out.setdefault("freq_max", s["value_max"])
                continue
            v = (s["value_typ"] if s["value_typ"] is not None
                 else s["value_min"] if s["value_min"] is not None
                 else s["value_max"])
            if isinstance(v, (int, float)):
                out.setdefault(s["key"], v)
    except Exception:
        pass
    return out


def conflicts(mined, stored, tol=0.15):
    """Specs where datasheet and catalogue disagree beyond a tolerance.

    A tolerance rather than equality: min/typ/max and band-edge differences are
    honest disagreements, and flagging every one of them would bury the cases
    where a value is simply wrong."""
    out = []
    for key, val in mined.items():
        v = val[0] if isinstance(val, (tuple, list)) else val
        if not isinstance(v, (int, float)) or key not in stored:
            continue
        s = stored[key]
        if not isinstance(s, (int, float)):
            continue
        scale = max(abs(s), abs(v), 1e-9)
        if abs(s - v) / scale > tol:
            out.append(f"{key}={v:g} vs {s:g}")
    return out


def assess(path, mpn):
    """(verdict, detail, mined, chars) for one datasheet."""
    try:
        kind = dsmine._sniff(path)
    except Exception:
        kind = "?"
    if kind in ("corrupt", "unreadable"):
        return "CORRUPT", f"sniffed {kind}", {}, 0
    try:
        text = dsmine.datasheet_text(path)
    except Exception as e:
        return "CORRUPT", f"read failed: {type(e).__name__}", {}, 0
    chars = len(text or "")
    if chars < THIN_CHARS:
        return "THIN", f"only {chars} chars extracted", {}, chars
    try:
        mined = dsmine.mine_text(text, path=path, mpn=mpn)
    except TypeError:
        mined = dsmine.mine_text(text)
    except Exception as e:
        return "NO-SPECS", f"mine failed: {type(e).__name__}: {e}", {}, chars
    n = hard_count(mined)
    if n == 0:
        return "NO-SPECS", f"{chars} chars, no hard specs", mined, chars
    bad = conflicts(mined, db_specs(mpn))
    if bad:
        return "CONFLICT", "; ".join(bad[:3]), mined, chars
    if n < FEW_SPECS:
        return "FEW-SPECS", f"only {n} hard spec(s)", mined, chars
    return "OK", f"{n} hard specs", mined, chars


PROBLEM = ("CORRUPT", "THIN", "NO-SPECS", "FEW-SPECS", "CONFLICT")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--per-cell", type=int, default=3,
                    help="datasheets per vendor x category cell (default 3)")
    ap.add_argument("--categories", nargs="*", default=CATEGORIES)
    ap.add_argument("--vendors", nargs="*", default=None,
                    help="restrict to these vendors (canonical names)")
    ap.add_argument("--min-cell", type=int, default=1,
                    help="skip cells with fewer datasheets than this")
    ap.add_argument("--zip", default=None,
                    help="write the flagged datasheets here, with a manifest")
    ap.add_argument("--max-zip-mb", type=float, default=25.0,
                    help="stop adding files past this size (default 25 MB)")
    ap.add_argument("--max-zip-files", type=int, default=40)
    ap.add_argument("--only-problems", action="store_true",
                    help="list only the flagged datasheets, not every one")
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args(argv)

    random.seed(args.seed)
    parts = part_table()
    files = library_files()
    print(f"library : {len(files)} datasheet(s)")
    print(f"database: {len(parts)} part(s) with a category")

    # ---- build the vendor x category grid --------------------------------
    grid = defaultdict(list)
    for f in files:
        rec = parts.get(loose(f.stem))
        if not rec:
            continue
        mpn, vendor, cat = rec
        if cat not in args.categories:
            continue
        v = canon_vendor(vendor)
        if args.vendors and v not in args.vendors:
            continue
        grid[(v, cat)].append((f, mpn))

    vendors = sorted({v for v, _ in grid})
    if not vendors:
        print("\nNothing to sample. Are the datasheet filenames part numbers?")
        return 1

    # ---- sample and assess ------------------------------------------------
    results = []
    cell_stat = {}
    for v in vendors:
        for cat in args.categories:
            pool = grid.get((v, cat)) or []
            if len(pool) < args.min_cell:
                continue
            sample = random.sample(pool, min(args.per_cell, len(pool)))
            verdicts, specs_seen = Counter(), []
            for path, mpn in sample:
                verdict, detail, mined, chars = assess(path, mpn)
                verdicts[verdict] += 1
                specs_seen.append(hard_count(mined))
                results.append({"vendor": v, "category": cat, "mpn": mpn,
                                "verdict": verdict, "detail": detail,
                                "chars": chars, "path": str(path),
                                "specs": hard_count(mined)})
            cell_stat[(v, cat)] = {
                "n": len(sample), "pool": len(pool),
                "avg": sum(specs_seen) / max(1, len(specs_seen)),
                "bad": sum(verdicts[k] for k in PROBLEM),
                "verdicts": verdicts}

    # ---- the matrix -------------------------------------------------------
    print(f"\n{'=' * 78}")
    print("  AVERAGE HARD SPECS PER DATASHEET   (bad/total in brackets)")
    print(f"{'=' * 78}")
    cats = [c for c in args.categories if any((v, c) in cell_stat
                                              for v in vendors)]
    head = "  " + f"{'vendor':<20}" + "".join(f"{c[:12]:>14}" for c in cats)
    print(head)
    print("  " + "-" * max(40, len(head) - 2))
    for v in vendors:
        line = f"  {v[:20]:<20}"
        for c in cats:
            st = cell_stat.get((v, c))
            line += (f"{st['avg']:>7.1f} ({st['bad']}/{st['n']})"
                     if st else f"{'-':>14}")
        print(line)

    print(f"\n  {'verdict':<12}{'n':>6}   what it means")
    print("  " + "-" * 66)
    tally = Counter(r["verdict"] for r in results)
    meaning = {
        "OK": "parsed cleanly",
        "CORRUPT": "no text -- mangled download, re-fetch (do not re-parse)",
        "THIN": "almost no text -- scanned image or wrapper page",
        "NO-SPECS": "text fine, zero specs -- the layout is not being read",
        "FEW-SPECS": f"under {FEW_SPECS} hard specs -- partially read",
        "CONFLICT": "specs disagree with the catalogue by >15%",
    }
    for k in ("OK",) + PROBLEM:
        if tally.get(k):
            print(f"  {k:<12}{tally[k]:>6}   {meaning[k]}")

    # ---- worst cells ------------------------------------------------------
    worst = sorted((s for s in cell_stat.items()),
                   key=lambda kv: (-kv[1]["bad"], kv[1]["avg"]))[:10]
    if worst and worst[0][1]["bad"]:
        print(f"\n  worst cells")
        print("  " + "-" * 66)
        for (v, c), st in worst:
            if not st["bad"]:
                continue
            kinds = ", ".join(f"{k} {n}" for k, n in st["verdicts"].items()
                              if k in PROBLEM)
            print(f"  {v[:18]:<18} {c[:14]:<14} {st['bad']}/{st['n']} bad "
                  f"({kinds}); pool {st['pool']}")

    # ---- per-file listing -------------------------------------------------
    listing = [r for r in results
               if not args.only_problems or r["verdict"] in PROBLEM]
    if listing:
        print(f"\n  {'vendor':<14}{'category':<14}{'part':<20}"
              f"{'verdict':<11}detail")
        print("  " + "-" * 74)
        for r in sorted(listing, key=lambda r: (r["verdict"] != "OK",
                                                r["vendor"], r["category"])):
            print(f"  {r['vendor'][:13]:<14}{r['category'][:13]:<14}"
                  f"{r['mpn'][:19]:<20}{r['verdict']:<11}{r['detail'][:34]}")

    # ---- zip the problems -------------------------------------------------
    flagged = [r for r in results if r["verdict"] in PROBLEM]
    if args.zip and flagged:
        # Ordered worst-first so a size cap keeps the most informative files:
        # a datasheet that yielded nothing teaches more than one that yielded
        # two specs instead of three.
        rank = {k: i for i, k in enumerate(
            ["NO-SPECS", "THIN", "FEW-SPECS", "CONFLICT", "CORRUPT"])}
        flagged.sort(key=lambda r: rank.get(r["verdict"], 9))
        out = Path(args.zip)
        budget = args.max_zip_mb * 1024 * 1024
        used, added, skipped = 0, [], 0
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            for r in flagged:
                p = Path(r["path"])
                try:
                    size = p.stat().st_size
                except OSError:
                    continue
                if len(added) >= args.max_zip_files or used + size > budget:
                    skipped += 1
                    continue
                # Flattened into verdict folders so the problem class is
                # obvious from the tree alone.
                z.write(p, arcname=f"{r['verdict']}/{r['vendor']}/{p.name}")
                used += size
                added.append(r)
            buf = io.StringIO()
            w = csv.DictWriter(buf, fieldnames=["verdict", "vendor", "category",
                                                "mpn", "specs", "chars",
                                                "detail", "path"])
            w.writeheader()
            for r in flagged:
                w.writerow({k: r[k] for k in w.fieldnames})
            z.writestr("MANIFEST.csv", buf.getvalue())
            z.writestr("README.txt",
                       "Datasheets flagged by dbg_vendor_matrix.py.\n\n"
                       "Folders are the problem class:\n"
                       "  NO-SPECS   text extracted, zero specs found\n"
                       "  THIN       almost no text extracted\n"
                       "  FEW-SPECS  fewer specs than expected\n"
                       "  CONFLICT   specs disagree with the catalogue\n"
                       "  CORRUPT    no readable text at all\n\n"
                       "MANIFEST.csv lists every flagged part, including any "
                       "whose file was left out\nto stay under the size cap.\n")
        print(f"\n  wrote {out}  ({used / 1e6:.1f} MB, {len(added)} file(s)"
              + (f", {skipped} left out for size" if skipped else "") + ")")
        print(f"  MANIFEST.csv inside lists all {len(flagged)} flagged part(s).")
        if used / 1e6 > 20:
            print("  ! large. --max-zip-mb 10 if the upload does not go "
                  "through.")
    elif args.zip:
        print("\n  nothing flagged -- no zip written.")

    print("""
  Reading the matrix
    A low average in ONE cell while its neighbours are fine is a vendor layout
    problem, not a data gap. A whole row low means that vendor's datasheets are
    not being read at all. A whole column low means the category's specs are
    not in the registry yet.""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
