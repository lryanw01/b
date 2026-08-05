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
    cfg = specmatch.parse_throw_config(text[:4000])
    if cfg:
        out["throw_config"] = (cfg, "")
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
