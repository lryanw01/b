"""spacequal — predict whether an RF part is SPACE-QUALIFIABLE from its datasheet
text, using a locally-trained classifier. No API keys, no paid calls.

STANDALONE. Nothing here imports rfparts; it only *reads* the rfparts SQLite DB
(or a JSON export). Nothing is written back to that DB.

    LOCAL pipeline (preferred, fully offline -- trains on datasheets you have):
               local-scan -> train -> eval -> review

    python spacequal.py local-scan --local-dir "...\\data\\datasheets" \\
                                   --db "...\\Data\\parts.db" \\
                                   --erf-html "...\\Sources\\EverythingRFSpaceQual"
    python spacequal.py train --local-only --target-recall 0.9
    python spacequal.py review --out review_queue.csv

    Mini-Circuits pipeline (downloads its own PDFs):
               match -> fetch -> train -> eval -> review
               (selftest runs either one on synthetic data, offline)

    python spacequal.py match --catalog minicircuits_products_full.json
    python spacequal.py fetch --limit 600
    python spacequal.py selftest --local        # offline check of the local path

TRAINING ON DATASHEETS YOU ALREADY HAVE
---------------------------------------
`local-scan` reads a per-vendor datasheet folder:

    <root>/Qorvo/*.html            HTML product pages are read too, not just PDFs
    <root>/Marki-Microwave/*.pdf
    <root>/Skyworks/*.pdf
    <root>/MACOM/*.pdf

and labels each part WITHOUT looking at its text:

    positive   the part appears in your saved everythingRF space listings,
               and/or the rfparts DB already marks it space qualified / hi-rel
               (which covers the Qorvo aerospace catalog and the ADI space
               portfolio, since those ingests write a space signal).
    negative   a datasheet you hold that appears in NONE of those sources.

everythingRF is used ONLY as that label oracle -- its one-line descriptions are
far too thin to train on and are never used as text.

Absence is not proof: a part can be qualifiable and simply unlisted, which is
the whole premise of the PU machinery below. `--absent-as negative` (the default)
takes that trade deliberately, because real negatives are what make precision
measurable at all; `--absent-as unlabeled` puts them back in the U pool and
keeps the more conservative PU-only statistics. Either way, a FLAGGED negative
is a candidate to check, not an error -- it is exactly what you are hunting for.

Three things are done to stop the model learning shortcuts instead of
engineering, each added after it actually happened during testing:

  * per-vendor boilerplate (nav bars, cookie notices, footers) is stripped, or
    the model learns 'this is a Qorvo page' -- and vendor correlates with label
  * the part number is redacted from the text, or char n-grams memorise the
    family prefix ('QPA...') that correlates with the label
  * lines carrying engineering vocabulary are never treated as boilerplate,
    however often a vendor repeats them

CMOS driver/logic parts are pushed down twice over: the domain-prior table has
stacking penalties for driver/translator/logic language, and those negatives get
a heavier training weight (--neg-weight-cmos, default 3.0).

TWO BACKENDS
------------
--backend tfidf   From scratch, no downloads: TF-IDF over word 1-2 grams and
                  character 3-5 grams, then logistic regression. Fast, CPU-only,
                  needs only scikit-learn -- and it is INTERPRETABLE, so `train`
                  prints the tokens it actually learned. That matters here (see
                  LEAKAGE below).

--backend embed   A pretrained sentence-transformer from Hugging Face (default
                  sentence-transformers/all-MiniLM-L6-v2, ~90 MB, downloaded once
                  and cached) turns each blurb into a vector; a logistic head is
                  then TRAINED ON YOUR DATA. With only tens of known-positive
                  parts this is the right way to use a pretrained model: full
                  fine-tuning of the encoder on <100 positives overfits badly,
                  whereas a frozen encoder plus a trained head gets most of the
                  benefit. Use --hf-model to swap in any other ST model.

WHY PU LEARNING, NOT PLAIN CLASSIFICATION
-----------------------------------------
  P (positive) = Mini-Circuits parts everythingRF lists as space qualified/grade.
  U (unlabeled)= the rest of the Mini-Circuits catalog. MOST are not qualifiable,
                 but SOME are and simply aren't listed.

U is unlabeled, not negative. So:
  * Training uses PU BAGGING (Mordelet & Vert 2014): each bag draws a random
    subset of U the same size as P, calls it negative, and fits a model; scores
    are averaged out-of-bag. This tolerates unknown contamination in U far better
    than a single P-vs-all-U fit.
  * Held-out recall on P comes from an outer cross-validation over P, so the
    reported recall is never measured on parts the model trained on.
  * The DECISION THRESHOLD is chosen to hit a target recall on held-out P
    (--target-recall), which is the meaningful operating point when you cannot
    measure precision.
  * FLAGS IN U ARE CANDIDATES, NOT ERRORS. Precision is not identifiable from PU
    data; `eval` prints a sensitivity table and `review` produces a queue you can
    label to turn that estimate into a measurement.

LEAKAGE WARNING
---------------
Mini-Circuits' own space datasheets often contain the word "space". A classifier
can score beautifully by learning that one token and still be useless on unlisted
parts. Two defences are built in: `train` prints the top learned features so you
can see it happening, and `--mask space|strict` redacts space (or space + hi-rel)
vocabulary so you can measure how much signal comes from construction language
(hermetic, ceramic, bare die, -55/+125 C) alone. Compare the two runs; the masked
recall is the number that predicts real-world discovery.

POLITENESS
----------
Fetching obeys robots.txt, sends an honest identifying User-Agent, rate-limits
(default 1 req/sec), caches every PDF so nothing is fetched twice, and resumes.
No anti-bot circumvention of any kind.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import random
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from collections import Counter
from html import unescape as _html_unescape
from pathlib import Path

# ---------------------------------------------------------------- configuration
UA = ("rfparts-spacequal/2.0 (RF parts sourcing research; "
      "contact: set SPACEQUAL_CONTACT env var)")
MC_HOST = "https://www.minicircuits.com"
MC_PDF_TEMPLATE = MC_HOST + "/pdfs/{pn}.pdf"
WORKDIR = Path(os.environ.get("SPACEQUAL_HOME", Path.home() / ".spacequal"))
CACHE_PDF = WORKDIR / "pdfs"
CACHE_TXT = WORKDIR / "overview"
MATCH_JSON = WORKDIR / "matched.json"
PRED_JSON = WORKDIR / "predictions.json"
MODEL_FILE = WORKDIR / "model.joblib"

DEFAULT_RATE = 1.0          # seconds between network requests
OVERVIEW_CHARS = 4000       # chars kept in 'overview' text mode
FULL_TEXT_CHARS = 20000     # chars kept in 'full' text mode (whole datasheet)
_U_SHUFFLE_SEED = 20260729  # fixed so the unlabeled sample is reproducible
DEFAULT_HF_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
# Fraction of the unlabeled pool a bag may use as negatives when U is smaller
# than P. Keeping it below 1.0 guarantees out-of-bag rows exist to score.
_BAG_FRACTION = 0.7
SETTINGS_FILE = WORKDIR / "settings.json"

# Default location of the Mini-Circuits catalog. EDIT THIS to your own path (or
# change it in the menu under Settings) so the pipeline runs with no arguments.
DEFAULT_CATALOG = r"C:\Users\lane.white\Downloads\rfparts\rfparts\sources\minicircuits_products_full.json"

# ---------------------------------------------------- LOCAL datasheet library
# Datasheets you ALREADY have on disk, in per-vendor subfolders:
#
#     <root>/Qorvo/*.html          (Qorvo product pages -- HTML, no PDF)
#     <root>/Marki-Microwave/*.pdf
#     <root>/Skyworks/*.pdf
#     <root>/MACOM/*.pdf
#
# This is the preferred training corpus: it is REAL datasheet prose, which is
# what predicts qualifiability. everythingRF is used only as a LABEL ORACLE (a
# part appearing in your saved space listings is a positive) -- its one-line
# descriptions are far too thin to train on, so they are never used as text.
DEFAULT_LOCAL_DS = r"C:\Users\lane.white\Downloads\rfparts\data\datasheets"
LOCAL_JSON = WORKDIR / "local_corpus.json"
LOCAL_TXT = WORKDIR / "localtext"
LOCAL_MIN_CHARS = 120       # an HTML product page carries less prose than a PDF

# ------------------------------------------------------------- domain priors
# Hand-written engineering knowledge, applied on top of the learned model as an
# additive nudge in LOG-ODDS space. Kept deliberately separate from the trained
# weights so it stays auditable, tunable (--prior-weight) and switchable
# (--no-prior) -- and so it still works before you have many labels.
#
# Scale: +-1.0 log-odds is a strong opinion, +-0.4 is a nudge. A term fires at
# most once regardless of how often it appears.
#
# NOTE on CMOS: rad-hard CMOS is everywhere in space electronics generally (most
# QMLV digital and mixed-signal parts are CMOS). The penalty below is specific to
# THIS catalog, where "CMOS" on a Mini-Circuits RF datasheet almost always means a
# commercial silicon control/logic interface rather than a flight RF part. If you
# point this tool at a different vendor, revisit that line first.
DOMAIN_PRIOR = [
    # --- penalties -----------------------------------------------------------
    # CMOS is deliberately split into several terms, strongest first, because a
    # bare "\bCMOS\b" treated a rad-hard CMOS RF switch and a commercial logic
    # driver identically. These stack (each fires at most once), so a genuine
    # CMOS *driver* sheet accumulates a much heavier penalty than a part that
    # merely mentions a CMOS control interface -- which is the intent.
    (r"\bCMOS\s*(?:driver|drivers|logic|buffer|inverter|translator|receiver)\b",
     -1.40, "CMOS driver/logic device, not a flight RF part"),
    (r"\b(?:level|logic|voltage)\s*(?:translator|shifter)\b", -1.20,
     "level/logic translator"),
    (r"\b(?:gate|line|clock|LED|motor|display|relay)\s*driver\b", -1.10,
     "driver IC, not an RF component"),
    (r"\bdriver\s*(?:IC|array)\b|\boctal\s*(?:driver|buffer)\b", -1.00,
     "driver IC / octal buffer"),
    (r"\bLVCMOS\b|\bLVTTL\b|\bTTL\b|\bHCMOS\b", -0.70,
     "CMOS/TTL logic family interface"),
    (r"\b(?:NAND|NOR|XOR)\s*gate\b|\bflip-?flop\b|\bshift\s*register\b"
     r"|\bmultiplexer\s*logic\b", -0.90, "digital logic function"),
    (r"\bCMOS\b", -0.80, "CMOS: commercial silicon logic/control in this catalog"),
    (r"\bepox(?:y|ies|ied)\b", -0.90, "epoxy encapsulation is not a space package"),
    (r"\bover-?mould?ed\b|\bovermolded\b", -0.70, "overmolded plastic body"),
    (r"\bplastic\b", -0.50, "plastic package"),
    (r"\belectrolytic\b", -0.90, "electrolytic capacitors are not flight parts"),
    (r"\b(?:eval(?:uation)?\s*board|test\s*board|demo\s*board|kit)\b", -1.00,
     "evaluation/test hardware, not a component"),
    (r"\b0\s*(?:to|-|\u2013)\s*70\s*\u00b0?\s*C\b", -0.60,
     "commercial temperature range only"),
    # --- rewards -------------------------------------------------------------
    (r"\bGaAs\b", +0.50, "GaAs process, standard for space RF MMICs"),
    (r"\bGaN\b", +0.50, "GaN process, standard for space RF power"),
    (r"\bLTCC\b", +0.50, "LTCC ceramic construction"),
    (r"\balumina\b", +0.50, "alumina substrate"),
    (r"\bhermetic(?:ally)?\b", +0.80, "hermetic package"),
    (r"\bceramic\b", +0.40, "ceramic package/substrate"),
    (r"\bbare\s*die\b|\bchip\s*(?:and|&)\s*wire\b|\bMMIC\s*die\b", +0.60,
     "available as bare die"),
    (r"\bglass[-\s]*to[-\s]*metal\b", +0.70, "glass-to-metal seal"),
    (r"\bthin[-\s]*film\b", +0.40, "thin-film construction"),
    # Terms a real Mini-Circuits datasheet uses that the first prior table
    # missed entirely (the BXHF1275 blurb matched NOTHING despite saying
    # "laser welded housing" and offering a MIL-STD-883 screened version).
    (r"\blaser[-\s]*(?:weld(?:ed)?|seal(?:ed)?)\b", +0.80,
     "laser welded/sealed housing (hermetic construction)"),
    (r"\bsolder[-\s]*seal(?:ed)?\b|\bwelded\b", +0.50, "welded/solder-sealed body"),
    (r"\bMIL-STD-883\b", +0.80, "MIL-STD-883 screening available"),
    (r"\bMIL-PRF-38534\b|\bMIL-PRF-38535\b", +0.90, "MIL-PRF hybrid/micro flow"),
    (r"\bMIL-STD-\d{3,4}\b|\bMIL-PRF-\d+\b", +0.50, "MIL specification cited"),
    (r"\bscreen(?:ed|ing)\b|\bupscreen(?:ed|ing)?\b", +0.50,
     "screened variant offered"),
    (r"\bkovar\b|\bglass\s*bead\b|\bfeed-?through\b", +0.50,
     "kovar / glass bead feedthrough"),
    (r"\bhi-?rel\b|\bhigh\s*reliability\b", +0.60, "hi-rel product line"),
    (r"\bQML[VQPR]?\b|\bclass\s*[HKSV]\b", +0.80, "QML / class H,K,S,V flow"),
    (r"\bJANTX?V?\b", +0.60, "JAN qualified"),
    (r"-\s*55\s*\u00b0?\s*C?\s*(?:to|-|\u2013)\s*\+?\s*1(?:25|50)\s*\u00b0?\s*C\b",
     +0.60, "military temperature range"),
]
_DOMAIN_PRIOR_RX = [(re.compile(rx, re.I), w, why) for rx, w, why in DOMAIN_PRIOR]

# Negatives matching this get a HEAVIER sample weight during training (see
# --neg-weight-cmos). The domain prior above nudges the score at inference time;
# this makes the fitted model itself pay more attention to getting these wrong,
# which is what actually pushes CMOS driver/logic parts down the ranking.
_CMOS_NEG_RE = re.compile("|".join((
    r"\bCMOS\s*(?:driver|drivers|logic|buffer|inverter|translator|receiver)\b",
    r"\b(?:level|logic|voltage)\s*(?:translator|shifter)\b",
    r"\b(?:gate|line|clock|LED|motor|display|relay)\s*driver\b",
    r"\bdriver\s*(?:IC|array)\b", r"\boctal\s*(?:driver|buffer)\b",
    r"\bLVCMOS\b", r"\bLVTTL\b", r"\bHCMOS\b",
    r"\b(?:NAND|NOR|XOR)\s*gate\b", r"\bflip-?flop\b", r"\bshift\s*register\b",
)), re.I)


def neg_sample_weight(text, cmos_weight=3.0) -> float:
    """Training weight for a labelled negative. CMOS driver/logic language is the
    failure mode worth spending model capacity on, so it counts several times."""
    return float(cmos_weight) if _CMOS_NEG_RE.search(text or "") else 1.0


def prior_hits(text):
    """(term description, weight) for every prior that fires in `text`."""
    return [(why, w) for rx, w, why in _DOMAIN_PRIOR_RX if rx.search(text or "")]


def prior_logit(text) -> float:
    return sum(w for _why, w in prior_hits(text))


def _logit(p, eps=1e-6):
    import math
    p = min(1.0 - eps, max(eps, float(p)))
    return math.log(p / (1.0 - p))


def _sigmoid(z):
    import math
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


def blend_prior(prob, text, weight=1.0) -> float:
    """Model probability adjusted by the domain priors, in log-odds space."""
    if not weight:
        return float(prob)
    return _sigmoid(_logit(prob) + weight * prior_logit(text))


# Catalog items that are not flight components. Used ONLY as a calibration check
# ("sanity negatives"), never as training labels.
_NOT_FLIGHT_RE = re.compile("|".join((
    r"\beval(uation)?\b", r"\bkit\b", r"\btest\s*board\b", r"\bdemo\b",
    r"\badapter\b", r"\btorque\b", r"\bwrench\b", r"\bhand\s*held\b",
    r"\bcarrying\s*case\b", r"\bsoftware\b", r"\bpower\s*supply\b",
)), re.I)


# ------------------------------------------------------------ part-number keys
def norm_pn(pn) -> str:
    """Exact-ish key: uppercase, whitespace removed, RoHS '+' preserved."""
    return re.sub(r"\s+", "", str(pn or "")).upper()


def loose_pn(pn) -> str:
    """Cross-source key: alphanumerics only, '+' dropped. 'ZX60-V82-S+' ==
    'zx60 v82 s'. Mini-Circuits and everythingRF disagree on the '+' and on
    hyphenation often enough that exact matching alone loses real parts."""
    return re.sub(r"[^A-Z0-9]", "", norm_pn(pn).replace("+", ""))


# ------------------------------------------------------------- catalog loading
def load_catalog(path) -> list[dict]:
    path = Path(path)
    if not path.is_file():
        raise SystemExit(f"catalog not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):                       # tolerate a wrapped export
        for v in data.values():
            if isinstance(v, list):
                data = v
                break
    out = []
    for r in data:
        pn = r.get("pn") or r.get("part_number") or r.get("model")
        if not pn:
            continue
        out.append({"pn": pn, "category": r.get("cat") or "",
                    "group": r.get("group") or "", "f_lo": r.get("flo"),
                    "f_hi": r.get("fhi"), "case_style": r.get("case_style") or "",
                    "datasheet_url": r.get("datasheet_url") or "",
                    "url": r.get("url") or ""})
    return out


# --------------------------------------------------- ground truth (positives)
_MC_VENDOR_RE = re.compile(r"mini\s*-?\s*circuits", re.I)


def load_positives_from_db(db_path) -> list[dict]:
    """everythingRF Mini-Circuits space parts from the rfparts SQLite DB: parts
    carrying an 'erf-space-*' evidence signal whose vendor reads Mini-Circuits."""
    db_path = Path(db_path)
    if not db_path.is_file():
        raise SystemExit(
            f"rfparts DB not found: {db_path}\n"
            "Pass --db, or use --positives with a JSON list of part numbers.")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT p.id, p.mpn, p.vendor, p.category, p.description, p.product_url,
               (SELECT value_text FROM specs s WHERE s.part_id = p.id
                  AND s.key = 'space_variant' LIMIT 1) AS variant
        FROM parts p
        WHERE EXISTS (SELECT 1 FROM qual_evidence q
                       WHERE q.part_id = p.id AND q.signal LIKE 'erf-space-%')
    """).fetchall()
    out = [{"pn": r["mpn"], "vendor": r["vendor"], "category": r["category"] or "",
            "variant": r["variant"] or "space_qualified",
            "url": r["product_url"] or ""}
           for r in rows if _MC_VENDOR_RE.search(r["vendor"] or "")]
    conn.close()
    return out


def load_positives_from_json(path) -> list[dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    out = []
    for item in data:
        if isinstance(item, str):
            out.append({"pn": item, "vendor": "Mini-Circuits", "category": "",
                        "variant": "space_qualified", "url": ""})
        elif isinstance(item, dict) and item.get("pn"):
            item.setdefault("variant", "space_qualified")
            out.append(item)
    return out


# ------------------------------------------------------------------- matching
def build_match(catalog, positives) -> dict:
    """Join everythingRF space parts to the catalog: exact PN -> loose PN ->
    generic /pdfs/<PN>.pdf. Unresolved parts are printed, which is the point:
    it shows where the local catalog is stale."""
    by_exact, by_loose = {}, {}
    for rec in catalog:
        by_exact.setdefault(norm_pn(rec["pn"]), rec)
        by_loose.setdefault(loose_pn(rec["pn"]), rec)
    pos_loose = set()
    matched, guessed = [], []
    for p in positives:
        key_e, key_l = norm_pn(p["pn"]), loose_pn(p["pn"])
        pos_loose.add(key_l)
        rec = by_exact.get(key_e) or by_loose.get(key_l)
        if rec:
            matched.append({**p, "catalog_pn": rec["pn"],
                            "datasheet_url": rec["datasheet_url"],
                            "category_mc": rec["category"],
                            "case_style": rec["case_style"],
                            "match": "exact" if key_e in by_exact else "loose"})
        else:
            guessed.append({**p, "catalog_pn": "",
                            "datasheet_url": MC_PDF_TEMPLATE.format(
                                pn=urllib.parse.quote(norm_pn(p["pn"]), safe="+-_.")),
                            "category_mc": "", "case_style": "",
                            "match": "url-guess"})
    unlabeled = [{"pn": r["pn"], "datasheet_url": r["datasheet_url"],
                  "category_mc": r["category"], "case_style": r["case_style"],
                  "group": r["group"], "match": "catalog"}
                 for r in catalog if loose_pn(r["pn"]) not in pos_loose]
    return {"positives": matched + guessed, "unlabeled": unlabeled,
            "counts": {"erf_positives": len(positives),
                       "matched_in_catalog": len(matched),
                       "url_guess": len(guessed), "unlabeled": len(unlabeled)}}


# ------------------------------------------------------------ polite fetching
class Fetcher:
    """Rate-limited, robots-respecting, caching PDF fetcher."""

    def __init__(self, rate=DEFAULT_RATE, timeout=30, ignore_robots=False):
        self.rate = rate
        self.timeout = timeout
        self._last = 0.0
        contact = os.environ.get("SPACEQUAL_CONTACT", "")
        self.ua = UA if not contact else f"rfparts-spacequal/2.0 (+{contact})"
        self.rp = None
        self.robots_unreadable = False
        if not ignore_robots:
            self.rp = urllib.robotparser.RobotFileParser()
            self.rp.set_url(MC_HOST + "/robots.txt")
            try:
                self.rp.read()
            except Exception as e:
                print(f"  ! could not read robots.txt ({e}); "
                      f"continuing at {rate}s/request", file=sys.stderr)
                self.rp = None
            else:
                # RobotFileParser sets disallow_all when robots.txt itself
                # returned 401/403 -- indistinguishable from a blanket disallow,
                # so track it to give an accurate message.
                if getattr(self.rp, "disallow_all", False):
                    self.robots_unreadable = True

    def explain_block(self, url) -> str:
        if self.robots_unreadable:
            return (f"robots.txt at {MC_HOST}/robots.txt could not be read "
                    f"(401/403), so fetching is disabled as a precaution.\n"
                    f"If your network proxies or blocks that file, check it in a "
                    f"browser first; re-run with --ignore-robots only once you "
                    f"have confirmed the path is permitted.")
        return f"robots.txt disallows {url}"

    def allowed(self, url) -> bool:
        return True if self.rp is None else self.rp.can_fetch(self.ua, url)

    def get(self, url) -> bytes | None:
        if not self.allowed(url):
            raise PermissionError(self.explain_block(url))
        wait = self.rate - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        req = urllib.request.Request(url, headers={
            "User-Agent": self.ua, "Accept": "application/pdf,*/*"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code in (429, 503):                   # back off, retry once
                time.sleep(max(5.0, self.rate * 10))
                try:
                    with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                        return resp.read()
                except Exception:
                    return None
            return None
        except Exception:
            return None
        finally:
            self._last = time.time()


def pdf_url_candidates(pn, catalog_url=None):
    """Ordered (url, why) candidates for a part's datasheet.

    Mini-Circuits groups some variants onto ONE datasheet -- typically an 'X'
    suffix marking a heatsink or case option ('DBTC-10-13X+' is documented in
    'DBTC-10-13+.pdf'). 385 catalog parts end in 'X+' and 245 of those have an
    X-less counterpart, so when the direct URL 404s it is worth dropping the X
    before giving up. Add further rules here; each is tried only after the
    previous one fails, so a rule costs nothing on parts that resolve normally."""
    out = []

    def add(url, why):
        if url and url not in [u for u, _w in out]:
            out.append((url, why))

    norm = norm_pn(pn)
    add(catalog_url, "catalog url")
    add(MC_PDF_TEMPLATE.format(pn=urllib.parse.quote(norm, safe="+-_.")),
        "generic /pdfs/<PN>.pdf")
    # trailing 'X+' (or a bare trailing 'X') -> the grouped counterpart datasheet
    m = re.match(r"^(.*[^-\s])X(\+?)$", norm, re.I)
    if m:
        alt = m.group(1) + m.group(2)
        add(MC_PDF_TEMPLATE.format(pn=urllib.parse.quote(alt, safe="+-_.")),
            f"X dropped -> {alt}")
    return out


# ------------------------------------------------------ datasheet text mining
_SECTION_HEADS = ("product overview", "general description", "description",
                  "features", "applications", "product features",
                  "key features", "product highlights")


_TABLE_ROW = re.compile(r"^[\s\-+.,0-9eE()/%:|]*$")


def _strip_tables(text, max_chars):
    """Drop lines that are essentially numeric table rows. On a datasheet the
    parametric tables are most of the text and none of the signal: construction
    and materials language is what predicts qualifiability, and leaving the
    numbers in just dilutes every TF-IDF weight."""
    keep = []
    for ln in text.split("\n"):
        t = ln.strip()
        if not t or _TABLE_ROW.match(t):
            continue
        letters = sum(c.isalpha() for c in t)
        if letters < 3 or letters / max(1, len(t)) < 0.25:
            continue                      # mostly digits/symbols -> a table row
        keep.append(t)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(keep)).strip()[:max_chars]


def extract_full_text(pdf_bytes, max_chars=FULL_TEXT_CHARS) -> str:
    """EVERY page of the datasheet, with numeric tables stripped out.

    The overview-only extractor missed real signal: hermeticity notes, case
    material, MIL screening options and environmental ratings often sit on later
    pages or in the outline-drawing notes, not in the opening blurb."""
    text = _pdf_text(pdf_bytes, pages=None)
    return _strip_tables(text, max_chars) if text else ""


def _pdf_text(pdf_bytes, pages=3):
    """Raw text from the first `pages` pages (None = all)."""
    try:
        import io
        import warnings
        import pdfplumber
        warnings.filterwarnings("ignore")
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            seq = pdf.pages if pages is None else pdf.pages[:pages]
            return "\n".join((pg.extract_text() or "") for pg in seq)
    except Exception:
        pass
    try:
        import io
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(pdf_bytes))
        seq = reader.pages if pages is None else reader.pages[:pages]
        return "\n".join((pg.extract_text() or "") for pg in seq)
    except Exception:
        return ""


def extract_overview(pdf_bytes) -> str:
    """The readable overview of a datasheet: leading text of page 1 plus any
    Overview / Features / Applications section in the first pages. Parametric
    tables are left out -- construction and materials language predicts
    qualifiability, the numbers do not."""
    text = _pdf_text(pdf_bytes, pages=3)
    if not text.strip():
        return ""
    lines = [ln.rstrip() for ln in text.split("\n")]
    head = "\n".join(lines[:40])
    keep, grabbing = [], False
    for ln in lines:
        low = ln.strip().lower()
        if any(low.startswith(h) for h in _SECTION_HEADS):
            grabbing = True
            keep.append(ln)
            continue
        if grabbing:
            if not ln.strip():
                grabbing = False
            else:
                keep.append(ln)
    body = "\n".join(keep)
    out = (head + "\n" + body) if body else head
    return re.sub(r"\n{3,}", "\n\n", out).strip()[:OVERVIEW_CHARS]


def extract_text(pdf_bytes, mode="full"):
    """Datasheet text for the classifier. 'full' = whole document with numeric
    tables stripped (recommended); 'overview' = page-1 blurb + Features."""
    return (extract_full_text(pdf_bytes) if mode == "full"
            else extract_overview(pdf_bytes))


# ------------------------------------------------------ HTML datasheet reader
# Qorvo's saved sheets are HTML product pages, not PDFs, so the PDF extractors
# above cannot see them at all. Pure stdlib on purpose: no bs4/lxml dependency.
_HTML_SCRIPTISH = re.compile(
    r"(?is)<(script|style|noscript|svg|template|iframe)\b.*?</\1\s*>")
_HTML_COMMENT = re.compile(r"(?s)<!--.*?-->")
_HTML_META_DESC = re.compile(
    r"""(?is)<meta[^>]+?name\s*=\s*["']description["'][^>]*?"""
    r"""content\s*=\s*["'](.*?)["']""")
_HTML_TITLE = re.compile(r"(?is)<title[^>]*>(.*?)</title>")
_HTML_HEAD = re.compile(r"(?is)<head\b.*?</head\s*>")
# BOTH opening and closing block tags become newlines. Closing tags alone are not
# enough: '<title>X</title>...<nav><ul><li>Home | Products' has no closing tag
# until after 'Home | Products', so the title, the meta description and the first
# nav item all landed on ONE line. That line then contained the part number,
# making it unique per page, so the boilerplate detector could never see the nav
# chrome repeating and it survived into the training text.
_HTML_BLOCK = re.compile(
    r"(?i)</?(?:p|div|li|tr|h[1-6]|section|article|td|th|table|ul|ol|dd|dt|dl"
    r"|nav|footer|header|aside|main|form|option|label|blockquote|pre)\b[^>]*>"
    r"|<br\s*/?>")
_HTML_TAG = re.compile(r"(?s)<[^>]+>")


def html_to_text(raw, max_chars=FULL_TEXT_CHARS) -> str:
    """Readable prose from a saved HTML datasheet / product page.

    <title> and the meta description are pulled out first and put at the top:
    on a vendor product page those two fields hold the product summary ('GaAs
    MMIC power amplifier, hermetic, space qualified'), which is exactly the
    construction language the classifier needs, and they would otherwise be
    thrown away with the rest of <head>."""
    txt = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
    head = []
    mt = _HTML_TITLE.search(txt)
    if mt:
        head.append(_html_unescape(_HTML_TAG.sub(" ", mt.group(1))).strip())
    md = _HTML_META_DESC.search(txt)
    if md:
        head.append(_html_unescape(md.group(1)).strip())
    body = _HTML_COMMENT.sub(" ", txt)
    body = _HTML_SCRIPTISH.sub(" ", body)
    body = _HTML_HEAD.sub("\n", body)        # title/description already captured
    body = _HTML_BLOCK.sub("\n", body)
    body = _HTML_TAG.sub(" ", body)
    body = _html_unescape(body)
    body = re.sub(r"[ \t\xa0\u200b]+", " ", body)
    lead = "\n".join(h for h in head if h)
    return _strip_tables((lead + "\n" + body) if lead else body, max_chars)


def _has_engineering_signal(line) -> bool:
    """True if a line carries construction/qualification vocabulary.

    Such a line is NEVER treated as boilerplate, however often it repeats. Site
    chrome does not say 'hermetic', 'epoxy overmolded' or 'MIL-PRF-38534'; real
    product prose does, and vendors legitimately reuse the same sentence across
    a whole product family."""
    return bool(_SPACE_WORDS.search(line) or _HIREL_WORDS.search(line)
                or prior_hits(line))


def _learn_boilerplate(texts, frac=0.85, min_docs=4, max_len=200) -> set:
    """Lines that appear on MOST pages from one vendor: nav bars, cookie
    notices, footers, 'Add to cart'.

    This matters more than it sounds. Every Qorvo page shares a template, so
    without this the most common tokens in the corpus are site chrome. TF-IDF
    down-weights terms common to ALL documents, but here the chrome is common
    only within a vendor -- so it survives, and the model happily learns
    'this is a Qorvo page' instead of 'this part is hermetic'. Since vendor
    correlates with label, that is leakage dressed up as signal.

    Two guards keep it from eating real content, both added after it did exactly
    that in testing: the repetition bar is deliberately high (a line must appear
    on ~85% of a vendor's pages), and any line containing engineering vocabulary
    is exempt no matter how often it repeats."""
    if len(texts) < min_docs:
        return set()
    cnt = Counter()
    for t in texts:
        for ln in {l.strip() for l in t.split("\n") if l.strip()}:
            if len(ln) <= max_len and not _has_engineering_signal(ln):
                cnt[ln] += 1
    need = max(3, int(round(frac * len(texts))))
    return {ln for ln, c in cnt.items() if c >= need}


def _drop_lines(text, boiler) -> str:
    if not boiler:
        return text
    kept = [l for l in text.split("\n") if l.strip() not in boiler]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()


# space / hi-rel vocabulary, for the masking experiment described in the docstring
_SPACE_WORDS = re.compile(
    r"\b(space|spaceflight|space-?qualified|satellite|orbit(al)?|LEO|GEO|MEO|"
    r"deep\s*space|radiation|rad[-\s]?hard(ened)?|rad[-\s]?tolerant|TID|SEE|SEL|"
    r"outgassing|NASA|ESA|ESCC)\b", re.I)
_HIREL_WORDS = re.compile(
    r"\b(MIL-PRF-\d+|MIL-STD-\d+|QML[VQPR]?|class\s*[HKSV]\b|screen(ed|ing)|"
    r"upscreen(ed|ing)?|hi-?rel|high\s*reliability|JAN\b|38534|38535)\b", re.I)


def mask_text(text, level) -> str:
    if level == "none":
        return text
    out = _SPACE_WORDS.sub(" ", text)
    if level == "strict":
        out = _HIREL_WORDS.sub(" ", out)
    return out


# --------------------------------------------------------------- featurizers
def make_tfidf_featurizer(max_features=60000):
    """From-scratch text features: word 1-2 grams + character 3-5 grams. Char
    n-grams matter because package and material language shows up inside part
    codes and hyphenated terms ('hermetic', 'CDIP', 'thin-film')."""
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.pipeline import FeatureUnion
    word = TfidfVectorizer(lowercase=True, ngram_range=(1, 2), min_df=2,
                           sublinear_tf=True, strip_accents="unicode",
                           stop_words="english",   # kills 'with', 'terms', 'high'
                           max_features=max_features)
    char = TfidfVectorizer(lowercase=True, analyzer="char_wb",
                           ngram_range=(3, 5), min_df=3, sublinear_tf=True,
                           max_features=max_features)
    return FeatureUnion([("word", word), ("char", char)])


class EmbedFeaturizer:
    """Pretrained Hugging Face sentence-transformer as a frozen feature extractor.

    The encoder is NOT fine-tuned: with a few dozen positives, tuning a full
    transformer overfits, while a frozen encoder + trained logistic head keeps
    almost all the benefit. Swap encoders with --hf-model."""

    def __init__(self, model_name=DEFAULT_HF_MODEL, batch_size=32):
        self.model_name = model_name
        self.batch_size = batch_size
        self._model = None

    def _load(self):
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError:
                raise SystemExit(
                    "--backend embed needs sentence-transformers:\n"
                    "    pip install sentence-transformers\n"
                    "(first run downloads the model, then it is cached offline)")
            print(f"  loading {self.model_name} ...")
            self._model = SentenceTransformer(self.model_name)
        return self._model

    def fit(self, texts, y=None):
        self._load()
        return self

    def transform(self, texts):
        import numpy as np
        model = self._load()
        vecs = model.encode(list(texts), batch_size=self.batch_size,
                            show_progress_bar=False, convert_to_numpy=True,
                            normalize_embeddings=True)
        return np.asarray(vecs, dtype="float32")

    def fit_transform(self, texts, y=None):
        return self.fit(texts).transform(texts)


def make_classifier(seed=0):
    """Regularised logistic regression: with tens of positives, strong
    regularisation and balanced class weights matter more than model capacity."""
    from sklearn.linear_model import LogisticRegression
    return LogisticRegression(C=1.0, max_iter=2000, class_weight="balanced",
                              solver="liblinear", random_state=seed)


# ------------------------------------------------------------- PU bag training
def train_pu(Xp, Xu, n_bags=15, folds=5, seed=0, verbose=True,
             Xn=None, wn=None):
    """PU bagging with an outer CV over P -- and, when you have them, REAL
    labelled negatives N.

    Returns (p_scores, u_scores, n_scores, bagged_models).

    Two regimes, one code path:

      * N is empty (the original Mini-Circuits case): classic PU bagging. Each
        bag calls a random subset of U negative, and U rows are scored
        out-of-bag so a row's own training label cannot push its score down.

      * N is non-empty (the local datasheet corpus, where a part absent from
        every space source is treated as a negative): the labelled negatives go
        into every bag as real negatives, and are ALSO held out fold by fold.
        That is what makes precision measurable instead of merely assumed --
        with no negatives at all, precision is not identifiable from PU data.

    `wn` gives per-negative sample weights, which is how CMOS driver/logic
    negatives are made to count several times over (see neg_sample_weight).
    """
    import numpy as np
    from sklearn.model_selection import KFold

    n_p = Xp.shape[0]
    n_u = Xu.shape[0] if Xu is not None else 0
    n_n = Xn.shape[0] if Xn is not None else 0
    rng = np.random.default_rng(seed)
    p_scores = np.zeros(n_p)
    n_scores = np.zeros(n_n)
    u_sum = np.zeros(n_u)
    u_cnt = np.zeros(n_u)
    if n_n and wn is None:
        wn = np.ones(n_n)
    wn = np.asarray(wn, dtype=float) if n_n else np.zeros(0)

    if n_u == 0 and n_n == 0:
        raise SystemExit("train_pu: no negatives and no unlabeled pool")

    # Folds must divide BOTH P and N, since both are held out.
    folds = max(2, min(folds, n_p, n_n if n_n else folds))
    p_split = list(KFold(n_splits=folds, shuffle=True,
                         random_state=seed).split(np.arange(n_p)))
    if n_n:
        n_split = list(KFold(n_splits=folds, shuffle=True,
                             random_state=seed + 7).split(np.arange(n_n)))
    else:
        empty = np.array([], dtype=int)
        n_split = [(empty, empty)] * folds

    def _bag_fit(ptr, ntr, bag_seed, draw_u=True):
        """One bagged model. Returns (clf, u_indices_used_as_negative)."""
        blocks = [Xp[ptr]]
        ys = [np.ones(len(ptr))]
        ws = [np.ones(len(ptr))]
        if n_n and len(ntr):
            if n_u:
                idx = ntr                      # use every labelled negative
            else:
                # No unlabeled pool, so bag diversity has to come from
                # bootstrapping the negatives instead.
                idx = rng.choice(ntr, size=len(ntr), replace=True)
            blocks.append(Xn[idx])
            ys.append(np.zeros(len(idx)))
            ws.append(wn[idx])
        sel = None
        if n_u and draw_u:
            want = max(1, len(ptr) - (len(ntr) if n_n else 0))
            neg_size = min(want, n_u)
            if neg_size >= n_u:
                neg_size = max(1, int(round(_BAG_FRACTION * n_u)))
            sel = rng.choice(n_u, size=neg_size, replace=False)
            blocks.append(Xu[sel])
            ys.append(np.zeros(len(sel)))
            ws.append(np.ones(len(sel)))
        X = blocks[0]
        for extra in blocks[1:]:
            X = _vstack(X, extra)
        y = np.concatenate(ys)
        w = np.concatenate(ws)
        clf = make_classifier(seed=bag_seed)
        clf.fit(X, y, sample_weight=w)
        return clf, sel

    for fold, ((ptr, pte), (ntr, nte)) in enumerate(zip(p_split, n_split), 1):
        pf = np.zeros(len(pte))
        nf = np.zeros(len(nte))
        for b in range(n_bags):
            clf, sel = _bag_fit(ptr, ntr, seed + b)
            pf += clf.predict_proba(Xp[pte])[:, 1]
            if len(nte):
                nf += clf.predict_proba(Xn[nte])[:, 1]
            if sel is not None:
                oob = np.ones(n_u, dtype=bool)
                oob[sel] = False
                if oob.any():
                    u_sum[oob] += clf.predict_proba(Xu[oob])[:, 1]
                    u_cnt[oob] += 1
        p_scores[pte] = pf / n_bags
        if len(nte):
            n_scores[nte] = nf / n_bags
        if verbose:
            extra = (f", held-out N mean {n_scores[nte].mean():.3f}"
                     if len(nte) else "")
            print(f"    fold {fold}/{folds}: held-out P mean score "
                  f"{p_scores[pte].mean():.3f}{extra}")

    # final ensemble on ALL positives (and all negatives), for scoring new parts
    all_p = np.arange(n_p)
    all_n = np.arange(n_n)
    models = []
    for b in range(n_bags):
        clf, _sel = _bag_fit(all_p, all_n, 1000 + seed + b)
        models.append(clf)

    u_scores = np.divide(u_sum, np.maximum(u_cnt, 1))
    never = u_cnt == 0
    if n_u and never.any():
        # A row that landed in every bag's negative sample has no out-of-bag
        # score. Score it with the final ensemble rather than leaving it at 0,
        # which would silently exclude it from the candidate queue.
        u_scores[never] = np.mean(
            [m.predict_proba(Xu[never])[:, 1] for m in models], axis=0)
        if verbose:
            print(f"    {int(never.sum())} unlabeled row(s) had no out-of-bag "
                  f"score; scored with the full ensemble instead")
    return p_scores, u_scores, n_scores, models


def _vstack(a, b):
    """Stack sparse or dense feature blocks."""
    import scipy.sparse as sp
    if sp.issparse(a) or sp.issparse(b):
        return sp.vstack([a, b]).tocsr()
    import numpy as np
    return np.vstack([a, b])


def threshold_for_recall(p_scores, target_recall):
    """Score cut that yields `target_recall` on held-out positives. In a PU
    problem you cannot tune for precision, so you pick the recall you need."""
    import numpy as np
    if len(p_scores) == 0:
        return 0.5
    q = max(0.0, min(1.0, 1.0 - target_recall))
    return float(np.quantile(p_scores, q))


def top_features(featurizer, models, n=25):
    """Highest-weight tokens across the bagged models — read this to catch label
    leakage (e.g. the single token 'space' dominating)."""
    import numpy as np
    try:
        names = featurizer.get_feature_names_out()
    except Exception:
        return []
    coefs = np.mean([m.coef_[0] for m in models], axis=0)
    if len(names) != len(coefs):
        return []
    order = np.argsort(coefs)
    pos = [(names[i], float(coefs[i])) for i in order[::-1][:n]]
    neg = [(names[i], float(coefs[i])) for i in order[:n]]
    return pos, neg


# ----------------------------------------------------------------- PU metrics
def pu_metrics(preds) -> dict:
    """PU-aware scoring. A flagged unlabeled part is never called an error.

    When labelled negatives (label 'N') are present, held-out precision and a
    false-positive rate are also reported -- those ARE measurements, unlike the
    precision scenarios below, which are only assumptions."""
    P = [p for p in preds if p["label"] == "P"]
    U = [p for p in preds if p["label"] == "U"]
    N = [p for p in preds if p["label"] == "N"]
    flag_p = sum(1 for p in P if p["qualifiable"])
    flag_u = sum(1 for p in U if p["qualifiable"])
    flag_n = sum(1 for p in N if p["qualifiable"])
    n = len(P) + len(U)
    recall = flag_p / len(P) if P else 0.0
    flag_rate_u = flag_u / len(U) if U else 0.0
    pr_flag = (flag_p + flag_u) / n if n else 0.0
    # Lee & Liu (2003): recall^2 / P(flag) ranks classifiers like F1 does but
    # needs no negatives. Use it to compare backends/prompts, not as an accuracy.
    pu_score = (recall ** 2 / pr_flag) if pr_flag else 0.0
    sanity = [p for p in U if p.get("sanity_negative")]
    cmos_n = [p for p in N if p.get("cmos_negative")]
    return {"n_positives": len(P), "n_unlabeled": len(U),
            "flagged_positives": flag_p, "flagged_unlabeled": flag_u,
            "recall_on_P": recall, "flag_rate_on_U": flag_rate_u,
            "p_flag": pr_flag, "pu_score": pu_score,
            "missed_positives": len(P) - flag_p,
            "sanity_negatives": len(sanity),
            "sanity_flagged": sum(1 for p in sanity if p["qualifiable"]),
            "n_negatives": len(N), "flagged_negatives": flag_n,
            "fpr_on_N": (flag_n / len(N)) if N else 0.0,
            "precision_labeled": ((flag_p / (flag_p + flag_n))
                                  if (flag_p + flag_n) else 0.0),
            "cmos_negatives": len(cmos_n),
            "cmos_flagged": sum(1 for p in cmos_n if p["qualifiable"])}


def precision_sensitivity(m, priors=(0.01, 0.02, 0.03, 0.05, 0.10, 0.20)) -> list:
    """Estimated precision on U as a function of an ASSUMED prevalence.

    Precision is not measurable from PU data. Under the usual SCAR assumption
    (labeled positives are a random sample of all positives), the recall measured
    on P also applies to positives hiding in U, so
        expected true positives flagged in U = recall * prior * |U|
    Read it as scenarios, then replace it with a measurement via
    `eval --reviewed`."""
    rows = []
    U, r, flagged = m["n_unlabeled"], m["recall_on_P"], m["flagged_unlabeled"]
    for prior in priors:
        expected_true = r * prior * U
        rows.append({"assumed_prevalence": prior,
                     "assumed_qualifiable_in_U": round(prior * U),
                     "est_precision_on_U": (min(1.0, expected_true / flagged)
                                            if flagged else 0.0),
                     "est_true_finds": round(min(expected_true, flagged))})
    return rows


def _wilson(k, n, z=1.96):
    if not n:
        return 0.0, 0.0
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return max(0.0, centre - half), min(1.0, centre + half)


# --------------------------------------------------------------- persistence
def _load(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def _save(path, obj):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(obj, indent=1), encoding="utf-8")


def _work_items(match, include_unlabeled=True, interleave=True,
                shuffle_unlabeled=True):
    """The full work list, labelled.

    The fetch queue is INTERLEAVED 1:1 (P, U, P, U, ...) so that any prefix
    contains both classes. Strict positives-first ordering meant a run had to
    fetch every positive before a single unlabeled part arrived -- with several
    hundred positives that is many minutes of crawling that still cannot train,
    because PU learning needs an unlabeled pool to draw negatives from."""
    P = [{**p, "label": "P"} for p in match["positives"]]
    if not include_unlabeled:
        return P
    U = []
    for u in match["unlabeled"]:
        blob = f"{u['pn']} {u.get('group', '')} {u.get('category_mc', '')}"
        U.append({**u, "label": "U",
                  "sanity_negative": bool(_NOT_FLIGHT_RE.search(blob))})
    if shuffle_unlabeled:
        # CRITICAL: the catalog is ordered by product family, so taking U in
        # catalog order gave a pool of nothing but adapters. The classifier then
        # learned "adapter = no, attenuator = yes" -- category discrimination
        # from a biased sample, not qualifiability. Deterministic shuffle makes
        # the unlabeled pool representative of the whole catalog.
        random.Random(_U_SHUFFLE_SEED).shuffle(U)
    if not interleave:
        return P + U
    out, i, j = [], 0, 0
    while i < len(P) or j < len(U):
        if i < len(P):
            out.append(P[i])
            i += 1
        if j < len(U):
            out.append(U[j])
            j += 1
    return out


def _overview_path(pn):
    return CACHE_TXT / (re.sub(r"[^A-Za-z0-9._+-]", "_", norm_pn(pn)) + ".txt")


def load_texts(items, mask="none", min_chars=40):
    """Attach cached datasheet text; drop parts with too little text to judge."""
    kept, skipped = [], 0
    for it in items:
        p = _overview_path(it["pn"])
        txt = p.read_text(encoding="utf-8") if p.exists() else ""
        if len(txt.strip()) < min_chars:
            skipped += 1
            continue
        kept.append({**it, "text": mask_text(txt, mask)})
    return kept, skipped


# ------------------------------------------------------------------ commands
def cmd_match(args):
    catalog = load_catalog(args.catalog)
    positives = (load_positives_from_json(args.positives) if args.positives
                 else load_positives_from_db(args.db))
    match = build_match(catalog, positives)
    _save(MATCH_JSON, match)
    c = match["counts"]
    print(f"Mini-Circuits catalog        {len(catalog)}")
    print(f"everythingRF space parts     {c['erf_positives']}")
    print(f"  matched in catalog         {c['matched_in_catalog']}")
    print(f"  not in catalog, URL guess  {c['url_guess']}")
    print(f"unlabeled catalog parts (U)  {c['unlabeled']}")
    if match["positives"]:
        print(f"  match types: {dict(Counter(p['match'] for p in match['positives']))}")
    if c["url_guess"]:
        print("\nNot in the Mini-Circuits catalog -- will try "
              "minicircuits.com/pdfs/<PN>.pdf; `fetch` reports any that fail:")
        for p in match["positives"]:
            if p["match"] == "url-guess":
                print(f"  {p['pn']:<28} {p['datasheet_url']}")
    print(f"\nsaved {MATCH_JSON}")
    return 0


def cmd_fetch(args):
    match = _load(MATCH_JSON, None)
    if not match:
        raise SystemExit("run `match` first")
    CACHE_PDF.mkdir(parents=True, exist_ok=True)
    CACHE_TXT.mkdir(parents=True, exist_ok=True)
    items = _work_items(match, include_unlabeled=not args.positives_only)
    quiet = getattr(args, "quiet", False)
    fetcher = Fetcher(rate=args.rate, ignore_robots=args.ignore_robots)
    got = cached = failed = empty = recovered = attempts = 0
    missing = []
    total = len(items)
    # --limit caps NEW fetches, not queue position. Already-cached parts are
    # skipped for free and do not spend the budget, so each run continues where
    # the last one stopped. (Slicing the queue instead capped coverage at N
    # forever: re-running just re-walked the same first N parts.)
    budget = args.limit or 0
    started = time.time()
    print(f"queue {total} part(s); "
          + (f"fetching at most {budget} new one(s)" if budget
             else "fetching everything not already cached")
          + f" at {args.rate}s/request -> {CACHE_PDF}")
    print(f"{'':>4}{'#':>7}  {'part':<26} {'status':<10} detail")
    stopped_at = None
    for i, it in enumerate(items, 1):
        if budget and attempts >= budget:
            stopped_at = i - 1
            print(f"\n  hit the limit of {budget} new fetch(es) at queue position "
                  f"{stopped_at}/{total}; re-run to carry on from here.")
            break
        pn = norm_pn(it["pn"])
        safe = re.sub(r"[^A-Za-z0-9._+-]", "_", pn)
        txt_path, pdf_path = CACHE_TXT / f"{safe}.txt", CACHE_PDF / f"{safe}.pdf"
        status = detail = ""
        if txt_path.exists() and not args.refetch:
            # An empty overview only means something when we hold the PDF (a
            # scanned datasheet). Empty with no PDF = an earlier failed fetch,
            # so retry rather than skip forever.
            if txt_path.stat().st_size > 0 or pdf_path.exists():
                cached += 1
                # Don't print a line per cached part: on a resumed run that is
                # thousands of lines of noise. Show a periodic heartbeat instead.
                if not quiet and cached % 250 == 0:
                    print(f"  {'':>4}{i:>6}/{total}  ...{cached} already cached, "
                          f"skipping", flush=True)
                continue
        candidates = pdf_url_candidates(pn, it.get("datasheet_url"))
        blob = (pdf_path.read_bytes()
                if (pdf_path.exists() and not args.refetch) else None)
        from_disk = blob is not None
        used_why = "cached pdf"
        if blob is None:
            attempts += 1
            for url, why in candidates:
                try:
                    blob = fetcher.get(url)
                except PermissionError as e:
                    raise SystemExit(f"stopping: {e}")
                if blob:
                    used_why = why
                    if why != "catalog url":
                        recovered += 1
                    break
            if blob:
                pdf_path.write_bytes(blob)
        if not blob:
            # not cached: may be transient (timeout/429/offline), so retry next run
            failed += 1
            tried = " | ".join(u for u, _w in candidates)
            missing.append((it["pn"], it["label"], tried))
            _fetch_line(i, total, it, "FAILED",
                        f"tried {len(candidates)} url(s): {candidates[0][0]}",
                        started, quiet)
            continue
        overview = extract_text(blob, getattr(args, 'text_mode', 'full'))
        txt_path.write_text(overview, encoding="utf-8")
        got += 1
        note = "" if used_why in ("catalog url", "cached pdf") else f"  [{used_why}]"
        if overview:
            status = "ok" if not from_disk else "reparsed"
            detail = (f"{len(blob) // 1024} kB pdf, {len(overview)} chars"
                      f"{note}")
        else:
            empty += 1
            status, detail = "no text", f"{len(blob) // 1024} kB pdf (scanned?){note}"
        _fetch_line(i, total, it, status, detail, started, quiet)
    elapsed = time.time() - started
    print(f"\nfetched {got}   cached {cached}   no-text {empty}   failed {failed}"
          f"   in {elapsed / 60:.1f} min")
    if recovered:
        print(f"  {recovered} datasheet(s) recovered by a fallback URL rule "
              f"(e.g. dropping a trailing X)")
    # Coverage, split by label: training needs BOTH known-space parts and
    # unlabeled ones, and the queue puts positives first, so a short run can end
    # up with positives only.
    have_p, have_u = _cache_coverage(items)
    n_p = sum(1 for it in items if it["label"] == "P")
    n_u = total - n_p
    print(f"\ncached datasheet text: {have_p}/{n_p} known-space (P), "
          f"{have_u}/{n_u} unlabeled (U)")
    if have_p and not have_u:
        print("  ! Training needs unlabeled parts too, and the queue fetches all")
        print("    positives first. Run fetch again (cached parts are skipped for")
        print("    free) until the U count is at least a few hundred.")
    elif have_u and have_u < 200:
        print(f"  note: {have_u} unlabeled part(s) is thin; a few hundred makes "
              f"the PU bagging estimates steadier.")
    if missing:
        print("\nNo datasheet retrieved (not in catalog and no /pdfs/<PN>.pdf):")
        for pn, label, url in missing[: args.show_missing]:
            print(f"  [{label}] {pn:<28} {url}")
        if len(missing) > args.show_missing:
            print(f"  ... and {len(missing) - args.show_missing} more")
        _save(WORKDIR / "missing_datasheets.json",
              [{"pn": p, "label": l, "url": u} for p, l, u in missing])
        print(f"  full list: {WORKDIR / 'missing_datasheets.json'}")
    return 0


def _cache_coverage(items):
    """(positives, unlabeled) that currently have usable cached overview text."""
    p = u = 0
    for it in items:
        path = _overview_path(it["pn"])
        if path.exists() and path.stat().st_size >= 40:
            if it["label"] == "P":
                p += 1
            else:
                u += 1
    return p, u


def _fetch_line(i, total, item, status, detail, started, quiet=False):
    """One progress line per part. Printed by default -- a 1 req/sec crawl that
    prints nothing for a minute looks broken."""
    if quiet:
        return
    eta = ""
    if i >= 3 and total > i:
        rate = (time.time() - started) / i
        left = rate * (total - i)
        eta = f"  eta {left / 60:.0f}m" if left > 90 else f"  eta {left:.0f}s"
    tag = f"[{item['label']}]"
    print(f"  {tag:>4}{i:>6}/{total}  {item['pn'][:26]:<26} {status:<10} "
          f"{detail}{eta}", flush=True)


def _build_featurizer(args):
    if args.backend == "tfidf":
        return make_tfidf_featurizer(), "tfidf"
    return EmbedFeaturizer(args.hf_model), f"embed:{args.hf_model}"


def cmd_train(args):
    mask = getattr(args, "mask", "none")
    local_only = bool(getattr(args, "local_only", False))
    use_local = bool(getattr(args, "local", True))
    cmos_w = float(getattr(args, "neg_weight_cmos", 3.0))

    P, U, N = [], [], []
    skipped = 0

    # ---- local datasheet corpus (Qorvo HTML, Marki/Skyworks/MACOM PDFs) ------
    corpus = _load(LOCAL_JSON, None) if use_local else None
    if corpus:
        loc, loc_skipped = load_local_texts(corpus, mask=mask)
        skipped += loc_skipped
        P += [d for d in loc if d["label"] == "P"]
        N += [d for d in loc if d["label"] == "N"]
        U += [d for d in loc if d["label"] == "U"]
        by_v = Counter(f"{d['vendor_name']}/{d['label']}" for d in loc)
        print(f"local corpus ({corpus.get('root', '?')}):")
        print(f"  {len(loc)} part(s) with text  "
              f"[{sum(1 for d in loc if d['label'] == 'P')} P, "
              f"{sum(1 for d in loc if d['label'] == 'N')} N, "
              f"{sum(1 for d in loc if d['label'] == 'U')} U]"
              + (f"   {loc_skipped} skipped (thin text)" if loc_skipped else ""))
        for k in sorted(by_v):
            print(f"    {k:<34} {by_v[k]}")
    elif use_local and not local_only:
        print("(no local corpus yet -- run `local-scan` to train on the "
              "datasheets you already have)")

    # ---- the original Mini-Circuits match (still supported) -----------------
    match = _load(MATCH_JSON, None) if not local_only else None
    if match:
        items = _work_items(match)
        data, mc_skipped = load_texts(items, mask=mask)
        skipped += mc_skipped
        P += [d for d in data if d["label"] == "P"]
        U += [d for d in data if d["label"] == "U"]
        print(f"Mini-Circuits match: {sum(1 for d in data if d['label'] == 'P')} "
              f"P, {sum(1 for d in data if d['label'] == 'U')} U")
    if not corpus and not match:
        raise SystemExit(
            "no training data.\n"
            "Either run `local-scan` (trains on datasheets already on disk -- "
            "no network),\nor run `match` + `fetch` for the Mini-Circuits path.")

    print(f"\ntraining data: {len(P)} positives, {len(N)} labelled negatives, "
          f"{len(U)} unlabeled ({skipped} skipped for missing/thin text)")
    if N:
        n_cmos = sum(1 for d in N if _CMOS_NEG_RE.search(d["text"]))
        print(f"  of the negatives, {n_cmos} match CMOS driver/logic language "
              f"and are weighted x{cmos_w:g}")
    if len(P) < 5:
        raise SystemExit(
            f"only {len(P)} positives have datasheet text -- run `fetch` "
            f"(at least a few dozen positives makes this meaningful)")
    if len(P) < 25:
        print(f"  ! only {len(P)} positives: expect noisy estimates. Treat the "
              f"numbers as indicative and widen your review sample.")
    if U and len(U) < len(P):
        print(f"  ! only {len(U)} unlabeled part(s) for {len(P)} positives. PU "
              f"bagging draws its negatives from U, so a pool smaller than the\n"
              f"    positive set makes every bag nearly identical and the "
              f"estimates unstable.\n"
              f"    Fetch more parts (the queue interleaves P and U, so just run "
              f"fetch again).")
    if not U and not N:
        raise SystemExit(
            f"{len(P)} positives have text, but there are no negatives and no\n"
            f"unlabeled pool, so there is nothing to separate them from.\n"
            f"Either run `local-scan` (parts absent from your space sources become\n"
            f"labelled negatives), or run `fetch` again until a few hundred\n"
            f"unlabeled Mini-Circuits parts have datasheet text.")
    if N and len(N) < 5:
        print(f"  ! only {len(N)} labelled negative(s); the held-out precision "
              f"figure will be very rough.")

    featurizer, tag = _build_featurizer(args)
    texts = ([d["text"] for d in P] + [d["text"] for d in N]
             + [d["text"] for d in U])
    print(f"  featurizing with {tag} ...")
    X = featurizer.fit_transform(texts)
    Xp = X[: len(P)]
    Xn = X[len(P): len(P) + len(N)] if N else None
    Xu = X[len(P) + len(N):]
    wn = None
    if N:
        import numpy as _np
        wn = _np.array([neg_sample_weight(d["text"], cmos_w) for d in N])
    try:
        print(f"  feature matrix: {X.shape[0]} x {X.shape[1]}")
    except Exception:
        pass

    print(f"  PU bagging: {args.bags} bags x {args.folds} folds"
          + (f"  (+{len(N)} labelled negatives in every bag)" if N else ""))
    p_scores, u_scores, n_scores, models = train_pu(
        Xp, Xu, n_bags=args.bags, folds=args.folds, seed=args.seed,
        Xn=Xn, wn=wn)
    # Domain priors are applied BEFORE the threshold is chosen, so the reported
    # recall and flag rate describe the blended system you will actually run.
    pw = 0.0 if args.no_prior else args.prior_weight
    if pw:
        p_scores = [blend_prior(s, d["text"], pw) for s, d in zip(p_scores, P)]
        u_scores = [blend_prior(s, d["text"], pw) for s, d in zip(u_scores, U)]
        n_scores = [blend_prior(s, d["text"], pw) for s, d in zip(n_scores, N)]
        print(f"  domain priors blended in at weight {pw} "
              f"({len(DOMAIN_PRIOR)} terms)")
    else:
        p_scores, u_scores = list(p_scores), list(u_scores)
        n_scores = list(n_scores)
        print("  domain priors disabled")
    thr = (args.threshold if args.threshold is not None
           else threshold_for_recall(p_scores, args.target_recall))
    print(f"\n  decision threshold {thr:.4f}"
          + ("" if args.threshold is not None
             else f" (chosen for {args.target_recall:.0%} recall on held-out P)"))

    preds = []
    for d, s in zip(P, p_scores):
        preds.append(_pred_row(d, float(s), thr, tag, args.mask))
    for d, s in zip(N, n_scores):
        preds.append(_pred_row(d, float(s), thr, tag, args.mask))
    for d, s in zip(U, u_scores):
        preds.append(_pred_row(d, float(s), thr, tag, args.mask))
    _save(PRED_JSON, preds)

    if args.backend == "tfidf" and not args.no_features:
        feats = top_features(featurizer, models, n=args.show_features)
        if feats:
            pos, neg = feats
            print("\n  top features pushing TOWARD qualifiable:")
            for name, w in pos:
                print(f"    {w:+.3f}  {name}")
            print("  top features pushing AWAY:")
            for name, w in neg[:10]:
                print(f"    {w:+.3f}  {name}")
            leak = [n for n, _w in pos if _SPACE_WORDS.search(n)]
            if leak and args.mask == "none":
                print(f"\n  ! LEAKAGE CHECK: {leak[:5]} are doing the work. The "
                      f"model may be reading the label off the datasheet.\n"
                      f"    Re-run with --mask space (or strict) and compare "
                      f"recall to see what it infers from construction alone.")
    try:
        import joblib
        joblib.dump({"featurizer": featurizer, "models": models,
                     "threshold": thr, "backend": tag, "mask": args.mask,
                     "prior_weight": pw,
                     "trained": time.strftime("%Y-%m-%d %H:%M"),
                     "n_positives": len(P), "n_unlabeled": len(U),
                     "n_negatives": len(N), "neg_weight_cmos": cmos_w,
                     "boilerplate": (corpus or {}).get("boilerplate", {}),
                     "vendors": sorted({d.get("vendor_name", "")
                                        for d in P + N + U if d.get("vendor_name")})},
                    MODEL_FILE)
        print(f"\n  saved model -> {MODEL_FILE}")
    except Exception as e:
        print(f"\n  (model not saved: {e})")
    print(f"  saved predictions -> {PRED_JSON}\n")
    return cmd_eval(argparse.Namespace(min_confidence=0.0, reviewed=None))


def _pred_row(d, score, thr, tag, mask):
    return {"pn": d["pn"], "label": d["label"],
            "sanity_negative": d.get("sanity_negative", False),
            "cmos_negative": d.get("cmos_negative", False),
            "category_mc": d.get("category_mc", ""),
            "datasheet_url": d.get("datasheet_url", ""),
            "variant": d.get("variant", ""),
            "vendor": d.get("vendor_name") or d.get("vendor", ""),
            "source": d.get("source", ""),
            "label_why": d.get("label_why", ""),
            "local_file": d.get("file", ""),
            "score": round(score, 5), "confidence": round(score, 5),
            "qualifiable": bool(score >= thr),
            "threshold": round(thr, 5), "model": tag, "mask": mask}


def score_text(bundle, text):
    """Blended probability that a part described by `text` is space-qualifiable,
    plus the pieces that produced it."""
    import numpy as np
    masked = mask_text(text, bundle.get("mask", "none"))
    X = bundle["featurizer"].transform([masked])
    raw = float(np.mean([m.predict_proba(X)[:, 1][0] for m in bundle["models"]]))
    pw = bundle.get("prior_weight", 0.0)
    hits = prior_hits(masked) if pw else []
    final = blend_prior(raw, masked, pw)
    return {"probability": final, "model_only": raw, "prior_weight": pw,
            "prior_logit": prior_logit(masked) if pw else 0.0,
            "prior_hits": hits, "threshold": bundle["threshold"]}


def _print_score(name, r):
    pct = 100.0 * r["probability"]
    verdict = ("LIKELY QUALIFIABLE" if r["probability"] >= r["threshold"]
               else "not flagged")
    print(f"\n  {name}")
    print(f"    chance space-qualifiable   {pct:5.1f}%     -> {verdict}")
    print(f"    (threshold {100 * r['threshold']:.1f}% chosen at training time)")
    if r["prior_weight"]:
        print(f"    learned model alone        {100 * r['model_only']:5.1f}%")
        if r["prior_hits"]:
            print(f"    domain priors ({r['prior_logit']:+.2f} log-odds):")
            for why, w in sorted(r["prior_hits"], key=lambda h: -abs(h[1])):
                print(f"      {w:+.2f}  {why}")
        else:
            print("    domain priors: no terms matched")


def cmd_predict(args):
    """Score a paragraph of text, or a part already fetched, with the saved model."""
    import joblib
    if not MODEL_FILE.exists():
        raise SystemExit("no saved model -- run `train` (or menu option 1) first")
    bundle = joblib.load(MODEL_FILE)
    print(f"model: {bundle.get('backend')}  trained {bundle.get('trained', '?')}  "
          f"on {bundle.get('n_positives', '?')} positives  "
          f"(mask={bundle.get('mask')}, prior_weight={bundle.get('prior_weight')})")
    did = False
    if args.text:
        _print_score("(text)", score_text(bundle, args.text))
        did = True
    if args.file:
        _print_score(Path(args.file).name,
                     score_text(bundle, Path(args.file).read_text(encoding="utf-8")))
        did = True
    for pn in args.pn or []:
        p = _overview_path(pn)
        if not p.exists():
            print(f"\n  {pn}: no cached datasheet text (fetch it first)")
            continue
        _print_score(pn, score_text(bundle, p.read_text(encoding="utf-8")))
        did = True
    if not did:
        raise SystemExit("pass --text, --file or --pn")
    return 0


def interactive_score():
    """Paste-a-paragraph loop, for the menu."""
    import joblib
    if not MODEL_FILE.exists():
        print("  no trained model yet -- run option 1 (or 4) first.")
        return
    bundle = joblib.load(MODEL_FILE)
    print(f"\n  model: {bundle.get('backend')}  trained "
          f"{bundle.get('trained', '?')}  on {bundle.get('n_positives', '?')} "
          f"positives")
    print("  Paste a datasheet paragraph. Blank line scores it; 'q' returns.")
    while True:
        print("\n  > ", end="", flush=True)
        lines = []
        while True:
            try:
                ln = input()
            except EOFError:
                return
            if ln.strip().lower() in ("q", "quit", "exit"):
                return
            if not ln.strip():
                break
            lines.append(ln)
        text = "\n".join(lines).strip()
        if not text:
            continue
        _print_score("(pasted text)", score_text(bundle, text))


def cmd_eval(args):
    preds = _load(PRED_JSON, [])
    if not preds:
        raise SystemExit("no predictions yet -- run `train`")
    if getattr(args, "min_confidence", 0):
        preds = [dict(p, qualifiable=p["score"] >= args.min_confidence)
                 for p in preds]
    m = pu_metrics(preds)
    print("Positive-Unlabeled evaluation")
    print("=" * 58)
    print(f"  known space parts (P)        {m['n_positives']}")
    print(f"  unlabeled catalog parts (U)  {m['n_unlabeled']}")
    if getattr(args, "min_confidence", 0):
        print(f"  score threshold override     >= {args.min_confidence}")
    print(f"\n  RECALL ON P                  {m['recall_on_P']:.1%}"
          f"   ({m['flagged_positives']}/{m['n_positives']})   [held out]")
    print(f"    missed known space parts   {m['missed_positives']}"
          f"   <- real errors")
    if m["n_unlabeled"]:
        print(f"\n  flag rate on U               {m['flag_rate_on_U']:.1%}"
              f"   ({m['flagged_unlabeled']}/{m['n_unlabeled']})")
        print("    NOT an error rate: U is unlabeled, so a flag here is a")
        print("    candidate to review, and some are genuinely qualifiable.")
    if m["n_negatives"]:
        print(f"\n  LABELLED NEGATIVES (N)       {m['n_negatives']}"
              f"   [datasheets you hold that appear in no space source]")
        print(f"    flagged                    {m['flagged_negatives']} "
              f"({m['fpr_on_N']:.1%})")
        print(f"    PRECISION on labelled data {m['precision_labeled']:.1%}"
              f"   ({m['flagged_positives']}/"
              f"{m['flagged_positives'] + m['flagged_negatives']})   [held out]")
        print("    Measured, not assumed -- but read it as a LOWER bound: a")
        print("    flagged negative may be a genuinely qualifiable part that is")
        print("    simply not listed, which is exactly what you are hunting for.")
        if m["cmos_negatives"]:
            rate = m["cmos_flagged"] / m["cmos_negatives"]
            print(f"\n    CMOS driver/logic negatives {m['cmos_negatives']}"
                  f"  (up-weighted in training)")
            print(f"      still flagged            {m['cmos_flagged']} ({rate:.1%})"
                  f"   <- want near zero")
    print(f"\n  PU score (recall^2/P(flag))   {m['pu_score']:.3f}"
          f"   <- compare backends with this")
    if m["sanity_negatives"]:
        rate = m["sanity_flagged"] / m["sanity_negatives"]
        print(f"\n  sanity negatives (eval boards, kits, adapters): "
              f"{m['sanity_negatives']}")
        print(f"    flagged                    {m['sanity_flagged']} ({rate:.1%})"
              f"   <- want LOW; heuristic set, not ground truth")
    if m["n_unlabeled"]:
        print("\n  Precision on U cannot be measured from PU data. Scenarios:")
        print("    assumed prevalence | qualifiable in U | est. precision | est. finds")
        for row in precision_sensitivity(m):
            print(f"      {row['assumed_prevalence']:>14.0%} | "
                  f"{row['assumed_qualifiable_in_U']:>16} | "
                  f"{row['est_precision_on_U']:>14.1%} | {row['est_true_finds']:>10}")
    if getattr(args, "reviewed", None):
        _report_reviewed(args.reviewed, m)
    if m["n_unlabeled"]:
        print("\n  Next: `review` to export the ranked candidate queue, label a")
        print("  sample, then `eval --reviewed labelled.csv` for real precision.")
    else:
        print("\n  Next: `review` exports the flagged negatives -- parts you hold "
              "whose\n  datasheets read like space parts but which appear in no "
              "space source.\n  Those are the candidates worth checking by hand.")
    return 0


def _report_reviewed(path, m):
    rows = list(csv.DictReader(Path(path).open(encoding="utf-8")))
    judged = [r for r in rows if (r.get("verdict") or "").strip().lower()
              in ("y", "yes", "n", "no", "true", "false")]
    if not judged:
        print(f"\n  {path}: no rows with a y/n 'verdict' -- nothing to measure")
        return
    yes = sum(1 for r in judged
              if (r["verdict"] or "").strip().lower() in ("y", "yes", "true"))
    prec = yes / len(judged)
    est_total = prec * m["flagged_unlabeled"]
    lo, hi = _wilson(yes, len(judged))
    print(f"\n  MEASURED on {len(judged)} reviewed candidates")
    print(f"    precision on flagged U     {prec:.1%}  (95% CI {lo:.1%}-{hi:.1%})")
    print(f"    => est. qualifiable finds  {est_total:.0f} of "
          f"{m['flagged_unlabeled']} flagged")
    if m["recall_on_P"]:
        print(f"    => est. qualifiable in U   "
              f"{est_total / m['recall_on_P']:.0f} "
              f"(dividing by recall {m['recall_on_P']:.1%})")


def cmd_review(args):
    preds = _load(PRED_JSON, [])
    if not preds:
        raise SystemExit("no predictions yet -- run `train`")
    # Flagged N belongs in the queue too. Under --absent-as negative a part is a
    # negative only because it is absent from your space listings, so a flagged
    # one is precisely the candidate this tool exists to surface -- dropping it
    # would hide the discoveries.
    queue = sorted([p for p in preds
                    if p["label"] in ("U", "N") and p["qualifiable"]],
                   key=lambda p: -p["score"])
    if args.limit:
        queue = queue[: args.limit]
    out = Path(args.out)
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["pn", "label", "vendor", "score", "category",
                    "datasheet_url", "local_file", "verdict"])
        for p in queue:
            w.writerow([p["pn"], p["label"], p.get("vendor", ""),
                        f"{p['score']:.4f}", p.get("category_mc", ""),
                        p.get("datasheet_url", ""), p.get("local_file", ""), ""])
    print(f"wrote {len(queue)} candidates -> {out}")
    print("Fill the 'verdict' column with y/n, then:")
    print(f"  python {Path(sys.argv[0]).name} eval --reviewed {out}")
    missed = [p for p in preds if p["label"] == "P" and not p["qualifiable"]]
    if missed:
        mo = out.with_name(out.stem + "_missed_positives.csv")
        with mo.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["pn", "score", "datasheet_url"])
            for p in sorted(missed, key=lambda p: -p["score"]):
                w.writerow([p["pn"], f"{p['score']:.4f}",
                            p.get("datasheet_url", "")])
        print(f"also wrote {len(missed)} MISSED known-space parts -> {mo}")
        print("  (real errors; read these to improve features or text extraction)")
    return 0


# ------------------------------------------------------------------- selftest
_POS_BLURB = [
    "hermetic ceramic package, glass-to-metal seals, -55 to +125 C operating",
    "thin film on alumina substrate, available as bare die for chip and wire",
    "MMIC amplifier in hermetically sealed metal case, gold plated, no organics",
    "GaAs MMIC bare die, hermetic option, military temperature range, screened",
    "welded metal housing, hermetic, extended temperature -55C to +125C, MIL parts",
    "ceramic hermetic surface mount, glass to metal seal, wide temperature",
]
_NEG_BLURB = [
    "plastic overmolded surface mount package, 0 to 70 C commercial temperature",
    "evaluation board with SMA connectors for bench test, includes USB cable",
    "epoxy encapsulated module, commercial grade, RoHS plastic body",
    "handheld test accessory with LCD display and rechargeable battery",
    "torque wrench for SMA connectors, calibrated, carrying case included",
    "coaxial adapter, nickel plated brass body, commercial use",
]
_FILLER = ("50 ohm impedance frequency range insertion loss return loss VSWR "
           "typical performance dimensions outline drawing ordering information "
           "gain flatness input power dc supply current pin configuration")


def cmd_selftest(args):
    """Run the full train/eval loop on synthetic blurbs, entirely offline.

    Verifies the install and the PU machinery without needing the network, the
    catalog, or the rfparts DB. Positives get 'hermetic/ceramic/die' language,
    the unlabeled pool is mostly commercial-plastic language with a known
    fraction of hidden positives, so you can check the reported recall and see
    the estimator bracket a prevalence you actually control."""
    if getattr(args, "local", False):
        return cmd_selftest_local(args)
    rnd = random.Random(args.seed)
    CACHE_TXT.mkdir(parents=True, exist_ok=True)

    def blurb(pos):
        base = rnd.choice(_POS_BLURB if pos else _NEG_BLURB)
        extra = " ".join(rnd.sample(_FILLER.split(), 12))
        return f"Model SYN-{rnd.randrange(10**6)}\n{base}\n{extra}"

    n_p, n_u, hidden = args.positives, args.unlabeled, args.hidden
    positives, unlabeled = [], []
    for i in range(n_p):
        pn = f"SYNP-{i:04d}"
        _overview_path(pn).write_text(blurb(True), encoding="utf-8")
        positives.append({"pn": pn, "variant": "space_qualified", "match": "exact",
                          "datasheet_url": "", "category_mc": "syn",
                          "case_style": "", "vendor": "Mini-Circuits"})
    for i in range(n_u):
        pn = f"SYNU-{i:05d}"
        is_hidden = i < hidden
        _overview_path(pn).write_text(blurb(is_hidden), encoding="utf-8")
        unlabeled.append({"pn": pn, "datasheet_url": "", "category_mc": "syn",
                          "case_style": "", "group": "syn", "match": "catalog"})
    _save(MATCH_JSON, {"positives": positives, "unlabeled": unlabeled,
                       "counts": {"erf_positives": n_p, "matched_in_catalog": n_p,
                                  "url_guess": 0, "unlabeled": n_u}})
    print(f"synthetic set: {n_p} positives, {n_u} unlabeled "
          f"({hidden} of them secretly qualifiable = "
          f"{hidden / n_u:.1%} true prevalence)\n")
    rc = cmd_train(argparse.Namespace(
        backend=args.backend, hf_model=args.hf_model, mask="none",
        bags=args.bags, folds=args.folds, seed=args.seed,
        target_recall=args.target_recall, threshold=None,
        show_features=12, no_features=False,
        prior_weight=1.0, no_prior=False,
        # This selftest is about the Mini-Circuits PU path only. Without these
        # the run would silently absorb a real local corpus and the "ground
        # truth" printed below would no longer be ground truth.
        local=False, local_only=False, neg_weight_cmos=3.0))
    preds = {p["pn"]: p for p in _load(PRED_JSON, [])}
    flagged_hidden = sum(1 for i in range(hidden)
                         if preds.get(f"SYNU-{i:05d}", {}).get("qualifiable"))
    flagged_u = sum(1 for p in preds.values()
                    if p["label"] == "U" and p["qualifiable"])
    print("\n  SELFTEST ground truth (known only because this data is synthetic)")
    print(f"    hidden positives recovered  {flagged_hidden}/{hidden}")
    if flagged_u:
        print(f"    TRUE precision on U         "
              f"{flagged_hidden / flagged_u:.1%}  ({flagged_hidden}/{flagged_u})")
        print( "    compare with the scenario table above at the true prevalence")
    return rc


# -------------------------------------------- selftest: local datasheet path
# Builds a synthetic datasheet library on disk (HTML, per-vendor folders), a
# synthetic set of saved everythingRF space pages, and a synthetic parts.db,
# then runs local-scan + train against them. Entirely offline.
#
# It also plants HIDDEN positives: parts whose text is space-like but which are
# deliberately left out of every label source, so they are labelled negative.
# Recovering those is the whole point of the tool, and it is the honest way to
# see what "precision on labelled data" really means -- a flagged negative here
# is a success, not an error.
_SL_CHROME = [
    "Home | Products | Design Tools | Support | Contact Us",
    "Copyright Example Semiconductor Inc. All rights reserved.",
    "This site uses cookies to deliver and improve our services.",
    "Add to cart    Request a sample    Contact sales    Where to buy",
    "Follow us on social media for product announcements and news",
]
_SL_POS_TEXT = [
    "Hermetically sealed GaAs MMIC amplifier in a kovar package with alumina "
    "substrate and glass-to-metal seal feedthroughs. Laser welded housing. "
    "Screened to MIL-STD-883 method 5008 with an upscreened option available. "
    "Operating temperature -55 C to +125 C. Available as bare die for chip and "
    "wire assembly. QML Class K flow supported under MIL-PRF-38534.",
    "Thin film ceramic hybrid mixer built on alumina with a hermetic solder "
    "sealed lid. Kovar body, glass bead feedthrough. MIL-PRF-38534 Class K "
    "screening and radiation lot acceptance testing offered. Temperature range "
    "-55 C to +125 C. Bare die and MMIC die options for hybrid integration.",
    "GaN power amplifier die on a ceramic carrier, hermetic package option, "
    "MIL-STD-883 screened. Thin film matching network on alumina. Glass to "
    "metal sealed connectors, laser welded seam. QML V product flow available "
    "for high reliability and space programs. -55 C to +150 C rated.",
]
_SL_NEG_TEXT = [
    "Low cost overmolded plastic package amplifier for commercial wireless "
    "infrastructure. Epoxy encapsulated body on an organic laminate substrate. "
    "Operating temperature 0 to 70 C. RoHS compliant tape and reel packaging. "
    "An evaluation board is available for quick prototyping and bench testing.",
    "Plastic QFN packaged front end module with epoxy overmold for handset "
    "applications. Commercial temperature range 0 to 70 C. Electrolytic "
    "bypass capacitors recommended on the supply rail. Demo board and kit "
    "available. Consumer grade, high volume, no screening options offered.",
    "Surface mount plastic SOT package attenuator for consumer set top boxes. "
    "Overmolded epoxy body, laminate substrate, commercial 0 to 70 C rating. "
    "Test board and evaluation kit sold separately. Not recommended for high "
    "reliability or extended temperature applications.",
]
_SL_CMOS_TEXT = [
    "Octal CMOS driver and level translator in a plastic package for logic "
    "interface applications. LVCMOS and LVTTL compatible inputs. Contains "
    "shift register and flip-flop stages. Commercial 0 to 70 C temperature "
    "range, epoxy overmolded body, evaluation board available.",
    "CMOS logic buffer and gate driver array with a plastic body. LVCMOS "
    "compatible, TTL threshold inputs, NAND gate and inverter stages on chip. "
    "Voltage level shifter function included. Commercial temperature range, "
    "overmolded epoxy, low cost high volume consumer part.",
    "Line driver / clock driver IC with CMOS logic outputs, HCMOS compatible. "
    "Plastic package, epoxy encapsulation, 0 to 70 C commercial rating. "
    "Contains a shift register, multiplexer logic and flip-flop stages. "
    "Demo board and driver IC evaluation kit available.",
]


def _sl_page(vendor, pn, body, rnd):
    """A vendor product page: real template chrome around the product prose."""
    chrome_top = "\n".join(f"<li>{c}</li>" for c in _SL_CHROME[:3])
    chrome_bot = "\n".join(f"<p>{c}</p>" for c in _SL_CHROME[3:])
    filler = " ".join(rnd.sample(_FILLER.split(), 10))
    return (
        f"<!DOCTYPE html><html><head>"
        f"<title>{vendor} {pn} Product Page</title>"
        f"<meta name=\"description\" content=\"{vendor} {pn} RF component\">"
        f"<style>.x{{color:red}}</style><script>var a=1;</script>"
        f"</head><body><nav><ul>{chrome_top}</ul></nav>"
        f"<h1>{pn}</h1><div class=\"desc\"><p>{body}</p><p>{filler}</p></div>"
        f"<table><tr><td>1.0</td><td>2.5</td><td>3.7</td></tr></table>"
        f"<footer>{chrome_bot}</footer></body></html>")


def _sl_erf_page(rows):
    """A saved everythingRF space listing page, in the shape the real ones take."""
    boxes = []
    for pn, vendor in rows:
        boxes.append(
            f"<div class=\"product-box\">"
            f"<h3 class=\"prod-title\"><a dname=\"{pn}\">{vendor} - {pn}</a></h3>"
            f"<a class=\"manuName\" manu-name=\"{vendor}\">{vendor}</a>"
            f"<span class=\"nodeName\">Space Qualified RF Amplifier</span>"
            f"</div>")
    return ("<html><head><title>Space Qualified RF Amplifiers</title></head>"
            "<body><h1>Space Qualified RF Amplifiers</h1>"
            + "".join(boxes) + "</body></html>")


def _sl_make_db(path, space_rows, plain_rows):
    """A minimal parts.db with the columns the label oracle reads."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE parts(id INTEGER PRIMARY KEY, mpn TEXT, mpn_norm TEXT,
            vendor TEXT, category TEXT DEFAULT '', subcategory TEXT DEFAULT '',
            product_url TEXT DEFAULT '', description TEXT DEFAULT '');
        CREATE TABLE specs(part_id INT, key TEXT, value_text TEXT);
        CREATE TABLE qual_evidence(part_id INT, signal TEXT, weight REAL,
            source_url TEXT DEFAULT '', snippet TEXT DEFAULT '');
        CREATE TABLE documents(sha256 TEXT, url TEXT, part_id INT);
    """)
    pid = 0
    for pn, vendor in space_rows:
        pid += 1
        conn.execute("INSERT INTO parts(id, mpn, mpn_norm, vendor) VALUES(?,?,?,?)",
                     (pid, pn, norm_pn(pn), vendor))
        conn.execute("INSERT INTO specs VALUES(?,?,?)", (pid, "space", "qualified"))
        conn.execute("INSERT INTO qual_evidence VALUES(?,?,?,?,?)",
                     (pid, "qorvo-aerospace-brochure", 7.0, "", "synthetic"))
    for pn, vendor in plain_rows:
        pid += 1
        conn.execute("INSERT INTO parts(id, mpn, mpn_norm, vendor) VALUES(?,?,?,?)",
                     (pid, pn, norm_pn(pn), vendor))
    conn.commit()
    conn.close()


def cmd_selftest_local(args):
    """End-to-end check of the LOCAL datasheet path, offline."""
    rnd = random.Random(getattr(args, "seed", 0))
    base = WORKDIR / "selftest_local"
    ds_root = base / "datasheets"
    erf_dir = base / "everythingRF"
    db_path = base / "parts.db"
    for d in (ds_root, erf_dir):
        d.mkdir(parents=True, exist_ok=True)

    n_pos = max(12, getattr(args, "positives", 40))
    n_neg = max(12, getattr(args, "negatives", 60))
    hidden = max(1, getattr(args, "hidden", 6))

    # ---- lay out the synthetic library -------------------------------------
    vendors = ["qorvo", "skyworks", "macom"]
    listed, hidden_pns, neg_pns, cmos_pns = [], [], [], []
    for i in range(n_pos):
        vkey = vendors[i % len(vendors)]
        vname = VENDORS[vkey]["name"]
        pn = f"SPQ{i:04d}"
        body = _SL_POS_TEXT[i % len(_SL_POS_TEXT)]
        d = ds_root / VENDORS[vkey]["slug"]
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{pn}_datasheet.html").write_text(_sl_page(vname, pn, body, rnd),
                                                encoding="utf-8")
        # the last `hidden` positives are left out of every label source
        (hidden_pns if i >= n_pos - hidden else listed).append((pn, vname))
    for i in range(n_neg):
        vkey = vendors[i % len(vendors)]
        vname = VENDORS[vkey]["name"]
        pn = f"CMR{i:04d}"
        is_cmos = (i % 3 == 0)
        body = (_SL_CMOS_TEXT[i % len(_SL_CMOS_TEXT)] if is_cmos
                else _SL_NEG_TEXT[i % len(_SL_NEG_TEXT)])
        d = ds_root / VENDORS[vkey]["slug"]
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{pn}_datasheet.html").write_text(_sl_page(vname, pn, body, rnd),
                                                encoding="utf-8")
        neg_pns.append((pn, vname))
        if is_cmos:
            cmos_pns.append(pn)

    # ---- label sources: ERF pages for most, the DB for a Qorvo slice --------
    db_space = [r for r in listed if r[1] == "Qorvo"][: max(1, len(listed) // 6)]
    erf_rows = [r for r in listed if r not in db_space]
    (erf_dir / "space_amplifiers_1.html").write_text(_sl_erf_page(erf_rows),
                                                     encoding="utf-8")
    _sl_make_db(db_path, db_space, neg_pns[:10])

    print(f"synthetic local library: {ds_root}")
    print(f"  {n_pos} space-like part(s): {len(erf_rows)} listed on everythingRF, "
          f"{len(db_space)} labelled only in the DB, {hidden} HIDDEN")
    print(f"  {n_neg} commercial part(s), {len(cmos_pns)} of them CMOS "
          f"driver/logic")
    print(f"  everythingRF pages -> {erf_dir}")
    print(f"  synthetic DB       -> {db_path}\n")

    corpus = build_local_corpus(
        root=ds_root, db_path=db_path, erf_html=[erf_dir],
        absent_as="negative", text_mode="full", drop_boilerplate=True,
        use_db=True, min_chars=LOCAL_MIN_CHARS, verbose=True)
    _save(LOCAL_JSON, corpus)

    # Boilerplate must actually have been removed, or the model can separate
    # vendors instead of parts.
    leaked = [c for c in _SL_CHROME
              if any(c in Path(r["text_file"]).read_text(encoding="utf-8")
                     for r in corpus["records"][:20])]
    print(f"\n  boilerplate check: "
          + ("all template lines removed" if not leaked
             else f"! {len(leaked)} chrome line(s) survived: {leaked[:2]}"))

    rc = cmd_train(argparse.Namespace(
        backend=getattr(args, "backend", "tfidf"),
        hf_model=getattr(args, "hf_model", DEFAULT_HF_MODEL), mask="none",
        bags=getattr(args, "bags", 10), folds=getattr(args, "folds", 5),
        seed=getattr(args, "seed", 0),
        target_recall=getattr(args, "target_recall", 0.90), threshold=None,
        show_features=12, no_features=False, prior_weight=1.0, no_prior=False,
        local=True, local_only=True,
        neg_weight_cmos=getattr(args, "neg_weight_cmos", 3.0)))

    preds = {p["pn"]: p for p in _load(PRED_JSON, [])}
    hid = [pn for pn, _v in hidden_pns]
    got_hidden = sum(1 for pn in hid if preds.get(pn, {}).get("qualifiable"))
    cmos_flagged = sum(1 for pn in cmos_pns if preds.get(pn, {}).get("qualifiable"))
    print("\n  SELFTEST ground truth (known only because this data is synthetic)")
    print(f"    hidden positives recovered   {got_hidden}/{len(hid)}"
          f"   <- these were labelled NEGATIVE, so each one counted against")
    print(f"                                     the measured precision while "
          f"actually being a win")
    print(f"    CMOS negatives flagged       {cmos_flagged}/{len(cmos_pns)}"
          f"   <- want 0; they are weighted "
          f"x{getattr(args, 'neg_weight_cmos', 3.0):g}")
    print(f"\n  Synthetic tree left at {base} (delete it whenever you like).")
    return rc


# ------------------------------------------------------------- cache control
def _dir_stats(path):
    """(file count, bytes) for a cache directory."""
    if not path.exists():
        return 0, 0
    files = [f for f in path.glob("*") if f.is_file()]
    return len(files), sum(f.stat().st_size for f in files)


def _human(n):
    for unit in ("B", "kB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0


CACHE_PARTS = {
    "pdfs": (CACHE_PDF, "downloaded datasheet PDFs"),
    "text": (CACHE_TXT, "extracted overview text"),
}
CACHE_FILES = {
    "model": (MODEL_FILE, "trained model"),
    "predictions": (PRED_JSON, "per-part predictions"),
    "match": (MATCH_JSON, "catalog<->everythingRF match"),
    "missing": (WORKDIR / "missing_datasheets.json", "missing-datasheet report"),
}


def cache_report():
    lines, total = [], 0
    for key, (path, desc) in CACHE_PARTS.items():
        n, size = _dir_stats(path)
        total += size
        lines.append(f"    {key:<12} {n:>6} file(s)  {_human(size):>10}   {desc}")
    for key, (path, desc) in CACHE_FILES.items():
        exists = path.exists()
        size = path.stat().st_size if exists else 0
        total += size
        lines.append(f"    {key:<12} {'yes' if exists else '--':>6}          "
                     f"{_human(size) if exists else '-':>10}   {desc}")
    return lines, total


def _delete_dir_contents(path):
    n = 0
    if not path.exists():
        return 0
    # Safety: only ever remove files inside the tool's own work directory.
    if WORKDIR.resolve() not in path.resolve().parents and path.resolve() != WORKDIR.resolve():
        raise SystemExit(f"refusing to delete outside {WORKDIR}: {path}")
    for f in path.glob("*"):
        if f.is_file():
            f.unlink()
            n += 1
    return n


def cmd_clear(args):
    """Delete cached downloads and/or derived artefacts."""
    targets = set()
    if args.all:
        targets = set(CACHE_PARTS) | set(CACHE_FILES)
    else:
        for key in ("pdfs", "text", "model", "predictions", "match", "missing"):
            if getattr(args, key, False):
                targets.add(key)
    lines, total = cache_report()
    print("Cache contents:")
    for ln in lines:
        print(ln)
    print(f"    {'total':<12} {'':>6}          {_human(total):>10}")
    if not targets:
        print("\nNothing selected. Use --pdfs --text --model --predictions "
              "--match --missing, or --all.")
        return 0
    print(f"\nAbout to delete: {', '.join(sorted(targets))}")
    if not args.yes:
        try:
            if input("Type 'yes' to confirm: ").strip().lower() not in ("y", "yes"):
                print("cancelled.")
                return 0
        except EOFError:
            print("cancelled (no input).")
            return 0
    removed = 0
    for key in sorted(targets):
        if key in CACHE_PARTS:
            n = _delete_dir_contents(CACHE_PARTS[key][0])
            removed += n
            print(f"  {key}: removed {n} file(s)")
        else:
            path = CACHE_FILES[key][0]
            if path.exists():
                path.unlink()
                removed += 1
                print(f"  {key}: removed")
            else:
                print(f"  {key}: nothing to remove")
    print(f"\ndeleted {removed} file(s) from {WORKDIR}")
    return 0


def clear_menu():
    """Interactive cache clearing."""
    while True:
        lines, total = cache_report()
        print("\n  Cache / reset")
        print("  -------------")
        for ln in lines:
            print(ln)
        print(f"    {'total':<12} {'':>6}          {_human(total):>10}")
        print("""
  1) Clear extracted TEXT only        (keeps PDFs; re-parse without re-downloading)
  2) Clear PDFs only                  (frees the most space)
  3) Clear PDFs + text                (full re-download next fetch)
  4) Clear model + predictions        (keeps downloads; retrain from scratch)
  5) Clear match file
  6) Clear EVERYTHING
  0) back""")
        try:
            c = input("  choice> ").strip()
        except EOFError:
            return
        sets = {"1": ["text"], "2": ["pdfs"], "3": ["pdfs", "text"],
                "4": ["model", "predictions"], "5": ["match"],
                "6": list(CACHE_PARTS) + list(CACHE_FILES)}
        if c in ("0", "", "q"):
            return
        if c not in sets:
            print("  ? unknown choice")
            continue
        chosen = sets[c]
        print(f"  will delete: {', '.join(chosen)}")
        try:
            if input("  type 'yes' to confirm: ").strip().lower() not in ("y", "yes"):
                print("  cancelled.")
                continue
        except EOFError:
            return
        ns = _ns(all=False, yes=True,
                 **{k: (k in chosen) for k in
                    ("pdfs", "text", "model", "predictions", "match", "missing")})
        cmd_clear(ns)


# =========================================================================
# MULTI-VENDOR DATASHEET LIBRARY
# =========================================================================
# Datasheets are the raw material for the classifier, and one vendor's catalog
# is not enough to learn what "space-qualifiable construction" reads like. This
# section builds a library across vendors.
#
# HOW URLS ARE FOUND. Researched per vendor, because they differ:
#   Mini-Circuits  minicircuits.com/pdfs/<PN>.pdf                  direct
#   ADI            analog.com/media/.../data-sheets/<pn>.pdf       direct
#   MACOM          cdn.macom.com/datasheets/<PN>.pdf               direct
#   Skyworks (Si*) skyworksinc.com/-/media/.../<pn>-datasheet.pdf  direct
#   Skyworks (SKY*) .../Data-Sheet/SKY66318-21_205594E.pdf         doc-number: harvest
#   Qorvo          qorvo.com/products/d/da009703                   opaque id: harvest
#   Marki (legacy) markimicrowave.com/Assets/datasheets/<PN>.pdf   direct
#   Marki (new)    markimicrowave.com/assets/<uuid>/<PN>-....pdf   uuid: harvest
#
# Where the URL cannot be derived from the part number, there is no polite way
# to guess it, so those vendors are HARVEST-ONLY: you save their catalog or
# product pages from your own browser (exactly as erf_save_pages.py does -- your
# session, your clearance, no stealth and no challenge-solving) and point
# --harvest at the folder. This tool then reads the PDF links out of that saved
# HTML. It never tries to defeat a bot check.
#
# POLITENESS. Downloads round-robin across vendors, so consecutive requests go
# to different hosts and no single vendor sees a burst. Each host also keeps its
# own rate limit, robots.txt is honoured per host, every file is cached, and the
# run resumes.
#
# LAYOUT (built so the main rfparts pipeline can ingest it later):
#   <SPACEQUAL_HOME>/datasheets/
#       space/<Vendor>/<PN>.pdf     part is space-qualified/grade per everythingRF
#       general/<Vendor>/<PN>.pdf   everything else
#       manifest.jsonl              one JSON record per datasheet

DS_ROOT = WORKDIR / "datasheets"
DS_SPACE = DS_ROOT / "space"
DS_GENERAL = DS_ROOT / "general"
DS_MANIFEST = DS_ROOT / "manifest.jsonl"
HARVEST_JSON = WORKDIR / "harvested_urls.json"


def _pn_forms(pn):
    n = norm_pn(pn)
    return {"pn": n, "pn_lower": n.lower(), "pn_upper": n.upper(),
            "pn_nospace": re.sub(r"\s+", "", n),
            "pn_noplus": n.replace("+", ""),
            "pn_urlsafe": urllib.parse.quote(n, safe="+-_.")}


VENDORS = {
    "minicircuits": dict(
        name="Mini-Circuits", slug="Mini-Circuits",
        host="https://www.minicircuits.com",
        aliases=[r"mini\s*-?\s*circuits"],
        patterns=["https://www.minicircuits.com/pdfs/{pn_urlsafe}.pdf"],
        note="direct PN path; trailing-X variants share one datasheet"),
    "adi": dict(
        name="Analog Devices", slug="Analog-Devices",
        host="https://www.analog.com",
        aliases=[r"analog\s*devices", r"^adi$", r"\bhittite\b", r"\blinear\s*tech"],
        patterns=[
            "https://www.analog.com/media/en/technical-documentation/"
            "data-sheets/{pn_lower}.pdf",
            "https://www.analog.com/media/en/technical-documentation/"
            "data-sheets/{pn_upper}.pdf"],
        note="direct PN path; case varies, so both are tried"),
    "macom": dict(
        name="MACOM", slug="MACOM", host="https://cdn.macom.com",
        aliases=[r"\bmacom\b", r"m/?a-?com"],
        patterns=["https://cdn.macom.com/datasheets/{pn_urlsafe}.pdf"],
        note="direct PN path on the CDN"),
    "skyworks": dict(
        name="Skyworks", slug="Skyworks",
        host="https://www.skyworksinc.com",
        aliases=[r"skyworks"],
        patterns=[
            "https://www.skyworksinc.com/-/media/Skyworks/SL/documents/public/"
            "data-sheets/{pn_lower}-datasheet.pdf",
            "https://www.skyworksinc.com/-/media/Skyworks/SL/documents/public/"
            "data-sheets/{pn_lower}.pdf"],
        note="Si* parts follow a pattern; SKY* carry an opaque doc number "
             "(harvest those)"),
    "marki": dict(
        name="Marki Microwave", slug="Marki-Microwave",
        host="https://markimicrowave.com",
        aliases=[r"marki"],
        patterns=["https://markimicrowave.com/Assets/datasheets/{pn_urlsafe}.pdf"],
        note="legacy /Assets/datasheets path; newer sheets sit behind a UUID "
             "(harvest those)"),
    "qorvo": dict(
        name="Qorvo", slug="Qorvo", host="https://www.qorvo.com",
        aliases=[r"\bqorvo\b", r"triquint", r"\brfmd\b"],
        patterns=[],                      # /products/d/<opaque id> -- not derivable
        note="datasheet ids are opaque; harvest from saved pages"),
}


def vendor_key_for(vendor_name):
    """Map a free-text manufacturer name onto a vendor key, or None."""
    v = str(vendor_name or "")
    for key, cfg in VENDORS.items():
        for pat in cfg["aliases"]:
            if re.search(pat, v, re.I):
                return key
    return None


def vendor_url_candidates(vendor_key, pn):
    """Templated datasheet URLs to try for a part, in order."""
    cfg = VENDORS.get(vendor_key)
    if not cfg:
        return []
    forms = _pn_forms(pn)
    out = []
    for tpl in cfg["patterns"]:
        try:
            url = tpl.format(**forms)
        except KeyError:
            continue
        if url not in out:
            out.append(url)
    if vendor_key == "minicircuits":
        # trailing X+ variants are documented together (see pdf_url_candidates)
        m = re.match(r"^(.*[^-\s])X(\+?)$", forms["pn"], re.I)
        if m:
            alt = urllib.parse.quote(m.group(1) + m.group(2), safe="+-_.")
            out.append(f"https://www.minicircuits.com/pdfs/{alt}.pdf")
    return out


# ------------------------------------------------- everythingRF space index
def load_erf_space_index(db_path):
    """{loose PN -> (vendor_name, 'qualified'|'grade')} for EVERY vendor that
    everythingRF lists as space qualified or space grade.

    Read from the rfparts DB, which is the product of your own everythingRF HTML
    ingest -- so the space classification comes from those saved pages without
    this tool having to re-parse them."""
    db_path = Path(db_path)
    if not db_path.is_file():
        return {}
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT p.mpn, p.vendor,
               (SELECT value_text FROM specs s WHERE s.part_id = p.id
                  AND s.key = 'space_variant' LIMIT 1) AS variant
        FROM parts p
        WHERE EXISTS (SELECT 1 FROM qual_evidence q
                       WHERE q.part_id = p.id AND q.signal LIKE 'erf-space-%')
    """).fetchall()
    conn.close()
    idx = {}
    for r in rows:
        key = loose_pn(r["mpn"])
        if key:
            variant = ("grade" if (r["variant"] or "").endswith("grade")
                       else "qualified")
            # keep the original MPN: it is what filenames and the manifest use
            idx[key] = (r["vendor"] or "", variant, r["mpn"])
    return idx


# ---------------------------------------------------- harvesting saved HTML
_DOC_SUFFIX = re.compile(
    r"(?:[-_\s]+(?:data-?sheets?|ds|spec(?:ification)?s?|rev[a-z0-9.]*|"
    r"final|public|en|pdf)|_\d{6}[A-Z]?)$", re.I)


def _pn_from_filename(stem):
    """Best-effort part number from a datasheet filename.

    Vendor filenames append descriptions and document numbers
    ('QPA2263A_datasheet', 'MM1-1140HS-GaAs Double-Balanced Mixer',
    'SKY66318-21_205594E'), so trim anything that clearly isn't part of the PN."""
    s = urllib.parse.unquote(stem).strip()
    s = re.sub(r"\.pdf.*$", "", s, flags=re.I)
    s = s.split()[0] if s.split() else s          # description follows a space
    for _ in range(3):
        s2 = _DOC_SUFFIX.sub("", s)
        if s2 == s:
            break
        s = s2
    # On a mixed-case name, a segment containing lowercase is descriptive
    # ('-GaAs'), not part number. All-lowercase names (ADI, Skyworks) are left be.
    if any(c.isupper() for c in s):
        parts = re.split(r"([-_])", s)
        kept = []
        for seg in parts:
            if seg in ("-", "_"):
                kept.append(seg)
                continue
            descriptive = (re.search(r"[a-z]{2}", seg)
                           or (any(c.islower() for c in seg)
                               and any(c.isupper() for c in seg)))
            if descriptive and kept:
                break
            kept.append(seg)
        s = "".join(kept).rstrip("-_")
    return s[:60]


# ============================================================================
#  LOCAL DATASHEET CORPUS  --  train on the datasheets you already have
# ============================================================================
#  Why this exists
#  ---------------
#  The original pipeline could only learn from Mini-Circuits PDFs it downloaded
#  itself. Everything else you hold locally -- Qorvo HTML product pages, Marki /
#  Skyworks / MACOM PDFs -- was invisible to training even though it is better
#  data: real datasheet prose, already on disk, no network needed.
#
#  Labels come from sources that KNOW, never from the datasheet text:
#
#    positive  the part appears in your saved everythingRF space listings,
#              and/or the rfparts DB already marks it space qualified / hi-rel
#              (that covers the Qorvo aerospace catalog and the ADI space
#              portfolio, since your ingests write a space signal for those).
#    negative  a datasheet you hold whose part appears in NONE of those.
#
#  Read that second rule carefully: absence is not proof. A part can be
#  qualifiable and simply unlisted -- that is the entire premise of the PU
#  machinery in this file. Treating absence as a hard negative is a deliberate
#  trade (--absent-as negative, the default because it is what makes precision
#  measurable at all); --absent-as unlabeled puts them back in the U pool and
#  keeps the original, more conservative statistics.
_LOCAL_VENDOR_HINTS = {
    "marki-microwave": "marki", "marki_microwave": "marki",
    "markimicrowave": "marki", "marki": "marki",
    "analog-devices": "adi", "analog_devices": "adi", "analogdevices": "adi",
    "mini-circuits": "minicircuits", "minicircuits": "minicircuits",
    "mini_circuits": "minicircuits",
    "ti": "ti", "texas-instruments": "ti", "texasinstruments": "ti",
}

# Folders that group the library by qualification rather than by vendor; their
# CHILDREN are the vendor folders. The tool's own download library uses these.
_LOCAL_GROUP_DIRS = {"space", "general", "qualified", "grade", "unknown"}

_DS_EXT = {".html", ".htm", ".pdf", ".txt"}


def vendor_key_for_folder(name):
    """Map a datasheet folder name ('Marki-Microwave', 'MACOM') to a vendor key."""
    raw = str(name or "").strip()
    if not raw:
        return None
    flat = re.sub(r"[^a-z0-9]", "", raw.lower())
    hit = _LOCAL_VENDOR_HINTS.get(raw.lower().replace(" ", "-")) \
        or _LOCAL_VENDOR_HINTS.get(flat)
    if hit and hit in VENDORS:
        return hit
    key = vendor_key_for(raw)
    if key:
        return key
    for vk, cfg in VENDORS.items():
        if flat in (vk, re.sub(r"[^a-z0-9]", "", cfg["slug"].lower()),
                    re.sub(r"[^a-z0-9]", "", cfg["name"].lower())):
            return vk
    return None


def discover_local_library(root=None):
    """Locate the folder holding your saved datasheets.

    Tolerant of how the path actually appears on disk: 'data' vs 'Data',
    'datasheets' vs the mis-spelling 'datasheeets' -- a wrong guess here reads
    as 'you have no datasheets', which is a maddening thing to debug."""
    probes = []
    if root:
        probes.append(Path(root).expanduser())
    probes += [
        Path(DEFAULT_LOCAL_DS).expanduser(),
        Path.home() / "Downloads" / "rfparts" / "data" / "datasheets",
        Path.home() / "Downloads" / "rfparts" / "Data" / "datasheets",
        Path.home() / "Downloads" / "rfparts" / "rfparts" / "data" / "datasheets",
        DS_ROOT,
    ]
    for p in probes:
        if p.is_dir():
            return p
    # Nothing matched exactly: glob for any 'datashe...' folder next to the
    # places we looked (this is what catches 'datasheeets').
    bases = []
    for p in probes:
        for cand in (p.parent, p.parent.parent):
            if cand.is_dir() and cand not in bases:
                bases.append(cand)
    for base in bases:
        for cand in sorted(base.glob("datashe*")):
            if cand.is_dir():
                return cand
        for mid in sorted(base.glob("[Dd]ata")):
            for cand in sorted(mid.glob("datashe*")):
                if cand.is_dir():
                    return cand
    return None


def scan_local_library(root, vendors=None):
    """Every datasheet file on disk as (vendor_key, folder_label, path).

    Handles both layouts: <root>/<Vendor>/... and <root>/{space,general}/<Vendor>/...
    """
    root = Path(root)
    found, unknown = [], Counter()

    def walk_vendor_dir(d, label):
        vkey = vendor_key_for_folder(d.name)
        if not vkey:
            unknown[d.name] += 1
            return
        if vendors and vkey not in vendors:
            return
        for f in sorted(d.rglob("*")):
            if f.is_file() and f.suffix.lower() in _DS_EXT:
                found.append((vkey, label or d.name, f))

    for sub in sorted(p for p in root.iterdir() if p.is_dir()):
        if sub.name.lower() in _LOCAL_GROUP_DIRS:
            for inner in sorted(p for p in sub.iterdir() if p.is_dir()):
                walk_vendor_dir(inner, f"{sub.name}/{inner.name}")
        else:
            walk_vendor_dir(sub, sub.name)
    return found, unknown


def redact_pn(text, pn) -> str:
    """Replace the part number itself with a placeholder.

    The selftest caught this: with positives named SPQ#### and negatives CMR####,
    the top char n-grams were 'pq00', 'q004', ' spq0' -- the model had learned the
    part-number prefix, not the engineering. Real catalogues have exactly this
    structure (a vendor's space line often shares a prefix), so a model that
    memorises prefixes scores beautifully in cross-validation and then fails on
    the unlisted parts you actually want to find.

    Note the trade-off: grade information encoded in a suffix ('-QV', 'X+', '-EP')
    is redacted along with the rest. Use --keep-pn if you would rather keep it."""
    if not pn:
        return text
    out = text
    seen = set()
    for v in sorted({str(pn), norm_pn(pn), norm_pn(pn).replace("+", "")},
                    key=len, reverse=True):
        if len(v) >= 4 and v.lower() not in seen:
            seen.add(v.lower())
            out = re.sub(re.escape(v), " PARTNO ", out, flags=re.I)
    # Also catch the PN written with different separators ('QPA-2263' vs 'QPA2263').
    core = re.sub(r"[^A-Za-z0-9]", "", str(pn))
    if 5 <= len(core) <= 20:
        pat = r"[\W_]{0,2}".join(re.escape(c) for c in core)
        out = re.sub(pat, " PARTNO ", out, flags=re.I)
    return out


def _local_text_path(vendor_key, pn):
    slug = VENDORS[vendor_key]["slug"] if vendor_key in VENDORS else "Other"
    safe = re.sub(r"[^A-Za-z0-9._+-]", "_", norm_pn(pn)) or "part"
    return LOCAL_TXT / slug / f"{safe}.txt"


def extract_local_text(path, max_chars=FULL_TEXT_CHARS, text_mode="full"):
    """Classifier text from a local file, PDF or HTML."""
    path = Path(path)
    suffix = path.suffix.lower()
    try:
        raw = path.read_bytes()
    except OSError:
        return ""
    if suffix == ".pdf":
        return (extract_full_text(raw, max_chars) if text_mode == "full"
                else extract_overview(raw))
    if suffix in (".html", ".htm"):
        return html_to_text(raw, max_chars)
    return _strip_tables(raw.decode("utf-8", "replace"), max_chars)


# --------------------------------------------- label oracles (never text!)
_ERF_DNAME = re.compile(r"""(?is)\bdname\s*=\s*["']([^"']+)["']""")
_ERF_MANU = re.compile(r"""(?is)\bmanu-name\s*=\s*["']([^"']+)["']""")
_ERF_TITLE_A = re.compile(
    r"""(?is)<h3[^>]*prod-title[^>]*>\s*<a[^>]*>(.*?)</a>""")


def scan_erf_html_pns(folders, verbose=True):
    """{loose PN -> (mpn, vendor)} for every part in your saved everythingRF
    pages.

    Those saved pages ARE the space-qualified listings, so appearing in them is
    the positive label. Parsed straight from the HTML so this works whether or
    not the rfparts DB has been rebuilt lately."""
    if isinstance(folders, (str, Path)):
        folders = [folders]
    idx, n_files, n_boxes = {}, 0, 0
    for folder in folders or []:
        folder = Path(folder)
        if not folder.is_dir():
            continue
        pages = sorted(list(folder.rglob("*.html")) + list(folder.rglob("*.htm")))
        for f in pages:
            n_files += 1
            try:
                txt = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            chunks = re.split(r"(?i)product-box", txt)
            for ch in (chunks[1:] if len(chunks) > 1 else chunks):
                pn = ""
                m = _ERF_DNAME.search(ch)
                if m:
                    pn = _html_unescape(m.group(1)).strip()
                if not pn:
                    mt = _ERF_TITLE_A.search(ch)
                    if mt:
                        label = _html_unescape(
                            re.sub(r"(?s)<[^>]+>", " ", mt.group(1))).strip()
                        # listings render as 'Vendor - MPN'
                        pn = label.split(" - ")[-1].strip() if " - " in label else ""
                if not pn:
                    continue
                n_boxes += 1
                mv = _ERF_MANU.search(ch)
                vendor = _html_unescape(mv.group(1)).strip() if mv else ""
                key = loose_pn(pn)
                if key and key not in idx:
                    idx[key] = (pn, vendor)
    if verbose:
        print(f"  everythingRF pages scanned {n_files}; "
              f"{len(idx)} distinct space part number(s) found")
    return idx, n_files


_DB_SPACE_SIGNAL_RE = re.compile(
    r"erf-space|qorvo|aerospace|ti-space|space|qml|class[-_ ]?[ksv]\b|"
    r"38534|38535|escc|nasa|jans", re.I)


def load_db_space_index(db_path):
    """{loose PN -> (mpn, vendor, why)} for every part the rfparts pipeline has
    already labelled space-qualified or hi-rel.

    Reads BOTH the specs table ('space', 'space_variant') and qual_evidence, so
    it picks up the Qorvo aerospace catalog (qorvo_ingest writes
    space=qualified) and the ADI space portfolio (adi_space_ingest writes QML /
    Class S / NASA evidence) without either of them needing to be re-run."""
    db_path = Path(db_path)
    if not db_path.is_file():
        return {}
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("""
            SELECT p.mpn, p.vendor,
                   (SELECT value_text FROM specs s WHERE s.part_id = p.id
                      AND s.key = 'space' LIMIT 1)          AS sp,
                   (SELECT value_text FROM specs s WHERE s.part_id = p.id
                      AND s.key = 'space_variant' LIMIT 1)  AS variant,
                   (SELECT group_concat(q.signal, '|') FROM qual_evidence q
                      WHERE q.part_id = p.id)               AS signals
            FROM parts p
        """).fetchall()
    except sqlite3.Error as e:
        print(f"  ! could not read {db_path}: {e}")
        conn.close()
        return {}
    conn.close()
    out = {}
    for r in rows:
        sp = (r["sp"] or "").strip().lower()
        variant = (r["variant"] or "").strip().lower()
        signals = r["signals"] or ""
        why = ""
        if sp in ("qualified", "hi_rel", "hirel", "space_qualified"):
            why = f"DB space={sp}"
        elif variant in ("space_qualified", "space_grade"):
            why = f"DB space_variant={variant}"
        elif signals and _DB_SPACE_SIGNAL_RE.search(signals):
            why = "DB evidence: " + signals.split("|")[0][:40]
        if not why:
            continue
        key = loose_pn(r["mpn"])
        if key and key not in out:
            out[key] = (r["mpn"], r["vendor"] or "", why)
    return out


def load_db_part_index(db_path):
    """{loose PN -> (mpn, vendor)} for EVERY part in the DB, space or not. Used
    only to report how much of the local library the DB actually knows about."""
    db_path = Path(db_path)
    if not db_path.is_file():
        return {}
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT mpn, vendor FROM parts").fetchall()
    except sqlite3.Error:
        conn.close()
        return {}
    conn.close()
    idx = {}
    for r in rows:
        key = loose_pn(r["mpn"])
        if key and key not in idx:
            idx[key] = (r["mpn"], r["vendor"] or "")
    return idx


def build_label_oracle(db_path=None, erf_html=None, use_db=True, verbose=True):
    """{loose PN -> (mpn, vendor, why)} of everything known to be space.

    Union of the saved everythingRF listings and the rfparts DB. Deliberately a
    union: the ERF pages cover the vendors you saved, the DB covers Qorvo and
    ADI, and neither alone labels the whole local library."""
    pos = {}
    erf_files = 0
    if erf_html:
        idx, erf_files = scan_erf_html_pns(erf_html, verbose=verbose)
        for key, (mpn, vendor) in idx.items():
            pos[key] = (mpn, vendor, "everythingRF saved listing")
    if use_db and db_path:
        db_idx = load_db_space_index(db_path)
        added = 0
        for key, val in db_idx.items():
            if key not in pos:
                pos[key] = val
                added += 1
        if verbose and db_idx:
            print(f"  rfparts DB space-labelled parts {len(db_idx)} "
                  f"({added} not already in the everythingRF set)")
    return pos, erf_files


# ------------------------------------------------------- corpus assembly
def build_local_corpus(root=None, db_path=None, erf_html=None, vendors=None,
                       absent_as="negative", text_mode="full",
                       drop_boilerplate=True, use_db=True, keep_pn=False,
                       min_chars=LOCAL_MIN_CHARS, verbose=True):
    """Turn the local datasheet folders into a labelled training corpus."""
    root = discover_local_library(root)
    if not root:
        raise SystemExit(
            "could not find your datasheet folder.\n"
            f"Looked for {DEFAULT_LOCAL_DS} and the usual variants.\n"
            "Pass --local-dir <folder> (or set it in Settings).")
    if verbose:
        print(f"local datasheet library: {root}")
    files, unknown = scan_local_library(root, vendors=vendors)
    if not files:
        raise SystemExit(
            f"no .pdf/.htm/.html files under {root}\n"
            "Expected per-vendor subfolders, e.g. Qorvo/, Skyworks/, MACOM/.")
    if unknown and verbose:
        print("  ! folders skipped (vendor not recognised): "
              + ", ".join(sorted(unknown)[:8]))

    oracle, erf_files = build_label_oracle(db_path=db_path, erf_html=erf_html,
                                           use_db=use_db, verbose=verbose)
    if not oracle:
        raise SystemExit(
            "no space labels available, so every datasheet would be a negative.\n"
            "Give --erf-html <folder of saved everythingRF pages> and/or --db "
            "<parts.db>.")
    db_all = load_db_part_index(db_path) if (use_db and db_path) else {}

    # ---- extract text once per (vendor, part); keep the richest file ----------
    LOCAL_TXT.mkdir(parents=True, exist_ok=True)
    best = {}
    empty = Counter()
    for vkey, folder, path in files:
        pn = _pn_from_filename(path.stem)
        if not pn:
            continue
        text = extract_local_text(path, text_mode=text_mode)
        if len(text.strip()) < min_chars:
            empty[vkey] += 1
            continue
        if not keep_pn:
            text = redact_pn(text, pn)
        key = (vkey, loose_pn(pn))
        prev = best.get(key)
        if prev is None or len(text) > len(prev["text"]):
            best[key] = {"vendor": vkey, "pn": pn, "file": str(path),
                         "kind": path.suffix.lower().lstrip("."),
                         "folder": folder, "text": text}

    if not best:
        raise SystemExit(
            f"found {len(files)} file(s) but none yielded at least {min_chars} "
            f"characters of text.\nScanned-image PDFs need OCR; try "
            f"--min-chars 40 to see what is there.")

    # ---- strip per-vendor template chrome ------------------------------------
    boiler = {}
    if drop_boilerplate:
        by_vendor = {}
        for rec in best.values():
            by_vendor.setdefault(rec["vendor"], []).append(rec)
        for vkey, recs in by_vendor.items():
            lines = _learn_boilerplate([r["text"] for r in recs])
            if not lines:
                continue
            boiler[vkey] = sorted(lines)
            for r in recs:
                r["text"] = _drop_lines(r["text"], lines)
            if verbose:
                print(f"  {VENDORS[vkey]['name']}: dropped "
                      f"{len(lines)} boilerplate line(s) common to "
                      f"{len(recs)} page(s)")

    # ---- label, cache text, build records ------------------------------------
    records = []
    counts = Counter()
    per_vendor = {}
    thin = Counter()
    for (vkey, lkey), rec in sorted(best.items()):
        text = rec["text"]
        if len(text.strip()) < min_chars:
            thin[vkey] += 1
            continue
        hit = oracle.get(lkey)
        if hit:
            label, why = "P", hit[2]
        else:
            label = "N" if absent_as == "negative" else "U"
            why = ("absent from everythingRF space listings and DB space labels"
                   if label == "N" else "not labelled space by any source")
        tp = _local_text_path(vkey, rec["pn"])
        tp.parent.mkdir(parents=True, exist_ok=True)
        tp.write_text(text, encoding="utf-8")
        row = {"vendor": vkey, "vendor_name": VENDORS[vkey]["name"],
               "pn": rec["pn"], "label": label, "label_why": why,
               "file": rec["file"], "kind": rec["kind"],
               "folder": rec["folder"], "chars": len(text),
               "text_file": str(tp), "source": "local",
               "in_db": bool(db_all.get(lkey)),
               "cmos_negative": bool(label == "N"
                                     and _CMOS_NEG_RE.search(text))}
        records.append(row)
        counts[label] += 1
        st = per_vendor.setdefault(vkey, Counter())
        st[label] += 1
        st["files"] += 1
        st[rec["kind"]] += 1
        st["chars"] += len(text)
        if row["cmos_negative"]:
            st["cmos"] += 1

    corpus = {
        "root": str(root),
        "built": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "absent_as": absent_as,
        "text_mode": text_mode,
        "erf_pages": erf_files,
        "oracle_size": len(oracle),
        "boilerplate": boiler,
        "records": records,
        "counts": {"P": counts["P"], "N": counts["N"], "U": counts["U"],
                   "files_seen": len(files), "parts": len(records),
                   "no_text": sum(empty.values()) + sum(thin.values())},
        "per_vendor": {k: dict(v) for k, v in per_vendor.items()},
    }
    if verbose:
        _report_local_corpus(corpus, empty)
    return corpus


def _report_local_corpus(corpus, empty=None):
    c = corpus["counts"]
    print(f"\n  {c['files_seen']} file(s) on disk -> {c['parts']} part(s) with "
          f"usable text"
          + (f"  ({c['no_text']} skipped: too little text)"
             if c["no_text"] else ""))
    print(f"\n  {'vendor':<18} {'parts':>6} {'pos':>5} {'neg':>5} {'unlab':>6} "
          f"{'cmos':>5} {'avg chars':>10}  kinds")
    for vkey, st in sorted(corpus["per_vendor"].items(),
                           key=lambda kv: -kv[1].get("files", 0)):
        n = st.get("files", 0) or 1
        kinds = ", ".join(f"{k}:{st[k]}" for k in ("pdf", "html", "htm", "txt")
                          if st.get(k))
        print(f"  {VENDORS[vkey]['name']:<18} {st.get('files', 0):>6} "
              f"{st.get('P', 0):>5} {st.get('N', 0):>5} {st.get('U', 0):>6} "
              f"{st.get('cmos', 0):>5} {st.get('chars', 0) // n:>10}  {kinds}")
    print(f"  {'TOTAL':<18} {c['parts']:>6} {c['P']:>5} {c['N']:>5} "
          f"{c['U']:>6}")
    # A vendor with no positives is usually missing label coverage, not a vendor
    # with no space parts. Say so loudly: it silently poisons the negative set.
    blind = [VENDORS[v]["name"] for v, st in corpus["per_vendor"].items()
             if not st.get("P")]
    if blind:
        print(f"\n  ! no positives at all for: {', '.join(sorted(blind))}")
        print("    Every datasheet from those vendors became a NEGATIVE. If you "
              "expect\n    some to be space parts, your label sources do not "
              "cover that vendor --\n    add its everythingRF pages, or re-run "
              "the vendor's ingest so the DB\n    carries a space signal.")
    if corpus["counts"]["P"] < 10:
        print(f"\n  ! only {corpus['counts']['P']} positive(s). Expect very "
              f"noisy estimates.")


def load_local_texts(corpus, mask="none", min_chars=LOCAL_MIN_CHARS):
    """Attach the cached text to each corpus record."""
    kept, skipped = [], 0
    for r in corpus.get("records", []):
        p = Path(r.get("text_file", ""))
        txt = p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""
        if len(txt.strip()) < min_chars:
            skipped += 1
            continue
        kept.append({**r, "text": mask_text(txt, mask)})
    return kept, skipped


def cmd_localscan(args):
    """Build the labelled training corpus from datasheets already on disk."""
    s = load_settings()
    erf = getattr(args, "erf_html", None) or s.get("erf_html_dir") or None
    corpus = build_local_corpus(
        root=getattr(args, "local_dir", None) or s.get("local_ds_dir") or None,
        db_path=getattr(args, "db", None) or s.get("db"),
        erf_html=[erf] if erf else None,
        vendors=(set(args.vendors) if getattr(args, "vendors", None) else None),
        absent_as=getattr(args, "absent_as", "negative"),
        text_mode=getattr(args, "text_mode", "full"),
        drop_boilerplate=not getattr(args, "keep_boilerplate", False),
        use_db=not getattr(args, "no_db", False),
        keep_pn=bool(getattr(args, "keep_pn", False)),
        min_chars=getattr(args, "min_chars", LOCAL_MIN_CHARS))
    _save(LOCAL_JSON, corpus)
    print(f"\n  saved corpus -> {LOCAL_JSON}")
    print(f"  cached text  -> {LOCAL_TXT}")
    print("\n  Next: `train` (the local corpus is picked up automatically).")
    return 0


_PDF_HREF = re.compile(r"""href\s*=\s*["']([^"']+?\.pdf[^"']*)["']""", re.I)


def harvest_html_folder(folder, default_vendor=None):
    """Pull (pn, url, vendor) triples out of catalog/product pages you saved
    from your own browser. Pure local parsing -- no requests are made."""
    folder = Path(folder)
    if not folder.is_dir():
        raise SystemExit(f"harvest folder not found: {folder}")
    found = {}
    files = [f for f in folder.rglob("*") if f.suffix.lower() in (".html", ".htm")]
    for fp in files:
        try:
            html = fp.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for href in _PDF_HREF.findall(html):
            url = href.strip()
            if url.startswith("//"):
                url = "https:" + url
            elif url.startswith("/"):
                host = None
                for cfg in VENDORS.values():
                    if cfg["host"].split("//")[-1].split("/")[0] in html:
                        host = cfg["host"]
                        break
                if not host:
                    continue
                url = host + url
            elif not url.startswith("http"):
                continue
            vkey = None
            for key, cfg in VENDORS.items():
                if cfg["host"].split("//")[-1].split("/")[0] in url:
                    vkey = key
                    break
            vkey = vkey or default_vendor
            if not vkey:
                continue
            pn = _pn_from_filename(url.rsplit("/", 1)[-1])
            if len(re.sub(r"[^A-Za-z0-9]", "", pn)) < 3:
                continue
            found[(vkey, loose_pn(pn))] = {"vendor": vkey, "pn": pn, "url": url,
                                           "source_file": fp.name}
    return list(found.values()), len(files)


# ------------------------------------------------ multi-host polite fetching
class MultiHostFetcher:
    """Rate-limits and checks robots.txt PER HOST, so rotating between vendors
    genuinely spreads the load instead of just reordering one queue."""

    def __init__(self, rate=DEFAULT_RATE, timeout=30, ignore_robots=False):
        self.rate = rate
        self.timeout = timeout
        self.ignore_robots = ignore_robots
        self._last = {}
        self._robots = {}
        contact = os.environ.get("SPACEQUAL_CONTACT", "")
        self.ua = UA if not contact else f"rfparts-spacequal/2.0 (+{contact})"

    def _host(self, url):
        pr = urllib.parse.urlparse(url)
        return f"{pr.scheme}://{pr.netloc}"

    def _rp(self, host):
        if self.ignore_robots:
            return None
        if host not in self._robots:
            rp = urllib.robotparser.RobotFileParser()
            rp.set_url(host + "/robots.txt")
            try:
                rp.read()
            except Exception:
                rp = None
            self._robots[host] = rp
        return self._robots[host]

    def allowed(self, url):
        rp = self._rp(self._host(url))
        if rp is None:
            return True
        return rp.can_fetch(self.ua, url)

    def blocked_reason(self, url):
        host = self._host(url)
        rp = self._robots.get(host)
        if rp is not None and getattr(rp, "disallow_all", False):
            return (f"{host}/robots.txt could not be read (401/403); skipping "
                    f"this host as a precaution")
        return f"robots.txt disallows {url}"

    def get(self, url):
        if not self.allowed(url):
            return None, self.blocked_reason(url)
        host = self._host(url)
        wait = self.rate - (time.time() - self._last.get(host, 0.0))
        if wait > 0:
            time.sleep(wait)
        req = urllib.request.Request(url, headers={
            "User-Agent": self.ua, "Accept": "application/pdf,*/*"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                blob = resp.read()
            ctype = ""
            return (blob, None) if blob else (None, "empty response")
        except urllib.error.HTTPError as e:
            if e.code in (429, 503):
                time.sleep(max(5.0, self.rate * 10))
                try:
                    with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                        return resp.read(), None
                except Exception:
                    return None, f"HTTP {e.code} (after backoff)"
            return None, f"HTTP {e.code}"
        except Exception as e:
            return None, type(e).__name__
        finally:
            self._last[host] = time.time()


def _looks_like_pdf(blob):
    return bool(blob) and blob[:5] == b"%PDF-"


def load_db_datasheet_urls(db_path):
    """(vendor_name, mpn, [urls]) for parts your pipeline already found a
    datasheet URL for.

    The rfparts `documents` table records the URL of every datasheet the crawler
    parsed, so these are REAL, already-verified links rather than guesses from a
    filename pattern -- much better download candidates than the templated URLs.
    parts.product_url is not used here: it points at an HTML product page, not a
    PDF, so it would just fail the PDF check."""
    db_path = Path(db_path or "")
    if not db_path.is_file():
        return []
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("""
            SELECT p.mpn AS mpn, p.vendor AS vendor, d.url AS url
            FROM documents d JOIN parts p ON p.id = d.part_id
            WHERE d.url IS NOT NULL AND d.url != ''
        """).fetchall()
    except sqlite3.Error as e:
        print(f"  ! could not read documents from {db_path}: {e}")
        conn.close()
        return []
    conn.close()
    merged = {}
    for r in rows:
        key = (r["vendor"] or "", loose_pn(r["mpn"]))
        rec = merged.setdefault(key, {"vendor": r["vendor"] or "",
                                      "pn": r["mpn"], "urls": []})
        if r["url"] not in rec["urls"]:
            rec["urls"].append(r["url"])
    return list(merged.values())


def build_datasheet_targets(catalog_path=None, db_path=None, harvest_dir=None,
                            include_general=True, from_db_docs=False):
    """Everything worth downloading: (vendor, pn, space, urls) records.

    Sources, in priority order:
      1. everythingRF space parts in the rfparts DB -- every vendor, and the
         reason for the separate space/ folder.
      2. the Mini-Circuits catalog JSON (the one vendor catalog we hold).
      3. PDF links harvested from catalog pages you saved yourself.
    """
    space_idx = load_erf_space_index(db_path) if db_path else {}
    # The everythingRF evidence signal is not the only space label in the DB:
    # qorvo_ingest and adi_space_ingest write space=qualified / QML evidence of
    # their own. Without this, a Qorvo aerospace part filed to general/ instead
    # of space/, which then misleads anything reading the library layout.
    db_space = load_db_space_index(db_path) if db_path else {}
    targets, skipped_vendors = {}, Counter()

    def space_of(loose_key):
        if loose_key in space_idx:
            return space_idx[loose_key][1]
        if loose_key in db_space:
            return "qualified"
        return "unknown"

    def add(vendor_key, pn, urls, space, source):
        key = (vendor_key, loose_pn(pn))
        if not key[1] or not urls:
            return
        prev = targets.get(key)
        if prev:
            for u in urls:
                if u not in prev["urls"]:
                    prev["urls"].append(u)
            if space != "unknown":
                prev["space"] = space
            return
        targets[key] = {"vendor": vendor_key, "pn": pn, "urls": list(urls),
                        "space": space, "source": source}

    # 1. everythingRF space parts, all vendors
    for lkey, (vendor_name, variant, mpn) in space_idx.items():
        vkey = vendor_key_for(vendor_name)
        if not vkey:
            skipped_vendors[vendor_name or "(blank)"] += 1
            continue
        add(vkey, mpn, vendor_url_candidates(vkey, mpn), variant, "erf-space")

    # 1b. use the real (un-normalised) MPNs where the catalog knows them
    if catalog_path and Path(catalog_path).is_file():
        for rec in load_catalog(catalog_path):
            lk = loose_pn(rec["pn"])
            hit = targets.get(("minicircuits", lk))
            urls = ([rec["datasheet_url"]] if rec["datasheet_url"] else []) + \
                   vendor_url_candidates("minicircuits", rec["pn"])
            if hit:
                hit["pn"] = rec["pn"]
                for u in urls:
                    if u not in hit["urls"]:
                        hit["urls"].insert(0, u)
            elif include_general:
                add("minicircuits", rec["pn"], urls,
                    space_of(lk), "mc-catalog")

    # 2. harvested links
    if harvest_dir:
        rows, n_files = harvest_html_folder(harvest_dir)
        print(f"  harvested {len(rows)} PDF link(s) from {n_files} saved page(s)")
        for r in rows:
            lk = loose_pn(r["pn"])
            space = space_of(lk)
            if space == "unknown" and not include_general:
                continue
            add(r["vendor"], r["pn"], [r["url"]], space, "harvest")

    # 3. datasheet URLs the pipeline already discovered, straight from the DB
    if from_db_docs and db_path:
        db_rows = load_db_datasheet_urls(db_path)
        n_added = 0
        for r in db_rows:
            vkey = vendor_key_for(r["vendor"])
            if not vkey:
                skipped_vendors[r["vendor"] or "(blank)"] += 1
                continue
            lk = loose_pn(r["pn"])
            space = space_of(lk)
            if space == "unknown" and not include_general:
                continue
            add(vkey, r["pn"], r["urls"], space, "db-documents")
            n_added += 1
        print(f"  {n_added} part(s) with a datasheet URL already recorded in the "
              f"rfparts DB")

    if skipped_vendors:
        top = ", ".join(f"{v} ({n})" for v, n in skipped_vendors.most_common(6))
        print(f"  note: {sum(skipped_vendors.values())} space part(s) "
              f"are from vendors this tool has no URL rule for: {top}")
    return list(targets.values())


def _rotate(targets):
    """Round-robin by vendor so consecutive requests hit different hosts."""
    buckets = {}
    for t in targets:
        buckets.setdefault(t["vendor"], []).append(t)
    order = sorted(buckets)
    out = []
    while any(buckets[k] for k in order):
        for k in order:
            if buckets[k]:
                out.append(buckets[k].pop(0))
    return out


def _ds_path(vendor_key, pn, space, ext=".pdf"):
    root = DS_SPACE if space in ("qualified", "grade") else DS_GENERAL
    slug = VENDORS[vendor_key]["slug"]
    safe = re.sub(r"[^A-Za-z0-9._+-]", "_", norm_pn(pn)) or "part"
    return root / slug / f"{safe}{ext}"


def _looks_like_html(blob):
    if not blob:
        return False
    head = blob[:2000].lstrip()[:200].lower()
    return head.startswith(b"<!doctype html") or head.startswith(b"<html") \
        or b"<html" in head


def _load_manifest():
    seen = {}
    if DS_MANIFEST.exists():
        for line in DS_MANIFEST.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except Exception:
                continue
            seen[(rec.get("vendor_key"), loose_pn(rec.get("mpn")))] = rec
    return seen


def _append_manifest(rec):
    DS_MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    with DS_MANIFEST.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")


def cmd_datasheets(args):
    """Download datasheets across vendors, rotating hosts, into the library."""
    import hashlib
    s = load_settings()
    catalog = args.catalog or s.get("catalog")
    db = args.db or s.get("db")
    print("Building the target list ...")
    targets = build_datasheet_targets(
        catalog_path=catalog if Path(str(catalog)).is_file() else None,
        db_path=db, harvest_dir=args.harvest,
        include_general=not args.space_only,
        from_db_docs=bool(getattr(args, "from_db", False)))
    if not targets:
        raise SystemExit(
            "nothing to download.\n"
            "Provide --db (your rfparts DB, for everythingRF space parts), a "
            "--catalog, and/or --harvest <folder of saved catalog pages>.")
    by_vendor = Counter(t["vendor"] for t in targets)
    by_space = Counter(t["space"] for t in targets)
    print(f"\n  {len(targets)} target part(s)")
    for v, n in by_vendor.most_common():
        pats = len(VENDORS[v]["patterns"])
        print(f"    {VENDORS[v]['name']:<18} {n:>6}   "
              f"{'pattern URLs' if pats else 'harvest-only'}")
    print(f"  space-classified: {by_space.get('qualified', 0)} qualified, "
          f"{by_space.get('grade', 0)} grade, {by_space.get('unknown', 0)} unknown")

    done = _load_manifest()
    queue = [t for t in _rotate(targets)
             if (t["vendor"], loose_pn(t["pn"])) not in done or args.refetch]
    print(f"  {len(done)} already in the library; {len(queue)} to try"
          + (f", limited to {args.limit}" if args.limit else ""))
    if not queue:
        print("\nnothing new to fetch.")
        return 0

    fetcher = MultiHostFetcher(rate=args.rate, ignore_robots=args.ignore_robots)
    got = failed = skipped_host = 0
    blocked_hosts = set()
    started = time.time()
    attempts = 0
    print(f"\n  rotating across {len(by_vendor)} vendor(s) at {args.rate}s per "
          f"host; Ctrl-C is safe (progress is written as it goes)\n")
    print(f"  {'#':>7}  {'vendor':<16} {'part':<24} {'status':<10} detail")
    for i, t in enumerate(queue, 1):
        if args.limit and attempts >= args.limit:
            print(f"\n  reached the limit of {args.limit} download attempt(s).")
            break
        vcfg = VENDORS[t["vendor"]]
        if vcfg["host"] in blocked_hosts:
            skipped_host += 1
            continue
        allow_html = bool(getattr(args, "allow_html", False))
        path = _ds_path(t["vendor"], t["pn"], t["space"])
        if (path.exists() or (allow_html
                              and path.with_suffix(".html").exists())) \
                and not args.refetch:
            continue
        attempts += 1
        blob = err = used = None
        ext = ".pdf"
        for url in t["urls"]:
            blob, err = fetcher.get(url)
            if blob and _looks_like_pdf(blob):
                used = url
                ext = ".pdf"
                break
            if blob and allow_html and _looks_like_html(blob):
                # local-scan reads HTML as happily as PDF, so for vendors whose
                # datasheet ids are opaque (Qorvo) the product page is still
                # usable training text.
                used = url
                ext = ".html"
                break
            if err and "robots" in str(err):
                blocked_hosts.add(vcfg["host"])
                print(f"  {i:>7}  {vcfg['name']:<16} {'-':<24} "
                      f"{'SKIP HOST':<10} {err}")
                blob = None
                break
            blob = None
        if not blob:
            failed += 1
            print(f"  {i:>7}  {vcfg['name']:<16} {t['pn'][:24]:<24} "
                  f"{'FAILED':<10} tried {len(t['urls'])} url(s)"
                  f"{'' if not err else ': ' + str(err)}", flush=True)
            continue
        path = _ds_path(t["vendor"], t["pn"], t["space"], ext=ext)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(blob)
        rec = {"mpn": t["pn"], "vendor_key": t["vendor"],
               "vendor": vcfg["name"], "space": t["space"],
               "datasheet_url": used, "local_path": str(path.relative_to(WORKDIR)),
               "bytes": len(blob),
               "sha256": hashlib.sha256(blob).hexdigest(),
               "discovery": t["source"],
               "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        _append_manifest(rec)
        got += 1
        tag = "SPACE" if t["space"] in ("qualified", "grade") else "general"
        eta = ""
        if attempts >= 3 and args.limit:
            per = (time.time() - started) / attempts
            left = per * (args.limit - attempts)
            eta = f"  eta {left / 60:.0f}m" if left > 90 else f"  eta {left:.0f}s"
        print(f"  {i:>7}  {vcfg['name']:<16} {t['pn'][:24]:<24} "
              f"{'ok':<10} {len(blob) // 1024} kB -> {tag}{eta}", flush=True)

    print(f"\n  downloaded {got}   failed {failed}"
          + (f"   skipped (blocked host) {skipped_host}" if skipped_host else "")
          + f"   in {(time.time() - started) / 60:.1f} min")
    n_space = sum(1 for _ in DS_SPACE.rglob("*.pdf")) if DS_SPACE.exists() else 0
    n_gen = sum(1 for _ in DS_GENERAL.rglob("*.pdf")) if DS_GENERAL.exists() else 0
    print(f"  library now: {n_space} space datasheet(s), {n_gen} general")
    print(f"    space   -> {DS_SPACE}")
    print(f"    general -> {DS_GENERAL}")
    print(f"    manifest-> {DS_MANIFEST}")
    if blocked_hosts:
        print("\n  hosts skipped on robots grounds: " + ", ".join(blocked_hosts))
        print("  For those, save their catalog pages from your own browser and "
              "re-run with --harvest <folder>.")
    return 0


# --------------------------------------------------------- settings + menu
DEFAULT_SETTINGS = {
    "catalog": DEFAULT_CATALOG,
    "db": str(Path.home() / ".rfparts" / "parts.db"),
    "backend": "tfidf",
    "hf_model": DEFAULT_HF_MODEL,
    "mask": "none",
    "fetch_limit": 800,   # new fetches per run; re-run to extend coverage
    "rate": DEFAULT_RATE,
    "bags": 15,
    "folds": 5,
    "target_recall": 0.90,
    "prior_weight": 1.0,
    "text_mode": "full",
    "harvest_dir": "",
    "ds_limit": 400,
    "review_out": "review_queue.csv",
    # local datasheet corpus
    "local_ds_dir": DEFAULT_LOCAL_DS,
    "erf_html_dir": "",
    "absent_as": "negative",
    "neg_weight_cmos": 3.0,
    "local_only": True,     # train on local datasheets alone by default
}


def load_settings():
    s = dict(DEFAULT_SETTINGS)
    s.update(_load(SETTINGS_FILE, {}) or {})
    return s


def save_settings(s):
    _save(SETTINGS_FILE, s)


def _ns(**kw):
    return argparse.Namespace(**kw)


def _fetch_ns(s, quiet=False):
    return _ns(limit=s["fetch_limit"], rate=s["rate"], positives_only=False,
               refetch=False, ignore_robots=False, show_missing=40,
               verbose=True, quiet=quiet, text_mode=s.get("text_mode", "full"))


def _train_ns(s):
    return _ns(backend=s["backend"], hf_model=s["hf_model"], mask=s["mask"],
               bags=s["bags"], folds=s["folds"], seed=0,
               target_recall=s["target_recall"], threshold=None,
               show_features=25, no_features=False,
               prior_weight=s["prior_weight"], no_prior=False,
               local=True, local_only=s.get("local_only", True),
               neg_weight_cmos=s.get("neg_weight_cmos", 3.0))


def _localscan_ns(s):
    return _ns(local_dir=s.get("local_ds_dir") or None,
               db=s.get("db"), erf_html=s.get("erf_html_dir") or None,
               vendors=None, absent_as=s.get("absent_as", "negative"),
               text_mode=s.get("text_mode", "full"), keep_boilerplate=False,
               no_db=False, keep_pn=False, min_chars=LOCAL_MIN_CHARS)


def _age(path):
    """Human age of a file, e.g. '12 min ago'."""
    try:
        secs = time.time() - Path(path).stat().st_mtime
    except Exception:
        return "unknown age"
    for div, unit in ((86400, "day"), (3600, "h"), (60, "min")):
        if secs >= div:
            n = secs / div
            return f"{n:.0f} {unit}{'s' if unit == 'day' and n >= 2 else ''} ago"
    return "just now"


def step_evidence():
    """What already looks done, so a re-run can offer to skip it."""
    ev = {}
    corpus = _load(LOCAL_JSON, None)
    if corpus:
        c = corpus.get("counts", {})
        ev["LOCAL-SCAN"] = (f"{c.get('parts', 0)} local datasheet(s): "
                            f"{c.get('P', 0)} P / {c.get('N', 0)} N / "
                            f"{c.get('U', 0)} U, {_age(LOCAL_JSON)}")
    match = _load(MATCH_JSON, None)
    if match:
        c = match["counts"]
        ev["MATCH"] = (f"{c['matched_in_catalog'] + c['url_guess']} positives / "
                       f"{c['unlabeled']} unlabeled, {_age(MATCH_JSON)}")
        items = _work_items(match)
        have_p, have_u = _cache_coverage(items)
        if have_p or have_u:
            ev["FETCH"] = (f"{have_p} positives + {have_u} unlabeled already have "
                           f"datasheet text")
    if MODEL_FILE.exists():
        try:
            import joblib
            b = joblib.load(MODEL_FILE)
            ev["TRAIN"] = (f"{b.get('backend')} model, threshold "
                           f"{100 * b.get('threshold', 0):.0f}%, "
                           f"trained on {b.get('n_positives', '?')} positives, "
                           f"{_age(MODEL_FILE)}")
        except Exception:
            ev["TRAIN"] = f"model file present, {_age(MODEL_FILE)}"
    return ev


def _ask_skip(step, detail, mode):
    """True to skip a step that already has evidence of having run."""
    if mode == "force":
        return False
    if mode == "resume":
        print(f"  (skipping {step}: {detail})")
        return True
    print(f"\n  {step} looks already done: {detail}")
    print(f"  Skip {step}? [Y/n] ", end="", flush=True)
    try:
        ans = input().strip().lower()
    except EOFError:
        print("y (no input)")
        return True
    return ans in ("", "y", "yes")


def cmd_runall(args):
    """match -> fetch -> train -> review, using saved settings.

    Re-running is expected: everything is cached, so steps that already have
    results offer to be skipped (see --force / --resume)."""
    s = load_settings()
    if getattr(args, "catalog", None):
        s["catalog"] = args.catalog
    local_root = discover_local_library(s.get("local_ds_dir") or None)
    have_catalog = Path(str(s["catalog"])).is_file()
    if not local_root and not have_catalog:
        raise SystemExit(
            f"nothing to train on.\n"
            f"  local datasheets: not found (set 'local datasheet dir' in "
            f"Settings)\n"
            f"  Mini-Circuits catalog: {s['catalog']} (not found)\n"
            f"Point one of them at real data and re-run.")
    mode = ("force" if getattr(args, "force", False)
            else "resume" if getattr(args, "resume", False) else "ask")
    ev = step_evidence()
    if ev and mode == "ask":
        print("Found existing work; you'll be asked which steps to skip.")
    steps = []
    if local_root:
        # Preferred path: real datasheet prose already on disk, no network.
        steps.append(("LOCAL-SCAN", lambda: cmd_localscan(_localscan_ns(s))))
    if have_catalog and not s.get("local_only", True):
        steps += [
            ("MATCH", lambda: cmd_match(_ns(catalog=s["catalog"], db=s["db"],
                                            positives=None))),
            ("FETCH", lambda: cmd_fetch(_fetch_ns(s))),
        ]
    steps += [
        ("TRAIN", lambda: cmd_train(_train_ns(s))),
        ("REVIEW", lambda: cmd_review(_ns(out=s["review_out"], limit=0))),
    ]
    for i, (name, fn) in enumerate(steps, 1):
        if name in ev and _ask_skip(name, ev[name], mode):
            continue
        print(f"\n{'=' * 62}\n  STEP {i}/{len(steps)}: {name}\n{'=' * 62}")
        rc = fn()
        if rc:
            print(f"  {name} returned {rc}; stopping.")
            return rc
    print(f"\n{'=' * 62}")
    print(f"  Done. Model: {MODEL_FILE}")
    lim = s.get("fetch_limit") or 0
    if lim:
        print(f"  Coverage grows each time: re-run option 1 to fetch the next "
              f"{lim} part(s).")
        print(f"  Cached datasheets are reused, so nothing is downloaded twice.")
    print(f"  Score a paragraph:  python {Path(sys.argv[0]).name} "
          f"predict --text \"...\"")
    print(f"  Or menu option 7.")
    return 0


def _status_line():
    match = _load(MATCH_JSON, None)
    if match:
        c = match["counts"]
        ds = f"{c['matched_in_catalog'] + c['url_guess']} P / {c['unlabeled']} U"
    else:
        ds = "not matched yet"
    corpus = _load(LOCAL_JSON, None)
    if corpus:
        c = corpus.get("counts", {})
        local = (f"{c.get('parts', 0)} datasheet(s): {c.get('P', 0)} P / "
                 f"{c.get('N', 0)} N / {c.get('U', 0)} U")
    else:
        local = "not scanned yet  (menu option L)"
    n_txt = len(list(CACHE_TXT.glob("*.txt"))) if CACHE_TXT.exists() else 0
    model = "none"
    if MODEL_FILE.exists():
        try:
            import joblib
            b = joblib.load(MODEL_FILE)
            model = (f"{b.get('backend')}, thr "
                     f"{100 * b.get('threshold', 0):.0f}%, "
                     f"{b.get('trained', '?')}")
        except Exception:
            model = "unreadable"
    return ds, n_txt, model, local


MENU = """
==============================================================
  spacequal - is this RF part space-qualifiable?
==============================================================
  dataset : {ds}
  local   : {local}
  cache   : {n_txt} datasheet text file(s)
  model   : {model}
  catalog : {catalog}
--------------------------------------------------------------
  L) SCAN LOCAL DATASHEETS     <- train on files you already have (offline)
  1) Build datasheet LIBRARY   (multi-vendor, rotates hosts)
  2) Run everything            (local-scan -> train -> review)
  3) Match catalog to everythingRF space parts
  4) Fetch Mini-Circuits datasheets  (verbose progress)
  5) Train classifier
  6) Evaluate (PU metrics)
  7) Export review queue
  8) Score a paragraph of text  <- the everyday tool
  9) Settings
  s) Offline selftest (no network or catalog needed)
  c) Clear cache / reset
  0) Quit
--------------------------------------------------------------"""

SETTINGS_MENU = """
  Settings
  --------
  1) catalog path      {catalog}
  2) rfparts DB        {db}
  3) backend           {backend}
  4) mask              {mask}
  5) fetch limit       {fetch_limit}   (0 = all)
  6) request rate      {rate} s
  7) target recall     {target_recall}
  8) prior weight      {prior_weight}   (0 disables domain priors)
  9) PU bags / folds   {bags} / {folds}
 10) text mode         {text_mode}   (full = whole datasheet)
 11) harvest folder    {harvest_dir}
 12) datasheet limit   {ds_limit}   (downloads per run)
 13) local datasheets  {local_ds_dir}
 14) everythingRF HTML {erf_html_dir}
 15) absent parts are  {absent_as}   (negative | unlabeled)
 16) CMOS neg. weight  {neg_weight_cmos}
 17) local only        {local_only}   (skip the Mini-Circuits path)
  0) back"""


def _ask(prompt, current):
    print(f"  {prompt} [{current}]: ", end="", flush=True)
    try:
        v = input().strip()
    except EOFError:
        return current
    return v or current


def settings_menu(s):
    while True:
        print(SETTINGS_MENU.format(**s))
        try:
            choice = input("  choice> ").strip()
        except EOFError:
            return
        if choice in ("0", "", "q"):
            save_settings(s)
            print("  saved.")
            return
        try:
            if choice == "1":
                s["catalog"] = _ask("catalog path", s["catalog"])
            elif choice == "2":
                s["db"] = _ask("rfparts DB path", s["db"])
            elif choice == "3":
                v = _ask("backend (tfidf/embed)", s["backend"])
                s["backend"] = v if v in ("tfidf", "embed") else s["backend"]
            elif choice == "4":
                v = _ask("mask (none/space/strict)", s["mask"])
                s["mask"] = v if v in ("none", "space", "strict") else s["mask"]
            elif choice == "5":
                s["fetch_limit"] = int(_ask("fetch limit", s["fetch_limit"]))
            elif choice == "6":
                s["rate"] = float(_ask("seconds per request", s["rate"]))
            elif choice == "7":
                s["target_recall"] = float(_ask("target recall 0-1",
                                                s["target_recall"]))
            elif choice == "8":
                s["prior_weight"] = float(_ask("prior weight", s["prior_weight"]))
            elif choice == "9":
                s["bags"] = int(_ask("bags", s["bags"]))
                s["folds"] = int(_ask("folds", s["folds"]))
            elif choice == "10":
                v = _ask("text mode (full/overview)", s.get("text_mode", "full"))
                s["text_mode"] = v if v in ("full", "overview") else s["text_mode"]
            elif choice == "11":
                s["harvest_dir"] = _ask("folder of saved catalog pages "
                                        "(blank for none)", s.get("harvest_dir", ""))
            elif choice == "12":
                s["ds_limit"] = int(_ask("datasheet downloads per run",
                                         s.get("ds_limit", 400)))
            elif choice == "13":
                s["local_ds_dir"] = _ask("folder of saved datasheets "
                                         "(per-vendor subfolders)",
                                         s.get("local_ds_dir", ""))
            elif choice == "14":
                s["erf_html_dir"] = _ask("folder of saved everythingRF space "
                                         "pages (label oracle)",
                                         s.get("erf_html_dir", ""))
            elif choice == "15":
                v = _ask("parts absent from every space source are "
                         "(negative/unlabeled)", s.get("absent_as", "negative"))
                s["absent_as"] = v if v in ("negative", "unlabeled") \
                    else s["absent_as"]
            elif choice == "16":
                s["neg_weight_cmos"] = float(
                    _ask("weight for CMOS driver/logic negatives",
                         s.get("neg_weight_cmos", 3.0)))
            elif choice == "17":
                v = _ask("local only (yes/no)",
                         "yes" if s.get("local_only", True) else "no")
                s["local_only"] = str(v).strip().lower() in ("y", "yes", "true", "1")
        except ValueError:
            print("  ! not a number, unchanged")


def menu():
    s = load_settings()
    while True:
        ds, n_txt, model, local = _status_line()
        print(MENU.format(ds=ds, n_txt=n_txt, model=model, local=local,
                          catalog=s["catalog"]))
        try:
            choice = input("  choice> ").strip()
        except EOFError:
            return 0
        try:
            if choice in ("0", "q", "quit", "exit"):
                return 0
            elif choice.lower() == "l":
                cmd_localscan(_localscan_ns(s))
            elif choice == "1":
                cmd_datasheets(_ns(
                    catalog=s["catalog"], db=s["db"],
                    harvest=(s.get("harvest_dir") or None),
                    limit=s.get("ds_limit", 400), rate=s["rate"],
                    space_only=False, refetch=False, ignore_robots=False,
                    from_db=True, allow_html=True))
            elif choice == "2":
                cmd_runall(_ns(catalog=s["catalog"], force=False, resume=False))
            elif choice == "3":
                cmd_match(_ns(catalog=s["catalog"], db=s["db"], positives=None))
            elif choice == "4":
                cmd_fetch(_fetch_ns(s))
            elif choice == "5":
                cmd_train(_train_ns(s))
            elif choice == "6":
                cmd_eval(_ns(min_confidence=0.0, reviewed=None))
            elif choice == "7":
                cmd_review(_ns(out=s["review_out"], limit=0))
            elif choice == "8":
                interactive_score()
            elif choice == "9":
                settings_menu(s)
            elif choice.lower() == "s":
                cmd_selftest(_ns(backend=s["backend"], hf_model=s["hf_model"],
                                 positives=60, unlabeled=1500, hidden=45,
                                 bags=10, folds=5,
                                 target_recall=s["target_recall"], seed=0,
                                 local=False, negatives=60,
                                 neg_weight_cmos=s.get("neg_weight_cmos", 3.0)))
            elif choice.lower() == "c":
                clear_menu()
            else:
                print("  ? unknown choice")
        except SystemExit as e:                 # keep the menu alive on errors
            print(f"\n  ! {e}")
        except KeyboardInterrupt:
            print("\n  (interrupted; cached progress is kept)")
        input("\n  press Enter to continue ")


def build_parser():
    p = argparse.ArgumentParser(
        prog="spacequal", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd")

    ra = sub.add_parser("runall",
                        help="match -> fetch -> train -> review in one go")
    ra.add_argument("--catalog", help=f"default: {DEFAULT_CATALOG}")
    ra.add_argument("--force", action="store_true",
                    help="re-run every step, ignoring existing results")
    ra.add_argument("--resume", action="store_true",
                    help="skip completed steps without asking")
    ra.set_defaults(func=cmd_runall)

    mn = sub.add_parser("menu", help="interactive menu (also the default)")
    mn.set_defaults(func=lambda a: menu())

    ls = sub.add_parser("local-scan",
                        help="build the training corpus from datasheets you "
                             "already have on disk (offline)")
    ls.add_argument("--local-dir", help=f"default: {DEFAULT_LOCAL_DS}")
    ls.add_argument("--db", help="rfparts parts.db (Qorvo/ADI space labels)")
    ls.add_argument("--erf-html",
                    help="folder of saved everythingRF space pages, used ONLY "
                         "as the positive-label oracle")
    ls.add_argument("--vendors", nargs="*",
                    help="restrict to these vendor keys, e.g. qorvo marki")
    ls.add_argument("--absent-as", choices=("negative", "unlabeled"),
                    default="negative",
                    help="how to treat a datasheet absent from every space "
                         "source (default negative)")
    ls.add_argument("--text-mode", choices=("full", "overview"), default="full")
    ls.add_argument("--min-chars", type=int, default=LOCAL_MIN_CHARS,
                    help="skip files yielding less text than this")
    ls.add_argument("--keep-boilerplate", action="store_true",
                    help="do NOT strip per-vendor nav/footer chrome")
    ls.add_argument("--keep-pn", action="store_true",
                    help="keep the part number in the training text (default is "
                         "to redact it, so the model cannot memorise PN "
                         "prefixes that correlate with the label)")
    ls.add_argument("--no-db", action="store_true",
                    help="ignore the rfparts DB; label from --erf-html only")
    ls.set_defaults(func=cmd_localscan)

    m = sub.add_parser("match", help="join everythingRF space parts to the catalog")
    m.add_argument("--catalog", required=True, help="minicircuits_products_full.json")
    m.add_argument("--db", default=str(Path.home() / ".rfparts" / "parts.db"),
                   help="rfparts SQLite DB holding the everythingRF parts")
    m.add_argument("--positives", help="JSON list of space PNs (instead of --db)")
    m.set_defaults(func=cmd_match)

    f = sub.add_parser("fetch", help="download + cache datasheets, extract overviews")
    f.add_argument("--limit", type=int, default=0)
    f.add_argument("--rate", type=float, default=DEFAULT_RATE,
                   help="seconds between requests (default 1.0)")
    f.add_argument("--positives-only", action="store_true")
    f.add_argument("--refetch", action="store_true")
    f.add_argument("--ignore-robots", action="store_true",
                   help="default is to obey robots.txt")
    f.add_argument("--show-missing", type=int, default=40)
    f.add_argument("--verbose", action="store_true",
                   help="kept for compatibility; progress is on by default")
    f.add_argument("--quiet", action="store_true",
                   help="suppress the per-part progress lines")
    f.add_argument("--text-mode", choices=("full", "overview"), default="full",
                   help="full = whole datasheet with numeric tables stripped "
                        "(default); overview = page-1 blurb only")
    f.set_defaults(func=cmd_fetch)

    t = sub.add_parser("train", help="fit the PU classifier and score every part")
    t.add_argument("--backend", choices=("tfidf", "embed"), default="tfidf")
    t.add_argument("--hf-model", default=DEFAULT_HF_MODEL,
                   help="sentence-transformers model for --backend embed")
    t.add_argument("--mask", choices=("none", "space", "strict"), default="none",
                   help="redact space (or space+hi-rel) vocabulary to measure "
                        "what the model infers from construction alone")
    t.add_argument("--bags", type=int, default=15, help="PU bagging rounds")
    t.add_argument("--folds", type=int, default=5, help="CV folds over positives")
    t.add_argument("--target-recall", type=float, default=0.90,
                   help="held-out recall the threshold aims for (default 0.90)")
    t.add_argument("--threshold", type=float, default=None,
                   help="fixed score cut instead of --target-recall")
    t.add_argument("--show-features", type=int, default=25)
    t.add_argument("--no-features", action="store_true")
    t.add_argument("--prior-weight", type=float, default=1.0,
                   help="strength of the hand-written domain priors (0 = off)")
    t.add_argument("--no-prior", action="store_true",
                   help="train and score on the learned model alone")
    t.add_argument("--seed", type=int, default=0)
    t.add_argument("--no-local", dest="local", action="store_false",
                   help="ignore the local datasheet corpus")
    t.add_argument("--local-only", action="store_true",
                   help="train ONLY on the local datasheet corpus (skip the "
                        "Mini-Circuits match)")
    t.add_argument("--neg-weight-cmos", type=float, default=3.0,
                   help="sample weight for negatives whose text is CMOS "
                        "driver/logic language (default 3.0)")
    t.set_defaults(func=cmd_train, local=True)

    pr = sub.add_parser("predict", help="score new text or a cached part")
    pr.add_argument("--text", help="datasheet paragraph to score")
    pr.add_argument("--file", help="text file to score")
    pr.add_argument("--pn", nargs="*", help="part numbers already fetched")
    pr.set_defaults(func=cmd_predict)

    e = sub.add_parser("eval", help="PU-aware scoring")
    e.add_argument("--min-confidence", type=float, default=0.0,
                   help="override the trained threshold")
    e.add_argument("--reviewed", help="CSV from `review` with verdicts filled in")
    e.set_defaults(func=cmd_eval)

    r = sub.add_parser("review", help="export the ranked candidate queue")
    r.add_argument("--out", default="review_queue.csv")
    r.add_argument("--limit", type=int, default=0)
    r.set_defaults(func=cmd_review)

    ds = sub.add_parser("datasheets",
                        help="download datasheets across vendors (rotates hosts)")
    ds.add_argument("--catalog", help="Mini-Circuits catalog JSON")
    ds.add_argument("--db", help="rfparts DB (for everythingRF space parts)")
    ds.add_argument("--harvest", help="folder of catalog pages you saved yourself; "
                                      "PDF links are read out of them")
    ds.add_argument("--limit", type=int, default=400,
                    help="max download attempts this run (0 = no limit)")
    ds.add_argument("--rate", type=float, default=DEFAULT_RATE,
                    help="seconds between requests TO THE SAME HOST")
    ds.add_argument("--space-only", action="store_true",
                    help="only parts everythingRF lists as space qualified/grade")
    ds.add_argument("--refetch", action="store_true")
    ds.add_argument("--ignore-robots", action="store_true")
    ds.add_argument("--from-db", action="store_true",
                    help="also download the datasheet URLs your pipeline "
                         "already recorded in parts.db (documents table)")
    ds.add_argument("--allow-html", action="store_true",
                    help="keep HTML product pages too (usable training text "
                         "for vendors with opaque PDF ids, e.g. Qorvo)")
    ds.set_defaults(func=cmd_datasheets)

    cl = sub.add_parser("clear", help="delete cached downloads / derived files")
    cl.add_argument("--pdfs", action="store_true", help="downloaded PDFs")
    cl.add_argument("--text", action="store_true", help="extracted overview text")
    cl.add_argument("--model", action="store_true", help="trained model file")
    cl.add_argument("--predictions", action="store_true", help="predictions.json")
    cl.add_argument("--match", action="store_true", help="matched.json")
    cl.add_argument("--missing", action="store_true",
                    help="missing_datasheets.json report")
    cl.add_argument("--all", action="store_true", help="everything above")
    cl.add_argument("--yes", "-y", action="store_true", help="skip confirmation")
    cl.set_defaults(func=cmd_clear)

    s = sub.add_parser("selftest", help="offline end-to-end check on synthetic data")
    s.add_argument("--backend", choices=("tfidf", "embed"), default="tfidf")
    s.add_argument("--hf-model", default=DEFAULT_HF_MODEL)
    s.add_argument("--positives", type=int, default=60)
    s.add_argument("--unlabeled", type=int, default=1500)
    s.add_argument("--hidden", type=int, default=45,
                   help="unlabeled parts that are secretly qualifiable")
    s.add_argument("--bags", type=int, default=10)
    s.add_argument("--folds", type=int, default=5)
    s.add_argument("--target-recall", type=float, default=0.90)
    s.add_argument("--seed", type=int, default=0)
    s.add_argument("--local", action="store_true",
                   help="exercise the LOCAL datasheet path instead: synthetic "
                        "HTML library + everythingRF pages + parts.db")
    s.add_argument("--negatives", type=int, default=60,
                   help="synthetic labelled negatives for --local")
    s.add_argument("--neg-weight-cmos", type=float, default=3.0)
    s.set_defaults(func=cmd_selftest)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    WORKDIR.mkdir(parents=True, exist_ok=True)
    if not getattr(args, "func", None):        # bare `python spacequal.py`
        return menu()
    try:
        return args.func(args)
    except BrokenPipeError:            # piped into head/less
        try:
            sys.stdout.close()
        except Exception:
            pass
        return 0
    except KeyboardInterrupt:
        print("\ninterrupted; cached progress means the next run resumes.",
              file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())