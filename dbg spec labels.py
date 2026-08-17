"""dbg_spec_labels.py — what spec NAMES appear in each category's datasheets.

    python dbg_spec_labels.py
    python dbg_spec_labels.py --per-category 40 --top 30
    python dbg_spec_labels.py --categories mixer switch --show-raw
    python dbg_spec_labels.py --csv labels.csv

Reads datasheets grouped by the pipeline's own category, pulls the LABEL of every
spec-table row it can find, and reports which labels each category actually uses.
Values are ignored entirely -- the question here is what to look for, not what the
answer is.

WHY
    One set of patterns cannot serve every category. A mixer states conversion
    loss and three separate isolations (L-R, L-I, R-I) and three separate port
    frequencies; a phase shifter states phase range, resolution and RMS error; a
    coupler states coupling, directivity and mainline loss. Matching all of them
    with the same handful of regexes finds the specs the categories share and
    misses everything that makes them different.

    Port qualifiers are treated as part of the name, because they are: "RF
    frequency" and "IF frequency" are two specs, not one spec seen twice, and
    collapsing them loses the distinction the part is sold on.

Read-only: no database writes, no files touched.
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
    for pkg in ("pythonrfparts", "rfparts"):
        try:
            return __import__(f"{pkg}.{name}", fromlist=[name])
        except Exception:
            continue
    return None


dsmine = _opt("dsmine")
partdb = _opt("partdb")
specmatch = _opt("specmatch")
registry = _opt("registry")
if dsmine is None or partdb is None:
    sys.exit("Could not import dsmine/partdb. Run this from the folder that "
             "holds the package.")

CATEGORIES = ["amplifier", "mixer", "filter", "attenuator", "divider",
              "coupler", "switch", "phase_shifter"]

# Units that mark a table cell. A label is what sits in front of one of these.
UNITS = (r"dBm|dBc/Hz|dBc|dB|GHz|MHz|kHz|Hz|nsec|ns|psec|ps|usec|us|msec|ms|"
         r"mA|uA|A|mW|W|kW|mV|V|Ohms?|\u03a9|deg|degrees|\u00b0|%|dBm/Hz|"
         r"ratio|VSWR|:1")

# Rows in a flattened spec table: a label, then a unit, then numbers. PDF text
# extraction collapses columns onto one line, so the unit is the anchor that
# survives -- the column headers do not.
_ROW_UNIT_FIRST = re.compile(
    rf"([A-Za-z][A-Za-z0-9 ,()/&'.\-\u2013+]{{2,44}}?)\s*"
    rf"[\(\[]?\s*({UNITS})\s*[\)\]]?\s*[:=]?\s+[-+]?\d", re.I)
# and the other order: "Gain 22 dB"
_ROW_VALUE_FIRST = re.compile(
    rf"([A-Za-z][A-Za-z0-9 ,()/&'.\-\u2013+]{{2,44}}?)\s*[:=]?\s*"
    rf"[-+]?\d+(?:\.\d+)?\s*({UNITS})\b", re.I)

# Qualifiers that describe the measurement, not the spec. Stripped so "Gain
# (min)", "Gain (typ.)" and "Gain" count as one label.
_QUAL = re.compile(
    r"\b(min|max|typ|typical|nom|nominal|avg|average|peak|rms|"
    r"guaranteed|meas(?:ured)?|spec|specified|units?|note\s*\d*|"
    r"over\s+temp(?:erature)?|worst\s*case)\b\.?", re.I)
_PAREN_EMPTY = re.compile(r"\(\s*\)|\[\s*\]")
_LEAD_JUNK = re.compile(r"^[\s\-\u2013.,;:*|/()\[\]]+|[\s\-\u2013.,;:*|/]+$")

# Port and path qualifiers. These are KEPT -- they make separate specs.
_PORTS = [
    (r"\bL\s*-\s*R\b|\bLO\s*(?:to|-)\s*RF\b", "LO-RF"),
    (r"\bL\s*-\s*I\b|\bLO\s*(?:to|-)\s*IF\b", "LO-IF"),
    (r"\bR\s*-\s*I\b|\bRF\s*(?:to|-)\s*IF\b", "RF-IF"),
    (r"\bRF\b", "RF"), (r"\bLO\b|\blocal\s*oscillator\b", "LO"),
    (r"\bIF\b|\bintermediate\s*freq", "IF"),
    (r"\binput\b|\bin\b(?!ch)", "input"), (r"\boutput\b|\bout\b", "output"),
    (r"\bpassband\b|\bpass\s*band\b", "passband"),
    (r"\bstopband\b|\bstop\s*band\b", "stopband"),
    (r"\bmainline\b|\bthrough\b|\bthru\b", "mainline"),
    (r"\bcoupled\b", "coupled"), (r"\bisolated\b", "isolated"),
    (r"\bon\s*state\b|\binsertion\b", "on-state"),
    (r"\boff\s*state\b", "off-state"),
]
_PORTS = [(re.compile(p, re.I), tag) for p, tag in _PORTS]

# Lines that are prose or boilerplate rather than a spec row.
_NOT_A_SPEC = re.compile(
    r"\b(rev|revision|page|figure|fig|table|note|www|http|copyright|"
    r"all rights|patent|ordering|part number|model|case style|tel|fax|"
    r"email|features|applications|description|typical performance|"
    r"specifications subject|lead free|rohs|msl|esd|warning|caution)\b", re.I)


# Words that never appear in a spec name but are everywhere in prose. A label
# containing one is a sentence fragment the unit-anchored regex happened to end
# on -- "The HMC900LP5E is ... 50 MHz" looks exactly like a table row otherwise.
_PROSE = re.compile(
    r"\b(the|this|that|these|those|is|are|was|were|be|been|it|its|which|"
    r"includes?|provides?|allows?|offers?|designed|available|shown|see|"
    r"when|where|while|from|into|drive|street|road|suite|inc|ltd|corp)\b",
    re.I)
# Trailing connectives left behind when a row is cut mid-phrase: "at IF freq of".
_TRAIL = re.compile(r"\s+(at|of|for|to|in|on|with|per|and|or|vs|versus|by)$",
                    re.I)
_LEAD = re.compile(r"^(at|of|for|to|in|on|with|per|and|or|the|a|an)\s+", re.I)


def clean_label(raw):
    """A comparable spec name, or None if it is not one."""
    s = _PAREN_EMPTY.sub(" ", _QUAL.sub(" ", str(raw or "")))
    s = re.sub(r"\s+", " ", s)
    s = _LEAD_JUNK.sub("", s)
    if len(s) < 3 or len(s) > 44:
        return None
    if _NOT_A_SPEC.search(s):
        return None
    if not re.search(r"[A-Za-z]{3}", s):
        return None
    if _PROSE.search(s):
        return None
    for _ in range(3):
        s2 = _LEAD.sub("", _TRAIL.sub("", s)).strip()
        if s2 == s:
            break
        s = s2
    if len(s) < 4 or len(s) > 44:
        return None
    # A spec name is a noun phrase, not a clause: more than five words means the
    # regex ran past the label into the sentence around it.
    if len(s.split()) > 5:
        return None
    # Address and sentence punctuation.
    if re.search(r"[.,;]\s*[a-z]", s) or s.count(",") > 1:
        return None
    # A label that is mostly digits is a value that swallowed its neighbour.
    if sum(c.isdigit() for c in s) > len(s) / 3:
        return None
    return s.lower()


def ports_in(label):
    tags = []
    for rx, tag in _PORTS:
        if rx.search(label):
            tags.append(tag)
    # LO-RF implies LO and RF; keep only the most specific.
    for pair in ("LO-RF", "LO-IF", "RF-IF"):
        if pair in tags:
            return [pair]
    return tags[:2]


def resolved_key(label):
    """What the current pipeline would call this label, if anything."""
    if specmatch is None:
        return None
    try:
        return specmatch.resolve_key(label)
    except Exception:
        return None


def labels_in(text):
    """{clean label: raw form} for every spec-table row in a datasheet."""
    found = {}
    for rx in (_ROW_UNIT_FIRST, _ROW_VALUE_FIRST):
        for m in rx.finditer(text or ""):
            raw = m.group(1).strip()
            lab = clean_label(raw)
            if lab:
                found.setdefault(lab, raw)
    return found


def category_index():
    out = {}
    try:
        for r in partdb.db().execute(
                "SELECT mpn, category FROM parts WHERE category IS NOT NULL "
                "AND category != ''"):
            out.setdefault(re.sub(r"[^A-Z0-9]", "", r["mpn"].upper()),
                           r["category"])
    except Exception:
        pass
    return out


def library_files():
    files = []
    for root in dsmine.default_roots():
        try:
            for f in Path(root).rglob("*"):
                if f.is_file() and f.suffix.lower() in (".pdf", ".html", ".htm",
                                                        ".txt"):
                    files.append(f)
        except OSError:
            continue
    return files


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--categories", nargs="*", default=CATEGORIES)
    ap.add_argument("--per-category", type=int, default=30,
                    help="datasheets to read per category (default 30)")
    ap.add_argument("--top", type=int, default=25,
                    help="labels to list per category (default 25)")
    ap.add_argument("--min-count", type=int, default=2,
                    help="ignore labels seen in fewer datasheets than this")
    ap.add_argument("--show-raw", action="store_true",
                    help="also show the raw text each label came from")
    ap.add_argument("--unmapped-only", action="store_true",
                    help="only labels the pipeline does not already resolve")
    ap.add_argument("--csv", default=None)
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args(argv)

    random.seed(args.seed)
    index = category_index()
    files = library_files()
    print(f"library : {len(files)} datasheet(s)")
    print(f"database: {len(index)} part(s) with a category")

    buckets = defaultdict(list)
    for f in files:
        cat = index.get(re.sub(r"[^A-Z0-9]", "", f.stem.upper()))
        if cat in args.categories:
            buckets[cat].append(f)
    print("available per category: "
          + ", ".join(f"{c} {len(buckets.get(c) or [])}"
                      for c in args.categories))

    rows = []
    for cat in args.categories:
        pool = buckets.get(cat) or []
        if not pool:
            print(f"\n{'=' * 74}\n  {cat.upper()} -- no datasheets\n{'=' * 74}")
            continue
        sample = random.sample(pool, min(args.per_category, len(pool)))
        counts, raws, read, empty = Counter(), {}, 0, 0
        for f in sample:
            try:
                text = dsmine.datasheet_text(f)
            except Exception:
                continue
            if not text.strip():
                empty += 1
                continue
            read += 1
            for lab, raw in labels_in(text).items():
                counts[lab] += 1
                raws.setdefault(lab, raw)
        print(f"\n{'=' * 74}")
        print(f"  {cat.upper()}   {read} datasheet(s) read"
              + (f", {empty} unreadable" if empty else ""))
        print(f"{'=' * 74}")
        print(f"  {'n':>4}  {'%':>4}  {'label':<34}{'ports':<10}maps to")
        print("  " + "-" * 70)
        shown = 0
        for lab, n in counts.most_common():
            if n < args.min_count or shown >= args.top:
                continue
            key = resolved_key(lab)
            if args.unmapped_only and key:
                continue
            pct = 100 * n / max(1, read)
            ports = ",".join(ports_in(lab))
            print(f"  {n:>4}  {pct:>3.0f}%  {lab[:34]:<34}{ports[:10]:<10}"
                  f"{key or '-'}")
            if args.show_raw:
                print(f"        raw: {raws.get(lab, '')[:60]!r}")
            shown += 1
            rows.append({"category": cat, "label": lab, "datasheets": n,
                         "pct": f"{pct:.0f}", "ports": ports,
                         "maps_to": key or "", "raw": raws.get(lab, "")})
        if not shown:
            print("  (nothing above the --min-count threshold)")

    if args.csv and rows:
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        print(f"\nfull table written to {args.csv} ({len(rows)} row(s))")

    print("""
  How to read this
    n / %        datasheets in that category carrying the label
    ports        port or path qualifier found in the name -- RF/LO/IF, in/out,
                 passband/stopband, mainline/coupled. These are SEPARATE specs
                 and each needs its own key.
    maps to      what the pipeline resolves the label to today; '-' means
                 nothing does, so the spec is currently invisible.

  Send this back and the per-category templates get built from it.""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
