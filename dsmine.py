"""dsmine — mine specs out of the datasheets already sitting on disk.

WHY THIS EXISTS
---------------
The rebuild read vendor CATALOG LISTINGS only. `space_dataset.rebuild()` calls
the vendor ingests and the everythingRF page parser, and none of them open a
datasheet. `extract.py`'s pattern engine -- which knows how to read switching
speed, P1dB, OIP3, noise figure and the rest out of prose -- runs only in the
live-crawler path, which a local rebuild never touches.

The practical consequence was that thousands of downloaded datasheets sat in
the library contributing nothing, and any spec a listing did not happen to
tabulate was simply absent. Switching speed is the clearest case: it is on
essentially every switch datasheet, and everythingRF's switch listings do not
carry it as an attribute, so the field was empty across the entire dataset.

WHAT IT DOES
------------
Builds a part-number index of the local datasheet library once, then for each
part: reads the datasheet (PDF or HTML), mines the parametric patterns, and
returns SpecRows at LOW confidence so a catalog value always wins over a
text-mined one. Nothing is overwritten; these only fill blanks.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from . import specmatch
from .partdb import SpecRow
from .registry import PARAM_SPECS

# Confidence deliberately below every catalog source (aggregator 0.75, catalog
# 0.8+): text mining is the last resort, not a competing opinion.
MINED_CONFIDENCE = 0.45
MAX_TEXT_CHARS = 60000
# Stop paging once this many distinct specs have been found. Four is enough to
# have cleared the summary table; going further chases the long tail at full cost.
EARLY_EXIT_SPECS = 4
_DS_EXT = {".pdf", ".htm", ".html", ".txt"}


def default_roots():
    env = os.environ.get("RFPARTS_DATASHEETS", "").strip()
    roots = [Path(env)] if env else []
    home = Path.home()
    roots += [home / "Downloads" / "rfparts" / "data" / "datasheets",
              home / "Downloads" / "rfparts" / "Data" / "datasheets"]
    try:
        from .paths import DATASHEET_DIR
        roots.append(Path(DATASHEET_DIR))
    except Exception:
        pass
    return [r for r in roots if r.is_dir()]


def _loose(pn):
    return re.sub(r"[^A-Z0-9]", "", str(pn or "").upper())


def build_index(roots=None, say=None):
    """{loose part number: path} for every datasheet on disk.

    One walk, then O(1) lookups. Globbing per part across a 4000-file tree would
    dominate the rebuild."""
    roots = roots or default_roots()
    index = {}
    for root in roots:
        root = Path(root)
        if not root.is_dir():
            continue
        for f in root.rglob("*"):
            try:
                if not f.is_file() or f.suffix.lower() not in _DS_EXT:
                    continue
            except OSError:
                continue
            key = _loose(f.stem)
            if not key:
                continue
            # Prefer the larger file when two match: a stub redirect page loses
            # to the real datasheet.
            prev = index.get(key)
            if prev is None:
                index[key] = f
            else:
                try:
                    if f.stat().st_size > prev.stat().st_size:
                        index[key] = f
                except OSError:
                    pass
    if say:
        say(f"    datasheet library: {len(index)} file(s) indexed from "
            + ", ".join(str(r) for r in roots) if roots else
            "    datasheet library: none found")
    return index


_SCRIPTISH = re.compile(r"(?is)<(script|style|noscript|svg)\b.*?</\1\s*>")
_TAG = re.compile(r"(?s)<[^>]+>")


_SNIFF_BYTES = 65536


def _sniff(path):
    # 64 kB, not 1 kB. The corruption test counts UTF-8 replacement bytes, and a
    # 1 kB sample was too small to reach the threshold on the text-mangled Qorvo
    # PDFs -- so they were classed as readable, handed to pypdf, and it spent a
    # long time attempting xref recovery on each of nearly a thousand files
    # before returning nothing. 64 kB is the sample size that was measured to
    # separate them cleanly.
    try:
        head = Path(path).open("rb").read(_SNIFF_BYTES)
    except OSError:
        return "unreadable"
    # Corruption is tested BEFORE the PDF magic bytes. It used to come after, and
    # since a mangled PDF still begins with "%PDF-" the function returned "pdf"
    # and the corruption branch was unreachable for the entire population it
    # exists to catch. Every one of those files then went to pypdf, which spent a
    # long time on xref recovery before returning nothing.
    replacement_ratio = head.count(b"\xef\xbf\xbd") * 3 / max(1, len(head))
    if replacement_ratio > 0.02:
        return "corrupt"
    if head[:5] == b"%PDF-":
        return "pdf"
    low = head.lstrip()[:400].lower()
    if low.startswith(b"<!doctype html") or b"<html" in low:
        return "html"
    return "text"


def _pdf_pages_pypdf(path, pages):
    """Per-page text via pypdf. Measured at ~99 ms/file against ~448 ms for
    pdfplumber on the same seven real datasheets, and it extracted MORE text
    (44k vs 41k chars) -- pdfplumber builds a full layout model that this step
    does not use. Kept as a generator so reading can stop early."""
    from pypdf import PdfReader
    reader = PdfReader(str(path))
    for pg in reader.pages[:pages]:
        try:
            yield pg.extract_text() or ""
        except Exception:
            yield ""


def _pdf_pages_plumber(path, pages):
    """Fallback for PDFs pypdf cannot read. Slower but differently capable."""
    import pdfplumber
    with pdfplumber.open(str(path)) as pdf:
        for pg in pdf.pages[:pages]:
            try:
                yield pg.extract_text() or ""
            except Exception:
                yield ""


def datasheet_text(path, max_chars=MAX_TEXT_CHARS, pages=6, want=None):
    """Readable text from a local datasheet, or ''.

    File type comes from the bytes, not the extension: parts of the library are
    PDFs saved with a .html name, and dispatching on the suffix fed PDF bytes to
    the HTML stripper and produced nothing at all.

    `want` is an optional callable taking the accumulated text and returning True
    once enough has been read. Datasheets put the summary table on page one, so
    stopping there avoids parsing five more pages for nothing -- which on a
    four-thousand-file library is most of the run.
    """
    path = Path(path)
    kind = _sniff(path)
    if kind in ("unreadable", "corrupt"):
        return ""
    if kind == "pdf":
        import logging
        for _n in ("pdfminer", "pypdf", "pypdf._reader", "PyPDF2"):
            logging.getLogger(_n).setLevel(logging.ERROR)
        for reader in (_pdf_pages_pypdf, _pdf_pages_plumber):
            chunks = []
            try:
                for page_text in reader(path, pages):
                    chunks.append(page_text)
                    if want is not None and want("\n".join(chunks)):
                        break
                    if sum(len(c) for c in chunks) >= max_chars:
                        break
            except Exception:
                continue
            text = "\n".join(chunks)[:max_chars]
            # pypdf occasionally returns almost nothing for a PDF pdfplumber can
            # read, so a thin result falls through to the slower backend rather
            # than being accepted.
            if len(text.strip()) >= 200:
                return text
        return ""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if kind == "html":
        raw = _SCRIPTISH.sub(" ", raw)
        raw = re.sub(r"(?i)</(p|div|li|tr|td|th|h[1-6]|table)\s*>", "\n", raw)
        raw = _TAG.sub(" ", raw)
        import html as _h
        raw = _h.unescape(raw)
    return re.sub(r"[ \t\xa0]+", " ", raw)[:max_chars]


# Compile the parametric patterns once, exactly as extract.py does, so a spec
# added to PARAM_SPECS is mined from datasheets too with no extra wiring.
_PATTERNS = {k: [re.compile(p, re.I) for p in m["patterns"]]
             for k, m in PARAM_SPECS.items() if m.get("patterns")}



# ------------------------------------------------- space-qualification hint
# Weighted evidence for how space-capable a datasheet SOUNDS. This is a hint for
# the many parts where no source states a qualification, not a replacement for
# the `space` field -- a stated qualification always wins, and this is stored
# under its own key so the two can never be confused.
#
# It is deliberately rule-based and explainable: every score comes with the list
# of terms that produced it, so a number can always be argued with. spacequal.py
# remains the statistical view; this is the cheap one that runs during a rebuild.
_SPACE_EVIDENCE = [
    # (weight, label, pattern)  -- strong: an actual qualification standard
    (30, "MIL-PRF-38534 Class K", r"38534.{0,20}class\s*k|class\s*k.{0,20}38534"),
    (30, "MIL-PRF-38535 Class V/QML-V", r"38535|qml-?\s*v\b|class\s*v\b"),
    (28, "JANS", r"\bjans\b"),
    (26, "ESCC", r"\bescc\b|european space components"),
    (26, "radiation hardened", r"radiation[- ]hardened|rad[- ]?hard\b"),
    (22, "TID rating", r"\btid\b.{0,24}\d+\s*k?rad|\d+\s*krad"),
    (22, "SEL/SEE rating", r"\bsel\b.{0,24}mev|mev\s*[\u00b7*x]?\s*cm|single event"),
    (20, "space qualified", r"space[- ]qualified|qualified for space|space[- ]grade"),
    (18, "Class S", r"\bclass\s*s\b"),
    # moderate: screening and construction that space parts require
    (12, "hermetic", r"\bhermetic|hermetically sealed"),
    (12, "MIL-STD-883", r"mil-?std-?883|method\s*20\d\d"),
    (10, "screened", r"\bscreen(?:ed|ing)\b|\bburn-?in\b|\b100%\s*test"),
    (10, "MIL-STD (other)", r"mil-?std-?(?!883)\d{3}"),
    (8,  "outgassing/ASTM E595", r"outgassing|astm\s*e-?595|tml\b"),
    (8,  "extended temp -55/+125", r"-\s*55\s*\u00b0?\s*c.{0,18}\+?\s*12[05]"),
    # weak: materials common in space parts but also in ordinary ones
    (6,  "LTCC/ceramic", r"\bltcc\b|\bceramic\b|\balumina\b"),
    (5,  "GaAs", r"\bgaas\b|gallium arsenide"),
    (5,  "GaN", r"\bgan\b|gallium nitride"),
    (4,  "Kovar/gold", r"\bkovar\b|gold[- ]plated|au plated"),
    (4,  "MMIC", r"\bmmic\b"),
    # negative: statements that rule it out
    (-30, "commercial only", r"commercial\s*(use|grade)\s*only|not\s*for\s*space"),
    (-14, "plastic encapsulated", r"plastic\s*(encapsulat|package)"),
    (-10, "consumer/automotive", r"\bconsumer\b|\bautomotive\b|aec-?q\d+"),
]
_SPACE_EVIDENCE = [(w, lbl, re.compile(pat, re.I)) for w, lbl, pat in _SPACE_EVIDENCE]

# Above this, calling it a strong hint is fair; the cap keeps one very wordy
# datasheet from scoring higher than a genuinely qualified part.
_SPACE_MAX = 100


def space_score(text):
    """(percent 0-100, [evidence labels]) for how space-capable a datasheet reads.

    Returns (None, []) when nothing at all fires, because "no evidence" and
    "evidence for zero" are different things and storing 0 for the first would
    look like a judgement that was never made.
    """
    if not text:
        return None, []
    hits, score = [], 0
    for weight, label, rx in _SPACE_EVIDENCE:
        if rx.search(text):
            score += weight
            hits.append(label if weight > 0 else f"NOT: {label}")
    if not hits:
        return None, []
    pct = max(0, min(_SPACE_MAX, score))
    return pct, hits



# ------------------------------------------------------------ frequency band
# Not one of the PARAM_SPECS patterns, because a band is a PAIR and those
# patterns capture a single number. Every example datasheet states its range and
# none of them were being read: the wordings in the wild are
#
#   "7.5 - 22.5 GHz"      "2-30 GHz"        "1 to 2500 MHz"
#   "3.5 MHz to 50 MHz"   "DC - 18 GHz"     "0.5 to 6 GHz"
#
# with the unit on the second number, on both, or attached to neither.
_FREQ_UNIT = {"thz": 1e3, "ghz": 1.0, "mhz": 1e-3, "khz": 1e-6, "hz": 1e-9}
_RANGE_RE = re.compile(
    r"(?<![\w.])(DC|\d+(?:\.\d+)?)\s*(thz|ghz|mhz|khz)?\s*"
    r"(?:-|\u2013|\u2014|to|through)\s*"
    r"(\d+(?:\.\d+)?)\s*(thz|ghz|mhz|khz)\b", re.I)
# An RF part lives inside this window; anything outside is a page number, a part
# number fragment or a temperature that happened to sit beside a unit.
_MIN_GHZ, _MAX_GHZ = 1e-6, 500.0


# Words that mark a range as belonging to THIS part, and words that mark it as
# belonging to something else. MAC-60LH+ is a 1600-6000 MHz mixer whose front
# page advertises "MAC Series Key Features 300 MHz to 12 GHz" -- the family's
# span, not the part's. Taking the first range near the top reads the marketing
# line and gets the part wrong by a factor of four at both ends.
# "RF" earns a range its place; "IF" and "LO" disqualify it as THE band. An IQ
# mixer reads "4 to 16 GHz on the RF and LO ports with an IF from DC to 6 GHz" --
# every one of those is a real range, and only the RF one is the part's band.
_FREQ_GOOD = re.compile(
    r"\b(rf\s*(?:frequency|range|port|input)?|frequency\s*range|"
    r"operating\s*frequency|passband|freq(?:uency)?)\b", re.I)
_FREQ_PORT_OTHER = re.compile(r"\b(if|lo|local\s*oscillator|"
                              r"intermediate\s*frequency)\b\s*(from|:|range)?",
                              re.I)
# "typical" spans are wider than the specified band and are advertised as such.
# 200-2800 MHz (TYP.) sits beside a spec'd 200-2600, and the spec is the answer.
_FREQ_LOOSE = re.compile(r"\b(typ\.?|typical|wide\s*bandwidth|up to|nominal)\b",
                         re.I)
_FREQ_BAD = re.compile(
    r"\b(series|family|portfolio|key features|product line|catalog|"
    r"other models|selection guide|available in|models? from)\b", re.I)
_FREQ_UNIT_ONLY = re.compile(r"\b(temperature|storage|humidity|altitude)\b", re.I)


def _range_score(text, m):
    """How likely this range describes the part rather than something near it."""
    before = text[max(0, m.start() - 90):m.start()]
    after = text[m.end():m.end() + 40]
    score = 0
    if _FREQ_BAD.search(before):
        score -= 6              # a family span, not this part
    if _FREQ_GOOD.search(before):
        score += 3
    # An IF or LO label immediately before the range means this is that port's
    # span, not the part's headline band.
    tail = text[max(0, m.start() - 26):m.start()]
    if _FREQ_PORT_OTHER.search(tail):
        score -= 5
    if _FREQ_LOOSE.search(before) or _FREQ_LOOSE.search(after):
        score -= 2
    if _FREQ_UNIT_ONLY.search(before):
        score -= 8
    # A range inside a spec table usually has more numbers around it than a
    # sentence does.
    if sum(c.isdigit() for c in after) >= 3:
        score += 1
    return score


def freq_range_ghz(text, head_chars=6000):
    """(low, high) in GHz for the part's own band, or None.

    Candidates are SCORED rather than taken in order: a datasheet's front page
    carries the family's span, the operating temperature and sometimes a
    competitor comparison, all of which look like ranges. Wording nearby is what
    separates the part's band from the rest.
    """
    best, best_score = None, -99
    for chunk in (text[:head_chars], text):
        for m in _RANGE_RE.finditer(chunk or ""):
            lo_raw, lo_u, hi_raw, hi_u = m.groups()
            hi_scale = _FREQ_UNIT.get((hi_u or "").lower())
            if hi_scale is None:
                continue
            # A unit given only on the second number governs both: "1 to 2500
            # MHz" is megahertz at each end, not 1 GHz to 2.5 GHz.
            lo_scale = _FREQ_UNIT.get((lo_u or "").lower(), hi_scale)
            lo = 0.0 if lo_raw.lower() == "dc" else float(lo_raw) * lo_scale
            hi = float(hi_raw) * hi_scale
            if hi <= lo or hi > _MAX_GHZ or lo < 0 or hi < _MIN_GHZ:
                continue
            s = _range_score(chunk, m)
            if s > best_score:
                best, best_score = (round(lo, 9), round(hi, 9)), s
        if best is not None:
            # ANY candidate in the head wins over the rest of the document. The
            # break used to require a positive score, so a correctly-chosen band
            # that merely scored zero fell through to a full-text scan and was
            # overridden by a stray range from deep in the tables.
            break
    return best


# Table rows put the number AFTER the unit -- "Small Signal Gain (min) dB 8.5" --
# while the PARAM_SPECS patterns expect "Gain 8.5 dB". Neither ordering is more
# correct, and datasheets use both, so the reversed form is read here rather
# than doubling every pattern in the registry.
_TABLE_ROW = [
    ("gain_db", r"(?:small\s*signal\s*)?gain(?:\s*\((?:min|typ|max)\))?", "dB"),
    ("nf_db", r"noise\s*figure(?:\s*\((?:min|typ|max)\))?|\bNF\b", "dB"),
    ("p1db_dbm", r"(?:output\s*)?P\s*1\s*-?\s*dB(?:\s*\((?:min|typ|max)\))?", "dBm"),
    ("oip3_dbm", r"(?:output\s*)?(?:OIP3|IP3)(?:\s*\((?:min|typ|max)\))?", "dBm"),
    ("isolation_db", r"isolation(?:\s*\((?:min|typ|max)\))?", "dB"),
    ("insertion_loss_db", r"insertion\s*loss(?:\s*\((?:min|typ|max)\))?", "dB"),
    ("conversion_loss_db", r"conversion\s*loss(?:\s*\((?:min|typ|max)\))?", "dB"),
    ("psat_dbm", r"(?:saturated\s*)?output\s*power(?:\s*\((?:min|typ|max)\))?", "dBm"),
]
# The label must be wrapped: an un-grouped alternation like "noise figure|NF"
# binds only its last branch to what follows, so a match on the first branch
# leaves the value group unset and float(None) raises.
_TABLE_ROW = [(k, re.compile(rf"(?:{pat})\s*{unit}\s+([-+]?\d+(?:\.\d+)?)",
                             re.I), unit) for k, pat, unit in _TABLE_ROW]


def mine_table_rows(text):
    """Specs written as 'Label unit value', the way spec tables flatten."""
    out = {}
    for key, rx, unit in _TABLE_ROW:
        m = rx.search(text or "")
        if not m:
            continue
        try:
            out[key] = (float(m.group(1)), unit)
        except (TypeError, ValueError):
            continue
    return out


def mine_text(text):
    """{spec key: (value, unit)} from datasheet prose."""
    out = {}
    if not text:
        return out
    for key, pats in _PATTERNS.items():
        meta = PARAM_SPECS[key]
        ugroup = meta.get("unit_group")
        uscale = meta.get("unit_scale") or {}
        for pat in pats:
            m = pat.search(text)
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
                    continue        # unknown unit: refuse rather than guess
                value *= factor
            out[key] = (value, meta.get("unit", ""))
            break
    # Table-row form fills in what the prose patterns missed; setdefault, so a
    # prose match (usually the headline figure) keeps precedence.
    for k, v in mine_table_rows(text).items():
        out.setdefault(k, v)
    band = freq_range_ghz(text)
    if band:
        out["freq_min"] = (band[0], "GHz")
        out["freq_max"] = (band[1], "GHz")
    cfg = specmatch.parse_throw_config(text[:4000])
    if cfg:
        out["throw_config"] = (cfg, "")
    pct, why = space_score(text)
    if pct is not None:
        out["space_score_pct"] = (float(pct), "%")
        out["space_evidence"] = (", ".join(why)[:180], "")
    return out



# ---------------------------------------------------------------- parse cache
# Parsing is the whole cost of this step: several thousand PDFs at up to six
# pages each. Without a cache that work is repeated in full on EVERY rebuild,
# which is what made the enrichment pass take ten minutes every single time.
# Keyed on path + size + mtime, so an edited or replaced datasheet is re-read and
# nothing else is.
def _cache_path():
    from .paths import CACHE_DIR
    return Path(CACHE_DIR) / "datasheet_mining.json"


def load_cache():
    fp = _cache_path()
    if fp.is_file():
        try:
            import json
            data = json.loads(fp.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def save_cache(cache):
    fp = _cache_path()
    try:
        import json
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(json.dumps(cache), encoding="utf-8")
        return True
    except OSError as e:
        # Never silent: a cache that fails to persist looks exactly like "mining
        # is slow again", which is the bug this exists to fix.
        print(f"  ! could not save the datasheet mining cache: {e}")
        return False


def _file_key(path):
    try:
        st = Path(path).stat()
        return f"{st.st_size}:{int(st.st_mtime)}"
    except OSError:
        return ""


def part_file_key(mpn, index):
    """Identity of the datasheet that would be used for this part.

    Path + size + mtime, so replacing or editing the file changes the key and the
    part is re-enriched, while an untouched file is skipped."""
    path = index.get(_loose(mpn))
    if not path:
        return ""
    fk = _file_key(path)
    return f"{path}|{fk}" if fk else ""


def has_datasheet(mpn, index):
    """Is there a local datasheet for this part? O(1), no file opened."""
    return _loose(mpn) in index


def mine_part(mpn, index, cache=None, disk_cache=None, stats=None):
    """SpecRows mined from this part's datasheet, or []."""
    key = _loose(mpn)
    path = index.get(key)
    if not path:
        return []
    if cache is not None and key in cache:
        mined = cache[key]                      # already parsed this run
    else:
        mined = None
        fkey = _file_key(path)
        if disk_cache is not None and fkey:
            hit = disk_cache.get(str(path))
            if isinstance(hit, dict) and hit.get("k") == fkey:
                mined = {kk: tuple(vv) for kk, vv in (hit.get("s") or {}).items()}
                if stats is not None:
                    stats["reused"] = stats.get("reused", 0) + 1
        if mined is None:
            # Stop reading pages as soon as the mine is productive. The specs this
            # step is after live in the page-one summary table on nearly every
            # datasheet, so the remaining pages are usually pure cost.
            def _enough(text, _n=EARLY_EXIT_SPECS):
                return len(mine_text(text)) >= _n
            # Count REAL parses. Overwriting an existing cache entry does not
            # change the dict length, so measuring progress by len(cache)
            # reported "0 newly parsed" even when a changed datasheet had just
            # been re-read.
            if stats is not None:
                stats["parsed"] = stats.get("parsed", 0) + 1
            mined = mine_text(datasheet_text(path, want=_enough))
            if disk_cache is not None and fkey:
                disk_cache[str(path)] = {"k": fkey,
                                         "s": {kk: list(vv)
                                               for kk, vv in mined.items()}}
        if cache is not None:
            cache[key] = mined
    rows = []
    for spec_key, (value, unit) in mined.items():
        snippet = f"mined from {Path(path).name}"
        if isinstance(value, str):
            rows.append(SpecRow(key=spec_key, value_text=value[:60],
                                method="datasheet", confidence=MINED_CONFIDENCE,
                                snippet=snippet))
        else:
            rows.append(SpecRow(key=spec_key, value_typ=float(value), unit=unit,
                                method="datasheet",
                                confidence=MINED_CONFIDENCE, snippet=snippet))
    return rows


def mine_parts(parts, roots=None, say=None, every=250, limit=None):
    """Mine a sequence of {'mpn': ...} records. Returns (rows_by_mpn, stats)."""
    index = build_index(roots, say=say)
    if not index:
        if say:
            say("    no local datasheets found; skipping datasheet mining")
        return {}, {"indexed": 0, "matched": 0, "mined": 0}
    out, cache = {}, {}
    matched = mined = 0
    for i, p in enumerate(parts, 1):
        mpn = p.get("mpn") if isinstance(p, dict) else p
        if not mpn:
            continue
        if _loose(mpn) in index:
            matched += 1
            rows = mine_part(mpn, index, cache)
            if rows:
                out[mpn] = rows
                mined += 1
        if say and every and i % every == 0:
            say(f"      mined {i} part(s): {matched} with a datasheet, "
                f"{mined} yielding specs")
        if limit and i >= limit:
            break
    if say:
        say(f"    datasheet mining: {matched} part(s) had a datasheet, "
            f"{mined} yielded at least one spec")
    return out, {"indexed": len(index), "matched": matched, "mined": mined}
