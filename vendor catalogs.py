"""Walk vendor product catalogues and put every part into partdb.

Five vendors, each needing a different route. What is here was established by
probing the live sites (see the debug reports), not guessed:

QORVO      https://www.qorvo.com/products/product-list?categoryID=<caNNNN>
           Server-rendered PARAMETRIC TABLES. Each row carries the part number,
           a datasheet id, and the family's spec columns -- so the listing alone
           yields frequency, gain, NF, OIP3, isolation and so on. The
           /products/<family>/<sub> pages are marketing landing pages with no
           table, so only the categoryID form is useful. Datasheet ids are opaque
           (/products/d/da009265), which is why the table must be parsed rather
           than the URL guessed.

MACOM      https://www.macom.com/products/... category pages
           Parts live at /products/product-detail/<PN>. Anything else that looks
           like a last path segment is a filter facet (200_V, SWITCHES-SP3T), so
           the product-detail path is required. Datasheets follow a clean
           pattern: https://cdn.macom.com/datasheets/<PN>.pdf

SKYWORKS   https://www.skyworksinc.com/en/Products/<Category>/<PN>
           Categories are listed on /en/Products. The datasheet document number
           is opaque (SKY58281-11_205951B_PS.pdf), so each product page has to be
           opened to find its PDF link.

MARKI      https://markimicrowave.com/products/<form>/<category>/[?page=N]
           form = connectorized | surface-mount | bare-die | waveguide.
           Listings are PAGED. Their per-part PDFs are Chrome print renderings
           (producer=Skia/PDF, no text layer), so the /datasheet/ PAGE is the
           real source -- it carries the full spec tables as HTML.

MINI-CIRCUITS is not here: the local products JSON already covers it.

Politeness: one rate limit per host, robots.txt honoured per host with a hard
timeout (urllib.robotparser has none of its own and will hang forever on a host
that accepts the connection then goes quiet), every page cached on disk, and the
walk rotates between vendors so no single host sees a burst.
"""
from __future__ import annotations

import html as htmllib
import json
import os
import re
import socket
import time
import urllib.error
import urllib.parse

from . import specmatch
import urllib.request
import urllib.robotparser
from collections import Counter
from pathlib import Path

try:
    from . import partdb
    from .partdb import SpecRow, upsert_part, put_specs, put_evidence
except ImportError:                                     # loose-script fallback
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from rfparts import partdb                            # type: ignore
    from rfparts.partdb import (                           # type: ignore
        SpecRow, upsert_part, put_specs, put_evidence)

UA = ("rfparts/2.0 (RF parts sourcing research; "
      "contact: set RFPARTS_CONTACT env var)")
CACHE = partdb.DATA / "vendor_cache"
DEFAULT_RATE = 1.0
socket.setdefaulttimeout(30)

class Reporter:
    """Separate channels for separate kinds of information.

    The previous design serialised each part to JSON, prefixed it with the
    literal text "PART ROW |", and pushed it down the same progress(str) channel
    the human log used -- then the GUI re-parsed it by prefix. That tunnelled a
    data channel through a text channel: a description containing "PART ROW |",
    an embedded newline, or any log line that happened to start with "SCRAPE |"
    would corrupt it, and failures surfaced only as a parse error in a text box.

    Here a part is passed as a dict, progress text as text, and structured
    milestones as events. Nothing has to be parsed back out of a string.

    `log` keeps the old str signature so existing callers still work.
    """

    def __init__(self, log=None, part=None, event=None, cancel=None):
        self._log = log or print
        self._part = part
        self._event = event
        self._cancel = cancel

    def __call__(self, msg):                 # so `say("...")` keeps working
        self._log(msg)

    def log(self, msg):
        self._log(msg)

    def part(self, row):
        """One parsed part, as a dict. Never serialised through the log."""
        if self._part:
            try:
                self._part(row)
            except Exception:
                pass

    def event(self, **kw):
        if self._event:
            try:
                self._event(kw)
            except Exception:
                pass

    def stopped(self):
        """Cooperative cancel. A multi-vendor walk can run for hours; there was
        no way to stop one."""
        try:
            if self._cancel is None:
                return False
            return bool(self._cancel.is_set() if hasattr(self._cancel, "is_set")
                        else self._cancel())
        except Exception:
            return False


def as_reporter(progress, part=None, event=None, cancel=None):
    if isinstance(progress, Reporter):
        return progress
    return Reporter(log=progress or print, part=part, event=event,
                    cancel=cancel)


# --------------------------------------------------------------- vendor table
VENDORS = {
    "qorvo": {"name": "Qorvo", "host": "https://www.qorvo.com"},
    "macom": {"name": "MACOM", "host": "https://www.macom.com"},
    "skyworks": {"name": "Skyworks", "host": "https://www.skyworksinc.com"},
    "marki": {"name": "Marki Microwave", "host": "https://markimicrowave.com"},
    "adi": {"name": "Analog Devices", "host": "https://www.analog.com"},
}
ALL_VENDORS = list(VENDORS)

# Qorvo categoryIDs. ca0021 and ca0118 confirmed live by fetching them; the rest
# are probed and silently skipped when empty, so an unknown id costs one request.
QORVO_CATEGORY_IDS = [f"ca{n:04d}" for n in range(1, 200)]
QORVO_KNOWN_GOOD = ["ca0021", "ca0118"]

MARKI_FORMS = ["connectorized", "surface-mount", "bare-die", "waveguide"]
MARKI_KNOWN_CATEGORIES = [
    "mixers", "amplifiers", "filters", "couplers", "power-dividers", "baluns",
    "bias-tees", "equalizers", "limiters", "multipliers", "detectors",
    "attenuators", "switches", "phase-shifters", "diplexers", "terminations"]

MACOM_SEEDS = ["https://www.macom.com/products"]
SKYWORKS_SEED = "https://www.skyworksinc.com/en/Products"

# ----------------------------------------------------------- text/HTML helpers
_DROP = re.compile(r"<(script|style|noscript|svg|head)\b.*?</\1>", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")
_HREF = re.compile(r'href\s*=\s*["\']([^"\']+)["\']', re.I)
_ROW = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.S | re.I)
_CELL = re.compile(r"<(t[dh])\b[^>]*>(.*?)</\1>", re.S | re.I)
_TABLE = re.compile(r"<table\b[^>]*>(.*?)</table>", re.S | re.I)
_HEADING = re.compile(r"<h[1-4][^>]*>\s*(.{2,80}?)\s*</h[1-4]>", re.S | re.I)
_WORDY = re.compile(r"^[A-Za-z]{4,}$")


def _txt(fragment):
    s = _TAG.sub(" ", fragment or "")
    return re.sub(r"\s+", " ", htmllib.unescape(s)).strip()


def category_allowed(cat, allowed):
    return (not allowed) or (cat in allowed)


class ResumeState:
    """Which units of work are already finished, loaded once per vendor.

    Resume used to be inferred from `use_cache`, which conflated two unrelated
    ideas and meant the vendor walks never resumed at all -- only the local
    everythingRF ingest did. This reads partdb.scrape_log, so a re-run genuinely
    skips catalogue pages, product pages and datasheet files it has already
    completed."""

    def __init__(self, vendor_name, enabled):
        self.vendor = vendor_name
        self.enabled = bool(enabled)
        self._done = set()
        if self.enabled:
            try:
                self._done = {k for (_v, k) in
                              partdb.scraped_keys(vendor=vendor_name)}
            except Exception:
                self._done = set()
        self.skipped = 0

    def done(self, key):
        if self.enabled and str(key)[:400] in self._done:
            self.skipped += 1
            return True
        return False

    def mark(self, key, kind, parts_found=0, status="ok", detail=""):
        key = str(key)[:400]
        self._done.add(key)
        try:
            partdb.mark_scraped(self.vendor, key, kind=kind, status=status,
                                detail=detail, parts_found=parts_found)
        except Exception:
            pass


_NOT_A_PART = re.compile(
    r"^(evb|ev|eval|dk|dvk|ek|ref|rd)[-_]|[-_](evb|eval|kit|brd|board)(\b|$)|"
    r"^(evb|eval)", re.I)


def is_orderable_part(pn):
    """Reject evaluation boards, dev kits and reference designs.

    The parse inspector caught EVB-ADM-10699P being stored next to the amplifier
    it evaluates. An eval board has no RF performance of its own, so it pollutes
    both the pick list and every coverage percentage it lands in."""
    return not _NOT_A_PART.search((pn or "").strip())


def looks_like_pn(s):
    """A part number, not a category slug or a filter facet."""
    s = (s or "").strip().strip("-_")
    if not (2 < len(s) <= 40) or not any(c.isdigit() for c in s):
        return False
    return not any(_WORDY.match(seg) for seg in re.split(r"[-_]", s)[1:])


def _num(v):
    if v is None:
        return None
    s = str(v).replace(",", "")
    if re.search(r"\bDC\b", s, re.I) and not re.search(r"\d", s):
        return 0.0
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    return float(m.group(0)) if m else None


# Column header -> (spec key, unit hint). Units usually live in the header text
# ("Frequency Min GHz"), so they are read from there.
_COLS = [
    (r"freq(uency)?\s*min", "freq_min"), (r"freq(uency)?\s*max", "freq_max"),
    (r"^freq(uency)?\b(?!.*(min|max))", "freq_range"),
    (r"\bgain\b(?!.*block)", "gain_db"),
    (r"\bnf\b|noise figure", "nf_db"),
    (r"oip3|\bip3\b|iip3", "oip3_dbm"),
    (r"op1db|ip1db|p1db|p0\.1db|ip0\.1db", "p1db_dbm"),
    (r"insertion loss", "insertion_loss_db"),
    (r"isolation", "isolation_db"),
    (r"conversion gain", "conversion_gain_db"),
    (r"conversion loss", "conversion_loss_db"),
    (r"attenuation", "attenuation_db"),
    (r"psat|pout|output power|\bpower\b", "psat_dbm"),
    (r"switching speed", "switching_speed_ns"),
    (r"phase error|rms phase", "phase_error_deg"),
    (r"package type", "package"),
    (r"^package\b", "package_size"),
    (r"voltage|vcc|vdd", "supply_v"),
    (r"current", "current_ma"),
]


def _unit_from_header(h):
    m = re.search(r"\b(GHz|MHz|kHz|dBm|dBc|dBi|dB|ns|ps|mA|uA|V|W|mm|degrees?|%)\b",
                  h, re.I)
    return m.group(1) if m else ""


def map_headers(headers):
    """{column index: (spec key, unit)} for a parametric table."""
    out = {}
    for i, h in enumerate(headers):
        hl = h.lower()
        for pat, key in _COLS:
            if re.search(pat, hl):
                out[i] = (key, _unit_from_header(h))
                break
    return out


def parse_tables(html):
    """[(headers, [row cells...], raw_row_html), ...] for every table."""
    out = []
    for tbl in _TABLE.findall(html):
        rows = _ROW.findall(tbl)
        if len(rows) < 2:
            continue
        parsed = [[(_txt(c[1]), c[1]) for c in _CELL.findall(r)] for r in rows]
        parsed = [r for r in parsed if r]
        if len(parsed) < 2:
            continue
        headers = [c[0] for c in parsed[0]]
        out.append((headers, parsed[1:]))
    return out


# ------------------------------------------------------------ polite fetching
class Fetcher:
    """Per-host rate limit, per-host robots.txt WITH a timeout, disk cache."""

    def __init__(self, rate=DEFAULT_RATE, timeout=30, ignore_robots=False,
                 robots_timeout=10, cache=True, progress=print):
        self.rate = rate
        self.timeout = timeout
        self.ignore_robots = ignore_robots
        self.robots_timeout = robots_timeout
        self.use_cache = cache
        self.say = progress
        self._last = {}
        self._robots = {}
        self.robots_state = {}
        self.stats = Counter()
        contact = os.environ.get("RFPARTS_CONTACT", "")
        self.ua = UA if not contact else f"rfparts/2.0 (+{contact})"
        CACHE.mkdir(parents=True, exist_ok=True)

    def _cache_path(self, url):
        import hashlib
        h = hashlib.sha256(url.encode()).hexdigest()[:20]
        host = urllib.parse.urlparse(url).netloc.replace(":", "_")
        d = CACHE / host
        d.mkdir(parents=True, exist_ok=True)
        return d / (h + (".pdf" if url.lower().endswith(".pdf") else ".html"))

    def _robots_ok(self, url):
        if self.ignore_robots:
            return True, "ignored"
        host = urllib.parse.urlparse(url).netloc
        if host not in self._robots:
            rp = None
            state = "error"
            try:
                req = urllib.request.Request(f"https://{host}/robots.txt",
                                             headers={"User-Agent": self.ua})
                with urllib.request.urlopen(req,
                                            timeout=self.robots_timeout) as r:
                    text = r.read(200_000).decode("utf-8", "replace")
                rp = urllib.robotparser.RobotFileParser()
                rp.parse(text.splitlines())
                state = "ok"
            except urllib.error.HTTPError as e:
                state = "missing" if e.code == 404 else f"HTTP {e.code}"
            except Exception as e:
                state = "timeout" if "timed out" in str(e).lower() else \
                        type(e).__name__
            self._robots[host] = rp
            self.robots_state[host] = state
            self.say(f"      robots.txt {host}: {state}")
        state = self.robots_state[host]
        rp = self._robots[host]
        if state == "missing":
            return True, state
        if rp is None:
            # Unreadable robots: do not crawl this host. Failing closed is the
            # conservative reading, and it is also what stops the run silently
            # hammering a host that is refusing to state its policy.
            return False, state
        return rp.can_fetch(self.ua, url), state

    def get(self, url, kind="html", force=False):
        """(text_or_bytes, source) where source is 'cache' | 'net'."""
        path = self._cache_path(url)
        if self.use_cache and path.exists() and not force:
            self.stats["cache_hit"] += 1
            data = path.read_bytes()
            return (data if kind == "pdf"
                    else data.decode("utf-8", "replace")), "cache"
        ok, state = self._robots_ok(url)
        if not ok:
            self.stats["robots_blocked"] += 1
            raise PermissionError(f"robots.txt ({state}) disallows {url}")
        host = urllib.parse.urlparse(url).netloc
        wait = self.rate - (time.time() - self._last.get(host, 0.0))
        if wait > 0:
            time.sleep(wait)
        req = urllib.request.Request(url, headers={
            "User-Agent": self.ua,
            "Accept": "application/pdf,*/*" if kind == "pdf"
                      else "text/html,*/*"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                blob = r.read(8_000_000)
            self.stats["fetched"] += 1
        except urllib.error.HTTPError as e:
            self.stats[f"http_{e.code}"] += 1
            raise
        finally:
            self._last[host] = time.time()
        if self.use_cache:
            path.write_bytes(blob)
            # Record the URL alongside the bytes. The cache filename is a hash,
            # so without this a cached page cannot be traced back to where it came
            # from -- and the URL is what decides how it should be parsed.
            try:
                path.with_suffix(path.suffix + ".url").write_text(
                    url, encoding="utf-8")
            except OSError:
                pass
        return (blob if kind == "pdf"
                else blob.decode("utf-8", "replace")), "net"


# =============================================================== Qorvo
_QORVO_PD = re.compile(r"/products/(p|d)/([^\"'?#]+)", re.I)
# Qorvo's section titles are NOT "<h2>Name (21)</h2>". A real page says:
#   <h3 class="pst-header-title">Digital Step Attenuators
#       <span class="pst-header-amount">(21)</span></h3>
# The count sits in a NESTED span, so a [^<] run stops before it and the old
# pattern matched nothing at all -- which is why every Qorvo part was filed as
# 'ic' instead of attenuator / limiter / phase shifter.
_QORVO_HEAD = re.compile(
    r'class="pst-header-title"[^>]*>\s*([^<]{2,80}?)\s*(?:<|$)', re.I)
# kept as a fallback for any page that does use a plain heading
_QORVO_HEAD_ALT = re.compile(
    r"<h[23][^>]*>\s*([^<]{3,70}?)\s*\((\d+)\)\s*</h[23]>", re.I)

_QORVO_SECTION_CAT = [
    ("low noise amplif", ("amplifier", "lna")),
    ("power amplif", ("amplifier", "pa")),
    ("driver amplif", ("amplifier", "driver")),
    ("gain block", ("amplifier", "buffer")),
    ("variable gain", ("amplifier", "vga")),
    ("distributed amplif", ("amplifier", "")),
    ("amplif", ("amplifier", "")),
    ("attenuator", ("attenuator", "")),
    ("limiter", ("limiter", "")),
    ("phase shifter", ("phase_shifter", "")),
    ("mixer", ("mixer", "")),
    ("multiplier", ("multiplier", "")),
    ("modulator", ("modulator", "")),
    ("upconverter", ("mixer", "")), ("downconverter", ("mixer", "")),
    ("synthesizer", ("synthesizer", "")), ("vco", ("oscillator", "")),
    ("switch", ("switch", "")),
    ("filter", ("filter", "")), ("diplexer", ("filter", "")),
    ("duplexer", ("filter", "")), ("multiplexer", ("filter", "")),
    ("transformer", ("balun", "")), ("balun", ("balun", "")),
    ("beamformer", ("beamformer", "")),
    ("transistor", ("transistor", "")), ("hemt", ("transistor", "")),
    ("front end module", ("ic", "")), ("transceiver", ("transceiver", "")),
    ("pmic", ("power", "")), ("power management", ("power", "")),
    ("converter", ("power", "")),
]


def _qorvo_category(section):
    low = (section or "").lower()
    for needle, pair in _QORVO_SECTION_CAT:
        if needle in low:
            return pair
    return ("ic", "")


def walk_qorvo(fetcher, say, catids=None, max_ids=None, resume=None,
               categories=None):
    """Parts from Qorvo's parametric tables, with their spec columns."""
    ids = list(catids or (QORVO_KNOWN_GOOD +
                          [c for c in QORVO_CATEGORY_IDS
                           if c not in QORVO_KNOWN_GOOD]))
    if max_ids:
        ids = ids[:max_ids]
    say(f"    walking {len(ids)} categoryID(s) "
        f"(tables live only behind product-list?categoryID=)")
    parts, live = {}, 0
    for n, cid in enumerate(ids, 1):
        if say.stopped():
            say("      stop requested; leaving Qorvo")
            break
        url = f"https://www.qorvo.com/products/product-list?categoryID={cid}"
        if resume and resume.done(url):
            continue
        say.event(type="page", vendor="qorvo", url=url,
                  detail=f"categoryID {cid} ({n}/{len(ids)})")
        try:
            html, src = fetcher.get(url)
        except PermissionError as e:
            say(f"      {cid}: {e}")
            break
        except Exception as e:
            continue
        # section headings in document order, to label each table
        heads = [htmllib.unescape(h).strip()
                 for h in _QORVO_HEAD.findall(html)]
        if not heads:
            heads = [h[0] for h in _QORVO_HEAD_ALT.findall(html)]
        tables = parse_tables(html)
        if not tables:
            continue
        live += 1
        before = len(parts)
        for ti, (headers, rows) in enumerate(tables):
            section = heads[ti] if ti < len(heads) else ""
            cat, sub = _qorvo_category(section)
            if not category_allowed(cat, categories):
                continue
            colmap = map_headers(headers)
            for cells in rows:
                joined = " ".join(c[1] for c in cells)
                pn = ds = ""
                for kind, ident in _QORVO_PD.findall(joined):
                    if kind.lower() == "p" and not pn:
                        pn = urllib.parse.unquote(ident).strip("/")
                    elif kind.lower() == "d" and not ds:
                        ds = f"https://www.qorvo.com/products/d/{ident}"
                if not pn or not looks_like_pn(pn) or not is_orderable_part(pn):
                    continue
                rec = parts.setdefault(pn.upper(), {
                    "mpn": pn, "vendor": "Qorvo", "category": cat,
                    "subcategory": sub, "specs": {},
                    "product_url": f"https://www.qorvo.com/products/p/{pn}",
                    "datasheet_url": ds, "description": "",
                    "section": section, "source": f"qorvo:{cid}"})
                if ds and not rec["datasheet_url"]:
                    rec["datasheet_url"] = ds
                for i, (text, _raw) in enumerate(cells):
                    if i == 1 and text and not rec["description"]:
                        rec["description"] = text[:200]
                    if i not in colmap:
                        continue
                    key, unit = colmap[i]
                    _absorb_spec(rec, key, text, unit)
        got = len(parts) - before
        if resume:
            resume.mark(url, "catalog-page", parts_found=got)
        if got:
            say(f"      {cid}: {len(tables):>2} table(s), +{got} part(s)"
                f"   [{src}]   {'; '.join(heads[:2])[:52]}")
    say(f"    Qorvo: {live} live categoryID(s), {len(parts)} part(s)")
    return list(parts.values())


def _absorb_spec(rec, key, text, unit):
    """Put one table cell into rec['specs'], normalising frequency to GHz."""
    if not text or text in ("-", "--", "N/A", "n/a", "TBD"):
        return
    if key == "package":
        rec["specs"]["package"] = (text[:60], "")
        return
    if key in ("freq_min", "freq_max", "freq_range"):
        u = (unit or "GHz").lower()
        if key == "freq_range":
            m = re.search(r"(-?[\d.]+)\s*(?:to|-|–)\s*(-?[\d.]+)", text)
            if m:
                a, b = float(m.group(1)), float(m.group(2))
            else:
                v = _num(text)
                if v is None:
                    return
                a = b = v
        else:
            v = _num(text)
            if v is None:
                return
            a = b = v
        if u == "mhz":
            a, b = a / 1e3, b / 1e3
        elif u == "khz":
            a, b = a / 1e6, b / 1e6
        elif u == "hz":
            a, b = a / 1e9, b / 1e9
        if key == "freq_min":
            rec["specs"]["freq_min"] = (a, "GHz")
        elif key == "freq_max":
            rec["specs"]["freq_max"] = (b, "GHz")
        else:
            rec["specs"]["freq_min"] = (min(a, b), "GHz")
            rec["specs"]["freq_max"] = (max(a, b), "GHz")
        return
    v = _num(text)
    if v is not None:
        rec["specs"][key] = (v, unit)
        # a negative conversion gain is a loss; record both
        if key == "conversion_gain_db" and v < 0:
            rec["specs"]["conversion_loss_db"] = (abs(v), unit or "dB")


# =============================================================== MACOM
_MACOM_PART = re.compile(r"/products/product-detail/([^\"'?#/]+)", re.I)


def walk_macom(fetcher, say, max_categories=None, resume=None,
               categories=None):
    fetch_errors = Counter()
    first_error = {}
    parse_dropped = 0
    cats = []
    for seed in MACOM_SEEDS:
        try:
            html, src = fetcher.get(seed)
        except Exception as e:
            say(f"      seed {seed}: {e}")
            continue
        for href in _HREF.findall(html):
            u = urllib.parse.urljoin(seed, href.strip())
            pr = urllib.parse.urlparse(u)
            if pr.netloc != "www.macom.com":
                continue
            if not re.match(r"^/products/[a-z0-9-]+(/[a-z0-9-]+){1,4}/?$",
                            pr.path, re.I):
                continue
            if _MACOM_PART.search(pr.path):
                continue
            if u not in cats:
                cats.append(u)
    # deepest paths first: those are the leaf listings that hold parts
    cats.sort(key=lambda u: -urllib.parse.urlparse(u).path.count("/"))
    if max_categories:
        cats = cats[:max_categories]
    say(f"    {len(cats)} category page(s) discovered")
    parts = {}
    for ci, u in enumerate(cats, 1):
        if say.stopped():
            say("      stop requested; leaving MACOM")
            break
        cat, sub = _category_from_path(urllib.parse.urlparse(u).path)
        if not category_allowed(cat, categories):
            continue
        if resume and resume.done(u):
            continue
        say.event(type="page", vendor="macom", url=u,
                  detail=f"{cat} ({ci}/{len(cats)})")
        try:
            html, src = fetcher.get(u)
        except PermissionError as e:
            say(f"      {e}")
            break
        except Exception as e:
            # These were swallowed silently, so a walk that fetched nothing looked
            # identical to a walk that found nothing. Tally by error type and
            # report once at the end -- enough to tell a 403 from a timeout
            # without a message per page.
            fetch_errors[type(e).__name__] += 1
            first_error.setdefault(type(e).__name__, f"{u} -> {e}"[:160])
            continue
        before = len(parts)
        # the JSON payload first: it carries the specs, and its partNumber field
        # avoids the trailing '&' that scraping the escaped href produced
        rich = parse_macom_data_parts(html, urllib.parse.urlparse(u).path)
        parse_dropped += getattr(parse_macom_data_parts, "last_dropped", 0)
        for key, rec in rich.items():
            if not category_allowed(rec["category"], categories):
                continue
            if key not in parts:
                parts[key] = rec
        if rich:
            withspec = sum(1 for r in rich.values() if r["specs"])
            say(f"        data-part JSON: {len(rich)} part(s), "
                f"{withspec} with specs")
        for pn in {urllib.parse.unquote(m) for m in _MACOM_PART.findall(html)}:
            pn = pn.split("&")[0].strip()        # drop &#034; escape residue
            if not looks_like_pn(pn) or not is_orderable_part(pn):
                continue
            if pn.upper() in parts:
                continue
            parts.setdefault(pn.upper(), {
                "mpn": pn, "vendor": "MACOM", "category": cat,
                "subcategory": sub, "specs": {}, "description": "",
                "product_url": f"https://www.macom.com/products/"
                               f"product-detail/{pn}",
                # legacy path: only a guess, used when the JSON payload
                # for this row was unavailable
                "datasheet_url": f"https://cdn.macom.com/datasheets/"
                                 f"{urllib.parse.quote(pn, safe='+-_.')}.pdf",
                "source": "macom:" + urllib.parse.urlparse(u).path})
        got = len(parts) - before
        if resume:
            resume.mark(u, "catalog-page", parts_found=got)
        if got:
            say(f"      +{got:>4} part(s)  [{src}]  {cat:<12} "
                f"{urllib.parse.urlparse(u).path[:56]}")
    say(f"    MACOM: {len(parts)} part(s)")
    if fetch_errors:
        total = sum(fetch_errors.values())
        say(f"      {total} page(s) could not be fetched: "
            + ", ".join(f"{k} x{v}" for k, v in fetch_errors.most_common(4)))
        for k in list(fetch_errors)[:2]:
            say(f"        e.g. {first_error.get(k, '')}")
        say("        (HTTPError/403 usually means rate limiting on a cold full "
            "walk -- resume instead of rebuilding from scratch, or lower the "
            "request rate in Settings)")
    if parse_dropped:
        say(f"      {parse_dropped} data-part row(s) across the whole walk could "
            f"not be parsed (these were silently skipped before, and are not new)")
    return list(parts.values())


_PATH_CAT = [
    ("low-noise", ("amplifier", "lna")), ("lna", ("amplifier", "lna")),
    ("power-amplifier", ("amplifier", "pa")),
    ("gain-block", ("amplifier", "buffer")),
    ("driver", ("amplifier", "driver")),
    ("amplifier", ("amplifier", "")),
    ("attenuator", ("attenuator", "")), ("limiter", ("limiter", "")),
    ("phase-shifter", ("phase_shifter", "")), ("mixer", ("mixer", "")),
    ("multiplier", ("multiplier", "")), ("modulator", ("modulator", "")),
    ("switch", ("switch", "")), ("filter", ("filter", "")),
    ("coupler", ("coupler", "")), ("divider", ("divider", "")),
    ("splitter", ("divider", "")), ("balun", ("balun", "")),
    ("transformer", ("balun", "")), ("detector", ("detector", "")),
    ("oscillator", ("oscillator", "")), ("vco", ("oscillator", "")),
    ("synthesizer", ("synthesizer", "")), ("diode", ("diode", "")),
    ("transistor", ("transistor", "")), ("isolator", ("isolator", "")),
    ("circulator", ("circulator", "")), ("equalizer", ("equalizer", "")),
    ("bias-tee", ("bias_tee", "")), ("terminat", ("termination", "")),
    ("beamformer", ("beamformer", "")), ("transceiver", ("transceiver", "")),
]


def _category_from_path(path):
    low = (path or "").lower()
    for needle, pair in _PATH_CAT:
        if needle in low:
            return pair
    return ("ic", "")


# ------------------------------------------------ MACOM: the data-part payload
# Every row of a MACOM listing carries the WHOLE record in an HTML-escaped JSON
# attribute:
#   <tr data-part="{ &#034;partNumber&#034;: &#034;CGH55015&#034;, ...
#                    &#034;specs&#034;: [{&#034;specName&#034;:&#034;Gain&#034;,
#                                      &#034;uom&#034;:&#034;dB&#034;,
#                                      &#034;value&#034;:12}, ...] }">
# One page holds ~1000 parts and ~7600 spec entries. We were reading only the
# /products/product-detail/<PN> href out of those rows and discarding all of it,
# which is why thousands of MACOM parts had no specs at all.
#
# Taking partNumber from the JSON also fixes a second bug: scraping the href out
# of the ESCAPED json captured a trailing '&' (from &#034;), producing duplicate
# rows like 2N6439 and 2N6439&.

_MACOM_DATA_PART = re.compile(r'data-part\s*=\s*"(\{.*?\})"\s*>', re.S)

# specName (lowercased) -> (our key, unit family)
_MACOM_SPEC_MAP = [
    (r"^min frequency$|^frequency min$", "freq_min", "freq"),
    (r"^max frequency$|^frequency max$", "freq_max", "freq"),
    (r"^frequency$", "freq_range", "freq"),
    (r"noise figure", "nf_db", "db"),
    (r"^gain$|small signal gain", "gain_db", "db"),
    (r"peak output power|^pout$|output power|^psat$", "psat_dbm", "power"),
    # Switch speed and data rate were unmapped, so MACOM switches and
    # modulator drivers surfaced with those specs blank.
    (r"switching\s*(?:speed|time)|^t_?(?:on|off)$|rise\s*time|fall\s*time",
     "switching_time_ns", "time_ns"),
    (r"data.?rate", "data_rate_gbps", "num"),
    (r"^p1db$|p1db|1 db compression", "p1db_dbm", "dbm"),
    (r"oip3|^ip3$|iip3", "oip3_dbm", "dbm"),
    (r"isolation", "isolation_db", "db"),
    (r"insertion loss", "insertion_loss_db", "db"),
    (r"return loss", "return_loss_db", "db"),
    (r"attenuation", "attenuation_db", "db"),
    (r"efficiency", "efficiency_pct", "pct"),
    (r"operating voltage|bias voltage|supply voltage|^vd$|^vdd$", "supply_v",
     "volt"),
    (r"current", "current_ma", "ma"),
    (r"^package$|package type", "package", "text"),
]
_MACOM_ATTR_TEXT = {
    "short description": "description", "form": "form_factor",
    "technology": "process", "frequency band": "frequency_band",
    "features": "features", "application": "industry_application",
    # MACOM states the package under this attribute, not in the specs array. It
    # was unmapped, which is the missing-spec audit's MACOM 'package' gap.
    "package category": "package", "package": "package",
    "package type": "package", "configuration": "configuration",
    "switch type": "configuration", "throws": "configuration",
}


def _macom_scale(value, uom, family):
    """Normalise a JSON spec value to our unit conventions."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None, ""
    u = str(uom or "").strip().lower()
    if family == "freq":
        if u in ("mhz",):
            return v / 1e3, "GHz"
        if u in ("khz",):
            return v / 1e6, "GHz"
        if u in ("hz",):
            return v / 1e9, "GHz"
        return v, "GHz"
    if family == "power":
        # MACOM quotes power in watts; everything downstream speaks dBm
        if u in ("w", "watt", "watts"):
            if v <= 0:
                return None, ""
            import math
            return round(10.0 * math.log10(v * 1000.0), 1), "dBm"
        return v, "dBm"
    if family == "time_ns":
        # Normalise to nanoseconds. MACOM quotes switching speed in ns or us
        # depending on the switch family; without this a 2 us part would store a
        # bare 2 and rank as faster than a 50 ns one.
        if u in ("ns", "nsec"):
            return v, "ns"
        if u in ("us", "usec", "\u00b5s", "\u03bcs"):
            return v * 1e3, "ns"
        if u in ("ms", "msec"):
            return v * 1e6, "ns"
        if u in ("s", "sec"):
            return v * 1e9, "ns"
        return None, ""          # unknown unit: refuse rather than guess
    if family == "dbm":
        return v, "dBm"
    if family == "db":
        return v, "dB"
    if family == "pct":
        return v, "%"
    if family == "volt":
        return v, "V"
    if family == "ma":
        return (v * 1000.0, "mA") if u == "a" else (v, "mA")
    return v, uom or ""


def parse_macom_data_parts(html, page_path="", say=None):
    """Records from the data-part JSON on a MACOM listing page.

    `say`, when given, is called with a one-line note if any row failed to parse,
    so a systematic payload change shows up instead of quietly halving the yield.
    """
    out = {}
    dropped = 0
    for raw in _MACOM_DATA_PART.findall(html):
        try:
            # strict=False is load-bearing. MACOM descriptions occasionally end
            # with a raw control character (ENGAD00039 carries a vertical tab),
            # and strict JSON forbids unescaped control characters inside a
            # string. The whole record then raised, was swallowed by the except,
            # and the part landed in the database with NO specs at all -- which
            # is exactly what the missing-spec audit reported as a parser gap for
            # its frequency, gain and Psat. One stray byte should not cost a part
            # every spec it has.
            d = json.loads(htmllib.unescape(raw), strict=False)
        except Exception:
            dropped += 1
            continue
        pn = str(d.get("partNumber") or "").strip()
        if not pn or not looks_like_pn(pn) or not is_orderable_part(pn):
            continue
        url = str(d.get("partUrl") or "")
        if url and not url.startswith("http"):
            url = "https://www.macom.com" + url
        # MACOM publishes the real datasheet URL in this very payload, in
        # datasheetHref. It used to be ignored in favour of a URL GUESSED from the
        # part number, and measured against 1125 real records the guess is right
        # only 48% of the time: 40% have a different filename (PA511 ->
        # PA511_SMPA511.pdf, CAL7 -> AL7_SMAL7.pdf, MAGx-... -> MAGX-...) and 12%
        # publish no datasheet at all, so the guess was a request that could never
        # succeed. That is a ~52% download failure rate manufactured by us, plus
        # the rate limiting that hundreds of doomed requests provoke.
        href = str(d.get("datasheetHref") or "").strip()
        if href and not href.startswith("http"):
            href = "https://www.macom.com" + href
        rec = {"mpn": pn, "vendor": "MACOM", "specs": {}, "description": "",
               "product_url": url or f"https://www.macom.com/products/"
                                     f"product-detail/{pn}",
               # No href means MACOM lists no datasheet: leave it EMPTY rather
               # than inventing one, so the downloader skips the part instead of
               # spending a request to earn a 404.
               "datasheet_url": href,
               "source": f"macom:data-part{(':' + page_path) if page_path else ''}"}
        for spec in (d.get("specs") or []):
            name = str(spec.get("specName") or "").strip().lower()
            if not name:
                continue
            matched = False
            for pat, key, family in _MACOM_SPEC_MAP:
                if re.search(pat, name):
                    matched = True
                    if family == "text":
                        txt = str(spec.get("value") or "").strip()
                        if txt:
                            rec["specs"].setdefault(key, (txt[:60], ""))
                    else:
                        val, unit = _macom_scale(spec.get("value"),
                                                 spec.get("uom"), family)
                        if val is not None:
                            if key == "freq_range":
                                rec["specs"].setdefault("freq_min", (val, unit))
                                rec["specs"].setdefault("freq_max", (val, unit))
                            else:
                                rec["specs"].setdefault(key, (val, unit))
                    break
            if not matched:
                # Anything MACOM words differently from our regex list. The
                # shared matcher resolves it or leaves it alone; either way the
                # spec is no longer dropped just for being phrased unusually.
                sk, skind = specmatch.resolve(name)
                if sk:
                    fam = {"freq": "freq", "time_ns": "time_ns",
                           "power": "power", "text": "text"}.get(skind, "num")
                    if fam == "text":
                        txt = str(spec.get("value") or "").strip()
                        if txt:
                            rec["specs"].setdefault(sk, (txt[:60], ""))
                    else:
                        val, unit = _macom_scale(spec.get("value"),
                                                 spec.get("uom"), fam)
                        if val is not None:
                            rec["specs"].setdefault(sk, (val, unit))
        for attr in (d.get("attributes") or []):
            name = str(attr.get("attributeName") or "").strip().lower()
            val = attr.get("value")
            if not name or val in (None, "", "null"):
                continue
            key = _MACOM_ATTR_TEXT.get(name)
            if not key:
                # 'Package Category' -> package. MACOM states the package in the
                # attributes array, not the specs array, and this table had no
                # entry for it -- the audit's one MACOM parser gap.
                sk, skind = specmatch.resolve(name)
                if sk and skind == "text":
                    key = sk
            if key == "description":
                rec["description"] = str(val)[:200]
            elif key:
                prev = rec["specs"].get(key)
                if prev:                       # several Application rows
                    joined = f"{prev[0]}, {val}"[:120]
                    rec["specs"][key] = (joined, "")
                else:
                    rec["specs"][key] = (str(val)[:120], "")
        cat, sub = _category_from_path(
            " ".join([rec["description"],
                      str(rec["specs"].get("form_factor", ("",))[0]),
                      page_path]))
        rec["category"], rec["subcategory"] = cat, sub
        out[pn.upper()] = rec
    # Deliberately NOT reported per page. A note on every page turned a small,
    # pre-existing number of unparseable rows into a wall of warnings that read
    # like a new failure. The count is handed back so the caller can report it
    # ONCE for the whole walk.
    parse_macom_data_parts.last_dropped = dropped
    return out


# =============================================================== Skyworks
_SKY_PART = re.compile(r"^/en/Products/([^/]+)/([^/?#]+)/?$", re.I)
_SKY_CAT = re.compile(r"^/en/Products/([^/?#]+)/?$", re.I)


def walk_skyworks(fetcher, say, max_categories=None, fetch_product_pages=True,
                  resume=None, category_filter=None,
                  max_products=None):
    try:
        html, src = fetcher.get(SKYWORKS_SEED)
    except Exception as e:
        say(f"      seed failed: {e}")
        return []
    cats = []
    for href in _HREF.findall(html):
        u = urllib.parse.urljoin(SKYWORKS_SEED, href.strip().split("#")[0])
        pr = urllib.parse.urlparse(u)
        if pr.netloc == "www.skyworksinc.com" and _SKY_CAT.match(pr.path):
            if u not in cats:
                cats.append(u)
    if max_categories:
        cats = cats[:max_categories]
    say(f"    {len(cats)} category page(s) discovered")
    parts = {}
    for ci, u in enumerate(cats, 1):
        if say.stopped():
            say("      stop requested; leaving Skyworks")
            break
        if resume and resume.done(u):
            continue
        say.event(type="page", vendor="skyworks", url=u,
                  detail=f"category ({ci}/{len(cats)})")
        try:
            page, src = fetcher.get(u)
        except PermissionError as e:
            say(f"      {e}")
            break
        except Exception:
            continue
        before = len(parts)
        for href in _HREF.findall(page):
            pu = urllib.parse.urljoin(u, href.strip().split("#")[0])
            pr = urllib.parse.urlparse(pu)
            m = _SKY_PART.match(pr.path)
            if not m or pr.netloc != "www.skyworksinc.com":
                continue
            pn = urllib.parse.unquote(m.group(2))
            if not looks_like_pn(pn) or not is_orderable_part(pn):
                continue
            cat, sub = _category_from_path(m.group(1))
            if not category_allowed(cat, category_filter):
                continue
            parts.setdefault(pn.upper(), {
                "mpn": pn, "vendor": "Skyworks", "category": cat,
                "subcategory": sub, "specs": {}, "description": "",
                "product_url": pu, "datasheet_url": "",
                "source": "skyworks:" + m.group(1)})
        got = len(parts) - before
        if resume:
            resume.mark(u, "catalog-page", parts_found=got)
        if got:
            say(f"      +{got:>4} part(s)  [{src}]  "
                f"{urllib.parse.urlparse(u).path[:58]}")
    say(f"    Skyworks: {len(parts)} part(s) from listings")

    # The datasheet document number is opaque, so the product page is the only
    # place the PDF URL can be found.
    if fetch_product_pages:
        todo = list(parts.values())
        if max_products:
            todo = todo[:max_products]
        say(f"    opening {len(todo)} product page(s) for datasheet links")
        found = 0
        for i, rec in enumerate(todo, 1):
            if say.stopped():
                say("      stop requested; leaving Skyworks product pages")
                break
            pkey = f"product:{rec['mpn']}"
            if resume and resume.done(pkey):
                continue
            say.event(type="product", vendor="skyworks",
                      url=rec["product_url"],
                      detail=f"{rec['mpn']} ({i}/{len(todo)})")
            try:
                page, src = fetcher.get(rec["product_url"])
            except PermissionError as e:
                say(f"      {e}")
                break
            except Exception:
                continue
            pdfs = [urllib.parse.urljoin(rec["product_url"], h)
                    for h in _HREF.findall(page)
                    if re.search(r"\.pdf($|[?#])", h, re.I)]
            best = _best_pdf(pdfs, rec["mpn"])
            if best:
                rec["datasheet_url"] = best
                found += 1
            if resume:
                resume.mark(pkey, "product-page", parts_found=1 if best else 0)
            if i % 25 == 0 or i == len(todo):
                say(f"      {i}/{len(todo)} page(s), {found} datasheet link(s)")
        say(f"    Skyworks: {found} datasheet URL(s) resolved")
    return list(parts.values())


_CATALOGUE_WORDS = re.compile(
    r"catalog|catalogue|brochure|selection|short.?form|price|portfolio", re.I)


def _best_pdf(urls, pn):
    """Prefer a PDF whose filename names the part; never a catalogue."""
    key = re.sub(r"[^A-Z0-9]", "", (pn or "").upper())
    best, best_score = None, 0
    for u in dict.fromkeys(urls):
        name = urllib.parse.unquote(u.rsplit("/", 1)[-1])
        flat = re.sub(r"[^A-Z0-9]", "", name.upper())
        score = 0
        if key and key in flat:
            score += 100
        elif key and len(key) > 5 and key[:6] in flat:
            score += 40
        if _CATALOGUE_WORDS.search(name):
            score -= 200
        if score > best_score:
            best, best_score = u, score
    return best


# =============================================================== Marki
_MARKI_PART = re.compile(
    r"^(?:https?://[^/]+)?(/products/[a-z-]+/[a-z0-9-]+/"
    r"([A-Za-z0-9][A-Za-z0-9._-]*)/?)$", re.I)
# 'copyright ©' and 'privacy policy' appear INSIDE Marki's datasheet body (they
# stamp "Rev: - | Copyright © 2025 ..." into the revision history), and trimming
# there cut the Electrical Specifications table off. Only strong footers here.
_MARKI_END = ("stay informed", "be the first to know")
_MARKI_START = ("device overview", "general description")
_SPEC_MARK = re.compile(r"electrical specification|absolute maximum|"
                        r"recommended operating|package information", re.I)


def marki_datasheet_text(raw):
    """Readable datasheet text from a Marki /datasheet/ page.

    Their per-part PDFs are Chrome print renderings with no text layer, so this
    HTML is the real source; it carries General Description, Features, Absolute
    Maximum Ratings, Electrical Specifications and Package Information."""
    s = _DROP.sub(" ", raw)
    s = re.sub(r"</(td|th)\s*>", " | ", s, flags=re.I)
    s = re.sub(r"</(p|div|li|tr|h[1-6]|table|section|br)\s*/?>", "\n", s,
               flags=re.I)
    s = _TAG.sub(" ", s)
    s = htmllib.unescape(s)
    s = re.sub(r"[ \t\xa0]+", " ", s)
    lines = [ln.strip(" |").strip() for ln in s.split("\n")]
    lines = [ln for ln in lines if ln]
    lo = 0
    for i, ln in enumerate(lines):
        if any(h in ln.lower() for h in _MARKI_START):
            lo = i
            break
    hi = len(lines)
    for i in range(lo + 1, len(lines)):
        if any(h in lines[i].lower() for h in _MARKI_END):
            hi = i
            break
    body = lines[lo:hi] or lines
    seen, out = set(), []
    for ln in body:
        k = re.sub(r"\s+", " ", ln.lower())
        if len(k) > 12 and k in seen:
            continue
        seen.add(k)
        out.append(ln)
    return "\n".join(out).strip()


def walk_marki(fetcher, say, forms=None, categories=None, max_pages=40,
               resume=None, category_filter=None,
               fetch_datasheets=True, max_products=None, on_records=None,
               batch_size=15):
    forms = forms or MARKI_FORMS
    parts = {}
    pending = []

    def flush(force=False, update=False):
        nonlocal pending
        if on_records and pending and (force or len(pending) >= batch_size):
            try:
                on_records(list(pending), update=update)
            except TypeError:            # older callback without the flag
                on_records(list(pending))
            pending = []
    for form in forms:
        cats = categories
        if not cats:
            cats = []
            try:
                html, src = fetcher.get(f"https://markimicrowave.com/products/"
                                        f"{form}/")
                for href in _HREF.findall(html):
                    m = re.match(rf"^(?:https?://[^/]+)?/products/"
                                 rf"{re.escape(form)}/([a-z0-9-]+)/?$",
                                 href.strip(), re.I)
                    if m and not looks_like_pn(m.group(1)):
                        c = m.group(1).lower()
                        if c not in cats:
                            cats.append(c)
            except Exception as e:
                say(f"      {form}: category listing failed ({e})")
            cats = cats or list(MARKI_KNOWN_CATEGORIES)
        say(f"    {form}: {len(cats)} category slug(s)")
        for cat in cats:
            ccat, _ = _category_from_path(cat)
            if not category_allowed(ccat, category_filter):
                continue
            if say.stopped():
                say("      stop requested; leaving Marki")
                return list(parts.values())
            page, empties, before = 1, 0, len(parts)
            while page <= max_pages:
                url = (f"https://markimicrowave.com/products/{form}/{cat}/"
                       if page == 1 else
                       f"https://markimicrowave.com/products/{form}/{cat}/"
                       f"?page={page}")
                if resume and resume.done(url):
                    page += 1
                    continue
                say.event(type="page", vendor="marki", url=url,
                          detail=f"{form}/{cat} page {page}")
                try:
                    html, src = fetcher.get(url)
                except PermissionError as e:
                    say(f"      {e}")
                    return list(parts.values())
                except Exception:
                    break
                new = 0
                for href in _HREF.findall(html):
                    m = _MARKI_PART.match(href.strip())
                    if not m or not looks_like_pn(m.group(2)) \
                            or not is_orderable_part(m.group(2)):
                        continue
                    pn = m.group(2).upper()
                    if pn in parts:
                        continue
                    ccat, sub = _category_from_path(cat)
                    parts[pn] = {
                        "mpn": m.group(2), "vendor": "Marki Microwave",
                        "category": ccat, "subcategory": sub, "specs": {},
                        "description": "",
                        "product_url": urllib.parse.urljoin(
                            "https://markimicrowave.com", m.group(1)),
                        "datasheet_url": "",
                        "source": f"marki:{form}/{cat}"}
                    pending.append(parts[pn])
                    flush()
                    new += 1
                # one record per PAGE, which is the unit a resumed run skips
                if resume:
                    resume.mark(url, "catalog-page", parts_found=new)
                if not new:
                    empties += 1
                    if empties >= 1:
                        break
                page += 1
            # per-category reporting. This previously sat one level out, at the
            # FORM loop, with a second resume.mark() that referenced `url` and
            # `got` from whatever the last page happened to be -- and raised
            # UnboundLocalError outright when a category filter skipped every
            # category, because neither name was ever assigned.
            got = len(parts) - before
            if got:
                say(f"      {form}/{cat:<16} +{got:>4} part(s) over "
                    f"{max(1, page - 1)} page(s)")
                say.event(type="parts", vendor="marki", new=got,
                          total=len(parts), detail=f"{form}/{cat}")
    flush(force=True)
    say(f"    Marki: {len(parts)} part(s) from listings")

    if fetch_datasheets:
        todo = list(parts.values())
        if max_products:
            todo = todo[:max_products]
        say(f"    reading {len(todo)} /datasheet/ page(s) for spec text")
        withspec = 0
        for i, rec in enumerate(todo, 1):
            if say.stopped():
                say("      stop requested; leaving Marki datasheet pages")
                break
            ds = rec["product_url"].rstrip("/") + "/datasheet/"
            dkey = f"datasheet:{rec['mpn']}"
            if resume and resume.done(dkey):
                continue
            say.event(type="product", vendor="marki", url=ds,
                      detail=f"{rec['mpn']} ({i}/{len(todo)})")
            try:
                html, src = fetcher.get(ds)
            except PermissionError as e:
                say(f"      {e}")
                break
            except Exception:
                continue
            text = marki_datasheet_text(html)
            if not text:
                continue
            rec["datasheet_url"] = ds
            rec["datasheet_text"] = text
            if _SPEC_MARK.search(text):
                withspec += 1
                _marki_specs_from_text(rec, text)
            if not rec["description"]:
                m = re.search(r"General Description\s*\n(.{20,300})", text)
                if m:
                    rec["description"] = m.group(1).strip()[:200]
            pending.append(rec)
            flush(update=True)
            if i % 15 == 0 or i == len(todo):
                say(f"      {i}/{len(todo)} page(s), {withspec} with spec tables")
        flush(force=True, update=True)
        say(f"    Marki: {withspec} part(s) with spec tables")
    return list(parts.values())


# Marki's spec tables come out of the HTML as pipe-separated rows with a HEADER
# row naming the columns, e.g.
#   Parameter | Test Conditions | Minimum Frequency (GHz) | Maximum Frequency (GHz) | Min | Typ | Max | Unit
#   Small Signal Gain | 3V bias, -30 dBm Input Power | 2 | 20 | - | 15.1 | - | dB
# A naive "take the middle number" read 20 (the max frequency) for every
# parameter, so the columns have to be mapped from the header first.
_MARKI_PARAM_KEYS = [
    (r"small signal gain|^gain\b", "gain_db"),
    (r"noise figure|^nf\b", "nf_db"),
    (r"output ip3|input ip3|\boip3\b|\biip3\b|\bip3\b", "oip3_dbm"),
    (r"output p1db|input p1db|\bp1db\b", "p1db_dbm"),
    (r"reverse isolation|isolation", "isolation_db"),
    (r"conversion loss", "conversion_loss_db"),
    (r"insertion loss", "insertion_loss_db"),
    (r"input return loss", "input_return_loss_db"),
    (r"output return loss", "output_return_loss_db"),
    (r"current consumption|positive dc current|supply current", "current_ma"),
    (r"power supply dc voltage|drain supply voltage", "supply_v"),
    (r"maximum operating temperature", "temp_max_c"),
    (r"minimum operating temperature", "temp_min_c"),
    (r"attenuation", "attenuation_db"),
    (r"psat|saturated output power|output power", "psat_dbm"),
]


def _marki_param_key(name):
    low = (name or "").strip().lower()
    for pat, key in _MARKI_PARAM_KEYS:
        if re.search(pat, low):
            return key
    # Marki's table headers carry qualifiers and units ("Insertion Loss (dB)",
    # "Switching Speed, ns"), which the regex list above does not allow for.
    return specmatch.resolve_key(low)


# Which table a row came from decides what the number means. Absolute Maximum
# Ratings are destruction limits: the inspector caught 110 mA (abs-max) and 6 V
# (abs-max) being stored as if they were the operating current and supply, when
# the Recommended Operating table says 54 mA and 3 V. Abs-max values are still
# recorded, under absmax_* keys, so nothing is lost and nothing is confused.
_ABSMAX_KEYS = {"current_ma": "absmax_current_ma",
                "supply_v": "absmax_supply_v",
                "temp_max_c": "absmax_temp_max_c",
                "temp_min_c": "absmax_temp_min_c",
                "psat_dbm": "absmax_input_power_dbm"}
_PERFORMANCE_KEYS = {"gain_db", "nf_db", "oip3_dbm", "p1db_dbm",
                     "isolation_db", "conversion_loss_db",
                     "insertion_loss_db", "input_return_loss_db",
                     "output_return_loss_db", "attenuation_db"}


# Marki documents every orderable variant in a "Part Ordering Options" table:
#
#   Part Number | Description | Package | Green Status | Product Lifecycle | ...
#   ADM-8344PSM | DC - 18 GHz Distributed Amplifier | PSM | RoHS ...
#
# The package sits in its own column, which is why a text sweep for package
# keywords never found it. Rows are keyed by part number and matched EXACTLY
# (case and dashes normalised): a sheet often lists the surface-mount sibling
# (-PSM) alongside the bare-die part (-PC), and copying one variant's package
# onto the other would invent a fact the page never states.
_MARKI_ORDER_HEAD = re.compile(r"part\s*number\s*\|.*?\bpackage\b", re.I)


def _norm_variant_pn(s):
    return re.sub(r"[^A-Z0-9]", "", str(s or "").upper())


def marki_order_options(text):
    """{normalised part number: package} from the Part Ordering Options table."""
    out = {}
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if "|" not in line or not _MARKI_ORDER_HEAD.search(line):
            continue
        cols = [c.strip().lower() for c in line.split("|")]
        try:
            pn_i = next(j for j, c in enumerate(cols) if c.startswith("part number"))
            pkg_i = next(j for j, c in enumerate(cols) if c.startswith("package"))
        except StopIteration:
            continue
        for row in lines[i + 1:]:
            if "|" not in row:
                # Cells wrap onto their own lines in this table, so a blank-ish
                # continuation is not the end of it; a heading is.
                if row.strip() and not row.strip().startswith(("Rev:", "www.")):
                    continue
                break
            cells = [c.strip() for c in row.split("|")]
            if len(cells) <= max(pn_i, pkg_i):
                continue
            pn, pkg = cells[pn_i], cells[pkg_i]
            if not pn or not pkg or pkg == "-":
                continue
            if pn.lower().startswith("evb"):
                continue                      # evaluation board, not the part
            out[_norm_variant_pn(pn)] = pkg[:40]
    return out


# Marki's category listings put a few headline numbers inline on each card:
#   ADM-8344PC DC - 18 GHz Distributed Amplifier P1dB: 18 dBm Gain: 18 dB Psat: -
# A dash means the part has no value for that spec, so it must NOT be read as a
# number -- the missing-spec audit flagged Psat for ADM-8344PC as a parser gap
# when the listing actually says "Psat: -".
_MARKI_INLINE = re.compile(
    r"\b(P1dB|Psat|Gain|NF|Noise Figure|Isolation|Insertion Loss|"
    r"Conversion Loss)\s*:\s*(-|[-+]?\d+(?:\.\d+)?)\s*(dBm|dB)?", re.I)
_MARKI_INLINE_KEYS = {
    "p1db": "p1db_dbm", "psat": "psat_dbm", "gain": "gain_db",
    "nf": "nf_db", "noise figure": "nf_db", "isolation": "isolation_db",
    "insertion loss": "insertion_loss_db", "conversion loss": "conversion_loss_db",
}


def marki_inline_specs(text):
    """{key: (value, unit)} from the inline 'Label: value unit' pairs."""
    out = {}
    for m in _MARKI_INLINE.finditer(text or ""):
        label = m.group(1).strip().lower()
        raw = m.group(2)
        if raw == "-":
            continue                          # explicitly no value
        key = _MARKI_INLINE_KEYS.get(label)
        if not key or key in out:
            continue
        try:
            out[key] = (float(raw), m.group(3) or "")
        except ValueError:
            continue
    return out


def _marki_specs_from_text(rec, text):
    """Read the pipe-separated spec tables, mapping columns from their header.

    Also tracks which SECTION each table sits under, because an identical
    parameter name means different things in Absolute Maximum Ratings versus
    Electrical Specifications."""
    specs = rec.setdefault("specs", {})

    # Package, from the Part Ordering Options table, for THIS part number only.
    options = marki_order_options(text)
    if options:
        rec["variant_packages"] = options
        mine = options.get(_norm_variant_pn(rec.get("mpn")))
        if mine and not specs.get("package"):
            specs["package"] = (mine, "")

    # Headline numbers written inline on listing cards ("Gain: 18 dB").
    for key, (val, unit) in marki_inline_specs(text).items():
        specs.setdefault(key, (val, unit))

    lines = text.split("\n")
    cols = None
    kind = ""
    for line in lines:
        if "|" not in line:
            cols = None
            continue
        cells = [c.strip() for c in line.split("|")]
        low = [c.lower() for c in cells]
        if low and low[0].startswith("parameter"):
            # Header row. Classify the table from its own columns:
            #   "maximum rating"          -> Absolute Maximum Ratings
            #   "nominal"                 -> Recommended Operating Conditions
            #   "typ" / frequency columns -> Electrical Specifications
            joined = " ".join(low)
            if "maximum rating" in joined:
                kind = "absmax"
            elif "nominal" in joined:
                kind = "operating"
            elif "typ" in low or "typical" in low:
                kind = "electrical"
            else:
                kind = "other"
            cols = {}
            for i, c in enumerate(low):
                if c in ("typ", "typical"):
                    cols["typ"] = i
                elif c == "min" or c == "minimum":
                    cols.setdefault("min", i)
                elif c == "max" or c == "maximum":
                    cols.setdefault("max", i)
                elif c.startswith("unit"):
                    cols["unit"] = i
                elif c.startswith("nominal"):
                    cols["nom"] = i
                elif "maximum rating" in c:
                    cols["typ"] = i
                elif "minimum frequency" in c:
                    cols["fmin"] = i
                elif "maximum frequency" in c:
                    cols["fmax"] = i
            continue
        if not cols:
            continue
        key = _marki_param_key(cells[0])
        if key and kind == "absmax":
            # a destruction limit, not a performance number
            key = _ABSMAX_KEYS.get(key)
            if key is None:
                continue
        elif key in _PERFORMANCE_KEYS and kind == "operating":
            continue        # operating conditions, not measured performance
        # frequency range travels with each row; take it once
        if "fmin" in cols and "fmax" in cols:
            a = _num(cells[cols["fmin"]]) if cols["fmin"] < len(cells) else None
            b = _num(cells[cols["fmax"]]) if cols["fmax"] < len(cells) else None
            if a is not None and b is not None and b > 0:
                rec["specs"].setdefault("freq_min", (min(a, b), "GHz"))
                rec["specs"].setdefault("freq_max", (max(a, b), "GHz"))
        if not key:
            continue
        unit = ""
        if "unit" in cols and cols["unit"] < len(cells):
            unit = cells[cols["unit"]][:8]
        val = None
        for pref in ("typ", "nom", "min", "max"):
            i = cols.get(pref)
            if i is not None and i < len(cells):
                v = _num(cells[i])
                if v is not None:
                    val = v
                    break
        if val is None:
            continue
        rec["specs"].setdefault(key, (val, unit))


# ============================================================ datasheet files
def download_datasheets(records, fetcher, say, outdir=None, limit=0, resume=None,
                        rotate=True):
    """Save datasheet PDFs (and Marki HTML) to disk, rotating between hosts."""
    outdir = Path(outdir or (partdb.DATA / "datasheets"))
    todo = [r for r in records if r.get("datasheet_url")]
    if rotate:
        buckets = {}
        for r in todo:
            buckets.setdefault(r["vendor"], []).append(r)
        order, todo = sorted(buckets), []
        while any(buckets[k] for k in order):
            for k in order:
                if buckets[k]:
                    todo.append(buckets[k].pop(0))
    if limit:
        todo = todo[:limit]
    say(f"    downloading up to {len(todo)} datasheet(s), rotating hosts")
    saved = failed = 0
    for i, r in enumerate(todo, 1):
        url = r.get("datasheet_url") or ""
        if not url:
            continue        # vendor publishes no datasheet; nothing to request
        is_pdf = url.lower().endswith(".pdf")
        folder = outdir / re.sub(r"[^A-Za-z0-9]+", "-", r["vendor"]).strip("-")
        folder.mkdir(parents=True, exist_ok=True)
        name = re.sub(r"[^A-Za-z0-9._+-]", "_", r["mpn"])[:80]
        path = folder / (name + (".pdf" if is_pdf else ".html"))
        if path.exists():
            r["local_path"] = str(path)
            continue
        fkey = f"file:{r['mpn']}"
        if resume is not None and resume.done(fkey):
            continue
        if say.stopped():
            say("      stop requested; leaving datasheet downloads")
            break
        say.event(type="datasheet", vendor=r.get("vendor", ""), url=url,
                  detail=f"{r['mpn']} ({i}/{len(todo)})")
        try:
            blob, src = fetcher.get(url, kind="pdf" if is_pdf else "html")
        except PermissionError as e:
            say(f"      {e}")
            break
        except Exception as e:
            failed += 1
            # A 404/410 will never succeed, so record it as done: without this a
            # resume re-attempts every dead URL on every run for ever. Timeouts
            # and connection errors are left unmarked, because those SHOULD be
            # retried.
            code = getattr(e, "code", None)
            if code in (404, 410, 401) and resume is not None:
                resume.mark(fkey, "datasheet-file", parts_found=0,
                            status="fail", detail=f"HTTP {code}")
            if failed <= 12:
                say(f"      [fail] {r['vendor'][:14]:<14} {r['mpn'][:22]:<22} "
                    f"{type(e).__name__}"
                    + (f" {code}" if code else ""))
            continue
        data = blob if isinstance(blob, bytes) else blob.encode("utf-8", "replace")
        if is_pdf and data[:5] != b"%PDF-":
            failed += 1
            continue
        path.write_bytes(data)
        r["local_path"] = str(path)
        if resume is not None:
            resume.mark(fkey, "datasheet-file", parts_found=1)
        saved += 1
        if saved % 25 == 0:
            say(f"      {saved} saved, {failed} failed  ({i}/{len(todo)})")
    say(f"    datasheets: {saved} saved, {failed} failed -> {outdir}")
    return saved, failed


# ============================================================ partdb writing
def write_records(records, say, source_signal="vendor-catalog"):
    """One partdb row per part, with its specs and an evidence marker."""
    n = 0
    per_vendor = Counter()
    for rec in records:
        pid = upsert_part(mpn=rec["mpn"], vendor=rec.get("vendor", ""),
                          category=rec.get("category", ""),
                          subcategory=rec.get("subcategory", ""),
                          product_url=rec.get("product_url", ""),
                          description=rec.get("description", "")[:200])
        rows = []
        s = rec.get("specs", {})
        lo, hi = s.get("freq_min"), s.get("freq_max")
        if lo or hi:
            a = (lo or hi)[0]
            b = (hi or lo)[0]
            rows.append(SpecRow(key="freq_ghz", value_min=min(a, b),
                                value_max=max(a, b), unit="GHz",
                                method="catalog", confidence=0.85,
                                source_url=rec.get("product_url", ""),
                                snippet=rec.get("section", "")[:100]))
        for key, val in s.items():
            if key in ("freq_min", "freq_max"):
                continue
            v, unit = val if isinstance(val, tuple) else (val, "")
            if isinstance(v, str):
                rows.append(SpecRow(key=key, value_text=v[:200], unit=unit,
                                    method="catalog", confidence=0.8,
                                    source_url=rec.get("product_url", "")))
            else:
                rows.append(SpecRow(key=key, value_typ=v, unit=unit,
                                    method="catalog", confidence=0.8,
                                    source_url=rec.get("product_url", "")))
        if rec.get("datasheet_url"):
            rows.append(SpecRow(key="datasheet_url",
                                value_text=rec["datasheet_url"][:200],
                                method="catalog", confidence=0.9,
                                source_url=rec.get("product_url", "")))
        if rec.get("local_path"):
            rows.append(SpecRow(key="datasheet_file",
                                value_text=str(rec["local_path"])[:200],
                                method="catalog", confidence=0.9))
        if rows:
            put_specs(pid, rows)
        put_evidence(pid, [(source_signal, 2.0, rec.get("product_url", ""),
                            rec.get("source", "")[:120])])
        n += 1
        per_vendor[rec.get("vendor", "?")] += 1
    for v, c in per_vendor.most_common():
        say(f"      {v:<18} {c:>6} row(s)")
    return n


# ==================================================================== driver
def _say_resume(say, rs):
    """Report how much a resumed run skipped -- otherwise resume looks like the
    walker silently doing nothing."""
    if rs and rs.enabled:
        say(f"    RESUME | skipped {rs.skipped} unit(s) already recorded")
        say.event(type="resume", vendor=rs.vendor, skipped=rs.skipped)


def ingest(vendors=None, rate=DEFAULT_RATE, progress=None, limits=None,
           download=True, download_limit=0, ignore_robots=False,
           adi_dir=None, use_cache=True, categories=None, resume=False,
           part=None, event=None, cancel=None):
    """Walk the selected vendors and write every part into partdb.

    vendors    subset of ('adi','qorvo','macom','skyworks','marki')
    limits     {'qorvo_ids':N,'macom_categories':N,'skyworks_categories':N,
                'skyworks_products':N,'marki_products':N,'marki_forms':[...]}
    use_cache  reuse pages already on disk (saves the request)
    resume     skip work recorded in partdb.scrape_log (saves the request AND
               the parse). Deliberately independent of use_cache: they answer
               different questions, and welding them together is why the vendor
               walks never resumed.
    part       callback(dict) for each parsed part
    event      callback(dict) for structured milestones
    cancel     threading.Event or callable; checked between requests
    """
    say = as_reporter(progress, part=part, event=event, cancel=cancel)
    vendors = [v for v in (vendors or ALL_VENDORS) if v in VENDORS]
    limits = limits or {}
    category_filter = {str(c).strip().lower() for c in (categories or []) if str(c).strip()}
    fetcher = Fetcher(rate=rate, ignore_robots=ignore_robots,
                      cache=use_cache, progress=say)
    summary = {"per_vendor": {}, "errors": [], "records": 0}
    all_records = []
    streamed_keys = set()

    def stream_records(records, update=False):
        """The ONLY place vendor records are committed.

        Two code paths used to write parts: this one (Marki, via on_records) and a
        direct write_records() call at the end of each vendor. The direct path
        neither reported parts on the part channel nor applied the category
        filter, so the live table stayed empty for Qorvo/MACOM/Skyworks and a
        category selection silently did nothing for them. Everything funnels
        through here now."""
        selected = []
        for rec in records:
            cat = str(rec.get("category", "")).strip().lower()
            key = (str(rec.get("vendor", "")).lower(),
                   str(rec.get("mpn", "")).upper())
            if key in streamed_keys and not update:
                continue                      # already committed this run
            # update=True means this record has gained specs since it was first
            # written (Marki: enumeration commits the row, the /datasheet/ page
            # supplies the specs afterwards). upsert_part + put_specs are
            # idempotent, so re-writing fills them in.
            if category_filter and cat not in category_filter:
                continue
            selected.append(rec)
        if not selected:
            return 0
        write_records(selected, say)
        for rec in selected:
            key = (str(rec.get("vendor", "")).lower(), str(rec.get("mpn", "")).upper())
            streamed_keys.add(key)
            specs = rec.get("specs", {}) or {}
            clean_specs = {}
            for k, v in specs.items():
                clean_specs[k] = v[0] if isinstance(v, (tuple, list)) and v else v
            say.part({
                "vendor": rec.get("vendor", ""), "mpn": rec.get("mpn", ""),
                "category": rec.get("category", ""),
                "subcategory": rec.get("subcategory", ""),
                "specs": clean_specs, "space": rec.get("space_variant", ""),
                "url": rec.get("product_url", ""),
                "source": rec.get("source", ""),
                "datasheet_url": rec.get("datasheet_url", ""),
                "local_path": rec.get("local_path", ""),
            })
        say.event(type="db_batch", vendor=selected[0].get("vendor", ""),
                  written=len(selected))
        return len(selected)
        say(f"DB BATCH | {selected[0].get('vendor','')} | "
            f"{len(selected)} part(s) committed")

    say("=" * 66)
    say(f"VENDOR CATALOG INGEST -- {len(vendors)} vendor(s): "
        f"{', '.join(VENDORS[v]['name'] for v in vendors)}")
    say(f"  rate {rate}s per host, cache {'on' if use_cache else 'off'} "
        f"({CACHE})")
    say(f"  resume {'ON (skipping recorded work)' if resume else 'off'}"
        + (f", categories: {', '.join(sorted(categories))}"
           if categories else ", all categories"))
    say("=" * 66)

    for v in vendors:
        t0 = time.time()
        say(f"\n  {VENDORS[v]['name']}  [{v}]")
        recs = []
        try:
            if v == "adi":
                d = adi_dir or _guess_adi_dir()
                if not d:
                    say("    no ADI parametric folder found; pass adi_dir="
                        "<folder of ADIParametricSearch*.xlsx>")
                    summary["errors"].append("adi: no parametric folder")
                else:
                    say(f"    parametric exports from {d}")
                    from . import adi_parametric_ingest as adi_p
                    say.event(type="page", vendor="adi",
                              detail="reading parametric spreadsheets")
                    c = adi_p.ingest(d, dry_run=False, verbose=False,
                                     progress=say.log, categories=categories,
                                     part=say.part)
                    summary["per_vendor"]["adi"] = {
                        "parts": c.get("parts", 0), "secs": time.time() - t0}
                    say(f"    ADI: {c.get('parts', 0)} part(s) written directly")
                    say.event(type="vendor_done", vendor="adi",
                              parts=c.get("parts", 0),
                              with_datasheet=c.get("parts", 0),
                              with_freq=c.get("with_freq", 0),
                              secs=round(time.time() - t0, 1))
                    continue
            elif v == "qorvo":
                rs = ResumeState("Qorvo", resume)
                recs = walk_qorvo(fetcher, say,
                                  max_ids=limits.get("qorvo_ids"),
                                  resume=rs, categories=categories)
                _say_resume(say, rs)
            elif v == "macom":
                rs = ResumeState("MACOM", resume)
                recs = walk_macom(fetcher, say,
                                  max_categories=limits.get("macom_categories"),
                                  resume=rs, categories=categories)
                _say_resume(say, rs)
            elif v == "skyworks":
                rs = ResumeState("Skyworks", resume)
                recs = walk_skyworks(
                    fetcher, say,
                    max_categories=limits.get("skyworks_categories"),
                    max_products=limits.get("skyworks_products"),
                    resume=rs, category_filter=categories)
                _say_resume(say, rs)
            elif v == "marki":
                rs = ResumeState("Marki Microwave", resume)
                recs = walk_marki(
                    fetcher, say, forms=limits.get("marki_forms"),
                    max_products=limits.get("marki_products"),
                    resume=rs, category_filter=categories,
                    on_records=stream_records, batch_size=15)
                _say_resume(say, rs)
        except PermissionError as e:
            say(f"    stopped: {e}")
            summary["errors"].append(f"{v}: {e}")
        except Exception as e:
            say(f"    FAILED: {type(e).__name__}: {e}")
            summary["errors"].append(f"{v}: {type(e).__name__}: {e}")
        if recs:
            if category_filter:
                before = len(recs)
                recs = [r for r in recs
                        if str(r.get("category", "")).strip().lower() in category_filter]
                say(f"    category filter: kept {len(recs)}/{before} parsed part(s)")
            with_ds = sum(1 for r in recs if r.get("datasheet_url"))
            with_freq = sum(1 for r in recs if r.get("specs", {}).get("freq_min"))
            say(f"    parsed: {len(recs)} part(s), {with_ds} datasheet URL(s), "
                f"{with_freq} with frequency")
            cats = Counter(r.get("category") for r in recs)
            say(f"    categories: " +
                ", ".join(f"{k}={n}" for k, n in cats.most_common(8)))

            # Show a parsed row as soon as this scrape returns records.  Do this
            # before optional datasheet downloads, which may take long enough to
            # make the GUI look as though parsing has not started.
            example = recs[0]
            say("EXAMPLE ROW | "
                f"vendor={example.get('vendor', '')} | "
                f"mpn={example.get('mpn', '')} | "
                f"category={example.get('category', '') or '(uncategorized)'} | "
                f"subcategory={example.get('subcategory', '') or '-'} | "
                f"url={example.get('product_url', '') or '-'}")

            # COMMIT THE PARTS FIRST, then download documents.
            #
            # This order used to be reversed, and the consequence was severe: the
            # catalogue walk marks each page done in scrape_log as it parses it,
            # but vendors like MACOM stream nothing during the walk, so all their
            # parts waited in memory for the DB write that happened AFTER the
            # datasheet phase. Interrupting a long download therefore lost every
            # part from that vendor, while its pages stayed marked complete -- so
            # a resume skipped those pages and the parts were never re-parsed.
            # Silent, permanent loss recoverable only by a full Reset.
            #
            # Downloads are the slow, failure-prone part; specs are the valuable
            # part. Persist the cheap thing before risking the expensive one.
            remaining = [r for r in recs if
                         (str(r.get("vendor", "")).lower(),
                          str(r.get("mpn", "")).upper()) not in streamed_keys]
            say(f"DB WRITE | {VENDORS[v]['name']} | {len(remaining)} "
                f"remaining parsed part(s)")
            # same path as the streamed batches: reports on the part channel and
            # honours the category filter
            written = (stream_records(remaining) or 0) if remaining else 0
            summary["records"] += written
            if download:
                say(f"    DATASHEETS | {VENDORS[v]['name']}")
                download_datasheets(recs, fetcher, say, limit=download_limit)
                # local_path is only known after downloading, so refresh the rows
                # that gained one. Cheap, and it keeps the document links correct.
                got_files = [r for r in recs if r.get("local_path")]
                if got_files:
                    stream_records(got_files, update=True)
            summary["per_vendor"][v] = {
                "parts": written, "parsed": len(recs),
                "with_datasheet": with_ds, "with_freq": with_freq,
                "secs": time.time() - t0}

    say(f"\n  fetcher: " + ", ".join(f"{k}={v}"
                                    for k, v in sorted(fetcher.stats.items())))
    summary["fetch_stats"] = dict(fetcher.stats)
    summary["robots"] = dict(fetcher.robots_state)
    return summary


def _guess_adi_dir():
    for c in (Path.home() / "Downloads" / "ADIParametrics",
              Path.home() / "Downloads" / "newSources",
              Path.home() / "Downloads"):
        if c.is_dir() and any(c.glob("ADIParametricSearch*.xlsx")):
            return c
    return None
