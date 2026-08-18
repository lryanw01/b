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


def fingerprint(path, max_rows=400):
    """A short structural signature, plus the facts behind it."""
    try:
        rows = dsmine.rows_for(path)
    except Exception as e:
        return "ERROR", {"note": f"{type(e).__name__}"}
    if not rows:
        try:
            txt = dsmine.datasheet_text(path)
        except Exception:
            txt = ""
        if not txt.strip():
            return "NO-TEXT", {"note": "nothing extracted"}
        return "NO-TABLE", {"note": f"{len(txt)} chars, no table rows"}

    rows = rows[:max_rows]
    feats = {"rows": len(rows)}
    header_kind = None
    label_pos = None
    cond_kind = None

    widths = Counter(len(c) for _p, c in rows if c)
    feats["cols"] = widths.most_common(1)[0][0] if widths else 0

    has_minmax = has_temp = has_symbol = has_units = has_cond = False
    has_subport = band_in_cond = heading_above = centred_label = False
    unit_first = value_first = False

    for idx, (_p, cells) in enumerate(rows):
        toks = [str(c or "").strip() for c in cells]
        if not toks:
            continue
        if any(_MIN.match(x) for x in toks) and any(_TYP.match(x) or
                                                    _MAX.match(x)
                                                    for x in toks):
            has_minmax = True
            if any(_PARAM.match(x) for x in toks[:1]):
                header_kind = "param/min/typ/max"
        if sum(1 for x in toks if _TEMP.match(x)) >= 2:
            has_temp = True
        if any(_SYMBOL.match(x) for x in toks):
            has_symbol = True
        if any(_UNITS.match(x) for x in toks):
            has_units = True
        if any(_COND.match(x) for x in toks):
            has_cond = True
        if any(_SUBPORT.match(x) for x in toks[:2]):
            has_subport = True
        bands = [x for x in toks[:3] if dsmine.parse_band_cell(x)]
        if bands:
            band_in_cond = True
            # Where does this band row's name live?
            own = _SPECWORD.search(toks[0] or "")
            if own:
                centred_label = True
            else:
                for back in range(1, 4):
                    if idx - back < 0:
                        break
                    prev = rows[idx - back][1] or []
                    if prev and _SPECWORD.search(str(prev[0] or "")) \
                            and not any(dsmine.parse_band_cell(x)
                                        for x in list(prev)[:3]):
                        heading_above = True
                        break
        # spec names spread ACROSS a row is the column-oriented layout
        if sum(1 for x in toks if _SPECWORD.search(x)) >= 3:
            header_kind = header_kind or "spec-names-as-columns"
        # "Gain 22 dB" vs "Gain dB 22"
        joined = " ".join(toks[:4])
        if re.search(r"[A-Za-z]\s+[-+]?\d+(?:\.\d+)?\s*(dB|dBm|GHz|MHz|ns)\b",
                     joined):
            value_first = True
        if re.search(r"(dB|dBm|GHz|MHz|ns)\s+[-+]?\d+(?:\.\d+)?", joined):
            unit_first = True

    if header_kind is None:
        if has_temp:
            header_kind = "temperature-columns"
        elif has_symbol:
            header_kind = "symbol-row(Fc/F1-F2)"
        elif has_minmax:
            header_kind = "min/typ/max (no Parameter)"
        else:
            header_kind = "no-header"

    if centred_label and heading_above:
        label_pos = "mixed"
    elif heading_above:
        label_pos = "heading-above"
    elif centred_label:
        label_pos = "in-row"
    else:
        label_pos = "col0"

    cond_kind = "band" if band_in_cond else ("cond-col" if has_cond else "none")

    order = ("value-first" if value_first and not unit_first else
             "unit-first" if unit_first and not value_first else
             "both" if unit_first and value_first else "-")

    sig = (f"{header_kind} | label:{label_pos} | cond:{cond_kind} | "
           f"{order}" + (" | subports" if has_subport else "")
           + (" | units-col" if has_units else ""))
    feats.update({"header": header_kind, "label": label_pos, "cond": cond_kind,
                  "order": order, "subports": has_subport,
                  "units_col": has_units})
    return sig, feats


def vendor_dirs():
    out = []
    for root in dsmine.default_roots():
        r = Path(root)
        if r.is_dir():
            out += [d for d in sorted(r.iterdir()) if d.is_dir()]
    return out


def files_in(d, limit=0):
    fs = [f for f in d.rglob("*") if f.is_file() and f.suffix.lower() in EXTS]
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
        dirs = [d for d in dirs if args.vendor.lower() in d.name.lower()]
    if not dirs:
        print("No vendor folders found under the datasheet library.")
        return 1

    rows_out = []
    for d in dirs:
        files = files_in(d, args.sample)
        if not files:
            continue
        print(f"\n{'=' * 76}")
        print(f"  {d.name}   {len(files)} datasheet(s) sampled")
        print(f"{'=' * 76}")
        groups = defaultdict(list)
        for i, f in enumerate(files, 1):
            sig, _feats = fingerprint(f)
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
            rows_out.append({"vendor": d.name, "n": len(hits),
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
