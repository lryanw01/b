#!/usr/bin/env python3
"""spacequal — predict whether a Mini-Circuits part is SPACE-QUALIFIABLE from its
datasheet blurb, using a locally-trained classifier. No API keys, no paid calls.

STANDALONE. Nothing here imports rfparts; it only *reads* the rfparts SQLite DB
(or a JSON export). Nothing is written back to that DB.

    pipeline:  match -> fetch -> train -> eval -> review
               (selftest runs the whole thing on synthetic data, offline)

    python spacequal.py match --catalog minicircuits_products_full.json
    python spacequal.py fetch --limit 600
    python spacequal.py train --backend tfidf --target-recall 0.9
    python spacequal.py eval
    python spacequal.py review --out review_queue.csv

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
OVERVIEW_CHARS = 4000       # datasheet text kept per part
DEFAULT_HF_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
SETTINGS_FILE = WORKDIR / "settings.json"

# Default location of the Mini-Circuits catalog. EDIT THIS to your own path (or
# change it in the menu under Settings) so the pipeline runs with no arguments.
DEFAULT_CATALOG = r"C:\rfparts\rfparts\sources\minicircuits_products_full.json"

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
    (r"-\s*55\s*\u00b0?\s*C?\s*(?:to|-|\u2013)\s*\+?\s*1(?:25|50)\s*\u00b0?\s*C\b",
     +0.60, "military temperature range"),
]
_DOMAIN_PRIOR_RX = [(re.compile(rx, re.I), w, why) for rx, w, why in DOMAIN_PRIOR]


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


def extract_overview(pdf_bytes) -> str:
    """The readable overview of a datasheet: leading text of page 1 plus any
    Overview / Features / Applications section in the first pages. Parametric
    tables are left out -- construction and materials language predicts
    qualifiability, the numbers do not."""
    text = ""
    try:
        import io
        import warnings
        import pdfplumber
        warnings.filterwarnings("ignore")
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            text = "\n".join((p.extract_text() or "") for p in pdf.pages[:3])
    except Exception:
        try:
            import io
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(pdf_bytes))
            text = "\n".join((pg.extract_text() or "") for pg in reader.pages[:3])
        except Exception:
            return ""
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
def train_pu(Xp, Xu, n_bags=15, folds=5, seed=0, verbose=True):
    """PU bagging with an outer CV over P.

    Returns (p_scores, u_scores, bagged_models). Each bag samples |P_train|
    unlabeled rows, treats them as negative, and fits a model. P scores come only
    from folds where that positive was held out, so recall is honest. U scores are
    averaged out-of-bag (a U row is scored only by bags that did NOT use it as a
    negative), which keeps its score from being pushed down by its own training
    label.
    """
    import numpy as np
    from sklearn.model_selection import KFold

    n_p, n_u = Xp.shape[0], Xu.shape[0]
    rng = np.random.default_rng(seed)
    p_scores = np.zeros(n_p)
    u_sum = np.zeros(n_u)
    u_cnt = np.zeros(n_u)

    folds = max(2, min(folds, n_p))              # cannot have more folds than P
    splitter = KFold(n_splits=folds, shuffle=True, random_state=seed)
    for fold, (tr, te) in enumerate(splitter.split(np.arange(n_p)), 1):
        fold_scores = np.zeros(len(te))
        for b in range(n_bags):
            neg = rng.choice(n_u, size=min(len(tr), n_u), replace=False)
            X = _vstack(Xp[tr], Xu[neg])
            y = np.r_[np.ones(len(tr)), np.zeros(len(neg))]
            clf = make_classifier(seed=seed + b)
            clf.fit(X, y)
            fold_scores += clf.predict_proba(Xp[te])[:, 1]
            oob = np.ones(n_u, dtype=bool)
            oob[neg] = False
            u_sum[oob] += clf.predict_proba(Xu[oob])[:, 1]
            u_cnt[oob] += 1
        p_scores[te] = fold_scores / n_bags
        if verbose:
            print(f"    fold {fold}/{folds}: held-out P mean score "
                  f"{p_scores[te].mean():.3f}")

    # final ensemble on ALL positives, for scoring new parts later
    models = []
    for b in range(n_bags):
        neg = rng.choice(n_u, size=min(n_p, n_u), replace=False)
        X = _vstack(Xp, Xu[neg])
        y = np.r_[np.ones(n_p), np.zeros(len(neg))]
        clf = make_classifier(seed=1000 + seed + b)
        clf.fit(X, y)
        models.append(clf)
    u_scores = np.divide(u_sum, np.maximum(u_cnt, 1))
    return p_scores, u_scores, models


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
    """PU-aware scoring. A flagged unlabeled part is never called an error."""
    P = [p for p in preds if p["label"] == "P"]
    U = [p for p in preds if p["label"] == "U"]
    flag_p = sum(1 for p in P if p["qualifiable"])
    flag_u = sum(1 for p in U if p["qualifiable"])
    n = len(P) + len(U)
    recall = flag_p / len(P) if P else 0.0
    flag_rate_u = flag_u / len(U) if U else 0.0
    pr_flag = (flag_p + flag_u) / n if n else 0.0
    # Lee & Liu (2003): recall^2 / P(flag) ranks classifiers like F1 does but
    # needs no negatives. Use it to compare backends/prompts, not as an accuracy.
    pu_score = (recall ** 2 / pr_flag) if pr_flag else 0.0
    sanity = [p for p in U if p.get("sanity_negative")]
    return {"n_positives": len(P), "n_unlabeled": len(U),
            "flagged_positives": flag_p, "flagged_unlabeled": flag_u,
            "recall_on_P": recall, "flag_rate_on_U": flag_rate_u,
            "p_flag": pr_flag, "pu_score": pu_score,
            "missed_positives": len(P) - flag_p,
            "sanity_negatives": len(sanity),
            "sanity_flagged": sum(1 for p in sanity if p["qualifiable"])}


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


def _work_items(match, include_unlabeled=True):
    items = [{**p, "label": "P"} for p in match["positives"]]
    if include_unlabeled:
        for u in match["unlabeled"]:
            blob = f"{u['pn']} {u.get('group', '')} {u.get('category_mc', '')}"
            items.append({**u, "label": "U",
                          "sanity_negative": bool(_NOT_FLIGHT_RE.search(blob))})
    return items


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
    if args.limit:
        items = items[: args.limit]
    quiet = getattr(args, "quiet", False)
    fetcher = Fetcher(rate=args.rate, ignore_robots=args.ignore_robots)
    got = cached = failed = empty = recovered = 0
    missing = []
    total = len(items)
    started = time.time()
    print(f"fetching up to {total} datasheet(s) at {args.rate}s/request "
          f"-> {CACHE_PDF}")
    print(f"{'':>4}{'#':>7}  {'part':<26} {'status':<10} detail")
    for i, it in enumerate(items, 1):
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
        overview = extract_overview(blob)
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
    match = _load(MATCH_JSON, None)
    if not match:
        raise SystemExit("run `match` first")
    items = _work_items(match)
    data, skipped = load_texts(items, mask=args.mask)
    P = [d for d in data if d["label"] == "P"]
    U = [d for d in data if d["label"] == "U"]
    print(f"training data: {len(P)} positives, {len(U)} unlabeled "
          f"({skipped} parts skipped for missing datasheet text)")
    if len(P) < 5:
        raise SystemExit(
            f"only {len(P)} positives have datasheet text -- run `fetch` "
            f"(at least a few dozen positives makes this meaningful)")
    if len(P) < 25:
        print(f"  ! only {len(P)} positives: expect noisy estimates. Treat the "
              f"numbers as indicative and widen your review sample.")
    if not U:
        raise SystemExit("no unlabeled parts with text -- run `fetch` without "
                         "--positives-only")

    featurizer, tag = _build_featurizer(args)
    texts = [d["text"] for d in P] + [d["text"] for d in U]
    print(f"  featurizing with {tag} ...")
    X = featurizer.fit_transform(texts)
    Xp, Xu = X[: len(P)], X[len(P):]
    try:
        print(f"  feature matrix: {X.shape[0]} x {X.shape[1]}")
    except Exception:
        pass

    print(f"  PU bagging: {args.bags} bags x {args.folds} folds")
    p_scores, u_scores, models = train_pu(Xp, Xu, n_bags=args.bags,
                                          folds=args.folds, seed=args.seed)
    # Domain priors are applied BEFORE the threshold is chosen, so the reported
    # recall and flag rate describe the blended system you will actually run.
    pw = 0.0 if args.no_prior else args.prior_weight
    if pw:
        p_scores = [blend_prior(s, d["text"], pw) for s, d in zip(p_scores, P)]
        u_scores = [blend_prior(s, d["text"], pw) for s, d in zip(u_scores, U)]
        print(f"  domain priors blended in at weight {pw} "
              f"({len(DOMAIN_PRIOR)} terms)")
    else:
        p_scores, u_scores = list(p_scores), list(u_scores)
        print("  domain priors disabled")
    thr = (args.threshold if args.threshold is not None
           else threshold_for_recall(p_scores, args.target_recall))
    print(f"\n  decision threshold {thr:.4f}"
          + ("" if args.threshold is not None
             else f" (chosen for {args.target_recall:.0%} recall on held-out P)"))

    preds = []
    for d, s in zip(P, p_scores):
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
                     "n_positives": len(P), "n_unlabeled": len(U)},
                    MODEL_FILE)
        print(f"\n  saved model -> {MODEL_FILE}")
    except Exception as e:
        print(f"\n  (model not saved: {e})")
    print(f"  saved predictions -> {PRED_JSON}\n")
    return cmd_eval(argparse.Namespace(min_confidence=0.0, reviewed=None))


def _pred_row(d, score, thr, tag, mask):
    return {"pn": d["pn"], "label": d["label"],
            "sanity_negative": d.get("sanity_negative", False),
            "category_mc": d.get("category_mc", ""),
            "datasheet_url": d.get("datasheet_url", ""),
            "variant": d.get("variant", ""),
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
    print(f"\n  flag rate on U               {m['flag_rate_on_U']:.1%}"
          f"   ({m['flagged_unlabeled']}/{m['n_unlabeled']})")
    print("    NOT an error rate: U is unlabeled, so a flag here is a")
    print("    candidate to review, and some are genuinely qualifiable.")
    print(f"\n  PU score (recall^2/P(flag))   {m['pu_score']:.3f}"
          f"   <- compare backends with this")
    if m["sanity_negatives"]:
        rate = m["sanity_flagged"] / m["sanity_negatives"]
        print(f"\n  sanity negatives (eval boards, kits, adapters): "
              f"{m['sanity_negatives']}")
        print(f"    flagged                    {m['sanity_flagged']} ({rate:.1%})"
              f"   <- want LOW; heuristic set, not ground truth")
    print("\n  Precision on U cannot be measured from PU data. Scenarios:")
    print("    assumed prevalence | qualifiable in U | est. precision | est. finds")
    for row in precision_sensitivity(m):
        print(f"      {row['assumed_prevalence']:>14.0%} | "
              f"{row['assumed_qualifiable_in_U']:>16} | "
              f"{row['est_precision_on_U']:>14.1%} | {row['est_true_finds']:>10}")
    if getattr(args, "reviewed", None):
        _report_reviewed(args.reviewed, m)
    print("\n  Next: `review` to export the ranked candidate queue, label a")
    print("  sample, then `eval --reviewed labelled.csv` for real precision.")
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
    queue = sorted([p for p in preds if p["label"] == "U" and p["qualifiable"]],
                   key=lambda p: -p["score"])
    if args.limit:
        queue = queue[: args.limit]
    out = Path(args.out)
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["pn", "score", "category", "datasheet_url", "verdict"])
        for p in queue:
            w.writerow([p["pn"], f"{p['score']:.4f}", p.get("category_mc", ""),
                        p.get("datasheet_url", ""), ""])
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
        prior_weight=1.0, no_prior=False))
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


# --------------------------------------------------------- settings + menu
DEFAULT_SETTINGS = {
    "catalog": DEFAULT_CATALOG,
    "db": str(Path.home() / ".rfparts" / "parts.db"),
    "backend": "tfidf",
    "hf_model": DEFAULT_HF_MODEL,
    "mask": "none",
    "fetch_limit": 0,
    "rate": DEFAULT_RATE,
    "bags": 15,
    "folds": 5,
    "target_recall": 0.90,
    "prior_weight": 1.0,
    "review_out": "review_queue.csv",
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
               verbose=True, quiet=quiet)


def _train_ns(s):
    return _ns(backend=s["backend"], hf_model=s["hf_model"], mask=s["mask"],
               bags=s["bags"], folds=s["folds"], seed=0,
               target_recall=s["target_recall"], threshold=None,
               show_features=25, no_features=False,
               prior_weight=s["prior_weight"], no_prior=False)


def cmd_runall(args):
    """match -> fetch (with progress) -> train -> review, using saved settings."""
    s = load_settings()
    if getattr(args, "catalog", None):
        s["catalog"] = args.catalog
    if not Path(s["catalog"]).is_file():
        raise SystemExit(
            f"catalog not found: {s['catalog']}\n"
            f"Set the path with `settings` in the menu, pass --catalog, or edit "
            f"DEFAULT_CATALOG at the top of this file.")
    steps = [
        ("MATCH", lambda: cmd_match(_ns(catalog=s["catalog"], db=s["db"],
                                        positives=None))),
        ("FETCH", lambda: cmd_fetch(_fetch_ns(s))),
        ("TRAIN", lambda: cmd_train(_train_ns(s))),
        ("REVIEW", lambda: cmd_review(_ns(out=s["review_out"], limit=0))),
    ]
    for i, (name, fn) in enumerate(steps, 1):
        print(f"\n{'=' * 62}\n  STEP {i}/{len(steps)}: {name}\n{'=' * 62}")
        rc = fn()
        if rc:
            print(f"  {name} returned {rc}; stopping.")
            return rc
    print(f"\n{'=' * 62}")
    print(f"  Done. Model: {MODEL_FILE}")
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
    return ds, n_txt, model


MENU = """
==============================================================
  spacequal - is this RF part space-qualifiable?
==============================================================
  dataset : {ds}
  cache   : {n_txt} datasheet text file(s)
  model   : {model}
  catalog : {catalog}
--------------------------------------------------------------
  1) Run everything   (match -> fetch -> train -> review)
  2) Match catalog to everythingRF space parts
  3) Fetch datasheets            (verbose progress)
  4) Train classifier
  5) Evaluate (PU metrics)
  6) Export review queue
  7) Score a paragraph of text   <- the everyday tool
  8) Settings
  9) Offline selftest (no network or catalog needed)
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
        except ValueError:
            print("  ! not a number, unchanged")


def menu():
    s = load_settings()
    while True:
        ds, n_txt, model = _status_line()
        print(MENU.format(ds=ds, n_txt=n_txt, model=model,
                          catalog=s["catalog"]))
        try:
            choice = input("  choice> ").strip()
        except EOFError:
            return 0
        try:
            if choice in ("0", "q", "quit", "exit"):
                return 0
            elif choice == "1":
                cmd_runall(_ns(catalog=s["catalog"]))
            elif choice == "2":
                cmd_match(_ns(catalog=s["catalog"], db=s["db"], positives=None))
            elif choice == "3":
                cmd_fetch(_fetch_ns(s))
            elif choice == "4":
                cmd_train(_train_ns(s))
            elif choice == "5":
                cmd_eval(_ns(min_confidence=0.0, reviewed=None))
            elif choice == "6":
                cmd_review(_ns(out=s["review_out"], limit=0))
            elif choice == "7":
                interactive_score()
            elif choice == "8":
                settings_menu(s)
            elif choice.lower() == "c":
                clear_menu()
            elif choice == "9":
                cmd_selftest(_ns(backend=s["backend"], hf_model=s["hf_model"],
                                 positives=60, unlabeled=1500, hidden=45,
                                 bags=10, folds=5,
                                 target_recall=s["target_recall"], seed=0))
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
    ra.set_defaults(func=cmd_runall)

    mn = sub.add_parser("menu", help="interactive menu (also the default)")
    mn.set_defaults(func=lambda a: menu())

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
    t.set_defaults(func=cmd_train)

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
