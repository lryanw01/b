"""Ingest downloaded everythingRF space catalogs into the rfparts part DB.

Reads the LOCAL HTML pages you already saved under a parent folder such as
``C:\\Users\\lane.white\\Downloads\\EverythingRFSpaceQual`` and writes every
part into partdb (the same SQLite the rest of the pipeline reads). No network
access, no crawling -- this only parses files on disk.

Folder convention (both are picked up):
    EverythingRFSpace<Component>       -> parts marked space GRADE
    EverythingRFSpaceQual<Component>   -> parts marked space QUALIFIED

Space grade vs space qualified is decided PER PART, strongest signal first:
    1. the everythingRF "Grade" attribute row ("Space Qualified" / "Military, Space")
    2. the listing nodeName ("Space Qualified RF Amplifier" vs "RF Amplifier")
    3. the page <h1> ("Space Qualified ..." vs "... - Space")
    4. the folder name  (backstop)
so a mis-filed page still classifies correctly, and a part that shows up in
both a qual and a grade listing keeps the QUALIFIED result.

What lands in the DB per part:
    parts row        mpn / vendor / category / subcategory / product_url / desc
    specs (numeric)  freq_ghz, gain_db, nf_db, p1db_dbm, oip3_dbm, power_w,
                     psat_dbm, insertion_loss_db, isolation_db, attenuation_db,
                     vswr, coupling_db, directivity_db, power_avg_w, impedance_ohm
    specs (text)     connector, mount_type, configuration, subtype,
                     industry_application, pulsed_cw, temp_c, erf_grade
    specs (space)    space = "qualified"           (both variants are space-ready)
                     space_variant = "space_qualified" | "space_grade"   <-- the differentiator
    qual_evidence    erf-space-qualified-listing (+9) | erf-space-grade-listing (+7)
                     plus the raw Grade / nodeName strings as provenance

Why space="qualified" for BOTH: rank.py only scores "qualified"/"hi_rel"/
"qualifiable", so writing "qualified" keeps grade parts surfacing as space-ready
and passing the hard space check. The qual-vs-grade preference lives in
``space_variant`` -- to make "space qual slightly higher than space grade" take
effect in ranking, teach rank.py one line, e.g.:

    _VARIANT_SCORE = {"space_qualified": 6, "space_grade": 5}
    candidate["space_score"] = _VARIANT_SCORE.get(
        s.get("space_variant"), _SPACE_SCORE.get(s.get("space"), 0))

Run:
    python -m pythonrfparts.erf_space_ingest
    python -m pythonrfparts.erf_space_ingest "D:\\some\\other\\parent" --dry-run
"""
from __future__ import annotations

import argparse
import re
from collections import Counter
from pathlib import Path

from bs4 import BeautifulSoup

# Dual-mode import: works as `python -m pythonrfparts.erf_space_ingest` and as
# `python pythonrfparts\erf_space_ingest.py`.
try:
    from .partdb import SpecRow, upsert_part, put_specs, put_evidence
except ImportError:                                    # run as a loose script
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from pythonrfparts.partdb import (                 # type: ignore
        SpecRow, upsert_part, put_specs, put_evidence)

BASE = "https://www.everythingrf.com"
DEFAULT_PARENT = r"C:\Users\lane.white\Downloads\EverythingRFSpaceQual"
FOLDER_GLOB = "EverythingRFSpace*"          # matches Space* and SpaceQual*

# everythingRF listing attribute label -> (spec key, kind).
# kind: "freq" -> GHz min/max ; "num" -> numeric (range=min/max, single=typ,
# keeps unit) ; "text" -> stored verbatim.
_ATTRS = {
    "frequency": ("freq_ghz", "freq"),
    "gain": ("gain_db", "num"),
    "noise figure": ("nf_db", "num"),
    "p1db": ("p1db_dbm", "num"),
    "oip3": ("oip3_dbm", "num"),
    "output power": ("power_w", "num"),
    "average power": ("power_avg_w", "num"),
    "saturated power": ("psat_dbm", "num"),
    "insertion loss": ("insertion_loss_db", "num"),
    "isolation": ("isolation_db", "num"),
    "attenuation": ("attenuation_db", "num"),
    "coupling": ("coupling_db", "num"),
    "directivity": ("directivity_db", "num"),
    "vswr": ("vswr", "num"),
    "impedance": ("impedance_ohm", "num"),
    "connector": ("connector", "text"),
    "configuration": ("configuration", "text"),
    "package type": ("package", "text"),
    "type": ("subtype", "text"),
    "sub-category": ("subcategory_text", "text"),
    "industry application": ("industry_application", "text"),
    "pulsed/cw": ("pulsed_cw", "text"),
    "operating temperature": ("temp_c", "text"),
}

# everythingRF product-URL slug -> pipeline canonical category.
_SLUG_CAT = {
    "microwave-rf-amplifiers": "amplifier",
    "rf-attenuators": "attenuator",
    "rf-filters": "filter",
    "rf-mixers": "mixer",
    "rf-switches": "switch",
    "rf-directional-couplers": "coupler",
    "rf-couplers": "coupler",
    "rf-power-dividers": "divider",
    "phase-shifters": "phase_shifter",
    "rf-limiters": "limiter",
    "rf-oscillators": "oscillator",
    "rf-circulators": "circulator",
    "rf-isolators": "isolator",
    "rf-detectors": "detector",
    "frequency-multipliers": "multiplier",
    "rf-terminations": "termination",
    "dc-blocks": "dc_block",
    "bias-tees": "bias_tee",
}

_RANGE = re.compile(
    r"([-+]?\d+(?:\.\d+)?)\s*(?:to|–|—|-)\s*([-+]?\d+(?:\.\d+)?)")
_SINGLE = re.compile(r"([-+]?\d+(?:\.\d+)?)")
_UNIT = re.compile(r"(GHz|MHz|kHz|dBm|dBc|dBi|dB|mW|kW|W|Ω|ohms?|°?\s*C)", re.I)
_PROD = re.compile(r"/products/([^/]+)/")           # category slug in the URL


def _to_ghz(val: float, unit: str | None) -> float:
    u = (unit or "GHz").lower()
    if u == "mhz":
        return val / 1e3
    if u == "khz":
        return val / 1e6
    return val


def _norm_unit(u: str) -> str:
    if not u:
        return ""
    u = u.strip()
    if re.fullmatch(r"°?\s*C", u, re.I):
        return "C"
    if re.fullmatch(r"ohms?", u, re.I) or u == "Ω":
        return "ohm"
    return u


def _parse_value(kind: str, text: str) -> dict | None:
    """everythingRF cell text -> SpecRow numeric kwargs, or None."""
    t = (text or "").strip()
    if not t:
        return None
    if kind == "text":
        return {"value_text": t[:200]}
    um = _UNIT.search(t)
    unit = _norm_unit(um.group(1)) if um else ""
    rng = _RANGE.search(t)
    if kind == "freq":
        if rng:
            return {"value_min": _to_ghz(float(rng.group(1)), unit or "GHz"),
                    "value_max": _to_ghz(float(rng.group(2)), unit or "GHz"),
                    "unit": "GHz"}
        one = _SINGLE.search(t)
        if one:                       # "DC to 7 GHz" style single upper bound
            v = _to_ghz(float(one.group(1)), unit or "GHz")
            lo = 0.0 if re.search(r"\bdc\b", t, re.I) else v
            return {"value_min": min(lo, v), "value_max": max(lo, v), "unit": "GHz"}
        return None
    if rng:
        return {"value_min": float(rng.group(1)),
                "value_max": float(rng.group(2)), "unit": unit}
    one = _SINGLE.search(t)
    if one:
        return {"value_typ": float(one.group(1)), "unit": unit}
    return None


def _abs_url(href: str) -> str:
    """Normalise a saved relative href to an absolute everythingRF URL."""
    if not href:
        return ""
    href = href.strip()
    if href.startswith("http"):
        return href.split("?utm_source")[0]
    href = re.sub(r"^(?:\.\./)+", "/", href)          # ../../products -> /products
    if not href.startswith("/"):
        href = "/" + href
    return BASE + href


def _category(url: str, node_text: str, header: str, title: str) -> str:
    m = _PROD.search(url or "")
    if m and m.group(1) in _SLUG_CAT:
        return _SLUG_CAT[m.group(1)]
    # nodeName ("Space Qualified Band Pass Filter") names the component type
    # reliably even when the URL slug is per-response-type and the title/header
    # carry no category word — this is what fixes filters landing under a blank
    # category and vanishing from a filter search.
    blob = f"{node_text} {header} {title}".lower()
    for key, canon in (
        ("attenuator", "attenuator"), ("termination", "termination"),
        ("power divider", "divider"), ("power splitter", "divider"),
        ("combiner", "divider"), ("coupler", "coupler"),
        ("circulator", "circulator"), ("isolator", "isolator"),
        ("mixer", "mixer"), ("phase shifter", "phase_shifter"),
        ("multiplier", "multiplier"), ("oscillator", "oscillator"),
        ("limiter", "limiter"), ("detector", "detector"),
        ("diplexer", "filter"), ("duplexer", "filter"),
        ("switch", "switch"), ("filter", "filter"),
        ("amplifier", "amplifier"),
    ):
        if key in blob:
            return canon
    return ""


def _amp_subcategory(subtype: str) -> str:
    # keys must match registry.subcategories("amplifier")
    s = (subtype or "").lower()
    if "low noise" in s or re.search(r"\blna\b", s):
        return "lna"
    if "high power" in s or re.search(r"\bhpa\b", s):
        return "hpa"
    if "power amplifier" in s or re.search(r"\bpa\b", s):
        return "pa"
    if "variable gain" in s or re.search(r"\bvga\b", s):
        return "vga"
    if "driver" in s or "gain block" in s:
        return "driver"
    return ""


_MOUNT = [
    ("surface mount", "smt"), ("smt", "smt"), ("ic/mmic", "smt"),
    ("die", "die"), ("bare die", "die"), ("chip", "die"),
    ("connector", "connectorized"), ("coax", "connectorized"),
    ("flange", "flange"), ("drop-in", "drop_in"), ("module", "module"),
    ("waveguide", "waveguide"),
]


def _mount_type(configuration: str, package: str) -> str:
    blob = f"{configuration} {package}".lower()
    for key, canon in _MOUNT:
        if key in blob:
            return canon
    return ""


def _compact(s: str) -> str:
    return re.sub(r"\s+", "", (s or "").lower())


def classify_space(grade_text: str, node_text: str,
                   header_text: str, folder_name: str):
    """Return ("space_qualified"|"space_grade", reason) from strongest signal."""
    for src, label in ((grade_text, "grade-field"),
                       (node_text, "node-name"),
                       (header_text, "page-header"),
                       (folder_name, "folder-name")):
        c = _compact(src)
        if "space" not in c:
            continue
        if "spacequal" in c:                          # "Space Qualified"
            return "space_qualified", f"{label}: {src.strip()}"
        return "space_grade", f"{label}: {src.strip()}"
    return None, ""


# Frequency lives under different labels depending on the part type. Range-style
# labels (a passband) are preferred; a centre + bandwidth is turned into a
# passband; a lone centre/cutoff is taken as a point so the part still groups.
# NOTE: "Bandwidth" is a WIDTH, not a frequency range — treating it as the band
# corrupted band-pass filters (they list Center Frequency + Bandwidth), which is
# why real BPFs vanished from passband searches.
_FREQ_RANGE_LABELS = (
    "frequency", "frequency range", "operating frequency", "operating frequency range",
    "passband", "passband frequency", "pass band", "tunable frequency",
    "tuning range", "rf frequency")
_FREQ_POINT_LABELS = (
    "center frequency", "centre frequency", "cutoff frequency",
    "cut-off frequency", "cut off frequency", "notch frequency")


def _scalar_ghz(text):
    """A single frequency value in GHz (max endpoint if a range slips in)."""
    p = _parse_value("freq", text)
    if not p:
        return None
    lo, hi = p.get("value_min"), p.get("value_max")
    return hi if hi is not None else lo


def _bandwidth_ghz(text, center):
    """Bandwidth as a width in GHz. Handles absolute ('200 MHz') and percentage
    ('5 %', needs a centre) forms."""
    t = (text or "").strip()
    if not t:
        return None
    if "%" in t:
        m = re.search(r"(\d+(?:\.\d+)?)", t)
        return center * float(m.group(1)) / 100.0 if (m and center) else None
    p = _parse_value("freq", t)
    if not p:
        return None
    lo, hi = p.get("value_min"), p.get("value_max")
    if lo is None or hi is None:
        return None
    return (hi - lo) if hi > lo else hi          # a range -> width; a lone value -> itself


def _freq_rows(attrs, url):
    """freq_ghz (and bandwidth_ghz where present) SpecRows for a part.

    Resolution order: explicit passband range -> any frequency/passband field
    (never 'bandwidth') -> centre+bandwidth computed passband -> centre/cutoff
    point."""
    def _fr(label, kwargs):
        return SpecRow(key="freq_ghz", method="aggregator", confidence=0.75,
                       source_url=url, snippet=f"{label}: {attrs.get(label, '')}"[:150],
                       **kwargs)

    freq = None
    for lbl in _FREQ_RANGE_LABELS:                       # 1) explicit range
        if lbl in attrs:
            p = _parse_value("freq", attrs[lbl])
            if p:
                freq = _fr(lbl, p)
                break
    if freq is None:                                     # 2) generic freq/passband
        for lbl, val in attrs.items():
            if "bandwidth" in lbl or lbl in _FREQ_POINT_LABELS:
                continue
            if "frequency" in lbl or "passband" in lbl:
                p = _parse_value("freq", val)
                if p:
                    freq = _fr(lbl, p)
                    break

    center_lbl = next((l for l in _FREQ_POINT_LABELS
                       if l in attrs and ("center" in l or "centre" in l)), None)
    center = _scalar_ghz(attrs[center_lbl]) if center_lbl else None
    bw_lbl = next((l for l in attrs if "bandwidth" in l), None)
    bw = _bandwidth_ghz(attrs[bw_lbl], center) if bw_lbl else None

    if freq is None and center is not None:              # 3) centre (+ bandwidth)
        if bw:
            freq = _fr(center_lbl, {"value_min": round(max(0.0, center - bw / 2), 6),
                                    "value_max": round(center + bw / 2, 6), "unit": "GHz"})
        else:
            freq = _fr(center_lbl, {"value_min": center, "value_max": center, "unit": "GHz"})
    if freq is None:                                     # 4) any point label
        for lbl in _FREQ_POINT_LABELS:
            if lbl in attrs:
                p = _parse_value("freq", attrs[lbl])
                if p:
                    freq = _fr(lbl, p)
                    break

    rows = []
    if freq is not None:
        rows.append(freq)
    if bw_lbl and bw:
        rows.append(SpecRow(key="bandwidth_ghz", value_typ=round(bw, 6), unit="GHz",
                            method="aggregator", confidence=0.7, source_url=url,
                            snippet=f"{bw_lbl}: {attrs[bw_lbl]}"[:150]))
    return rows


# everythingRF puts a divider's way-count in a structured field (label varies)
# rather than always in the description, so read the field first and fall back to
# "N-way" phrasing in the type/config/description/nodeName/title.
_WAYS_INT_LABELS = (
    "no. of ways", "number of ways", "no of ways", "no.of ways", "ways",
    "no. of way", "no. of outputs", "number of outputs", "outputs",
    "no. of channels", "number of channels")
_WAY_PHRASE = re.compile(r"(\d+)\s*-?\s*way\b", re.I)


def _ways_of(attrs, *texts):
    """Number of ways for a divider/combiner, or None."""
    for lbl in _WAYS_INT_LABELS:
        if lbl in attrs:
            m = re.search(r"\d+", attrs[lbl])
            if m:
                return int(m.group())
    blob = " ".join([attrs.get("type", ""), attrs.get("configuration", ""),
                     attrs.get("sub-category", "")] + [t for t in texts if t])
    m = _WAY_PHRASE.search(blob)
    return int(m.group(1)) if m else None


def _filter_subcategory(*texts):
    """Filter response/type from the nodeName, Type, sub-category or title."""
    t = " ".join(x for x in texts if x).lower()
    if "band pass" in t or "bandpass" in t or re.search(r"\bbpf\b", t):
        return "bpf"
    if ("band stop" in t or "bandstop" in t or "band reject" in t or "notch" in t
            or re.search(r"\bbsf\b", t)):
        return "bsf"
    if "low pass" in t or "lowpass" in t or re.search(r"\blpf\b", t):
        return "lpf"
    if "high pass" in t or "highpass" in t or re.search(r"\bhpf\b", t):
        return "hpf"
    if "diplexer" in t:
        return "diplexer"
    if "duplexer" in t:
        return "duplexer"
    if "tunable" in t:
        return "tunable"
    if "cavity" in t:
        return "cavity"
    if "ceramic" in t:
        return "ceramic"
    return ""


def _page_header(soup: BeautifulSoup) -> str:
    for tag in soup.find_all(["h1", "h2"]):
        tx = tag.get_text(" ", strip=True)
        if tx:
            return tx
    return ""


def parse_page(html: str, folder_name: str):
    """Yield one dict per part on a saved everythingRF listing page."""
    soup = BeautifulSoup(html, "html.parser")
    header = _page_header(soup)
    for box in soup.find_all("div", class_="product-box"):
        link = box.find("a", href=re.compile(r"/products/"))
        if not link:
            continue
        url = _abs_url(link.get("href", ""))

        title_a = box.find("h3", class_="prod-title")
        tl = title_a.find("a") if title_a else None
        mpn = (tl.get("dname") if tl else "") or ""
        mpn = re.sub(r"\s*\((?:obsolete|new|featured)\)\s*", "", mpn, flags=re.I).strip()
        if not mpn and tl:
            txt = tl.get_text(" ", strip=True)
            mpn = txt.split("-", 1)[-1].strip() if "-" in txt else txt
        if not mpn:
            continue

        mfr = box.find("a", class_=re.compile("manuName"))
        vendor = ((mfr.get("manu-name") if mfr else "") or
                  (mfr.get_text(strip=True) if mfr else "")).strip()
        node = box.find("span", class_="nodeName")
        node_text = node.get_text(" ", strip=True) if node else ""
        desc_d = box.find("div", class_="descriptiontext")
        description = desc_d.get_text(" ", strip=True) if desc_d else ""

        attrs, rows = {}, []
        for dr in box.find_all("div", class_="data-row"):
            a_d, v_d = dr.find("div", class_="attribute"), dr.find("div", class_="value")
            if not (a_d and v_d):
                continue
            label = a_d.get_text(strip=True).rstrip(":").lower()
            value = v_d.get_text(" ", strip=True)
            if not value:
                continue
            attrs[label] = value
            mapped = _ATTRS.get(label)
            if not mapped:
                continue
            key, kind = mapped
            if kind == "freq":
                continue          # frequency handled centrally below (synonyms)
            parsed = _parse_value(kind, value)
            if parsed:
                rows.append(SpecRow(key=key, method="aggregator", confidence=0.75,
                                    source_url=url,
                                    snippet=f"{label}: {value}"[:150], **parsed))

        # Frequency, resolved across label variants (Frequency / Tunable
        # Frequency / Passband / centre+bandwidth) — never mistaking Bandwidth
        # for the band.
        rows.extend(_freq_rows(attrs, url))

        title = tl.get_text(" ", strip=True) if tl else mpn
        category = _category(url, node_text, header, title)
        if category == "amplifier":
            subcategory = _amp_subcategory(attrs.get("type", ""))
        elif category == "filter":
            subcategory = _filter_subcategory(node_text, attrs.get("type", ""),
                                              attrs.get("sub-category", ""),
                                              description, title)
        else:
            subcategory = ""
        mount = _mount_type(attrs.get("configuration", ""), attrs.get("package type", ""))

        # Number of ways (2/4/8-way) for dividers/combiners, so ranking can order
        # them — the count is usually a structured field, not the description.
        if category == "divider":
            ways = _ways_of(attrs, description, node_text, title)
            if ways:
                rows.append(SpecRow(key="no_of_ways", value_typ=float(ways),
                                    method="aggregator", confidence=0.8,
                                    source_url=url, snippet=f"ways: {ways}"))

        variant, reason = classify_space(
            attrs.get("grade", ""), node_text, header, folder_name)

        yield {
            "mpn": mpn, "vendor": vendor or "everythingrf", "url": url,
            "title": title, "description": description,
            "category": category, "subcategory": subcategory,
            "mount_type": mount, "grade_text": attrs.get("grade", ""),
            "spec_rows": rows, "variant": variant, "reason": reason,
        }


def _write(part: dict) -> int:
    pid = upsert_part(mpn=part["mpn"], vendor=part["vendor"],
                      category=part["category"], subcategory=part["subcategory"],
                      product_url=part["url"], description=part["description"])
    rows = list(part["spec_rows"])
    if part["mount_type"]:
        rows.append(SpecRow(key="mount_type", value_text=part["mount_type"],
                            method="aggregator", confidence=0.7,
                            source_url=part["url"], snippet="everythingRF configuration"))
    if part["grade_text"]:
        rows.append(SpecRow(key="erf_grade", value_text=part["grade_text"][:100],
                            method="aggregator", confidence=0.9,
                            source_url=part["url"], snippet="everythingRF Grade field"))
    # Both variants are space-ready -> space="qualified"; the qual/grade
    # preference is carried by space_variant.
    rows.append(SpecRow(key="space", value_text="qualified", method="catalog",
                        confidence=0.9, source_url=part["url"],
                        snippet=part["reason"][:150]))
    rows.append(SpecRow(key="space_variant", value_text=part["variant"],
                        method="catalog", confidence=0.9, source_url=part["url"],
                        snippet=part["reason"][:150]))
    put_specs(pid, rows)

    if part["variant"] == "space_qualified":
        put_evidence(pid, [("erf-space-qualified-listing", 9.0, part["url"],
                            part["reason"] or "everythingRF space-qualified catalog")])
    else:
        put_evidence(pid, [("erf-space-grade-listing", 7.0, part["url"],
                            part["reason"] or "everythingRF space-grade catalog")])
    return pid


def ingest(parent: Path, glob: str, dry_run: bool, verbose: bool) -> dict:
    if not parent.is_dir():
        raise SystemExit(f"parent folder not found: {parent}")
    folders = sorted(p for p in parent.glob(glob) if p.is_dir())
    if not folders:
        raise SystemExit(f"no folders matching {glob!r} under {parent}")

    # Deduplicate across pages/folders; strongest classification wins.
    best: dict[tuple[str, str], dict] = {}
    counts = Counter()
    for folder in folders:
        pages = sorted(folder.rglob("*.html")) + sorted(folder.rglob("*.htm"))
        for page in pages:
            counts["pages"] += 1
            try:
                html = page.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                print(f"  ! cannot read {page}: {e}")
                continue
            n = 0
            for part in parse_page(html, folder.name):
                n += 1
                if not part["variant"]:
                    counts["no_space_signal"] += 1
                    continue
                key = (re.sub(r"\s+", "", part["mpn"]).upper(), part["vendor"].lower())
                prev = best.get(key)
                if prev is None:
                    best[key] = part
                elif (prev["variant"] == "space_grade"
                      and part["variant"] == "space_qualified"):
                    best[key] = part          # upgrade grade -> qualified
                elif not prev["spec_rows"] and part["spec_rows"]:
                    best[key] = part
            if verbose:
                print(f"  {page.relative_to(parent)}: {n} parts")

    for part in best.values():
        counts[part["variant"]] += 1
        counts["parts"] += 1
        if not dry_run:
            _write(part)

    return dict(counts)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("parent", nargs="?", default=DEFAULT_PARENT,
                    help=f"parent folder of the EverythingRFSpace* dirs "
                         f"(default: {DEFAULT_PARENT})")
    ap.add_argument("--glob", default=FOLDER_GLOB,
                    help=f"folder pattern to ingest (default: {FOLDER_GLOB})")
    ap.add_argument("--dry-run", action="store_true",
                    help="parse and report, but write nothing to the DB")
    ap.add_argument("--verbose", action="store_true", help="print per-page counts")
    args = ap.parse_args(argv)

    c = ingest(Path(args.parent), args.glob, args.dry_run, args.verbose)
    print("\n=== everythingRF space ingest "
          + ("(dry run) ===" if args.dry_run else "===")
          + f"\n  pages parsed        {c.get('pages', 0)}"
          + f"\n  unique parts        {c.get('parts', 0)}"
          + f"\n    space_qualified   {c.get('space_qualified', 0)}"
          + f"\n    space_grade       {c.get('space_grade', 0)}"
          + (f"\n  skipped (no signal) {c['no_space_signal']}"
             if c.get("no_space_signal") else ""))
    if args.dry_run:
        print("  (nothing written -- drop --dry-run to commit)")


if __name__ == "__main__":
    main()
