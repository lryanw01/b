"""dbg_minicircuits.py — what is in the Mini-Circuits JSON, and what maps.

    python dbg_minicircuits.py
    python dbg_minicircuits.py --json "C:\\...\\Sources\\minicircuits_products_full.json"
    python dbg_minicircuits.py --show ampl --samples 3
    python dbg_minicircuits.py --db-only

Reads the catalogue JSON directly. No rebuild, no database writes, nothing
touched -- it just reports:

  1. every distinct cat/group code, how many records use it, and whether the
     ingest maps it to a category
  2. the raw records behind the biggest unmapped codes, so the mapping can be
     written from what is actually there
  3. which spec keys the records carry, and how many records have a case_style
     (the reason some categories show 0% package coverage)
  4. what the database currently holds for Mini-Circuits, if it has been built

WHY
    Answering "which category codes did we fail to map" by running a full
    dataset rebuild is absurd. The JSON is right there.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _opt(name):
    try:
        return __import__(f"pythonrfparts.{name}", fromlist=[name])
    except Exception:
        return None


mc = _opt("minicircuits_ingest")
partdb = _opt("partdb")


def find_json(explicit=None):
    if explicit:
        p = Path(explicit).expanduser()
        return p if p.is_file() else None
    roots = []
    try:
        from pythonrfparts.paths import SOURCES_ROOT, NEW_SOURCES
        roots += [Path(SOURCES_ROOT), Path(NEW_SOURCES)]
    except Exception:
        pass
    here = Path(__file__).resolve().parent
    roots += [here / "Sources", here, Path.home() / "Downloads" / "rfparts" / "Sources"]
    for r in roots:
        if not r.is_dir():
            continue
        for f in sorted(r.rglob("*minicircuit*.json")):
            return f
    return None


def load(path):
    data = json.loads(Path(path).read_text(encoding="utf-8", errors="replace"))
    if isinstance(data, dict):
        for key in ("products", "parts", "items", "data", "rows"):
            if isinstance(data.get(key), list):
                return data[key]
        return [v for v in data.values() if isinstance(v, dict)]
    return data if isinstance(data, list) else []


def report_json(path, show=None, samples=2, top=40):
    rows = load(path)
    print(f"\n{path}")
    print(f"  {len(rows)} record(s)")

    cats = Counter()
    pairs = Counter()
    for r in rows:
        c = str(r.get("cat") or "").strip()
        g = str(r.get("group") or "").strip()
        cats[c or "(blank)"] += 1
        pairs[(c or "(blank)", g or "(blank)")] += 1

    print(f"\n  {len(cats)} distinct 'cat' value(s)")
    print(f"  {'count':>7}  {'cat':<22} {'-> maps to':<20} group(s)")
    print("  " + "-" * 78)
    unmapped_total = 0
    for c, n in cats.most_common(top):
        raw = "" if c == "(blank)" else c
        mapped = mc.category_for({"cat": raw}) if mc else "?"
        groups = [g for (cc, g), _ in pairs.most_common() if cc == c][:2]
        # category_for() falls back to returning the code itself, so a code that
        # matched no rule still "has" a category -- it just invents a new one.
        # That is how spl, mix, cpl and friends became categories of their own
        # alongside divider, mixer and coupler, so it has to be called out.
        in_map = bool(mc) and raw.lower() in mc._CATEGORY_MAP
        if not raw:
            flag = "   <-- BLANK, becomes uncategorized"
            unmapped_total += n
        elif in_map:
            flag = ""
        else:
            flag = "   <-- NO RULE, invents a category"
            unmapped_total += n
        print(f"  {n:>7}  {c[:22]:<22} {str(mapped)[:20]:<20} "
              f"{', '.join(groups)[:26]}{flag}")
    if len(cats) > top:
        print(f"  ... {len(cats) - top} more distinct cat value(s)")
    print(f"\n  {unmapped_total} record(s) are blank or have no mapping rule")

    # what the records actually carry
    keys = Counter()
    speckeys = Counter()
    has_case = 0
    for r in rows:
        keys.update(r.keys())
        s = r.get("specs")
        if isinstance(s, dict):
            speckeys.update(s.keys())
        if str(r.get("case_style") or "").strip():
            has_case += 1
    print(f"\n  top-level fields present:")
    for k, n in keys.most_common(18):
        print(f"    {n:>7}  {k}")
    print(f"\n  spec keys inside 'specs':")
    for k, n in speckeys.most_common(18):
        print(f"    {n:>7}  {k}")
    print(f"\n  {has_case}/{len(rows)} record(s) have a non-empty case_style "
          f"-- this is why some categories show 0% package coverage")

    # raw records behind the unmapped codes, so a mapping can be written
    if show:
        targets = [show]
    else:
        targets = [c for c, _ in cats.most_common()
                   if c == "(blank)"
                   or not (mc and c.lower() in mc._CATEGORY_MAP)][:4]
    for code in targets:
        want = "" if code == "(blank)" else code
        picked = [r for r in rows
                  if str(r.get("cat") or "").strip() == want][:samples]
        if not picked:
            continue
        print(f"\n  {'=' * 74}\n  sample record(s) with cat={code!r}\n  {'=' * 74}")
        for r in picked:
            print(f"    pn={r.get('pn')!r}  group={r.get('group')!r}")
            for k, v in r.items():
                if k in ("pn", "group"):
                    continue
                if isinstance(v, dict):
                    print(f"      {k}:")
                    for kk, vv in v.items():
                        print(f"          {kk:<14} {vv!r}")
                else:
                    print(f"      {k:<16} {str(v)[:70]!r}")
            if mc:
                try:
                    print(f"      -> would parse to: "
                          f"{ {k: v[0] for k, v in mc.classify_values(r).items()} }")
                except Exception as e:
                    print(f"      -> parse failed: {type(e).__name__}: {e}")
            print()


def report_db():
    if not partdb:
        print("\n(could not import partdb; skipping the database section)")
        return
    conn = partdb.db()
    print(f"\n{'=' * 78}\n  DATABASE: {partdb.DB_PATH}\n{'=' * 78}")
    try:
        rows = conn.execute(
            "SELECT category AS c, COUNT(*) AS n FROM parts "
            "WHERE lower(vendor) LIKE '%mini%circuit%' "
            "GROUP BY category ORDER BY n DESC").fetchall()
    except Exception as e:
        print(f"  query failed: {e}")
        return
    total = sum(r["n"] for r in rows)
    print(f"  {total} Mini-Circuits part(s) stored, {len(rows)} categor(ies)")
    for r in rows[:30]:
        print(f"    {r['n']:>7}  {r['c'] or '(uncategorized)'}")
    # a couple of uncategorized examples with their stored specs
    try:
        ex = conn.execute(
            "SELECT id, mpn, subcategory, description FROM parts "
            "WHERE lower(vendor) LIKE '%mini%circuit%' "
            "AND (category IS NULL OR category='') LIMIT 5").fetchall()
    except Exception:
        ex = []
    if ex:
        print(f"\n  uncategorized examples:")
        for r in ex:
            specs = conn.execute(
                "SELECT key, value_text, value_typ FROM specs WHERE part_id=?",
                (r["id"],)).fetchall()
            got = {s["key"]: (s["value_text"] if s["value_text"] is not None
                              else s["value_typ"]) for s in specs}
            print(f"    {r['mpn']:<20} group={r['subcategory']!r:<18} {got}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--json", default=None, help="path to the catalogue JSON")
    ap.add_argument("--show", default=None,
                    help="dump raw records for this cat code")
    ap.add_argument("--samples", type=int, default=2)
    ap.add_argument("--top", type=int, default=40)
    ap.add_argument("--db-only", action="store_true")
    ap.add_argument("--json-only", action="store_true")
    a = ap.parse_args(argv)

    if not a.db_only:
        path = find_json(a.json)
        if not path:
            print("Could not find a *minicircuit*.json under Sources.\n"
                  "Pass --json with the full path.")
        else:
            report_json(path, show=a.show, samples=a.samples, top=a.top)
    if not a.json_only:
        report_db()
    print("\nPaste the UNMAPPED lines and one sample record per code, and I will "
          "add them to _CATEGORY_MAP.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
