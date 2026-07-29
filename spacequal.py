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
    fetcher = Fetcher(rate=args.rate, ignore_robots=args.ignore_robots)
    got = cached = failed = empty = 0
    missing = []
    for i, it in enumerate(items, 1):
        pn = norm_pn(it["pn"])
        safe = re.sub(r"[^A-Za-z0-9._+-]", "_", pn)
        txt_path, pdf_path = CACHE_TXT / f"{safe}.txt", CACHE_PDF / f"{safe}.pdf"
        if txt_path.exists() and not args.refetch:
            # An empty overview only means something when we hold the PDF (a
            # scanned datasheet). Empty with no PDF = an earlier failed fetch,
            # so retry rather than skip forever.
            if txt_path.stat().st_size > 0 or pdf_path.exists():
                cached += 1
                continue
        url = it.get("datasheet_url") or MC_PDF_TEMPLATE.format(pn=pn)
        blob = (pdf_path.read_bytes()
                if (pdf_path.exists() and not args.refetch) else None)
        if blob is None:
            try:
                blob = fetcher.get(url)
            except PermissionError as e:
                raise SystemExit(f"stopping: {e}")
            if blob:
                pdf_path.write_bytes(blob)
        if not blob:
            # not cached: may be transient (timeout/429/offline), so retry next run
            failed += 1
            missing.append((it["pn"], it["label"], url))
            continue
        overview = extract_overview(blob)
        txt_path.write_text(overview, encoding="utf-8")
        got += 1
        empty += 0 if overview else 1
        if args.verbose or i % 50 == 0:
            print(f"  [{i}/{len(items)}] {it['pn']:<26} "
                  f"{'ok' if overview else 'no text'}")
    print(f"\nfetched {got}   cached {cached}   no-text {empty}   failed {failed}")
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
                     "threshold": thr, "backend": tag, "mask": args.mask},
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


def cmd_predict(args):
    """Score arbitrary datasheet text with the saved model."""
    import joblib
    if not MODEL_FILE.exists():
        raise SystemExit("no saved model -- run `train` first")
    bundle = joblib.load(MODEL_FILE)
    texts, names = [], []
    if args.text:
        texts.append(mask_text(args.text, bundle["mask"]))
        names.append("(stdin)")
    for pn in args.pn or []:
        p = _overview_path(pn)
        if not p.exists():
            print(f"  no cached datasheet text for {pn}")
            continue
        texts.append(mask_text(p.read_text(encoding="utf-8"), bundle["mask"]))
        names.append(pn)
    if not texts:
        raise SystemExit("pass --text or --pn")
    import numpy as np
    X = bundle["featurizer"].transform(texts)
    scores = np.mean([m.predict_proba(X)[:, 1] for m in bundle["models"]], axis=0)
    for name, s in zip(names, scores):
        verdict = "QUALIFIABLE" if s >= bundle["threshold"] else "not flagged"
        print(f"  {name:<24} score {s:.3f}  (threshold "
              f"{bundle['threshold']:.3f})  -> {verdict}")
    return 0


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
        show_features=12, no_features=False))
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


def build_parser():
    p = argparse.ArgumentParser(
        prog="spacequal", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

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
    f.add_argument("--verbose", action="store_true")
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
    t.add_argument("--seed", type=int, default=0)
    t.set_defaults(func=cmd_train)

    pr = sub.add_parser("predict", help="score new text or a cached part")
    pr.add_argument("--text", help="datasheet blurb to score")
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
