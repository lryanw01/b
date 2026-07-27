"""Inspect the rfparts part database (read-only).

A diagnostic window into parts.db so you can see exactly what was stored and
why a search does or doesn't surface it — e.g. "I only find Mini-Circuits phase
shifters; where are my everythingRF space-qual ones?". This reads the RAW parts
table, NOT partdb.query_candidates(), so it shows the category/frequency exactly
as stored (which is what reveals a category or frequency mismatch).

Never writes. Opens the DB read-only.

Commands (default: summary):
    summary                      totals + breakdown by category / space / source
    categories                   every distinct category string, with counts
    vendors   [--category C]     vendor part counts
    list      [filters]          one row per part (mpn, vendor, category, space,
                                 freq, source)
    show MPN  [--vendor V]       full spec + evidence dump for one part

list filters (combine freely):
    --category C     canonical or loose ("phase shifter", "phase-shifter", "ps")
    --vendor V       substring, punctuation-insensitive ("mini circuits")
    --space X        space_qualified | space_grade | any-space | none
    --source S       everythingrf | satnow | digikey | other
    --freq LO-HI     parts whose stored band covers LO..HI GHz (e.g. 8-12)
    --mpn TEXT       MPN substring
    --limit N        cap rows (default 200)

Examples:
    python -m pythonrfparts.partdb_inspect categories
    python -m pythonrfparts.partdb_inspect list --category phase_shifter --source everythingrf
    python -m pythonrfparts.partdb_inspect list --space any-space --category phase_shifter
    python -m pythonrfparts.partdb_inspect show PS-2-250+ --vendor "Mini Circuits"
    python -m pythonrfparts.partdb_inspect --db "D:\\rfparts\\parts.db" summary
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
from pathlib import Path

# Default DB path from partdb (importing it does NOT open/create the DB).
try:
    from .partdb import DB_PATH as _DEFAULT_DB
except ImportError:
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from pythonrfparts.partdb import DB_PATH as _DEFAULT_DB   # type: ignore
    except Exception:
        _DEFAULT_DB = Path(os.environ.get("RFPARTS_HOME",
                           Path.home() / ".rfparts")) / "parts.db"

# Loose category input -> canonical key (mirrors registry/erf/catalog).
_CAT_ALIAS = {
    "amp": "amplifier", "amplifier": "amplifier", "lna": "amplifier",
    "att": "attenuator", "attenuator": "attenuator",
    "flt": "filter", "filter": "filter",
    "mix": "mixer", "mixer": "mixer",
    "cpl": "coupler", "coupler": "coupler", "directional coupler": "coupler",
    "spl": "divider", "divider": "divider", "splitter": "divider",
    "sw": "switch", "switch": "switch",
    "ps": "phase_shifter", "phase shifter": "phase_shifter",
    "phaseshifter": "phase_shifter",
    "lim": "limiter", "limiter": "limiter",
    "osc": "oscillator", "oscillator": "oscillator",
    "circulator": "circulator", "isolator": "isolator",
    "detector": "detector", "multiplier": "multiplier",
    "termination": "termination", "dc_block": "dc_block", "bias_tee": "bias_tee",
}


def _canon_cat(value):
    if not value:
        return None
    v = re.sub(r"[\s\-]+", " ", value.strip().lower())
    if v in _CAT_ALIAS:
        return _CAT_ALIAS[v]
    return v.replace(" ", "_")          # fall back to a plausible canonical form


def _norm_vendor(v):
    return re.sub(r"[^a-z0-9]", "", str(v or "").lower())


def _source(url):
    u = (url or "").lower()
    if "everythingrf" in u:
        return "everythingRF"
    if "satnow" in u:
        return "SATNow"
    if "digikey" in u or "digi-key" in u:
        return "DigiKey"
    return "other"


def _connect(path):
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"database not found: {p}\n"
                         f"(set RFPARTS_HOME or pass --db PATH)")
    try:
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        conn = sqlite3.connect(str(p))          # fallback if RO/URI unsupported
    conn.row_factory = sqlite3.Row
    return conn


# --- per-part spec helpers --------------------------------------------------

def _spec_map(conn, part_id):
    """key -> best display value for one part (text or numeric)."""
    out = {}
    for s in conn.execute("SELECT * FROM specs WHERE part_id=?", (part_id,)):
        k = s["key"]
        if s["value_text"] is not None:
            out.setdefault(k, s["value_text"])
        elif k == "freq_ghz" and s["value_min"] is not None and s["value_max"] is not None:
            out[k] = (s["value_min"], s["value_max"])
        else:
            v = s["value_typ"] if s["value_typ"] is not None else (
                s["value_min"] if s["value_min"] is not None else s["value_max"])
            if v is not None:
                out.setdefault(k, v)
    return out


def _freq_of(specs):
    fg = specs.get("freq_ghz")
    return fg if isinstance(fg, tuple) else None


def _fmt_freq(fg):
    return f"{fg[0]:g}-{fg[1]:g}" if fg else "—"


def _print_table(rows, headers):
    if not rows:
        print("  (none)")
        return
    widths = [len(h) for h in headers]
    for r in rows:
        for i, c in enumerate(r):
            widths[i] = max(widths[i], len(str(c)))
    line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    print(line)
    print("  ".join("-" * widths[i] for i in range(len(headers))))
    for r in rows:
        print("  ".join(str(c).ljust(widths[i]) for i, c in enumerate(r)))


# --- commands ---------------------------------------------------------------

def cmd_summary(conn, args):
    total = conn.execute("SELECT COUNT(*) FROM parts").fetchone()[0]
    print(f"parts.db: {args.db}")
    print(f"total parts: {total}\n")
    if not total:
        return

    print("by category:")
    rows = conn.execute(
        "SELECT CASE WHEN category='' THEN '(blank)' ELSE category END c, "
        "COUNT(*) n FROM parts GROUP BY category ORDER BY n DESC").fetchall()
    _print_table([(r["c"], r["n"]) for r in rows], ["category", "parts"])

    print("\nby space_variant:")
    rows = conn.execute(
        "SELECT COALESCE(value_text,'(none)') v, COUNT(*) n FROM specs "
        "WHERE key='space_variant' GROUP BY value_text ORDER BY n DESC").fetchall()
    tagged = sum(r["n"] for r in rows)
    _print_table([(r["v"], r["n"]) for r in rows]
                 + [("(untagged)", total - tagged)], ["space_variant", "parts"])

    print("\nby source (from product_url):")
    src = {}
    for r in conn.execute("SELECT product_url FROM parts"):
        src[_source(r["product_url"])] = src.get(_source(r["product_url"]), 0) + 1
    _print_table(sorted(src.items(), key=lambda x: -x[1]), ["source", "parts"])

    print("\ntop vendors:")
    rows = conn.execute(
        "SELECT CASE WHEN vendor='' THEN '(blank)' ELSE vendor END v, COUNT(*) n "
        "FROM parts GROUP BY vendor ORDER BY n DESC LIMIT 15").fetchall()
    _print_table([(r["v"], r["n"]) for r in rows], ["vendor", "parts"])


def cmd_categories(conn, args):
    rows = conn.execute(
        "SELECT CASE WHEN category='' THEN '(blank)' ELSE category END c, "
        "COUNT(*) n FROM parts GROUP BY category ORDER BY n DESC").fetchall()
    # annotate each category with everythingRF and space-tagged counts
    out = []
    for r in rows:
        cat = "" if r["c"] == "(blank)" else r["c"]
        erf = conn.execute(
            "SELECT COUNT(*) FROM parts WHERE category=? AND "
            "LOWER(product_url) LIKE '%everythingrf%'", (cat,)).fetchone()[0]
        spc = conn.execute(
            "SELECT COUNT(DISTINCT p.id) FROM parts p JOIN specs s ON s.part_id=p.id "
            "WHERE p.category=? AND s.key='space_variant'", (cat,)).fetchone()[0]
        out.append((r["c"], r["n"], erf, spc))
    _print_table(out, ["category", "parts", "fromERF", "space-tagged"])


def cmd_vendors(conn, args):
    cat = _canon_cat(args.category)
    if cat:
        rows = conn.execute(
            "SELECT CASE WHEN vendor='' THEN '(blank)' ELSE vendor END v, COUNT(*) n "
            "FROM parts WHERE category=? GROUP BY vendor ORDER BY n DESC", (cat,)).fetchall()
        print(f"vendors for category '{cat}':")
    else:
        rows = conn.execute(
            "SELECT CASE WHEN vendor='' THEN '(blank)' ELSE vendor END v, COUNT(*) n "
            "FROM parts GROUP BY vendor ORDER BY n DESC").fetchall()
        print("vendors (all categories):")
    _print_table([(r["v"], r["n"]) for r in rows], ["vendor", "parts"])
    if cat and not rows:
        _suggest_categories(conn, cat)


def _space_filter_ok(variant, want):
    if want in (None, ""):
        return True
    if want == "any-space":
        return bool(variant)
    if want == "none":
        return not variant
    return variant == want


def cmd_list(conn, args):
    cat = _canon_cat(args.category)
    where, params = [], []
    if cat:
        where.append("category=?")
        params.append(cat)
    if args.mpn:
        where.append("UPPER(mpn) LIKE ?")
        params.append(f"%{args.mpn.upper()}%")
    sql = "SELECT * FROM parts"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY vendor COLLATE NOCASE, mpn COLLATE NOCASE"

    want_vendor = _norm_vendor(args.vendor) if args.vendor else None
    space_want = _space_norm(args.space)
    src_want = args.source.lower() if args.source else None
    freq_band = _parse_band(args.freq) if args.freq else None

    rows, shown = [], 0
    for p in conn.execute(sql, params):
        if want_vendor and want_vendor not in _norm_vendor(p["vendor"]):
            continue
        specs = _spec_map(conn, p["id"])
        variant = specs.get("space_variant")
        if not _space_filter_ok(variant, space_want):
            continue
        source = _source(p["product_url"])
        if src_want and src_want not in source.lower():
            continue
        fg = _freq_of(specs)
        if freq_band:
            if not fg or fg[0] > freq_band[0] + 1e-9 or fg[1] < freq_band[1] - 1e-9:
                continue
        rows.append((p["mpn"], p["vendor"] or "—", p["category"] or "(blank)",
                     variant or "—", _fmt_freq(fg), source))
        shown += 1
        if shown >= args.limit:
            break
    _print_table(rows, ["mpn", "vendor", "category", "space_variant", "freq(GHz)", "source"])
    print(f"\n{shown} part(s) shown"
          + (f" (limit {args.limit})" if shown >= args.limit else ""))
    if cat and not rows:
        _suggest_categories(conn, cat)


def cmd_show(conn, args):
    q = "SELECT * FROM parts WHERE UPPER(REPLACE(mpn,' ',''))=?"
    params = [re.sub(r"\s+", "", args.mpn).upper()]
    if args.vendor:
        q += " AND vendor=?"
        params.append(args.vendor)
    parts = conn.execute(q, params).fetchall()
    if not parts:
        # loose fallback: substring
        parts = conn.execute(
            "SELECT * FROM parts WHERE UPPER(mpn) LIKE ?",
            (f"%{args.mpn.upper()}%",)).fetchall()
    if not parts:
        print(f"no part matching '{args.mpn}'")
        return
    for p in parts:
        print(f"\n=== {p['mpn']}  |  {p['vendor'] or '—'}  |  category={p['category'] or '(blank)'}"
              + (f" / {p['subcategory']}" if p["subcategory"] else "") + " ===")
        print(f"  url: {p['product_url'] or '—'}   source: {_source(p['product_url'])}")
        if p["description"]:
            print(f"  desc: {p['description'][:200]}")
        print("  specs:")
        srows = conn.execute(
            "SELECT key, value_min, value_typ, value_max, value_text, unit, "
            "method, confidence FROM specs WHERE part_id=? ORDER BY key", (p["id"],)).fetchall()
        for s in srows:
            if s["value_text"] is not None:
                val = s["value_text"]
            elif s["value_min"] is not None and s["value_max"] is not None:
                val = f"{s['value_min']:g}..{s['value_max']:g}"
            else:
                val = next((f"{s[c]:g}" for c in ("value_typ", "value_min", "value_max")
                            if s[c] is not None), "—")
            print(f"    {s['key']:<22} {val} {s['unit']}".rstrip()
                  + f"   [{s['method']}, conf {s['confidence']:g}]")
        ev = conn.execute(
            "SELECT signal, weight, snippet FROM qual_evidence WHERE part_id=? "
            "ORDER BY ABS(weight) DESC", (p["id"],)).fetchall()
        if ev:
            print("  qual_evidence:")
            for e in ev:
                print(f"    {e['signal']:<28} {e['weight']:+g}   {e['snippet'][:70]}")


def _suggest_categories(conn, missing):
    rows = conn.execute(
        "SELECT DISTINCT CASE WHEN category='' THEN '(blank)' ELSE category END c "
        "FROM parts ORDER BY c").fetchall()
    cats = [r["c"] for r in rows]
    print(f"\n  note: no parts stored under category '{missing}'.")
    print(f"  categories actually present: {', '.join(cats) or '(none)'}")
    print("  -> if your phase shifters landed under a different label (or "
          "'(blank)'), that's why the picker can't find them.")


def _space_norm(v):
    if not v:
        return None
    v = v.strip().lower().replace("-", "_")
    if v in ("qualified", "qual", "space_qualified"):
        return "space_qualified"
    if v in ("grade", "space_grade"):
        return "space_grade"
    if v in ("any", "any_space", "anyspace"):
        return "any-space"
    if v == "none":
        return "none"
    return v


def _parse_band(text):
    nums = re.findall(r"\d+(?:\.\d+)?", text or "")
    if len(nums) >= 2:
        a, b = float(nums[0]), float(nums[1])
        return (min(a, b), max(a, b))
    if len(nums) == 1:
        return (0.0, float(nums[0]))
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(_DEFAULT_DB), help="path to parts.db")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("summary", help="totals and breakdowns (default)")
    sub.add_parser("categories", help="distinct category strings with counts")

    v = sub.add_parser("vendors", help="vendor part counts")
    v.add_argument("--category")

    lp = sub.add_parser("list", help="filtered part rows")
    lp.add_argument("--category")
    lp.add_argument("--vendor")
    lp.add_argument("--space")
    lp.add_argument("--source")
    lp.add_argument("--freq")
    lp.add_argument("--mpn")
    lp.add_argument("--limit", type=int, default=200)

    sp = sub.add_parser("show", help="full spec/evidence dump for one part")
    sp.add_argument("mpn")
    sp.add_argument("--vendor")

    args = ap.parse_args(argv)
    conn = _connect(args.db)
    try:
        {"categories": cmd_categories, "vendors": cmd_vendors,
         "list": cmd_list, "show": cmd_show,
         "summary": cmd_summary, None: cmd_summary}[args.cmd](conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
