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

import json

import argparse
import json
import os
import re
from collections import Counter
from pathlib import Path

from bs4 import BeautifulSoup

# Dual-mode import: works as `python -m pythonrfparts.erf_space_ingest` and as
# `python pythonrfparts\erf_space_ingest.py`.
try:
    from .partdb import SpecRow, upsert_part, put_specs, put_evidence
    from .paths import CACHE_DIR
except ImportError:                                    # run as a loose script
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from pythonrfparts.partdb import (                 # type: ignore
        SpecRow, upsert_part, put_specs, put_evidence)
    from pythonrfparts.paths import CACHE_DIR           # type: ignore

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
    # baluns / RF transformers (everythingRF uses several slug spellings)
    "rf-baluns": "balun",
    "baluns": "balun",
    "balun": "balun",
    "rf-transformers": "balun",
    "transformers": "balun",
    "transformers-baluns": "balun",
    "baluns-transformers": "balun",
}

# Numbers may carry thousands separators on everythingRF ("9,000 MHz"); without
# the comma branch "9,000" parsed as 9 and the part landed three decades off.
_NUM = r"[-+]?\d+(?:,\d{3})*(?:\.\d+)?"
# Frequencies are never negative, so the frequency parser uses an UNSIGNED number
# pattern. With a signed pattern the hyphen in an unspaced "DC-18 GHz" was read
# as a minus sign, giving -18 and discarding the part.
_UNSIGNED_NUM = r"\d+(?:,\d{3})*(?:\.\d+)?"
_FREQ_UNITS = ("thz", "ghz", "mhz", "khz", "hz")
_FREQ_UNIT_ALT = r"THz|GHz|MHz|kHz|Hz"

_RANGE = re.compile(rf"({_NUM})\s*(?:to|through|–|—|~|-)\s*({_NUM})")
_SINGLE = re.compile(rf"({_NUM})")
# Unit token. Letters are bounded on both sides so that:
#   * the "C" inside "DC" is NOT read as °C — that bug made every "DC to 20000
#     MHz" cell skip the MHz->GHz conversion and store 20000 GHz;
#   * the "W" inside a package name ("WQFN") is not read as watts.
# °C therefore requires the degree sign.
_UNIT = re.compile(
    rf"(?<![A-Za-z])({_FREQ_UNIT_ALT}|dBm|dBc|dBi|dB|mW|kW|W|Ω|ohms?|°\s*C)"
    rf"(?![A-Za-z])", re.I)
# A frequency range with a unit optionally on EITHER endpoint, so mixed-unit
# spans ("500 MHz to 20 GHz") convert per endpoint instead of applying the first
# unit to both.
_FREQ_RANGE = re.compile(
    rf"({_UNSIGNED_NUM})\s*({_FREQ_UNIT_ALT})?\s*(?:to|through|–|—|~|-)\s*"
    rf"({_UNSIGNED_NUM})\s*({_FREQ_UNIT_ALT})?", re.I)
_FREQ_SINGLE = re.compile(rf"({_UNSIGNED_NUM})")
_LABEL_UNIT = re.compile(rf"\(\s*({_FREQ_UNIT_ALT})\s*\)", re.I)
# Phrasings that mean "a band starting at DC up to X" rather than a single point.
# everythingRF commonly lists broadband parts as "Up to 20 GHz"; read literally
# that became a 20-20 GHz point and the part then failed every band search it
# actually covers. "Maximum frequency" is deliberately NOT here — a band-pass
# part's upper edge says nothing about it passing DC.
_FROM_DC = re.compile(r"\bDC\b|\bup\s*to\b|\bupto\b|<=|[\u2264<]", re.I)
_PROD = re.compile(r"/products/([^/]+)/")           # category slug in the URL

# Nothing in an RF/microwave catalog operates near 1 THz, so a parsed value
# above this is a unit error, not a part: drop the spec rather than store it.
_MAX_PLAUSIBLE_GHZ = 1000.0
# With no unit anywhere, a bare frequency in the hundreds-plus is MHz. everythingRF
# lists mmWave parts with explicit units, so treating >300 as MHz is far safer
# than trusting a bare "20000" to mean GHz.
_BARE_MHZ_ABOVE = 300.0


def _num(text) -> float:
    """Numeric value tolerating thousands separators."""
    return float(str(text).replace(",", "").strip())


def _to_ghz(val: float, unit: str | None) -> float:
    u = (unit or "GHz").strip().lower()
    if u == "thz":
        return val * 1e3
    if u == "mhz":
        return val / 1e3
    if u == "khz":
        return val / 1e6
    if u == "hz":
        return val / 1e9
    return val


def _freq_unit(text: str) -> str:
    """The frequency unit named in `text`, or '' (a stray dB/°C token isn't one)."""
    m = _UNIT.search(text or "")
    if not m:
        return ""
    u = _norm_unit(m.group(1))
    return u if u.lower() in _FREQ_UNITS else ""


def _label_unit(label: str):
    """Unit declared in a column label, e.g. 'Frequency (MHz)' -> 'MHz'."""
    m = _LABEL_UNIT.search(str(label or ""))
    return m.group(1) if m else None


def _infer_freq_unit(*values) -> str:
    """Unit for a value that states none, inferred from magnitude."""
    vals = [abs(v) for v in values if isinstance(v, (int, float))]
    v = max(vals) if vals else 0.0
    if v > 1e6:
        return "kHz"
    if v > _BARE_MHZ_ABOVE:
        return "MHz"
    return "GHz"


def _freq_result(lo: float, hi: float):
    """Ordered GHz min/max, or None when the result is physically implausible."""
    if lo > hi:
        lo, hi = hi, lo
    if hi <= 0 or hi > _MAX_PLAUSIBLE_GHZ:
        return None
    return {"value_min": round(lo, 6), "value_max": round(hi, 6), "unit": "GHz"}


def _parse_freq(text: str, unit_hint=None):
    """everythingRF frequency cell -> GHz min/max. Handles per-endpoint units,
    thousands separators, 'DC to X', a unit named only in the column label, and
    bare numbers with no unit at all."""
    t = (text or "").strip()
    if not t:
        return None
    m = _FREQ_RANGE.search(t)
    if m:
        lo_raw, u_lo, hi_raw, u_hi = (_num(m.group(1)), m.group(2),
                                      _num(m.group(3)), m.group(4))
        fallback = unit_hint or _infer_freq_unit(lo_raw, hi_raw)
        return _freq_result(_to_ghz(lo_raw, u_lo or u_hi or fallback),
                            _to_ghz(hi_raw, u_hi or u_lo or fallback))
    one = _FREQ_SINGLE.search(t)
    if not one:
        return None
    raw = _num(one.group(1))
    unit = _freq_unit(t) or unit_hint or _infer_freq_unit(raw)
    v = _to_ghz(raw, unit)
    # "DC to 7 GHz", "Up to 20 GHz", "<= 6 GHz" all describe a band from DC.
    lo = 0.0 if _FROM_DC.search(t) else v
    return _freq_result(min(lo, v), max(lo, v))


def _norm_unit(u: str) -> str:
    if not u:
        return ""
    u = u.strip()
    if re.fullmatch(r"°?\s*C", u, re.I):
        return "C"
    if re.fullmatch(r"ohms?", u, re.I) or u == "Ω":
        return "ohm"
    return u


def _parse_value(kind: str, text: str, unit_hint=None) -> dict | None:
    """everythingRF cell text -> SpecRow numeric kwargs, or None.

    `unit_hint` lets the caller supply a unit named in the column label rather
    than the cell (e.g. 'Frequency (MHz)' with a bare '20000' value)."""
    t = (text or "").strip()
    if not t:
        return None
    if kind == "text":
        return {"value_text": t[:200]}
    if kind == "freq":
        return _parse_freq(t, unit_hint)
    um = _UNIT.search(t)
    unit = _norm_unit(um.group(1)) if um else ""
    rng = _RANGE.search(t)
    if rng:
        return {"value_min": _num(rng.group(1)),
                "value_max": _num(rng.group(2)), "unit": unit}
    one = _SINGLE.search(t)
    if one:
        return {"value_typ": _num(one.group(1)), "unit": unit}
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
        # balun before the generic sweeps: a listing named "RF Balun Transformer"
        # must not fall through to another category on a stray keyword.
        ("balun", "balun"), ("transformer", "balun"),
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


def _scalar_ghz(text, unit_hint=None):
    """A single frequency value in GHz (max endpoint if a range slips in)."""
    p = _parse_value("freq", text, unit_hint)
    if not p:
        return None
    lo, hi = p.get("value_min"), p.get("value_max")
    return hi if hi is not None else lo


def _bandwidth_ghz(text, center, unit_hint=None):
    """Bandwidth as a width in GHz. Handles absolute ('200 MHz') and percentage
    ('5 %', needs a centre) forms."""
    t = (text or "").strip()
    if not t:
        return None
    if "%" in t:
        m = re.search(r"(\d+(?:\.\d+)?)", t)
        return center * float(m.group(1)) / 100.0 if (m and center) else None
    p = _parse_value("freq", t, unit_hint)
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
            p = _parse_value("freq", attrs[lbl], _label_unit(lbl))
            if p:
                freq = _fr(lbl, p)
                break
    if freq is None:                                     # 2) generic freq/passband
        for lbl, val in attrs.items():
            if "bandwidth" in lbl or lbl in _FREQ_POINT_LABELS:
                continue
            if "frequency" in lbl or "passband" in lbl:
                p = _parse_value("freq", val, _label_unit(lbl))
                if p:
                    freq = _fr(lbl, p)
                    break

    center_lbl = next((l for l in _FREQ_POINT_LABELS
                       if l in attrs and ("center" in l or "centre" in l)), None)
    center = (_scalar_ghz(attrs[center_lbl], _label_unit(center_lbl))
              if center_lbl else None)
    bw_lbl = next((l for l in attrs if "bandwidth" in l), None)
    bw = (_bandwidth_ghz(attrs[bw_lbl], center, _label_unit(bw_lbl))
          if bw_lbl else None)

    if freq is None and center is not None:              # 3) centre (+ bandwidth)
        if bw:
            freq = _fr(center_lbl, {"value_min": round(max(0.0, center - bw / 2), 6),
                                    "value_max": round(center + bw / 2, 6), "unit": "GHz"})
        else:
            freq = _fr(center_lbl, {"value_min": center, "value_max": center, "unit": "GHz"})
    if freq is None:                                     # 4) any point label
        for lbl in _FREQ_POINT_LABELS:
            if lbl in attrs:
                p = _parse_value("freq", attrs[lbl], _label_unit(lbl))
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
                # everythingRF quotes output power in WATTS ("Output Power:
                # 3.16 W"), so it was stored as power_w only -- correct, but every
                # filter, the ranking code and the coverage report all speak dBm,
                # so the value was effectively invisible. The missing-spec audit
                # flagged this on its first run. Derive dBm alongside it.
                if key in ("power_w", "power_avg_w"):
                    watts = parsed.get("value_typ") or parsed.get("value_max") \
                        or parsed.get("value_min")
                    if watts and watts > 0:
                        import math
                        dbm = round(10.0 * math.log10(float(watts) * 1000.0), 1)
                        rows.append(SpecRow(
                            key="psat_dbm" if key == "power_w"
                            else "power_avg_dbm",
                            value_typ=dbm, unit="dBm", method="derived",
                            confidence=0.7, source_url=url,
                            snippet=f"derived from {label}: {value}"[:150]))

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


def flat_specs(part: dict) -> dict:
    """`spec_rows` (SpecRow objects) -> {key: scalar} for the live parts table.

    freq_ghz arrives as a min/max range, which the table wants as freq_min and
    freq_max, so it is split. Everything else prefers typ, then max, then min,
    then the text value."""
    out = {}
    for row in part.get("spec_rows") or []:
        key = getattr(row, "key", None)
        if not key:
            continue
        lo = getattr(row, "value_min", None)
        typ = getattr(row, "value_typ", None)
        hi = getattr(row, "value_max", None)
        txt = getattr(row, "value_text", None)
        if key == "freq_ghz":
            if lo is not None:
                out["freq_min"] = lo
            if hi is not None:
                out["freq_max"] = hi
            if lo is None and hi is None and typ is not None:
                out["freq_min"] = out["freq_max"] = typ
            continue
        for candidate in (typ, hi, lo):
            if candidate is not None:
                out[key] = candidate
                break
        else:
            if txt not in (None, ""):
                out[key] = txt
    if part.get("mount_type"):
        out.setdefault("mount_type", part["mount_type"])
    return out


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


def _resume_state_path(parent: Path, glob: str) -> Path:
    """Stable checkpoint file for one local EverythingRF source selection.

    The key is a hash of the path, so anything that changes the path STRING
    without changing the folder produces a different key and a fresh parse. On
    Windows that happens constantly: C:\\Users\\... and c:\\users\\... are the same
    directory but different strings, and a folder picker, a typed path and a
    saved setting can each yield a different case. Normalising the case on
    case-insensitive filesystems is what makes resume actually stick between
    runs. Separators are normalised too, so a trailing slash cannot matter."""
    import hashlib
    import os
    raw = str(parent.resolve())
    # os.path.normcase lowercases and converts / to \ on Windows; no-op on POSIX
    norm = os.path.normcase(os.path.normpath(raw))
    key = hashlib.sha1(f"{norm}|{glob.strip().lower()}".encode("utf-8")
                       ).hexdigest()[:16]
    return CACHE_DIR / f"everythingrf_{key}.json"


def _source_manifest(parent: Path, glob: str) -> dict[str, int]:
    """Map every HTML file in the selection to its byte size -- and NOTHING is
    read from disk beyond the stat() every directory walk already does.

    This is the cheap invariant the source-level resume decision is built on.
    A sticky `source_complete` boolean can't tell "nothing changed" from "the
    user just dropped in a new folder", so it either re-parsed everything or
    silently ignored the new source. A path->size manifest changes the instant
    a file is added, removed, or resized, so comparing it to the last completed
    run answers "is there anything new to do?" without reading a single page."""
    manifest: dict[str, int] = {}
    try:
        folders = sorted(p for p in parent.glob(glob) if p.is_dir())
    except OSError:
        return manifest
    for folder in folders:
        for pattern in ("*.html", "*.htm"):
            for page in folder.rglob(pattern):
                try:
                    manifest[str(page.relative_to(parent))] = page.stat().st_size
                except OSError:
                    continue
    return manifest


def _page_signature(page: Path) -> dict:
    """Content-based, so copying or syncing the Sources tree does not invalidate
    resume.

    The signature used to include st_mtime_ns. Re-extracting the zip, copying the
    folder, or a sync client touching the files all change mtime while leaving the
    bytes identical -- and every page then looked modified and was re-parsed. Size
    plus a hash of the head and tail is cheap (16 kB read) and stable."""
    import hashlib
    st = page.stat()
    h = hashlib.sha1()
    try:
        with page.open("rb") as fh:
            h.update(fh.read(8192))
            if st.st_size > 16384:
                fh.seek(-8192, 2)
                h.update(fh.read(8192))
    except OSError:
        return {"size": st.st_size, "digest": ""}
    return {"size": st.st_size, "digest": h.hexdigest()[:16]}


def _load_resume_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _save_resume_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def ingest(parent: Path, glob: str, dry_run: bool, verbose: bool,
           default_variant: str | None = None, resume: bool = False,
           progress=None, part=None, cancel=None) -> dict:
    """Parse local HTML and write each accepted part immediately.

    With ``resume=True``, unchanged files already checkpointed in Data/cache are
    skipped. A file is checkpointed only after all of its parsed parts have
    been committed, so interruption resumes at the first incomplete file.
    """
    # `part` is rebound as a loop variable further down, so keep the callback
    # under its own name.
    part_cb = part

    def _stopped() -> bool:
        """Cooperative cancel from the Stop button."""
        try:
            if cancel is None:
                return False
            return bool(cancel.is_set() if hasattr(cancel, "is_set")
                        else cancel())
        except Exception:
            return False

    def emit(message: str) -> None:
        if progress:
            progress(message)
        if verbose:
            print(message)

    if not parent.is_dir():
        raise SystemExit(f"parent folder not found: {parent}")

    state_path = _resume_state_path(parent, glob)
    state = _load_resume_state(state_path) if resume and not dry_run else {}
    completed = state.setdefault("completed", {})

    # Say out loud what the resume decision is and why. "It parses everythingRF
    # every time" is impossible to diagnose from the outside: the checkpoint is
    # keyed on the RESOLVED parent path plus the glob, so pointing the dialog at
    # a different-but-equivalent path (a mapped drive, a trailing slash, the
    # SpaceQual folder itself rather than its parent) produces a different key
    # and therefore a fresh checkpoint every run.
    emit(f"RESUME STATE | resume={'on' if resume else 'off'} "
         f"dry_run={dry_run}")
    emit(f"RESUME STATE | parent={parent.resolve()}")
    emit(f"RESUME STATE | glob={glob!r}")
    emit(f"RESUME STATE | checkpoint={state_path} "
         f"(exists={state_path.exists()})")
    if state:
        emit(f"RESUME STATE | manifest_files="
             f"{len(state.get('manifest', {}))} "
             f"completed_files={len(completed)}")
        if state.get("parent") and state["parent"] != str(parent.resolve()):
            emit(f"RESUME STATE | ! checkpoint was written for a DIFFERENT "
                 f"parent: {state['parent']}")
    elif resume and not dry_run:
        emit("RESUME STATE | no checkpoint yet -- this run will create one")

    # The whole source-level resume decision hangs on ONE cheap directory walk
    # (stat only, no page bodies read): a {path: size} manifest of what is on
    # disk right now.
    current_manifest = _source_manifest(parent, glob) if resume and not dry_run \
        else {}

    # Fast path: nothing on disk has changed since the last CLEAN run, so there
    # is genuinely nothing to do. This replaces the old sticky `source_complete`
    # boolean, which could not distinguish "all done" from "the user just added
    # a new folder" -- that flag either re-parsed the whole tree or silently
    # ignored the new source. Manifest equality is exact: add, remove, or resize
    # any file and this comparison fails, so a resumed run always notices new
    # sources yet never re-reads pages that have not changed.
    if resume and not dry_run and current_manifest \
            and state.get("manifest") == current_manifest:
        emit(f"SKIP completed local source | {parent} "
             f"| {len(current_manifest)} file(s) unchanged | checkpoint "
             f"{state_path}")
        return {
            "parts": 0,
            "pages": 0,
            "pages_skipped": len(current_manifest),
            "source_skipped": 1,
            "resume_state": str(state_path),
        }

    if resume and not dry_run and current_manifest:
        # Anything present whose size matches what we already committed will be
        # skipped WITHOUT reading its bytes; the rest (new or resized) get
        # parsed. Report the split up front so a resumed run is legible.
        already = sum(1 for rel, size in current_manifest.items()
                      if isinstance(completed.get(rel), dict)
                      and completed[rel].get("size") == size)
        emit(f"RESUME | {already}/{len(current_manifest)} local file(s) already "
             f"committed; parsing the remaining "
             f"{len(current_manifest) - already}")

    folders = sorted(p for p in parent.glob(glob) if p.is_dir())
    if not folders:
        raise SystemExit(f"no folders matching {glob!r} under {parent}")

    pages = []
    for folder in folders:
        pages.extend((folder, page) for page in sorted(folder.rglob("*.html")))
        pages.extend((folder, page) for page in sorted(folder.rglob("*.htm")))
    counts = Counter()
    seen: dict[tuple[str, str], str] = {}

    for page_index, (folder, page) in enumerate(pages, 1):
        counts["pages_seen"] += 1
        rel = str(page.relative_to(parent))

        # Skip an already-committed file using the size we ALREADY stat'd for the
        # manifest -- no 16 kB read, no hashing. Size is a reliable "unchanged"
        # signal for saved listing pages (a re-save changes the byte count), and
        # the strong head+tail hash is still what we STORE, so an explicit Reset
        # remains the way to force a byte-identical re-parse.
        prev = completed.get(rel)
        if resume and isinstance(prev, dict) \
                and prev.get("size") == current_manifest.get(rel):
            counts["pages_skipped"] += 1
            emit(f"SKIP unchanged file {page_index}/{len(pages)} | {page}")
            continue

        try:
            signature = _page_signature(page)
        except OSError as exc:
            counts["read_errors"] += 1
            emit(f"FILE ERROR | {page} | {exc}")
            continue

        emit(f"FILE {page_index}/{len(pages)} | {page}")
        try:
            html = page.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            counts["read_errors"] += 1
            emit(f"FILE ERROR | {page} | {exc}")
            continue

        page_parts = 0
        example_part = None
        # A failure on one page must not abort the whole source: an aborted run
        # never commits its manifest, so every future run would re-check
        # everythingRF from scratch. Swallow the per-page error and carry on.
        try:
            page_rows = list(parse_page(html, folder.name))
        except Exception as exc:
            emit(f"PARSE FAILED | {page} | {type(exc).__name__}: {exc}")
            counts["page_errors"] = counts.get("page_errors", 0) + 1
            page_rows = []
        for part in page_rows:
            if not part["variant"]:
                lname = folder.name.lower()
                if "spacequal" in lname or "space_qual" in lname:
                    part["variant"] = "space_qualified"
                    part["reason"] = f"source folder {folder.name}: space qualified"
                elif "space" in lname:
                    part["variant"] = "space_grade"
                    part["reason"] = f"source folder {folder.name}: space grade"
                elif default_variant:
                    part["variant"] = default_variant
                    part["reason"] = (f"trusted local source folder {folder.name}: "
                                      f"{default_variant}")
                else:
                    counts["no_space_signal"] += 1
                    continue

            key = (re.sub(r"\s+", "", part["mpn"]).upper(),
                   part["vendor"].lower())
            previous_variant = seen.get(key)
            if previous_variant == "space_qualified" and part["variant"] == "space_grade":
                counts["duplicates_skipped"] += 1
                continue
            seen[key] = part["variant"]

            counts[part["variant"]] += 1
            counts["parts"] += 1
            page_parts += 1
            if example_part is None:
                example_part = part
            if not dry_run:
                _write(part)
                counts["parts_written"] += 1
                if part_cb:
                    part_cb({
                        "vendor": part["vendor"], "mpn": part["mpn"],
                        "category": part["category"],
                        "subcategory": part.get("subcategory", ""),
                        "specs": flat_specs(part),
                        "space": part["variant"],
                        "url": part["url"], "source": str(page),
                    })

        if _stopped():
            emit("STOP requested; leaving local HTML ingest")
            break
        counts["pages_parsed"] += 1
        if example_part is not None:
            emit("EXAMPLE ROW | "
                 f"vendor={example_part['vendor']} | "
                 f"mpn={example_part['mpn']} | "
                 f"category={example_part['category']} | "
                 f"subcategory={example_part.get('subcategory', '') or '-'} | "
                 f"space={example_part['variant']} | "
                 f"url={example_part['url']}")
        emit(f"FILE DONE | {page} | {page_parts} part(s) committed")

        if resume and not dry_run:
            completed[rel] = signature
            state["parent"] = str(parent.resolve())
            state["glob"] = glob
            _save_resume_state(state_path, state)
            emit(f"CHECKPOINT | {state_path}")

    stopped_early = _stopped()
    errored = counts.get("page_errors", 0) > 0
    if resume and not dry_run and not stopped_early and not errored:
        # Record the manifest of what was on disk THIS run. Next run compares
        # its own fresh manifest against this one: identical -> fast-skip; any
        # file added/removed/resized -> the comparison fails and only the
        # difference is parsed. Only a clean run (no stop, no page errors) may
        # write it, so an interrupted run never fools a later run into skipping
        # the pages it never reached.
        state["manifest"] = current_manifest
        state["page_count"] = len(pages)
        state["parent"] = str(parent.resolve())
        state["glob"] = glob
        state.pop("source_complete", None)   # retire the old sticky flag
        _save_resume_state(state_path, state)
        emit(f"SOURCE CHECKPOINT COMPLETE | {state_path} | "
             f"{len(current_manifest)} file(s)")
    elif resume and not dry_run:
        # Withholding the manifest on a partial run means the next run re-checks
        # the remainder instead of skipping it for good; say plainly why.
        why = ("stopped by request" if stopped_early
               else f"{counts.get('page_errors', 0)} page error(s)")
        emit(f"SOURCE NOT MARKED COMPLETE | {why} | it will resume from the "
             f"{len(completed)} file(s) already checkpointed")

    emit(f"RESUME SUMMARY | {len(pages)} file(s) in source, "
         f"{counts.get('pages_skipped', 0)} skipped by resume, "
         f"{counts.get('pages_parsed', 0)} parsed, "
         f"manifest_committed={'manifest' in state}")

    counts["pages"] = counts["pages_parsed"]
    counts["resume_state"] = str(state_path)
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
