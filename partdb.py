"""Persistent part/spec database — the destination for mined datasheets.

SQLite (WAL) under $RFPARTS_HOME/parts.db. Everything the ephemeral-datasheet
pipeline learns lands here with provenance; the PDFs themselves are discarded
and survive only as a sha256 row in `documents`.

Design rules:
  * Specs are EAV rows with a (min, typ, max) numeric triple + unit + method +
    confidence + source URL + snippet — never a bare number with no origin.
  * DigiKey-derived data is NEVER written here (API UA sec 5.1); the DigiKey
    adapter remains an in-memory overlay at ranking time.
  * parser_version gates staleness exactly like specstore.py did.

Public API (all thread-safe; one connection per thread):
    db()                          -> sqlite3.Connection (per-thread)
    upsert_part(fields)           -> part_id
    put_specs(part_id, specs)     -> None      specs: list[SpecRow]
    put_document(sha, url, part_id, nbytes, pages) -> None
    document_seen(sha)            -> bool
    put_evidence(part_id, rows)   -> None
    put_vendor_fact(domain, **kw) -> None
    vendor_facts(domain)          -> dict
    query_candidates(category=None, f_lo=None, f_hi=None, limit=200)
                                  -> list[candidate dicts] (rank.py-shaped)
    frontier_push / frontier_pop / frontier_mark — crawl queue persistence
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field

from . import pedigree

PARSER_VERSION = 1

# Resolved by paths.py so every module agrees on one location. The old default
# (~/.rfparts) is kept only if paths.py is somehow unavailable.
def _resolve_data_root():
    """The one data root, or the closest thing to it if paths cannot be imported.

    partdb has always preferred paths.DATA_ROOT, but its FALLBACK pointed at
    ~/.rfparts while paths falls back to <repo>/Data. When the relative import
    failed -- which happens whenever this module is loaded outside the package,
    e.g. a script run from another directory -- the two silently disagreed and
    the pipeline ended up with several parts.db files: writes landing in one,
    reads coming from another, and results that appeared and vanished depending
    on how a tool was launched.

    The fallback now mirrors paths.py exactly, so the worst case is the same
    directory rather than a different one, and it says so instead of diverging
    quietly.
    """
    try:
        from .paths import DATA_ROOT
        return pathlib.Path(DATA_ROOT), None
    except Exception:
        pass
    try:
        from paths import DATA_ROOT                     # flat layout
        return pathlib.Path(DATA_ROOT), None
    except Exception as exc:
        env = os.environ.get("RFPARTS_HOME", "").strip()
        if env:
            return pathlib.Path(env), None
        # Same rule paths.py uses: <package parent>/Data.
        root = pathlib.Path(__file__).resolve().parent.parent / "Data"
        return root, (
            f"partdb: could not import paths ({type(exc).__name__}); "
            f"falling back to {root}. Set RFPARTS_HOME to be certain which "
            f"database is in use.")


_DATA_ROOT, _ROOT_WARNING = _resolve_data_root()
if _ROOT_WARNING:                                       # pragma: no cover
    import sys as _sys
    print(_ROOT_WARNING, file=_sys.stderr)
DATA = _DATA_ROOT

DB_PATH = DATA / "parts.db"

_LOCAL = threading.local()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS parts(
  id INTEGER PRIMARY KEY,
  mpn TEXT NOT NULL,
  mpn_norm TEXT NOT NULL,
  vendor TEXT NOT NULL DEFAULT '',
  category TEXT NOT NULL DEFAULT '',
  subcategory TEXT NOT NULL DEFAULT '',
  product_url TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '',
  first_seen REAL NOT NULL,
  last_seen REAL NOT NULL,
  UNIQUE(mpn_norm, vendor)
);
CREATE INDEX IF NOT EXISTS idx_parts_cat ON parts(category);

CREATE TABLE IF NOT EXISTS specs(
  part_id INTEGER NOT NULL REFERENCES parts(id) ON DELETE CASCADE,
  key TEXT NOT NULL,
  value_min REAL, value_typ REAL, value_max REAL,
  value_text TEXT,
  unit TEXT NOT NULL DEFAULT '',
  method TEXT NOT NULL DEFAULT '',
  confidence REAL NOT NULL DEFAULT 0.5,
  source_url TEXT NOT NULL DEFAULT '',
  snippet TEXT NOT NULL DEFAULT '',
  parser_version INTEGER NOT NULL,
  extracted_at REAL NOT NULL,
  PRIMARY KEY(part_id, key, source_url)
);

CREATE TABLE IF NOT EXISTS documents(
  sha256 TEXT PRIMARY KEY,
  url TEXT NOT NULL,
  part_id INTEGER REFERENCES parts(id) ON DELETE SET NULL,
  bytes_len INTEGER NOT NULL DEFAULT 0,
  pages_parsed INTEGER NOT NULL DEFAULT 0,
  parser_version INTEGER NOT NULL,
  parsed_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS qual_evidence(
  part_id INTEGER NOT NULL REFERENCES parts(id) ON DELETE CASCADE,
  signal TEXT NOT NULL,
  weight REAL NOT NULL,
  source_url TEXT NOT NULL DEFAULT '',
  snippet TEXT NOT NULL DEFAULT '',
  found_at REAL NOT NULL,
  PRIMARY KEY(part_id, signal, source_url)
);

CREATE TABLE IF NOT EXISTS vendors(
  domain TEXT PRIMARY KEY,
  name TEXT NOT NULL DEFAULT '',
  hirel_program INTEGER NOT NULL DEFAULT 0,
  facts TEXT NOT NULL DEFAULT '{}',
  updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS url_templates(
  domain TEXT NOT NULL,
  n_segments INTEGER NOT NULL,
  prefix TEXT NOT NULL,
  hits INTEGER NOT NULL DEFAULT 1,
  updated_at REAL NOT NULL,
  PRIMARY KEY(domain, n_segments, prefix)
);

-- Work already done, so a resumed run does not repeat it. One row per catalogue
-- page walked, product page opened, or datasheet fetched. The page cache alone is
-- not enough: it still costs a parse, and it records nothing about which parts
-- were written.
CREATE TABLE IF NOT EXISTS scrape_log(
  vendor TEXT NOT NULL DEFAULT '',
  key TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'ok',
  detail TEXT NOT NULL DEFAULT '',
  parts_found INTEGER NOT NULL DEFAULT 0,
  updated_at REAL NOT NULL,
  PRIMARY KEY (vendor, key)
);
CREATE INDEX IF NOT EXISTS scrape_log_vendor ON scrape_log(vendor, kind);

CREATE TABLE IF NOT EXISTS frontier(
  url TEXT PRIMARY KEY,
  priority REAL NOT NULL,
  depth INTEGER NOT NULL,
  domain TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',   -- pending | done | error | skipped
  added_at REAL NOT NULL,
  fetched_at REAL
);
CREATE INDEX IF NOT EXISTS idx_frontier_status ON frontier(status, priority);
-- Read by part_id on every search. Without these, each lookup scanned the whole
-- specs table; declared last so every referenced table already exists.
CREATE INDEX IF NOT EXISTS idx_specs_part ON specs(part_id);
CREATE INDEX IF NOT EXISTS idx_specs_part_key ON specs(part_id, key);
CREATE INDEX IF NOT EXISTS idx_qual_part ON qual_evidence(part_id);
"""


@dataclass
class SpecRow:
    """One extracted spec value with full provenance."""
    key: str
    value_min: float | None = None
    value_typ: float | None = None
    value_max: float | None = None
    value_text: str | None = None
    unit: str = ""
    method: str = "regex"          # table | regex | page | catalog
    confidence: float = 0.5
    source_url: str = ""
    snippet: str = ""


def db() -> sqlite3.Connection:
    """Per-thread connection; creates schema on first use."""
    conn = getattr(_LOCAL, "conn", None)
    if conn is not None:
        return conn
    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA)
    _LOCAL.conn = conn
    return conn


def _norm_mpn(mpn: str) -> str:
    return re.sub(r"\s+", "", str(mpn or "")).upper()


def upsert_part(*, mpn, vendor="", category="", subcategory="",
                product_url="", description="") -> int:
    now = time.time()
    conn = db()
    with conn:
        conn.execute(
            """INSERT INTO parts(mpn, mpn_norm, vendor, category, subcategory,
                                 product_url, description, first_seen, last_seen)
               VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(mpn_norm, vendor) DO UPDATE SET
                 last_seen=excluded.last_seen,
                 category=CASE WHEN excluded.category!='' THEN excluded.category ELSE parts.category END,
                 subcategory=CASE WHEN excluded.subcategory!='' THEN excluded.subcategory ELSE parts.subcategory END,
                 product_url=CASE WHEN excluded.product_url!='' THEN excluded.product_url ELSE parts.product_url END,
                 description=CASE WHEN excluded.description!='' THEN excluded.description ELSE parts.description END
            """,
            (mpn, _norm_mpn(mpn), vendor, category, subcategory,
             product_url, description, now, now))
        row = conn.execute("SELECT id FROM parts WHERE mpn_norm=? AND vendor=?",
                           (_norm_mpn(mpn), vendor)).fetchone()
    return row["id"]


def put_specs(part_id: int, rows: list[SpecRow]) -> None:
    now = time.time()
    conn = db()
    with conn:
        for r in rows:
            conn.execute(
                """INSERT INTO specs(part_id, key, value_min, value_typ, value_max,
                                     value_text, unit, method, confidence,
                                     source_url, snippet, parser_version, extracted_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(part_id, key, source_url) DO UPDATE SET
                     value_min=excluded.value_min, value_typ=excluded.value_typ,
                     value_max=excluded.value_max, value_text=excluded.value_text,
                     unit=excluded.unit, method=excluded.method,
                     confidence=excluded.confidence, snippet=excluded.snippet,
                     parser_version=excluded.parser_version,
                     extracted_at=excluded.extracted_at""",
                (part_id, r.key, r.value_min, r.value_typ, r.value_max,
                 r.value_text, r.unit, r.method, r.confidence,
                 r.source_url, r.snippet[:300], PARSER_VERSION, now))


def document_seen(sha256: str) -> bool:
    row = db().execute(
        "SELECT parser_version FROM documents WHERE sha256=?", (sha256,)).fetchone()
    return bool(row) and row["parser_version"] >= PARSER_VERSION


def put_document(sha256, url, part_id, bytes_len, pages_parsed) -> None:
    conn = db()
    with conn:
        conn.execute(
            """INSERT OR REPLACE INTO documents
               (sha256, url, part_id, bytes_len, pages_parsed, parser_version, parsed_at)
               VALUES(?,?,?,?,?,?,?)""",
            (sha256, url, part_id, bytes_len, pages_parsed,
             PARSER_VERSION, time.time()))


def put_evidence(part_id: int, rows) -> None:
    """rows: iterable of (signal, weight, source_url, snippet)."""
    conn = db()
    with conn:
        for signal, weight, source_url, snippet in rows:
            conn.execute(
                """INSERT OR REPLACE INTO qual_evidence
                   (part_id, signal, weight, source_url, snippet, found_at)
                   VALUES(?,?,?,?,?,?)""",
                (part_id, signal, weight, source_url, snippet[:300], time.time()))


def put_vendor_fact(domain: str, *, name="", hirel_program=None, **facts) -> None:
    conn = db()
    row = conn.execute("SELECT facts, hirel_program, name FROM vendors WHERE domain=?",
                       (domain,)).fetchone()
    merged = json.loads(row["facts"]) if row else {}
    merged.update(facts)
    hr = int(hirel_program) if hirel_program is not None else (row["hirel_program"] if row else 0)
    nm = name or (row["name"] if row else "")
    with conn:
        conn.execute(
            """INSERT OR REPLACE INTO vendors(domain, name, hirel_program, facts, updated_at)
               VALUES(?,?,?,?,?)""",
            (domain, nm, hr, json.dumps(merged), time.time()))


def vendor_facts(domain: str) -> dict:
    row = db().execute("SELECT * FROM vendors WHERE domain=?", (domain,)).fetchone()
    if not row:
        return {}
    out = json.loads(row["facts"])
    out["hirel_program"] = bool(row["hirel_program"])
    out["name"] = row["name"]
    return out


# --- frontier ---------------------------------------------------------------

def frontier_push(url, priority, depth, domain, requeue=False) -> bool:
    """Add a URL if never seen; returns True when newly queued.

    requeue=True additionally revives an EXISTING row: back to 'pending' at at
    least the given priority. Seed sources need this — a plain INSERT OR
    IGNORE meant a re-seeded listing whose row was already 'done' from an
    earlier run silently re-queued nothing, so its children never got the
    priority-inherited re-expansion. The fetch layer caches the page, so a
    revived listing costs ~no network.
    """
    conn = db()
    with conn:
        cur = conn.execute(
            """INSERT OR IGNORE INTO frontier(url, priority, depth, domain, status, added_at)
               VALUES(?,?,?,?, 'pending', ?)""",
            (url, priority, depth, domain, time.time()))
        if cur.rowcount == 1:
            return True
        if requeue:
            conn.execute(
                "UPDATE frontier SET status='pending', priority=MAX(priority, ?) "
                "WHERE url=?", (priority, url))
        else:
            # A re-push at higher priority must WIN on a pending row. INSERT OR
            # IGNORE alone silently discarded the new score, so children pushed
            # at 6.5 by old code stayed at 6.5 forever no matter how many
            # priority-19 re-expansions their parent listing produced.
            conn.execute(
                "UPDATE frontier SET priority=? WHERE url=? AND "
                "status='pending' AND priority < ?", (priority, url, priority))
    return cur.rowcount == 1


def frontier_boost_tokens(tokens, delta=3.0, cap=25.0) -> int:
    """Raise every pending row whose URL contains any of `tokens` by `delta`
    (bounded by cap). Drift-recovery: when a crawl wanders off-category, the
    on-category remainder of the frontier gets pulled forward."""
    toks = [t for t in tokens if t][:8]
    if not toks:
        return 0
    conn = db()
    where = " OR ".join("url LIKE ?" for _ in toks)
    args = [f"%{t}%" for t in toks]
    with conn:
        cur = conn.execute(
            f"UPDATE frontier SET priority=MIN(priority + ?, ?) "
            f"WHERE status='pending' AND ({where})",
            [delta, cap] + args)
    return cur.rowcount


def frontier_boost(domain, path_substr, priority) -> int:
    """Raise pending rows of a domain whose URL contains path_substr to at
    least `priority`. Repairs rows queued before priority inheritance existed.
    Returns the number of rows updated."""
    conn = db()
    with conn:
        cur = conn.execute(
            "UPDATE frontier SET priority=? WHERE domain=? AND status='pending' "
            "AND url LIKE ? AND priority < ?",
            (priority, domain, f"%{path_substr}%", priority))
    return cur.rowcount


# Keys with dedicated handling earlier in the spec loop. Listed once so the
# generic passthrough cannot silently reinterpret them.
# One canonical band key per mixer port, and every spelling the parsers have used
# mapped onto it. min/max/low/high/_ghz all mean the same edge of the same range.
# Each variant maps to (canonical key, which EDGE it supplies). The edge has to
# come from the key name: a "..._min" row often carries its number in value_typ
# rather than value_min, so treating every row as a whole band made the max row
# overwrite the min one and both ends came out the same.
_BAND_ALIASES = {}
for _port in ("rf", "lo", "if"):
    _canon = f"{_port}_freq_ghz"
    for _suffix, _edge in (("_min", "lo"), ("_low", "lo"),
                           ("_max", "hi"), ("_high", "hi"),
                           ("_ghz", "both"), ("_range", "both")):
        _BAND_ALIASES[f"{_port}_freq{_suffix}"] = (_canon, _edge)

# Marki combines RF and LO into shared low/high columns.  Keep that fact in the
# stored key, then fan it out to both grid bands on read.  LO drive power uses
# the same Low/High wording, so its dBm unit gets a separate range.
_SHARED_BAND_ALIASES = {
    "rf_lo_freq_low": (("rf_freq_ghz", "lo"), ("lo_freq_ghz", "lo")),
    "rf_lo_freq_min": (("rf_freq_ghz", "lo"), ("lo_freq_ghz", "lo")),
    "rf_lo_freq_high": (("rf_freq_ghz", "hi"), ("lo_freq_ghz", "hi")),
    "rf_lo_freq_max": (("rf_freq_ghz", "hi"), ("lo_freq_ghz", "hi")),
}
_POWER_RANGE_ALIASES = {
    "lo_power_dbm_low": ("lo_power_dbm", "lo"),
    "lo_power_dbm_min": ("lo_power_dbm", "lo"),
    "lo_power_dbm_high": ("lo_power_dbm", "hi"),
    "lo_power_dbm_max": ("lo_power_dbm", "hi"),
}

_SPECIAL_KEYS = (frozenset({"freq_ghz"}) | frozenset(_BAND_ALIASES) |
                 frozenset(_SHARED_BAND_ALIASES) |
                 frozenset(_POWER_RANGE_ALIASES))

_BAND_KEY = re.compile(r"^(.+)@(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?)GHz$")


def frontier_pop(domain_budget: dict[str, int] | None = None,
                 default_budget: int = 1, avoid_domains=(),
                 skip_url_tokens=()):
    """Highest-priority pending URL whose domain still has budget, or None.

    A domain absent from `domain_budget` gets `default_budget`, so manually or
    sitemap-seeded domains are crawlable (previously they read as 0 = skipped,
    which made externally seeded frontiers dead on arrival).
    """
    conn = db()
    # Scan in chunks until an eligible row is found or the frontier is truly
    # exhausted. A fixed LIMIT 50 window interacted badly with the off-category
    # skip filter: once the top-50 was all off-category rows (skipped but left
    # pending), pop returned None and the crawl exited in seconds while
    # hundreds of eligible rows sat just below the window.
    fallback = None
    offset = 0
    while offset < 5000:
        rows = conn.execute(
            "SELECT url, priority, depth, domain FROM frontier "
            "WHERE status='pending' ORDER BY priority DESC "
            "LIMIT 200 OFFSET ?", (offset,)).fetchall()
        if not rows:
            break
        offset += len(rows)
        for row in rows:
            if (domain_budget is not None
                    and domain_budget.get(row["domain"], default_budget) <= 0):
                continue
            low = row["url"].lower()
            # Off-category URLs stay PENDING (a future search for that
            # category will want them) — just never popped for this one.
            if any(t in low for t in skip_url_tokens):
                continue
            if row["domain"] in avoid_domains:
                fallback = fallback or dict(row)   # round-robin: prefer change
                continue
            return dict(row)
    return fallback


def frontier_mark(url: str, status: str) -> None:
    conn = db()
    with conn:
        conn.execute("UPDATE frontier SET status=?, fetched_at=? WHERE url=?",
                     (status, time.time(), url))


# --- read side: rank.py-shaped candidates -----------------------------------

# specs.key -> candidate specs field expected by rank.py / gui.py.
_KEY_MAP = {
    "gain_db": ("gain_db", "min"),
    "nf_db": ("noise_nf_db", "max"),
    "p1db_dbm": ("p1db_dbm", "min"),
    "oip3_dbm": ("oip3_dbm", "min"),
    "insertion_loss_db": ("insertion_loss_db", "max"),
    "isolation_db": ("isolation_db", "min"),
    "vswr": ("vswr", "max"),
    "attenuation_db": ("attenuation_db", "typ"),
    "power_w": ("power_w", "min"),
    "psat_dbm": ("psat_dbm", "min"),
    "conversion_loss_db": ("conversion_loss_db", "max"),
    # Switching speed: "max" is the guarantee-first pick, since a datasheet's
    # worst-case switching time is the one a design has to budget for.
    "switching_time_ns": ("switching_time_ns", "max"),
    "no_of_ways": ("no_of_ways", "typ"),
    "ports": ("ports", "typ"),
    # space-semiconductor radiation specs (ADI/TI catalogs), surfaced so the
    # datasheet-style results grid can show and colour them.
    "tid_krad": ("tid_krad", "typ"),
    "sel_mev": ("sel_mev", "typ"),
}


def _pick(row, pref):
    """Guarantee-first value pick: pref column, then typ, then whatever exists."""
    order = {"min": ("value_min", "value_typ", "value_max"),
             "max": ("value_max", "value_typ", "value_min"),
             "typ": ("value_typ", "value_max", "value_min")}[pref]
    for col in order:
        if row[col] is not None:
            return row[col]
    return None


# The same fact written under two names by different ingests: adi_parametric and
# one Marki path wrote switching_speed_ns, while everything that READS it -- the
# column, the filter, the ranker, the audit -- looks for switching_time_ns, so
# those parts had a switching speed nothing could see. Renamed on READ rather
# than in each writer: an ingest nobody has updated still lands in the right
# place, and rows already in the database are fixed without a re-parse.
_KEY_RENAMES = {
    "switching_speed_ns": "switching_time_ns",
    "switch_speed_ns": "switching_time_ns",
    "switching_time": "switching_time_ns",
}


# ---------------------------------------------------------------- normalising
# Vendor spellings that mean the same company. 152 distinct vendor strings with
# 3135 rows in the tail means the same manufacturer is being counted several
# times, and a vendor filter silently misses whichever spelling it was not given.
_VENDOR_ALIASES = {}
for _canon, _spellings in {
    "Mini-Circuits": ["mini circuits", "minicircuits", "mini-circuits",
                      "mini circuits ltd", "scientific components"],
    "MACOM": ["macom", "m/a-com", "ma-com", "m a com",
              "macom technology solutions", "m/a-com technology solutions"],
    "Analog Devices": ["analog devices", "adi", "analog devices inc",
                       "analog devices, inc", "hittite", "hittite microwave",
                       "linear technology", "maxim integrated"],
    "Qorvo": ["qorvo", "qorvo inc", "rfmd", "triquint"],
    "Marki Microwave": ["marki microwave", "marki", "marki microwave inc"],
    "Teledyne": ["teledyne", "teledyne microwave solutions", "teledyne e2v",
                 "teledyne defence"],
    "Texas Instruments": ["texas instruments", "ti", "texas instruments inc"],
    "Skyworks": ["skyworks", "skyworks solutions", "skyworks solutions inc"],
    "Planar Monolithics": ["planar monolithics", "planar monolithics (pmi)",
                           "pmi", "planar monolithics industries"],
    "Crane Aerospace": ["crane aerospace", "crane aerospace & electronics",
                        "crane aerospace and electronics", "crane"],
    "RF-Lambda": ["rf-lambda", "rf lambda", "rflambda"],
    "Mercury Systems": ["mercury systems", "mercury"],
    "Wainwright Instruments": ["wainwright instruments", "wainwright"],
}.items():
    for _s in _spellings:
        _VENDOR_ALIASES[_s] = _canon


def canonical_vendor(name):
    """One spelling per manufacturer. Unknown names pass through untouched --
    guessing at an unrecognised vendor would merge two real companies."""
    n = re.sub(r"[\s.,]+", " ", str(name or "").strip().lower()).strip()
    n = re.sub(r"\b(inc|incorporated|corp|corporation|ltd|llc|gmbh|co)\b", "", n)
    n = re.sub(r"\s+", " ", n).strip(" -,")
    return _VENDOR_ALIASES.get(n, str(name or "").strip())


# `package` holds THREE different facts at once -- how the part attaches, what
# body it is in, and how it is sealed -- across 1389 distinct spellings. A part
# is a QFN *and* surface mount *and* possibly hermetic, so one field cannot
# express it and no filter on it can be correct. These split the stored string
# into its parts without losing the original.
_MOUNT_PATTERNS = [
    ("connectorized", r"connectoriz|connectorised|module with connector|coax"),
    ("waveguide", r"\bwaveguide\b|\bwr-?\d+"),
    ("benchtop", r"benchtop|rackmount|rack mount|instrument"),
    ("die", r"\bbare die\b|\bdie\b|\bchip\b|chip and wire|unpackaged"),
    ("drop-in", r"drop[- ]?in"),
    ("flange", r"\bflange\b|bolt[- ]?down"),
    ("through-hole", r"through[- ]?hole|\bthru[- ]?hole\b|\btht\b|\bpth\b"),
    ("surface-mount", r"surface[- ]?mount|\bsmt\b|\bsmd\b|\bqfn\b|\blga\b|"
                      r"\blfcsp\b|\bbga\b|\bsot\b|\bsoic\b|\bmsop\b|\bdfn\b"),
    ("module", r"\bmodule\b|\bbrick\b|\bassembly\b"),
]
_BODY_RE = re.compile(
    r"\b(QFN|LFCSP|LGA|BGA|DFN|SOT-?\d*|SOIC|MSOP|TSSOP|SOP|CSP|WLCSP|"
    r"TO-?\d+|SMA|WR-?\d+|CH|DIP|PDIP|QFP|LCC|CQFP|SIP)\b", re.I)
_HERMETIC_RE = re.compile(r"hermetic|hermetically sealed|kovar|glass seal", re.I)


def split_package(raw):
    """(mount_type, package_body, hermetic) from a package string.

    Returns None for anything the string does not state -- an absent fact must
    stay absent rather than become a default that looks like data.
    """
    s = str(raw or "").strip()
    if not s:
        return None, None, None
    # "Die, Die" and "Connectorized-SMA, Connectorized" are the same value joined
    # to itself by the parsers. De-duplicate the comma parts before reading them.
    parts, seen = [], set()
    for chunk in re.split(r"\s*,\s*", s):
        c = chunk.strip()
        k = re.sub(r"[^a-z0-9]", "", c.lower())
        if c and k and k not in seen:
            seen.add(k)
            parts.append(c)
    s = ", ".join(parts)
    low = s.lower()
    mount = None
    for name, pat in _MOUNT_PATTERNS:
        if re.search(pat, low):
            mount = name
            break
    m = _BODY_RE.search(s)
    body = m.group(1).upper() if m else None
    herm = True if _HERMETIC_RE.search(s) else None
    return mount, body, herm


def dedupe_commas(raw):
    """'Die, Die' -> 'Die'. The joining bug shows up in several text fields."""
    s = str(raw or "").strip()
    if "," not in s:
        return s
    out, seen = [], set()
    for chunk in re.split(r"\s*,\s*", s):
        c = chunk.strip()
        k = re.sub(r"[^a-z0-9]", "", c.lower())
        if c and k and k not in seen:
            seen.add(k)
            out.append(c)
    return ", ".join(out)


def query_candidates(category=None, f_lo=None, f_hi=None, limit=200,
                     include_unstated_freq=True, ids=None, vendors=None):
    """Return stored parts as candidate dicts in the shape rank.py expects.

    `ids` and `vendors` let a caller that already knows WHICH parts it wants say
    so, instead of over-fetching and discarding. The catalogue browser used to
    pull limit*3 rows -- itself tripled here -- and filter in Python, so showing
    400 rows assembled the specs of roughly the whole database, on every refresh
    and every re-sort.
    """
    conn = db()
    q = "SELECT * FROM parts"
    args = []
    where = []
    if category:
        where.append("category=?")
        args.append(category)
    if vendors:
        where.append("vendor IN (%s)" % ",".join("?" * len(vendors)))
        args += list(vendors)
    if ids:
        ids = [int(i) for i in ids][:5000]
        where.append("id IN (%s)" % ",".join("?" * len(ids)))
        args += ids
    if where:
        q += " WHERE " + " AND ".join(where)
    q += " ORDER BY last_seen DESC LIMIT ?"
    # An explicit id list is already the answer -- no room needs leaving for the
    # frequency filter to throw rows away.
    args.append(limit if ids else limit * 3)
    out = []
    # Fetch the page of parts, then their specs and evidence in BULK. This loop
    # used to issue two SELECTs per part -- with the 3x over-fetch that is ~1500
    # round trips for a 250-row result, and it was the bulk of the search time.
    # Two grouped queries replace them.
    prows = list(conn.execute(q, args))
    _pids = [r["id"] for r in prows]
    specs_by, ev_by = {}, {}
    for i in range(0, len(_pids), 400):        # chunked: SQLite caps parameters
        chunk = _pids[i:i + 400]
        if not chunk:
            continue
        ph = ",".join("?" * len(chunk))
        for s in conn.execute(
                f"SELECT * FROM specs WHERE part_id IN ({ph})", chunk):
            specs_by.setdefault(s["part_id"], []).append(s)
        for e in conn.execute(
                "SELECT part_id, signal, weight, snippet FROM qual_evidence "
                f"WHERE part_id IN ({ph})", chunk):
            ev_by.setdefault(e["part_id"], []).append(e)

    for p in prows:
        specs = {}
        conf = {}
        banded = {}                       # base key -> [(lo, hi, row), ...]
        for s in specs_by.get(p["id"], ()):
            # Fold synonym keys before anything else looks at them.
            key = _KEY_RENAMES.get(s["key"], s["key"])
            val = (s["value_typ"] if s["value_typ"] is not None
                   else s["value_min"] if s["value_min"] is not None
                   else s["value_max"])

            # One Marki RF/LO edge applies to both RF and LO ranges.
            shared_aliases = _SHARED_BAND_ALIASES.get(key)
            if shared_aliases:
                for band_key, edge in shared_aliases:
                    prev = specs.get(band_key)
                    p_lo, p_hi = prev if isinstance(prev, tuple) else (None, None)
                    lo, hi = (val, p_hi) if edge == "lo" else (p_lo, val)
                    specs[band_key] = ((min(lo, hi), max(lo, hi))
                                       if lo is not None and hi is not None
                                       else (lo, hi))
                continue

            power_alias = _POWER_RANGE_ALIASES.get(key)
            if power_alias:
                range_key, edge = power_alias
                prev = specs.get(range_key)
                p_lo, p_hi = prev if isinstance(prev, tuple) else (None, None)
                lo, hi = (val, p_hi) if edge == "lo" else (p_lo, val)
                specs[range_key] = ((min(lo, hi), max(lo, hi))
                                    if lo is not None and hi is not None
                                    else (lo, hi))
                continue

            band_alias = _BAND_ALIASES.get(key)
            if band_alias:
                band_key, edge = band_alias
                # A mixer states three separate ranges -- RF, LO and IF -- and
                # they arrived under three different naming schemes depending on
                # which module parsed them (if_freq_min/max, rf_freq_low/high,
                # if_freq_ghz). None was in _KEY_MAP, so all three were parsed and
                # then dropped on read: stored in the database and impossible to
                # see, sort or filter. Folded into one canonical band per port.
                prev = specs.get(band_key)
                p_lo, p_hi = prev if isinstance(prev, tuple) else (None, None)
                if edge == "both":
                    lo = s["value_min"] if s["value_min"] is not None else val
                    hi = s["value_max"] if s["value_max"] is not None else val
                elif edge == "lo":
                    lo, hi = val, p_hi
                else:
                    lo, hi = p_lo, val
                if lo is not None and hi is not None:
                    specs[band_key] = (min(lo, hi), max(lo, hi))
                elif lo is not None or hi is not None:
                    specs[band_key] = (lo, hi)
                continue
            if key == "freq_ghz" and s["value_min"] is not None and s["value_max"] is not None:
                specs["freq_ghz"] = (s["value_min"], s["value_max"])
                continue
            bm = _BAND_KEY.match(key)
            if bm:
                banded.setdefault(bm.group(1), []).append(
                    (float(bm.group(2)), float(bm.group(3)), dict(s)))
                continue
            mapped = _KEY_MAP.get(key)
            if mapped:
                out_key, pref = mapped
                val = _pick(s, pref)
                if val is not None and s["confidence"] >= conf.get(out_key, 0):
                    specs[out_key] = val
                    conf[out_key] = s["confidence"]
            elif s["value_text"] and key in ("connector", "mount_type",
                                            "technology", "space",
                                            "space_variant", "package",
                                            "qual_level", "ti_suffix",
                                            "orderable", "source_category"):
                # "space" MUST be in this passthrough list. It was written to
                # the specs table by the qualification engine but filtered out
                # here on read, so every crawled part surfaced with no space
                # value at all — the GUI's space column showed "?" no matter
                # how much qualification evidence had been found and stored.
                specs[key] = s["value_text"]
            elif key in _SPECIAL_KEYS:
                # freq_ghz is assembled as a (min, max) TUPLE above. A row that
                # carries only one end used to fall through to the generic
                # passthrough below and overwrite that tuple with a bare float,
                # after which the frequency filter did fg[0] on a scalar, raised
                # TypeError, and every search returned nothing. Anything the
                # branches above own is skipped here rather than re-handled.
                continue
            else:
                # Everything else the parsers stored: VSWR variants, impedance,
                # supply, current, directivity, return loss, efficiency, the
                # absolute-maximum values. _KEY_MAP exists to RENAME and to pick
                # min/typ/max deliberately, not to decide what is worth keeping,
                # but read-side it was doing both -- so any spec nobody had added
                # to the map was silently dropped between the database and the
                # UI. It was stored, the part page implied it existed, and it
                # could never be shown, sorted or filtered. Pass it through under
                # its own key, guarantee-free (typ, else max, else min).
                val = _pick(s, "typ")
                if val is not None:
                    if s["confidence"] >= conf.get(key, 0):
                        specs[key] = val
                        conf[key] = s["confidence"]
                elif s["value_text"]:
                    specs.setdefault(key, s["value_text"])
        # Band-conditioned specs: choose the band matching the REQUESTED
        # frequency (best overlap; full coverage preferred). With no requested
        # band, use the worst case across bands so ranking never flatters.
        for base, rows_b in banded.items():
            mapped = _KEY_MAP.get(base)
            if not mapped:
                continue
            out_key, pref = mapped
            chosen = None
            if f_lo is not None and f_hi is not None:
                def _fit(b):
                    lo, hi, _r = b
                    overlap = max(0.0, min(hi, f_hi) - max(lo, f_lo))
                    covers = lo <= f_lo and hi >= f_hi
                    return (1 if covers else 0, overlap)
                best = max(rows_b, key=_fit)
                if _fit(best)[1] > 0:
                    chosen = best[2]
            if chosen is None:
                vals = [(_pick(r, pref), r) for _lo, _hi, r in rows_b]
                vals = [(v, r) for v, r in vals if v is not None]
                if not vals:
                    continue
                worst = max if pref == "max" else min
                chosen = worst(vals, key=lambda x: x[0])[1]
            val = _pick(chosen, pref)
            if val is not None:
                specs[out_key] = val
                specs[out_key + "_band"] = chosen["key"].split("@", 1)[-1] \
                    if "@" in chosen["key"] else ""
        fg = specs.get("freq_ghz")
        if f_lo is not None and f_hi is not None:
            # Tolerate a malformed value instead of raising: one bad spec row
            # should cost that part, not the entire result set.
            lo_hi = None
            if isinstance(fg, (tuple, list)) and len(fg) == 2:
                try:
                    lo_hi = (float(fg[0]), float(fg[1]))
                except (TypeError, ValueError):
                    lo_hi = None
            elif isinstance(fg, (int, float)):
                lo_hi = (float(fg), float(fg))   # a single stated frequency
            if lo_hi is None:
                # NO stated frequency. Previously dropped, which made a part that
                # says nothing lose to a part that says something WRONG: the wrong
                # value still satisfied the window and ranked tier A, while the
                # silent part was not in the result set at all. Keep it -- rank
                # marks the criterion unknown (45 points, tier B), so it sits
                # below a genuine match and above a genuine miss.
                if not include_unstated_freq:
                    continue
            elif lo_hi[0] > f_lo + 1e-9 or lo_hi[1] < f_hi - 1e-9:
                continue
        # Noise figure first; fall back to insertion loss, then conversion loss.
        # For a lossy network the two ARE the same number in dB, and a part that
        # quotes only insertion loss otherwise shows a blank NF column and fails
        # any NF filter. Derived here, once, so the grid and the ranking cannot
        # disagree.
        if specs.get("noise_nf_db") is None:
            for alt in ("insertion_loss_db", "conversion_loss_db"):
                v = specs.get(alt)
                if isinstance(v, (int, float)):
                    specs["noise_nf_db"] = abs(float(v))
                    specs["noise_nf_db_from"] = alt
                    break
        ev = ev_by.get(p["id"], ())
        # Normalise on READ so the whole database benefits without a re-parse.
        if specs.get("package"):
            _mt, _body, _herm = split_package(specs["package"])
            specs["package"] = dedupe_commas(specs["package"])
            if _body:
                specs.setdefault("package_body", _body)
            if _mt:
                specs.setdefault("mount_type", _mt)
            if _herm:
                specs.setdefault("hermetic", "yes")
        for _tk in ("industry_application", "configuration", "subtype",
                    "connector", "technology"):
            if isinstance(specs.get(_tk), str):
                specs[_tk] = dedupe_commas(specs[_tk])

        cand = {
            # One spelling per manufacturer, so a vendor filter matches the
            # company rather than whichever spelling the source happened to use.
            "vendor": canonical_vendor(p["vendor"]), "vendor_raw": p["vendor"],
            "model": p["mpn"], "url": p["product_url"],
            "category": p["category"] or None,
            "subcategory": p["subcategory"] or None,
            "title": p["mpn"], "description": p["description"] or "", "specs": specs,
            "source": "partdb",
            "qual_evidence": [dict(r) for r in ev],
            "qual_summary": ", ".join(
                f"{r['signal']} {'+' if r['weight'] >= 0 else ''}{r['weight']:g}"
                for r in ev[:6]),
        }
        out.append(cand)
        if len(out) >= limit:
            break
    return out


def distinct_vendors():
    """Sorted distinct, non-empty vendor names present in the parts table.
    Lets the GUI build its vendor picker from the actual dataset."""
    return [r["vendor"] for r in db().execute(
        "SELECT DISTINCT vendor FROM parts WHERE vendor != '' "
        "ORDER BY vendor COLLATE NOCASE").fetchall()]


def candidate_by_mpn(mpn, vendor=""):
    """One rank-ready candidate for a specific part, regardless of category —
    used by the live-stream path, which previously looked the part up through
    a category-filtered query and silently missed any part whose derived
    category differed from the search's."""
    conn = db()
    row = conn.execute(
        "SELECT category FROM parts WHERE mpn_norm=?" +
        (" AND vendor=?" if vendor else ""),
        ((_norm_mpn(mpn), vendor) if vendor else (_norm_mpn(mpn),))).fetchone()
    if not row:
        return None
    for c in query_candidates(category=row["category"] or None, limit=400):
        if _norm_mpn(c.get("model")) == _norm_mpn(mpn):
            return c
    return None


# --- SATNow cross-reference -------------------------------------------------
# Space qualification belongs to the PART, not to the page it was found on:
# if an MPN appears anywhere in SATNow's space parts database, the same part
# discovered on the manufacturer's own site is space qualified too.

def mpn_in_satnow(mpn) -> bool:
    """True when this MPN exists among parts sourced from satnow.com."""
    row = db().execute(
        "SELECT 1 FROM parts WHERE mpn_norm=? AND product_url LIKE '%satnow.com%' "
        "LIMIT 1", (_norm_mpn(mpn),)).fetchone()
    return row is not None

def apply_satnow_crosslink(mpn, source_url) -> int:
    """A part was just ingested FROM satnow: upgrade every same-MPN part from
    other vendors to qualified, with cross-reference evidence. Returns count."""
    conn = db()
    upgraded = 0
    for row in conn.execute(
            "SELECT id FROM parts WHERE mpn_norm=? AND product_url NOT LIKE "
            "'%satnow.com%'", (_norm_mpn(mpn),)):
        put_specs(row["id"], [SpecRow(
            key="space", value_text="qualified", method="evidence",
            confidence=0.9, source_url=source_url,
            snippet="cross-listed in SATNow space parts database")])
        put_evidence(row["id"], [("satnow-cross-listed", 8.0, source_url,
                                  "same MPN listed in SATNow space database")])
        upgraded += 1
    return upgraded


# --- learned product-URL templates ------------------------------------------
# A confirmed product page teaches the shape of that site's product URLs
# ((n_segments, static_prefix)); seeds.urls_matching_templates then pulls the
# whole catalog branch out of the sitemap on later runs, so discovery improves
# every time the crawler runs instead of restarting from keyword scoring.

def save_template(domain, template) -> None:
    if not template:
        return
    n_seg, prefix = template
    conn = db()
    with conn:
        conn.execute(
            """INSERT INTO url_templates(domain, n_segments, prefix, hits, updated_at)
               VALUES(?,?,?,1,?)
               ON CONFLICT(domain, n_segments, prefix) DO UPDATE SET
                 hits = hits + 1, updated_at = excluded.updated_at""",
            (domain, n_seg, prefix, time.time()))


def get_templates(domain, min_hits=3):
    """Templates confirmed by at least min_hits distinct product pages."""
    rows = db().execute(
        "SELECT n_segments, prefix FROM url_templates "
        "WHERE domain=? AND hits>=?", (domain, min_hits)).fetchall()
    return {(r["n_segments"], r["prefix"]) for r in rows}


# --- dataset maintenance: cross-catalog dedupe ------------------------------
# The same manufacturer part can arrive from several catalogs (everythingRF, an
# ADI/TI/Qorvo sheet, ...). merge_duplicates() collapses parts sharing a loose
# part-number key, KEEPING THE ROW WITH THE MOST SPECS and folding the others'
# specs and evidence into it, so richer wins and nothing is lost.

def _loose_key(mpn: str) -> str:
    """Cross-catalog part key: alphanumerics only, uppercase, trailing '+' kept
    (Mini-Circuits RoHS). 'XYZ-100' == 'XYZ 100' == 'xyz100'."""
    s = str(mpn or "").upper().replace("+", "\u0001")
    s = re.sub(r"[^A-Z0-9]", "", s).replace("\u0001", "+")
    return s


def _spec_count(conn, part_id):
    return conn.execute("SELECT COUNT(*) AS n FROM specs WHERE part_id=?",
                        (part_id,)).fetchone()["n"]


def _is_qualified(conn, part_id):
    row = conn.execute(
        "SELECT value_text FROM specs WHERE part_id=? AND key='space_variant'",
        (part_id,)).fetchone()
    return bool(row) and row["value_text"] == "space_qualified"


def merge_duplicates(progress=None) -> dict:
    """Collapse parts sharing a loose MPN key; return {'groups','deleted'}.

    Duplicates are matched vendor-agnostically on the alphanumeric part number
    (so the same part from everythingRF and a manufacturer sheet collapse).
    Winner = most specs, then space-qualified over grade, then longer
    description, then a real category. Losers' specs/evidence are copied onto the
    winner (their distinct source_url keeps provenance and avoids clobbering),
    then the loser rows are deleted (ON DELETE CASCADE clears the copied specs).
    """
    conn = db()
    buckets: dict[str, list[int]] = {}
    for r in conn.execute("SELECT id, mpn FROM parts"):
        buckets.setdefault(_loose_key(r["mpn"]), []).append(r["id"])
    merged = deleted = 0
    for key, ids in buckets.items():
        if not key or len(ids) < 2:
            continue
        rank = []
        for pid in ids:
            p = conn.execute("SELECT * FROM parts WHERE id=?", (pid,)).fetchone()
            rank.append((_spec_count(conn, pid), _is_qualified(conn, pid),
                         len(p["description"] or ""), bool(p["category"]), pid))
        rank.sort(reverse=True)
        winner = rank[0][4]
        losers = [pid for *_r, pid in rank[1:]]
        merged += 1
        with conn:
            for lid in losers:
                lp = conn.execute("SELECT * FROM parts WHERE id=?", (lid,)).fetchone()
                wp = conn.execute("SELECT * FROM parts WHERE id=?", (winner,)).fetchone()
                fills = {c: lp[c] for c in ("category", "subcategory",
                                            "product_url", "description")
                         if not wp[c] and lp[c]}
                if fills:
                    sets = ", ".join(f"{c}=?" for c in fills)
                    conn.execute(f"UPDATE parts SET {sets} WHERE id=?",
                                 (*fills.values(), winner))
                conn.execute(
                    """INSERT OR IGNORE INTO specs
                       SELECT ?, key, value_min, value_typ, value_max, value_text,
                              unit, method, confidence, source_url, snippet,
                              parser_version, extracted_at
                       FROM specs WHERE part_id=?""", (winner, lid))
                conn.execute(
                    """INSERT OR IGNORE INTO qual_evidence
                       SELECT ?, signal, weight, source_url, snippet, found_at
                       FROM qual_evidence WHERE part_id=?""", (winner, lid))
                conn.execute("DELETE FROM parts WHERE id=?", (lid,))
                deleted += 1
        if progress:
            progress(f"merged {key} ({len(losers)} duplicate)")
    return {"groups": merged, "deleted": deleted}



def vendor_counts() -> dict:
    """Current normalized part count grouped by vendor."""
    conn = db()
    rows = conn.execute(
        "SELECT COALESCE(NULLIF(TRIM(vendor),''),'(unknown)') AS vendor, "
        "COUNT(*) AS n FROM parts GROUP BY 1 ORDER BY n DESC, vendor"
    ).fetchall()
    return {r["vendor"]: int(r["n"]) for r in rows}

def dataset_stats() -> dict:
    """Counts for the GUI status line after a rebuild."""
    conn = db()
    parts = conn.execute("SELECT COUNT(*) AS n FROM parts").fetchone()["n"]
    qual = conn.execute(
        "SELECT COUNT(DISTINCT part_id) AS n FROM specs "
        "WHERE key='space_variant' AND value_text='space_qualified'").fetchone()["n"]
    grade = conn.execute(
        "SELECT COUNT(DISTINCT part_id) AS n FROM specs "
        "WHERE key='space_variant' AND value_text='space_grade'").fetchone()["n"]
    vendors = conn.execute(
        "SELECT COUNT(DISTINCT vendor) AS n FROM parts WHERE vendor!=''").fetchone()["n"]
    return {"parts": parts, "qualified": qual, "grade": grade, "vendors": vendors}


# --- part families: a base part number and its pedigree variants -------------
# Distinct from merge_duplicates (which collapses the *same* part). A family
# groups parts that share a base part number but differ by pedigree/screening —
# e.g. TPS7H4011-SP (space-qualified) and TPS7H4011-SEP (space-grade). These
# stay separate rows; family grouping just lets you see a part alongside its
# qualified/graded siblings.

def family_key(mpn) -> str:
    """Base part-number key shared by a part and its pedigree variants."""
    return pedigree.family_base(mpn)


def _part_pedigree(conn, part_id) -> str:
    specs = {}
    for r in conn.execute(
            "SELECT key, value_text FROM specs WHERE part_id=? "
            "AND key IN ('space','space_variant')", (part_id,)):
        specs[r["key"]] = r["value_text"]
    return pedigree.normalize(specs)


def family_members(part_id, include_self=False):
    """Sibling parts sharing the selected part's base number, strongest pedigree
    first. Each item: id, mpn, vendor, category, pedigree, pedigree_label, url."""
    conn = db()
    row = conn.execute("SELECT id, mpn FROM parts WHERE id=?", (part_id,)).fetchone()
    if not row:
        return []
    key = family_key(row["mpn"])
    if not key:
        return []
    out = []
    for r in conn.execute("SELECT id, mpn, vendor, category, product_url, "
                          "description FROM parts"):
        if r["id"] == part_id and not include_self:
            continue
        if family_key(r["mpn"]) != key:
            continue
        ped = _part_pedigree(conn, r["id"])
        # Noise figure first, insertion loss as the fallback. For a passive part
        # the two are the same number in dB, so filling noise_nf_db here means the
        # NF column, the ranking code and the coverage report all agree without
        # each needing its own special case.
        if specs.get("noise_nf_db") is None:
            for alt in ("insertion_loss_db", "conversion_loss_db"):
                v = specs.get(alt)
                if isinstance(v, (int, float)):
                    specs["noise_nf_db"] = abs(float(v))
                    specs["noise_nf_db_from"] = alt
                    break
        out.append({
            "id": r["id"], "mpn": r["mpn"], "vendor": r["vendor"],
            "category": r["category"], "url": r["product_url"],
            "description": r["description"],
            "pedigree": ped, "pedigree_label": pedigree.label(ped),
            "is_self": r["id"] == part_id,
        })
    out.sort(key=lambda m: (pedigree.rank_of(m["pedigree"]), str(m["mpn"])))
    return out


def _family_buckets(conn):
    buckets = {}
    for r in conn.execute("SELECT id, mpn FROM parts"):
        k = family_key(r["mpn"])
        if k:
            buckets.setdefault(k, []).append(r["id"])
    return buckets


def family_stats() -> dict:
    """Counts of multi-member families and how many span >1 pedigree level."""
    conn = db()
    buckets = _family_buckets(conn)
    multi = {k: ids for k, ids in buckets.items() if len(ids) > 1}
    multi_pedigree = 0
    for ids in multi.values():
        peds = {_part_pedigree(conn, pid) for pid in ids}
        if len(peds) > 1:
            multi_pedigree += 1
    return {"families": len(multi),
            "parts_in_families": sum(len(v) for v in multi.values()),
            "multi_pedigree_families": multi_pedigree}


# --- dataset health ---------------------------------------------------------
# Coverage-oriented snapshot: where is the data thick or thin? Everything is a
# straight read over partdb so it stays fast and honest.

# Which specs to report coverage on, PER CATEGORY. Keys are canonical DB spec
# keys (what the ingesters store); labels are for display. RF parametrics are
# only listed for the categories where they're meaningful, so a spec's coverage
# % is measured against the parts it actually applies to — not diluted by every
# other category. The universal set (package + radiation) is appended to every
# category, and categories not listed here fall back to frequency + universal.
_FREQ = ("frequency", "freq_ghz")
_UNIVERSAL_COVERAGE = [("package", "package"), ("TID", "tid_krad"),
                       ("SEL", "sel_mev")]
# Categories with no active gain stage. For a passive lossy network at ambient
# the noise figure equals the insertion loss in dB, so a part quoting only
# insertion loss DOES have a known noise figure -- it just is not labelled that
# way. Treating them as equivalent stops filters and coverage reporting from
# calling these parts "NF unknown".
PASSIVE_CATEGORIES = {
    "filter", "switch", "attenuator", "coupler", "divider", "balun",
    "phase_shifter", "limiter", "isolator", "circulator", "equalizer",
    "bias_tee", "termination", "diplexer", "transformer",
}
# Keys that stand in for nf_db on a passive part, best first.
NF_EQUIVALENT_KEYS = ("nf_db", "noise_nf_db", "insertion_loss_db",
                      "conversion_loss_db")


def effective_nf(category, specs):
    """(value, source_key) for the noise figure of a part, or (None, None).

    Explicit noise figure wins; otherwise insertion loss, then conversion loss.
    `category` is accepted for callers that want to reason about it, but the
    fallback is no longer gated on it -- a part quoting only insertion loss should
    still populate the NF column."""
    for key in ("nf_db", "noise_nf_db", "noise_figure_db"):
        v = specs.get(key)
        if isinstance(v, (int, float)):
            return float(v), key
    for key in ("insertion_loss_db", "conversion_loss_db"):
        v = specs.get(key)
        if isinstance(v, (int, float)):
            return abs(float(v)), key
    return None, None


_CATEGORY_COVERAGE = {
    "amplifier":     [_FREQ, ("gain", "gain_db"), ("NF", "nf_db"),
                      ("P1dB", "p1db_dbm"), ("OIP3", "oip3_dbm"), ("Psat", "psat_dbm")],
    "mixer":         [_FREQ, ("conversion loss", "conversion_loss_db"),
                      ("isolation", "isolation_db"), ("P1dB", "p1db_dbm"),
                      ("OIP3", "oip3_dbm")],
    "attenuator":    [_FREQ, ("attenuation", "attenuation_db"), ("power", "power_w")],
    "filter":        [_FREQ, ("NF / insertion loss", "insertion_loss_db")],
    "switch":        [_FREQ, ("isolation", "isolation_db"),
                      ("NF / insertion loss", "insertion_loss_db"), ("P1dB", "p1db_dbm")],
    "coupler":       [_FREQ, ("isolation", "isolation_db"),
                      ("NF / insertion loss", "insertion_loss_db")],
    "divider":       [_FREQ, ("isolation", "isolation_db"),
                      ("NF / insertion loss", "insertion_loss_db")],
    "balun":         [_FREQ, ("NF / insertion loss", "insertion_loss_db")],
    "phase_shifter": [_FREQ, ("NF / insertion loss", "insertion_loss_db")],
    "limiter":       [_FREQ, ("NF / insertion loss", "insertion_loss_db"),
                      ("power", "power_w")],
    "isolator":      [_FREQ, ("NF / insertion loss", "insertion_loss_db"),
                      ("isolation", "isolation_db")],
    "circulator":    [_FREQ, ("NF / insertion loss", "insertion_loss_db"),
                      ("isolation", "isolation_db")],
    "detector":      [_FREQ],
    "synthesizer":   [_FREQ],
    "oscillator":    [_FREQ],
    "beamformer":    [_FREQ, ("gain", "gain_db")],
    "transceiver":   [_FREQ],
    # non-RF space ICs — RF parametrics don't apply; radiation & package matter
    "data_converter": [], "power": [], "clock": [], "interface": [],
    "sensor": [], "ic": [],
    # An op-amp is not an RF amplifier. Asking it for RF gain, noise figure,
    # OIP3 and P1dB is what made 57 precision parts from ADI's space portfolio
    # look like amplifiers with missing specs.
    "op_amp": [],
}
_ALL_COVERAGE_KEYS = sorted(
    {key for specs in _CATEGORY_COVERAGE.values() for _l, key in specs}
    | {k for _l, k in _UNIVERSAL_COVERAGE} | {_FREQ[1]})


def _coverage_specs_for(category):
    """Ordered (label, db_key) coverage specs for a category with the universal
    set appended. Unlisted categories fall back to frequency + universal."""
    base = _CATEGORY_COVERAGE.get(category)
    if base is None:
        base = [_FREQ]
    out, seen = [], set()
    for lbl, key in list(base) + _UNIVERSAL_COVERAGE:
        if key not in seen:
            seen.add(key)
            out.append((lbl, key))
    return out


def dataset_health(vendor=None) -> dict:
    """Structured health snapshot: totals, category mix, pedigree distribution,
    category-aware spec coverage, radiation coverage, family & duplicate stats.

    `vendor` scopes every figure to a single vendor. This is the whole point of a
    per-vendor health window: dataset-wide coverage is dominated by whichever
    vendor contributed most rows, so it cannot tell you whether one vendor's walk
    actually collected specs. Pass the stored vendor name, or a loose fragment
    like 'marki' -- it is resolved against the distinct vendors present."""
    conn = db()
    if vendor:
        exact = conn.execute("SELECT 1 FROM parts WHERE vendor=? LIMIT 1",
                             (vendor,)).fetchone()
        if not exact:
            for r in conn.execute("SELECT DISTINCT vendor FROM parts"):
                if _vendor_matches(r["vendor"], vendor):
                    vendor = r["vendor"]
                    break
    where = " WHERE vendor=?" if vendor else ""
    args = [vendor] if vendor else []
    # scope for queries that reach parts through a join
    pwhere = " AND p.vendor=?" if vendor else ""

    parts = conn.execute(f"SELECT COUNT(*) AS n FROM parts{where}",
                         args).fetchone()["n"]
    if vendor:
        vendors = 1 if parts else 0
    else:
        vendors = conn.execute(
            "SELECT COUNT(DISTINCT vendor) AS n FROM parts "
            "WHERE vendor!=''").fetchone()["n"]

    sizes = [(r["category"], r["n"]) for r in conn.execute(
        f"SELECT category, COUNT(*) AS n FROM parts{where} "
        f"GROUP BY category ORDER BY n DESC", args)]
    by_category = [(c or "(uncategorized)", n) for c, n in sizes]

    # pedigree distribution (normalized, one bucket per part)
    dist = {k: 0 for k in pedigree.LADDER}
    pmap = {}
    for r in conn.execute(
            "SELECT s.part_id AS part_id, s.key AS key, s.value_text AS value_text "
            "FROM specs s JOIN parts p ON p.id = s.part_id "
            "WHERE s.key IN ('space','space_variant')" + pwhere, args):
        pmap.setdefault(r["part_id"], {})[r["key"]] = r["value_text"]
    for pid_row in conn.execute(f"SELECT id FROM parts{where}", args):
        ped = pedigree.normalize(pmap.get(pid_row["id"], {}))
        dist[ped] = dist.get(ped, 0) + 1

    # category-aware coverage: count parts-with-spec WITHIN each category, so the
    # % denominator is the parts the spec applies to.
    qmarks = ",".join("?" * len(_ALL_COVERAGE_KEYS))
    pair_counts = {}
    for r in conn.execute(
            f"SELECT p.category AS cat, s.key AS key, "
            f"COUNT(DISTINCT s.part_id) AS n FROM specs s "
            f"JOIN parts p ON p.id = s.part_id WHERE s.key IN ({qmarks})"
            + pwhere +
            f" GROUP BY p.category, s.key",
            list(_ALL_COVERAGE_KEYS) + args):
        pair_counts[(r["cat"], r["key"])] = r["n"]
    category_coverage = []
    for cat, size in sizes:
        rows = []
        for lbl, key in _coverage_specs_for(cat):
            n = pair_counts.get((cat, key), 0)
            rows.append((lbl, n, round(100.0 * n / size, 1) if size else 0.0))
        category_coverage.append({"category": cat or "(uncategorized)",
                                  "count": size, "specs": rows})

    rad_parts = conn.execute(
        "SELECT COUNT(DISTINCT s.part_id) AS n FROM specs s "
        "JOIN parts p ON p.id = s.part_id "
        "WHERE s.key IN ('tid_krad','sel_mev')" + pwhere, args).fetchone()["n"]

    # Source overlap. EverythingRF is identified by its dedicated evidence
    # signals or an everythingrf.com provenance URL. Direct vendor rows carry
    # the vendor-catalog evidence marker written by vendor_catalogs.py.
    source_rows = conn.execute(
        """SELECT p.id,
                  MAX(CASE WHEN q.signal LIKE 'erf-%'
                                OR LOWER(q.source_url) LIKE '%everythingrf.com%'
                           THEN 1 ELSE 0 END) AS from_erf,
                  MAX(CASE WHEN q.signal='vendor-catalog'
                           THEN 1 ELSE 0 END) AS from_vendor
             FROM parts p
             LEFT JOIN qual_evidence q ON q.part_id=p.id
             """ + ("WHERE p.vendor=? " if vendor else "") +
        """GROUP BY p.id""", args).fetchall()
    source_overlap = {"everythingrf_only": 0, "vendor_only": 0,
                      "both": 0, "other": 0}
    for r in source_rows:
        # NB: do not name these `vendor` -- that would rebind the scoping
        # argument mid-function and silently unscope every query after it.
        from_erf, from_vendor = bool(r["from_erf"]), bool(r["from_vendor"])
        if from_erf and from_vendor:
            source_overlap["both"] += 1
        elif from_erf:
            source_overlap["everythingrf_only"] += 1
        elif from_vendor:
            source_overlap["vendor_only"] += 1
        else:
            source_overlap["other"] += 1

    # remaining exact-duplicate collisions (loose key)
    dup_buckets = {}
    for r in conn.execute(f"SELECT id, mpn FROM parts{where}", args):
        dup_buckets.setdefault(_loose_key(r["mpn"]), []).append(r["id"])
    dup_groups = sum(1 for ids in dup_buckets.values() if len(ids) > 1)

    return {
        "parts": parts,
        "vendor": vendor or "",
        "vendors": vendors,
        "by_category": by_category,
        "pedigree": dist,
        "pedigree_labels": {k: pedigree.label(k) for k in pedigree.LADDER},
        "category_coverage": category_coverage,
        "radiation_parts": rad_parts,
        "radiation_pct": round(100.0 * rad_parts / parts, 1) if parts else 0.0,
        "families": family_stats(),
        "duplicate_groups": dup_groups,
        "source_overlap": source_overlap,
    }


def family_by_mpn(mpn, include_self=True):
    """Resolve an MPN (exact / case-insensitive / loose) and return its family."""
    conn = db()
    row = (conn.execute("SELECT id FROM parts WHERE mpn=?", (mpn,)).fetchone()
           or conn.execute("SELECT id FROM parts WHERE UPPER(mpn)=UPPER(?)",
                           (mpn,)).fetchone())
    pid = row["id"] if row else None
    if pid is None:
        target = _loose_key(mpn)
        for r in conn.execute("SELECT id, mpn FROM parts"):
            if _loose_key(r["mpn"]) == target:
                pid = r["id"]
                break
    return family_members(pid, include_self=include_self) if pid else []


def export_parts_json(folder) -> dict:
    """Export the normalized SQLite dataset as JSON files.

    Writes one ``parts.json`` file containing every part plus its specs and
    qualification evidence.  The function is intentionally implemented here
    because ``space_dataset.rebuild`` treats partdb as the dataset authority.
    """
    out = pathlib.Path(folder)
    out.mkdir(parents=True, exist_ok=True)
    conn = db()
    part_rows = conn.execute(
        "SELECT id, mpn, mpn_norm, vendor, category, subcategory, "
        "product_url, description, first_seen, last_seen FROM parts ORDER BY vendor, mpn"
    ).fetchall()
    records = []
    for row in part_rows:
        rec = dict(row)
        part_id = rec.pop("id")
        rec["specs"] = [dict(r) for r in conn.execute(
            "SELECT key, value_min, value_typ, value_max, value_text, unit, "
            "method, confidence, source_url, snippet, parser_version, extracted_at "
            "FROM specs WHERE part_id=? ORDER BY key, source_url", (part_id,)
        ).fetchall()]
        rec["qualification_evidence"] = [dict(r) for r in conn.execute(
            "SELECT signal, weight, source_url, snippet, found_at "
            "FROM qual_evidence WHERE part_id=? ORDER BY signal, source_url", (part_id,)
        ).fetchall()]
        records.append(rec)
    target = out / "parts.json"
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(target)
    return {"parts": len(records), "folder": str(out), "file": str(target)}


# ===========================================================================
# SCRAPE STATE  (resume) and RESET
# ===========================================================================
# space_dataset called partdb.reset_dataset(clear_cache=True) but no such
# function existed, so the Reset option raised AttributeError the moment it was
# used. It exists now, and resume is backed by the scrape_log table above rather
# than being inferred from whether the page cache happens to be enabled.

def mark_scraped(vendor, key, kind="page", status="ok", detail="",
                 parts_found=0):
    """Record one unit of finished work."""
    conn = db()
    with conn:
        conn.execute(
            """INSERT INTO scrape_log(vendor, key, kind, status, detail,
                                      parts_found, updated_at)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(vendor, key) DO UPDATE SET
                 kind=excluded.kind, status=excluded.status,
                 detail=excluded.detail, parts_found=excluded.parts_found,
                 updated_at=excluded.updated_at""",
            (vendor or "", str(key)[:400], kind, status, str(detail)[:200],
             int(parts_found), time.time()))


def already_scraped(vendor, key, ok_only=True):
    row = db().execute("SELECT status FROM scrape_log WHERE vendor=? AND key=?",
                       (vendor or "", str(key)[:400])).fetchone()
    if not row:
        return False
    return (row["status"] == "ok") if ok_only else True


def scraped_keys(vendor=None, kind=None, ok_only=True):
    """All finished keys in one query, so a walker can skip in bulk instead of
    issuing a SELECT per page."""
    sql = "SELECT vendor, key FROM scrape_log WHERE 1=1"
    args = []
    if vendor:
        sql += " AND vendor=?"
        args.append(vendor)
    if kind:
        sql += " AND kind=?"
        args.append(kind)
    if ok_only:
        sql += " AND status='ok'"
    return {(r["vendor"], r["key"]) for r in db().execute(sql, args)}


def scrape_summary():
    out = {}
    for r in db().execute(
            """SELECT vendor, kind, status, COUNT(*) AS n,
                      SUM(parts_found) AS parts
               FROM scrape_log GROUP BY vendor, kind, status"""):
        out.setdefault(r["vendor"], {}).setdefault(r["kind"], {})[r["status"]] = {
            "count": r["n"], "parts": r["parts"] or 0}
    return out


def clear_scrape_log(vendors=None):
    conn = db()
    with conn:
        if not vendors:
            n = conn.execute("SELECT COUNT(*) AS n FROM scrape_log").fetchone()["n"]
            conn.execute("DELETE FROM scrape_log")
            return n
        n = 0
        for v in vendors:
            cur = conn.execute("DELETE FROM scrape_log WHERE lower(vendor) LIKE ?",
                               (f"%{str(v).lower()}%",))
            n += cur.rowcount or 0
        return n


def vendor_part_counts():
    """{vendor: count}. `vendor_counts` is kept as an alias because the GUI grew
    up calling that name."""
    return {r["vendor"] or "(blank)": r["n"] for r in db().execute(
        "SELECT vendor, COUNT(*) AS n FROM parts GROUP BY vendor "
        "ORDER BY n DESC")}


def _vendor_matches(stored, wanted):
    """'Marki Microwave' should match a request for 'marki'."""
    a = re.sub(r"[^a-z0-9]", "", (stored or "").lower())
    b = re.sub(r"[^a-z0-9]", "", (wanted or "").lower())
    return bool(a and b) and (b in a or a in b)


def reset_dataset(vendors=None, clear_cache=True, drop_datasheets=True,
                  progress=None):
    """Delete parts and scrape state; optionally cached pages and files too.

    vendors        None wipes everything; a list wipes only those vendors
    clear_cache    also delete the vendor page cache and the local file cache
    drop_datasheets also delete downloaded datasheet files

    Note `clear_cache` also removes the everythingRF resume checkpoints, which
    live in the cache directory -- so a reset really does start from nothing."""
    import shutil
    say = progress or (lambda m: None)
    conn = db()
    out = {"parts": 0, "scrape_log": 0, "cache_files": 0, "datasheet_files": 0}
    if not vendors:
        with conn:
            out["parts"] = conn.execute(
                "SELECT COUNT(*) AS n FROM parts").fetchone()["n"]
            conn.execute("DELETE FROM parts")          # cascades to specs etc.
        out["scrape_log"] = clear_scrape_log(None)
        say(f"  deleted all {out['parts']} part(s), "
            f"{out['scrape_log']} scrape-log row(s)")
    else:
        with conn:
            for r in conn.execute("SELECT DISTINCT vendor FROM parts").fetchall():
                stored = r["vendor"] or ""
                if any(_vendor_matches(stored, v) for v in vendors):
                    n = conn.execute("SELECT COUNT(*) AS n FROM parts "
                                     "WHERE vendor=?", (stored,)).fetchone()["n"]
                    conn.execute("DELETE FROM parts WHERE vendor=?", (stored,))
                    out["parts"] += n
                    say(f"  deleted {n} part(s) for '{stored}'")
        out["scrape_log"] = clear_scrape_log(vendors)
    if clear_cache:
        for d in (DATA / "vendor_cache", DATA / "cache"):
            if d.exists():
                out["cache_files"] += sum(1 for f in d.rglob("*") if f.is_file())
                shutil.rmtree(d, ignore_errors=True)
        say(f"  cleared {out['cache_files']} cached file(s) "
            f"(including everythingRF resume checkpoints)")
    if drop_datasheets:
        d = DATA / "datasheets"
        if d.exists():
            out["datasheet_files"] = sum(1 for f in d.rglob("*") if f.is_file())
            shutil.rmtree(d, ignore_errors=True)
            say(f"  deleted {out['datasheet_files']} datasheet file(s)")
    return out
