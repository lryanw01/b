"""Build/refresh the space-qualified parts dataset from LOCAL catalogs.

One place that knows how to turn the catalogs on disk into partdb rows, then
dedupe. Everything is offline: it only reads files you already downloaded.

Sources understood:
  * everythingRF saved listing folders  -> erf_space_ingest  (EverythingRFSpace*)
  * ADI space portfolio .xlsx            -> adi_ingest
  * Qorvo defense/aerospace .pdf         -> qorvo_ingest
  * TI Space Products Guide .pdf         -> ti_ingest

Adding another catalog later = drop a new ``*_ingest`` module with an
``ingest(path, dry_run, verbose)`` and add one line to ``_ADAPTERS``.

Public API (used by the GUI "Rebuild dataset" button and the CLI `ingest`):
    rebuild(erf_parent=None, source_files=(), source_dir=None,
            dedupe=True, progress=None) -> summary dict
    scan_dir(folder) -> list[(path, kind)]
"""
from __future__ import annotations

import time
import traceback
from pathlib import Path
from .paths import EVERYTHING_RF, NEW_SOURCES, ADI_PARAMETRICS, EXPORT_DIR

try:
    from . import (adi_ingest, qorvo_ingest, ti_ingest, erf_space_ingest,
                   partdb, vendor_catalogs, adi_parametric_ingest,
                   adi_space_ingest)
except ImportError:                                    # loose-script fallback
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from rfparts import (adi_ingest, qorvo_ingest, ti_ingest,   # type: ignore
                         erf_space_ingest, partdb, vendor_catalogs,
                         adi_parametric_ingest, adi_space_ingest)

# Vendors whose live catalogues can be walked (see vendor_catalogs). Selectable
# so a refresh can target one vendor instead of re-walking everything.
CATALOG_VENDORS = vendor_catalogs.ALL_VENDORS

# What each source is CAPABLE of providing, declared rather than inferred.
#
# "Why is there no gain from the space portfolio?" is not answerable by looking at
# the dataset: an empty column looks identical whether the parser failed or the
# source never had the data. Verified against the real files -- the ADI space
# portfolio has 19 columns and not one of them is gain, NF, P1dB, OIP3 or Psat;
# the word "Gain" appears 5 times in the entire workbook, all inside description
# prose. So the honest answer is printed at the end of every rebuild instead of
# being re-investigated.
SOURCE_CAPABILITIES = {
    "ADI space portfolio": {
        "provides": ["space_qual_level", "tid_krad", "sel_mev", "tnid",
                     "temp_min_c", "temp_max_c", "package", "package_material",
                     "lead_finish", "datasheet_url", "freq_ghz (from the "
                     "description, ~117 of 234 parts)"],
        "cannot_provide": ["gain_db", "nf_db", "p1db_dbm", "oip3_dbm",
                           "psat_dbm"],
        "note": "19 columns, none parametric. RF numbers for its 39 genuine RF "
                "amplifiers arrive by merging with the ADI parametric exports on "
                "the same part number.",
    },
    "ADI parametric exports": {
        "provides": ["freq_ghz", "gain_db (Amplifiers + LNA/PA sheets only)",
                     "nf_db", "oip3_dbm", "iip3_dbm", "p1db_dbm", "psat_dbm",
                     "conversion_gain_db", "package", "price_usd", "lifecycle",
                     "temp_min_c", "temp_max_c"],
        "cannot_provide": ["tid_krad", "sel_mev", "space_qual_level"],
        "note": "3 of 9 sheets carry a gain column; the other 6 have none. "
                "Commercial catalogue, so it never sets a space classification.",
    },
    "everythingRF": {
        "provides": ["freq_ghz", "gain_db", "nf_db", "p1db_dbm", "oip3_dbm",
                     "psat_dbm (derived from Output Power in W)", "power_w",
                     "insertion_loss_db", "isolation_db", "package",
                     "space_variant (from the folder name)"],
        "cannot_provide": ["tid_krad", "sel_mev"],
        "note": "Listing cards vary by part: many state only frequency and type.",
    },
    "MACOM": {
        "provides": ["freq_ghz", "gain_db", "psat_dbm", "p1db_dbm", "oip3_dbm",
                     "supply_v", "current_ma", "efficiency_pct",
                     "datasheet_url"],
        "cannot_provide": ["tid_krad", "sel_mev", "space_qual_level"],
        "note": "From the data-part JSON embedded in each listing row.",
    },
    "Qorvo": {
        "provides": ["freq_ghz", "gain_db", "nf_db", "p1db_dbm", "oip3_dbm",
                     "insertion_loss_db", "isolation_db", "attenuation_db",
                     "package", "datasheet_url"],
        "cannot_provide": ["tid_krad", "sel_mev", "space_qual_level"],
        "note": "Whichever columns that category's parametric table happens to "
                "carry.",
    },
    "Marki Microwave": {
        "provides": ["freq_ghz", "gain_db", "nf_db", "p1db_dbm", "oip3_dbm",
                     "isolation_db", "conversion_loss_db", "supply_v",
                     "current_ma", "absmax_*"],
        "cannot_provide": ["tid_krad", "sel_mev", "space_qual_level"],
        "note": "Specs come from the /datasheet/ page, not the listing, so the "
                "per-part pass must complete.",
    },
    "Skyworks": {
        "provides": ["datasheet_url", "package"],
        "cannot_provide": ["gain_db", "nf_db", "p1db_dbm", "oip3_dbm",
                           "tid_krad", "sel_mev"],
        "note": "Listings carry no parametrics; the numbers are only in the PDF.",
    },
}


def capability_report(sources=None):
    """Lines describing what each source can and cannot supply."""
    out = ["Source capabilities (what each input can actually provide)"]
    for name, cap in SOURCE_CAPABILITIES.items():
        if sources and not any(s.lower() in name.lower() for s in sources):
            continue
        out.append(f"  {name}")
        out.append(f"    provides : {', '.join(cap['provides'])}")
        out.append(f"    CANNOT   : {', '.join(cap['cannot_provide'])}")
        out.append(f"    note     : {cap['note']}")
    return out

# kind -> (module, human label)
_ADAPTERS = {
    "adi": (adi_ingest, "ADI catalog sheet"),
    "adi_space": (adi_space_ingest, "ADI space portfolio "
                                    "(qual level, TID/SEL, package material)"),
    "adi_parametric": (adi_parametric_ingest, "ADI parametric search export"),
    "qorvo": (qorvo_ingest, "Qorvo defense/aerospace brochure"),
    "ti": (ti_ingest, "TI Space Products Guide"),
}


def classify_file(path: Path) -> str | None:
    """Best-effort adapter kind for a file, from its name/extension."""
    name = path.name.lower()
    ext = path.suffix.lower()
    if ext in (".xlsx", ".xlsm", ".csv"):
        # The ADI SPACE PORTFOLIO carries qualification level, TID/SEL
        # thresholds, package material and a real datasheet URL. Sending it to
        # the generic adi adapter read the part number and discarded the other
        # eighteen columns.
        if "space_portfolio" in name or "space portfolio" in name or \
                ("space" in name and "adi" in name):
            return "adi_space"
        if "parametricsearch" in name.replace(" ", "") or "parametric" in name:
            return "adi_parametric"
        return "adi"          # legacy ADI catalog sheets
    if ext == ".pdf" and "qorvo" in name:
        return "qorvo"
    if ext == ".pdf" and ("slyt" in name or name.startswith("ti") or "space-products" in name
                          or "space_products" in name):
        return "ti"
    if ext == ".pdf":
        return None           # unknown PDF -> let the caller decide
    return None


def scan_dir(folder) -> list[tuple[Path, str | None]]:
    """Every ingestable file under `folder` (one level), with its guessed kind."""
    folder = Path(folder)
    out = []
    if not folder.is_dir():
        return out
    for p in sorted(folder.rglob("*")):
        if p.is_file() and p.suffix.lower() in (".pdf", ".xlsx", ".xlsm", ".csv"):
            out.append((p, classify_file(p)))
    return out


def _cancelled(cancel):
    """True when the caller has asked to stop.

    `cancel` is a threading.Event from the GUI but a plain callable from the CLI,
    and assuming one shape breaks the other -- calling an Event raised "'Event'
    object is not callable" and killed the whole datasheet-mining step. The rest
    of the codebase already tests for .is_set() first; this matches it so there is
    one convention rather than two."""
    try:
        if cancel is None:
            return False
        return bool(cancel.is_set() if hasattr(cancel, "is_set") else cancel())
    except Exception:
        return False


def _file_checkpoint_path():
    from .paths import CACHE_DIR
    return Path(CACHE_DIR) / "file_sources.json"


def _file_fingerprint(path):
    """Content hash + size for a catalog file, so an unchanged file is skipped.

    The everythingRF ingest has always had a checkpoint; the FILE-based ingests
    (Qorvo brochure, ADI xlsx, TI guide) had none at all -- `ingest(path,
    dry_run, verbose)` takes no resume argument -- so every rebuild re-parsed
    them in full regardless of the Resume checkbox. Hashing the file is enough:
    these are single documents, so there is no partial-progress problem to model,
    only "has this file changed since we last read it".
    """
    import hashlib
    p = Path(path)
    h = hashlib.sha1()
    try:
        st = p.stat()
        with p.open("rb") as fh:
            while True:
                chunk = fh.read(1 << 20)
                if not chunk:
                    break
                h.update(chunk)
    except OSError:
        return None
    return f"{st.st_size}:{h.hexdigest()[:16]}"


def _load_file_checkpoints():
    fp = _file_checkpoint_path()
    if fp.is_file():
        try:
            import json as _json
            data = _json.loads(fp.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    return {}


def _save_file_checkpoint(key, fingerprint, parts):
    fp = _file_checkpoint_path()
    try:
        import json as _json
        data = _load_file_checkpoints()
        data[key] = {"fingerprint": fingerprint, "parts": int(parts or 0),
                     "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(_json.dumps(data, indent=1), encoding="utf-8")
        return True
    except Exception as e:
        # Never swallow this. A checkpoint that fails to persist looks exactly
        # like "resume does not work", which is the bug being fixed here.
        print(f"  ! could not save file-source checkpoint: "
              f"{type(e).__name__}: {e}")
        return False


def _run(kind, path, progress, part=None, categories=None, cancel=None,
         resume=False) -> dict:
    import inspect
    module, label = _ADAPTERS[kind]
    ckey = f"{kind}|{Path(path).name}"
    fingerprint = _file_fingerprint(path) if resume else None
    if resume and fingerprint:
        prev = _load_file_checkpoints().get(ckey)
        if isinstance(prev, dict) and prev.get("fingerprint") == fingerprint:
            progress(f"SKIP unchanged source: {label}: {Path(path).name} "
                     f"({prev.get('parts', 0)} part(s) last time) -- Reset to "
                     f"force a re-parse")
            return {"parts": 0, "source_skipped": 1}
    progress(f"ingesting {label}: {Path(path).name} ...")
    kwargs = {"dry_run": False, "verbose": False}
    # Older adapters take only (path, dry_run, verbose); the newer ones also
    # report parts to the live table and honour the category filter. Pass what
    # each actually accepts rather than assuming a common signature.
    try:
        accepted = inspect.signature(module.ingest).parameters
    except (TypeError, ValueError):
        accepted = {}
    if "progress" in accepted:
        kwargs["progress"] = progress
    if "part" in accepted and part is not None:
        kwargs["part"] = part
    if "categories" in accepted and categories:
        kwargs["categories"] = categories
    if "cancel" in accepted and cancel is not None:
        kwargs["cancel"] = cancel
    counts = module.ingest(Path(path), **kwargs)
    progress(f"  {label}: {counts.get('parts', 0)} parts "
             f"({counts.get('space_qualified', 0)} qualified, "
             f"{counts.get('space_grade', 0)} grade)")
    if resume and fingerprint:
        _save_file_checkpoint(ckey, fingerprint, counts.get("parts", 0))
    return counts


def rebuild(erf_parent=None, source_files=(), source_dir=None,
            erf_glob="EverythingRFSpace*", dedupe=True, progress=None,
            vendors=None, vendor_rate=1.0, vendor_limits=None,
            download_datasheets=True, download_limit=0, adi_dir=None,
            use_cache=True, categories=None, reset=False,
            resume=True, reset_vendors_only=False, part=None, event=None,
            cancel=None, mine_datasheets=True) -> dict:
    """Ingest everything selected into partdb, then dedupe.

    Local sources (unchanged):
      erf_parent   folder holding the EverythingRFSpace* listing subfolders
      source_files explicit catalog files (kind auto-detected by name)
      source_dir   a folder to scan for catalog files
      dedupe       run partdb.merge_duplicates() at the end (richest specs win)

    Live vendor catalogues (new):
      vendors      subset of CATALOG_VENDORS to walk, e.g. ['qorvo','marki'].
                   None or [] walks none, so existing callers behave as before.
      vendor_rate  seconds between requests to the SAME host
      vendor_limits e.g. {'qorvo_ids': 40, 'marki_products': 200}
      adi_dir      folder of ADIParametricSearch*.xlsx (ADI is never scraped)
      use_cache    reuse pages already downloaded into partdb.DATA/vendor_cache
    """
    def emit(m):
        if progress:
            progress(m)

    summary = {"sources": [], "errors": [], "parts_ingested": 0}
    if reset:
        # The GUI/CLI pass vendor KEYS ('adi'), but parts are stored under
        # display NAMES ('Analog Devices'). Without translating, a scoped reset
        # matched nothing and silently deleted zero rows.
        scope = None
        if reset_vendors_only and vendors:
            scope = []
            for v in vendors:
                scope.append(v)
                name = vendor_catalogs.VENDORS.get(v, {}).get("name")
                if name and name not in scope:
                    scope.append(name)
        emit("RESET dataset | deleting parts, scrape state and caches"
             + (f" for {', '.join(scope)}" if scope else " (everything)"))
        removed = partdb.reset_dataset(vendors=scope, clear_cache=True,
                                       progress=emit)
        summary["reset"] = removed
        emit(f"RESET DONE | {removed['parts']} part(s) deleted, "
             f"{removed['scrape_log']} scrape-log row(s), "
             f"{removed['cache_files']} cached page(s), "
             f"{removed['datasheet_files']} datasheet file(s)")
        if event:
            try:
                event({"type": "reset", **removed})
            except Exception:
                pass
        # Resume state is meaningless immediately after a reset.
        resume = False

    files: list[tuple[Path, str | None]] = [(Path(f), classify_file(Path(f)))
                                            for f in source_files]
    if source_dir:
        files += scan_dir(source_dir)

    # everythingRF first (it carries the real manufacturer part numbers that
    # ADI/TI/Qorvo rows later dedupe against).
    if erf_parent:
        try:
            erf_path = Path(erf_parent)
            ingest_parent, ingest_glob = erf_path, erf_glob
            if erf_path.is_dir() and erf_path.name.lower().startswith("everythingrfspace"):
                ingest_parent, ingest_glob = erf_path.parent, erf_path.name
            emit(f"ingesting everythingRF space folders under {ingest_parent} ...")
            c = erf_space_ingest.ingest(
                ingest_parent, ingest_glob, dry_run=False, verbose=False,
                resume=bool(resume), progress=emit, part=part, cancel=cancel)
            n = c.get("parts", 0)
            summary["parts_ingested"] += n
            summary["sources"].append(("everythingRF", n, c))
            emit(f"  everythingRF: {n} parts "
                 f"({c.get('space_qualified', 0)} qualified, "
                 f"{c.get('space_grade', 0)} grade)")
        except Exception as e:                          # keep going on one bad source
            summary["errors"].append(f"everythingRF: {e}")
            emit(f"  everythingRF FAILED: {e}")
            traceback.print_exc()

    # Every HTML catalog under newSources is a trusted space source. Folder
    # names containing SpaceQual are qualified; names containing Space (but not
    # SpaceQual) are grade; all other newSources folders default to qualified.
    if source_dir:
        html_root = Path(source_dir)
        if html_root.is_dir() and any(html_root.rglob("*.htm*")):
            try:
                emit(f"ingesting trusted space HTML folders under {html_root} ...")
                c = erf_space_ingest.ingest(
                    html_root, "*", dry_run=False, verbose=False,
                    default_variant="space_qualified",
                    resume=bool(resume), progress=emit, part=part,
                    cancel=cancel)
                n = c.get("parts", 0)
                summary["parts_ingested"] += n
                summary["sources"].append(("newSources-html", n, c))
                emit(f"  newSources HTML: {n} parts "
                     f"({c.get('space_qualified', 0)} qualified, "
                     f"{c.get('space_grade', 0)} grade)")
            except Exception as e:
                summary["errors"].append(f"newSources HTML: {e}")
                emit(f"  newSources HTML FAILED: {e}")
                traceback.print_exc()

    for path, kind in files:
        if kind not in _ADAPTERS:
            summary["errors"].append(f"unrecognized source: {path.name}")
            emit(f"  skipped (unrecognized): {path.name}")
            continue
        try:
            c = _run(kind, path, emit, part=part, resume=bool(resume),
                             categories=categories, cancel=cancel)
            summary["parts_ingested"] += c.get("parts", 0)
            summary["sources"].append((kind, c.get("parts", 0), c))
        except Exception as e:
            summary["errors"].append(f"{path.name}: {e}")
            emit(f"  {path.name} FAILED: {e}")
            traceback.print_exc()

    # --- live vendor catalogues -------------------------------------------
    if vendors:
        emit("")
        try:
            vsum = vendor_catalogs.ingest(
                vendors=vendors, rate=vendor_rate, progress=emit,
                limits=vendor_limits, download=download_datasheets,
                download_limit=download_limit, adi_dir=adi_dir,
                use_cache=use_cache, categories=categories,
                resume=bool(resume), part=part, event=event, cancel=cancel)
            summary["vendors"] = vsum
            for v, info in vsum.get("per_vendor", {}).items():
                n = info.get("parts", 0)
                summary["parts_ingested"] += n
                summary["sources"].append((f"vendor:{v}", n, info))
            summary["errors"].extend(vsum.get("errors", []))
        except Exception as e:
            summary["errors"].append(f"vendor catalogs: {e}")
            emit(f"  vendor catalogs FAILED: {e}")
            traceback.print_exc()

    for line in capability_report():
        emit(line)
    emit("")

    # A short coverage report per vendor, so a missing spec is visible here rather
    # than being discovered later in the picker.
    try:
        conn = partdb.db()
        emit("")
        emit("RF spec coverage by vendor (after this run)")
        emit(f"  {'vendor':<20} {'parts':>6} {'freq':>6} {'gain':>6} {'NF':>6} "
             f"{'P1dB':>6} {'OIP3':>6} {'Psat':>6}")
        for row in conn.execute(
                "SELECT vendor, COUNT(*) n FROM parts GROUP BY vendor "
                "ORDER BY n DESC").fetchall():
            v = row["vendor"] or "(blank)"
            cells = []
            for key in ("freq_ghz", "gain_db", "nf_db", "p1db_dbm",
                        "oip3_dbm", "psat_dbm"):
                cells.append(conn.execute(
                    "SELECT COUNT(DISTINCT s.part_id) n FROM specs s "
                    "JOIN parts p ON p.id = s.part_id "
                    "WHERE p.vendor=? AND s.key=?",
                    (row["vendor"], key)).fetchone()["n"])
            emit(f"  {v[:20]:<20} {row['n']:>6} "
                 + " ".join(f"{c:>6}" for c in cells))
        emit("")
    except Exception as e:
        emit(f"  (coverage report unavailable: {e})")

    # ---- fill blanks from the datasheets already on disk ---------------------
    # Catalog listings only tabulate the handful of specs a vendor chose to put
    # in its comparison table. Everything else -- switching speed above all -- is
    # in the datasheet, and nothing in the rebuild path used to open one.
    if mine_datasheets:
        emit("mining local datasheets for specs the listings did not carry ...")
        try:
            from . import dsmine
            conn = partdb.db()
            rows = conn.execute("SELECT id, mpn FROM parts").fetchall()
            index = dsmine.build_index(say=emit)
            if index:
                cache, filled, added = {}, 0, 0
                for i, r in enumerate(rows, 1):
                    if _cancelled(cancel):
                        emit("  stop requested; leaving datasheet mining")
                        break
                    mined = dsmine.mine_part(r["mpn"], index, cache)
                    if mined:
                        partdb.put_specs(r["id"], mined)
                        filled += 1
                        added += len(mined)
                    if i % 500 == 0:
                        emit(f"  {i}/{len(rows)} part(s) checked, "
                             f"{filled} enriched")
                emit(f"  datasheet mining: {filled} part(s) gained "
                     f"{added} spec row(s) (stored at low confidence, so any "
                     f"catalog value still wins)")
                summary["datasheet_mining"] = {"parts": filled, "rows": added,
                                               "indexed": len(index)}
        except Exception as e:
            emit(f"  (datasheet mining unavailable: {e})")
            summary["errors"].append(f"datasheet mining: {e}")

    if dedupe:
        emit("de-duplicating (keeping the richest copy of each part) ...")
        d = partdb.merge_duplicates(progress=None)
        summary["dedupe"] = d
        emit(f"  merged {d['groups']} duplicate group(s), removed {d['deleted']} row(s)")

    summary["stats"] = partdb.dataset_stats()
    s = summary["stats"]
    emit(f"dataset now: {s['parts']} parts, {s['qualified']} space-qualified, "
         f"{s['grade']} space-grade, {s['vendors']} vendors")
    exported = partdb.export_parts_json(EXPORT_DIR)
    summary["json_export"] = exported
    emit(f"normalized JSON export: {exported['parts']} parts -> {exported['folder']}")
    return summary
