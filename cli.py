"""rfparts command-line interface.

Subcommands:
  search   crawl vendors for a category + criteria, rank, write a report
  report   re-render the last saved results as markdown
  rfq      draft an RFQ email for a given vendor from the last results
  vendors  list vendors (optionally filtered by category)
  gui      launch the desktop GUI (all specs entered in the form)

Everything runs locally. The only network traffic is polite, robots.txt-obeying
GETs to the vendor sites in the registry. Mini-Circuits is catalog-backed and is
never fetched live here; its S-parameter port counts are overlaid from the
standalone background scanner cache (see minicircuits_cache).
"""
import argparse
import json
import re
import sys
import threading
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

from . import discover, extract, fetch, minicircuits_cache, rank, registry, rfq, spec, specstore, timing, websearch

# v2 modules (partdb-backed candidates + background crawl). Optional: the
# pipeline still runs if they're absent.
try:
    from . import crawler as _crawler, partdb as _partdb
except ImportError:
    _crawler = _partdb = None

CRAWL_IN_BACKGROUND = os.environ.get("RFPARTS_CRAWL", "1") not in ("", "0")

CRAWL_WAIT = float(os.environ.get("RFPARTS_CRAWL_WAIT", "20"))
from .registry import DATA, load_vendors, vendors_for

RESULTS = DATA / "results.json"
MAX_WORKERS = 12

# How many stored parts to pull from partdb per search. query_candidates caps
# the returned set AND over-fetches by last_seen, so a small cap silently drops
# older parts in any category that has more than the cap — they'd still show in
# the inspector (direct SQL) but never reach ranking or the GUI. Generous by
# default; override with RFPARTS_DB_LIMIT.
DB_CANDIDATE_LIMIT = int(os.environ.get("RFPARTS_DB_LIMIT", "5000"))

# The four S-parameter-derived fields owned exclusively by the background cache.
_MC_SNP_FIELDS = ("ports", "ports_source", "sparams_url", "sparams_filename")

# Keys a catalog/api record may use for the manufacturer part number.
_MPN_KEYS = ("pn", "model", "model_name", "modelName", "part_number",
             "partNumber", "sku")


def _norm_mpn(value):
    """Loose part-number key: uppercase, strip punctuation, keep the trailing
    '+' (Mini-Circuits RoHS marker). 'ZX60-P33+' == 'zx60 p33 +'."""
    if value is None:
        return ""
    s = str(value).upper().strip().replace("+", "\u0001")
    s = re.sub(r"[^A-Z0-9]", "", s).replace("\u0001", "+")
    return s


def _norm_vendor(value):
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _mpn_of(candidate):
    """Best-effort manufacturer part number for a candidate."""
    rec = candidate.get("_api_record") or candidate.get("_catalog_record")
    if isinstance(rec, dict):
        # DigiKey record uses ManufacturerProductNumber; catalogs use pn/model.
        for k in ("ManufacturerProductNumber", "ManufacturerPartNumber") + _MPN_KEYS:
            v = rec.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    # Fall back to a part-number-shaped token (letters+digits) from the title.
    for tok in re.findall(r"[A-Za-z0-9][A-Za-z0-9._/\-]*\+?", candidate.get("title", "")):
        if any(c.isalpha() for c in tok) and any(c.isdigit() for c in tok) and len(tok) >= 3:
            return tok
    return ""


def _dedupe_digikey(cands):
    """Drop a DigiKey candidate when its manufacturer maps to a registry vendor
    AND its part number matches a part that native vendor already supplied.

    Before dropping, any spec DigiKey has that the native listing is missing
    (price, stock, datasheet, dimensions, parametrics, ...) is merged INTO the
    native part — so the native row keeps its identity but gains DigiKey's extra
    data. Returns (kept_list, dropped_count).
    """
    vendor_keys = {_norm_vendor(v["name"]) for v in load_vendors()
                   if v["name"] != "Digi-Key"}
    native_by_mpn = {}  # normalized MPN -> list of native candidate dicts
    for c in cands:
        if c.get("vendor") == "Digi-Key":
            continue
        key = _norm_mpn(_mpn_of(c))
        if key:
            native_by_mpn.setdefault(key, []).append(c)

    kept, dropped, merged = [], 0, 0
    for c in cands:
        if c.get("vendor") == "Digi-Key":
            mpn = _norm_mpn(_mpn_of(c))
            mfr = _norm_vendor(c.get("specs", {}).get("manufacturer", ""))
            natives = native_by_mpn.get(mpn) if mpn else None
            if natives:
                native_vendors = {_norm_vendor(n.get("vendor", "")) for n in natives}
                mfr_is_vendor = bool(mfr) and any(
                    mfr == vk or mfr in vk or vk in mfr for vk in vendor_keys)
                if mfr_is_vendor or mfr in native_vendors:
                    dk_specs = c.get("specs", {}) or {}
                    for n in natives:
                        nspecs = n.get("specs")
                        if not isinstance(nspecs, dict):
                            nspecs = {}
                            n["specs"] = nspecs
                        for k, v in dk_specs.items():
                            if k.startswith("_") or v is None:
                                continue
                            if nspecs.get(k) is None:  # native lacks it -> take DigiKey's
                                nspecs[k] = v
                                merged += 1
                    dropped += 1
                    continue
        kept.append(c)
    if merged:
        timing.tlog(f"  merged {merged} spec value(s) from dropped DigiKey rows into native parts")
    return kept, dropped


def _subcategory_filter(cands, query):
    """Drop candidates that clearly belong to a *sibling* subcategory.

    When a subcategory is chosen (e.g. amplifier -> LNA), a candidate whose
    title/text names a different subcategory (e.g. "high power amplifier") and
    does NOT name the selected one is dropped. Candidates that name the selected
    subcategory are always kept; ambiguous ones (naming neither) are kept too, so
    this only removes clear mismatches. Returns (kept, dropped).
    """
    cat = query.get("category")
    sub = query.get("subcategory")
    if not (cat and sub):
        return cands, 0
    own = [t.lower() for t in (query.get("subcategory_terms") or [])
           or registry.subcategory_terms(cat, sub)]
    excl = registry.subcategory_exclusion_terms(cat, sub)
    if not excl:
        return cands, 0
    kept, dropped = [], 0
    for c in cands:
        blob = f"{c.get('title', '')} {c.get('text', '')}".lower()
        if any(t in blob for t in own):        # explicitly the requested subcat
            kept.append(c)
            continue
        # Model/spec-based class beats free-text for amplifiers: this is what
        # catches HPA-/ZHL-/ZVE-/LZY- power amps in an LNA search (they carry no
        # "low noise" text, so the sibling-term check below can't see them).
        if cat == "amplifier":
            model = _mpn_of(c) or c.get("title", "")
            if registry.amp_subcat_conflict(sub, model, c.get("specs", {})):
                dropped += 1
                continue
        if any(t in blob for t in excl):       # explicitly a sibling subcat
            dropped += 1
        else:                                   # names neither -> keep (ambiguous)
            kept.append(c)
    return kept, dropped


def _force_utf8():
    """Windows consoles default to cp1252, which can't encode ✓/✗/– in reports.

    Reconfigure stdout/stderr to UTF-8 so printing never crashes. File writes
    below pass encoding='utf-8' explicitly for the same reason.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _apply_background_specs(candidate, specs):
    """Overlay the standalone Mini-Circuits background cache onto ``specs``.

    For Mini-Circuits candidates this returns a *copy* of ``specs`` with the four
    S-parameter-derived fields removed and then re-merged from
    ``minicircuits_cache.cached_for`` (so a freshly written background scan shows
    up even when the general spec cache is stale). Non-Mini-Circuits candidates
    are returned unchanged.
    """
    if candidate.get("vendor") != "Mini-Circuits":
        return specs
    merged = dict(specs) if isinstance(specs, dict) else {}
    for key in _MC_SNP_FIELDS:
        merged.pop(key, None)
    for key, value in minicircuits_cache.cached_for(candidate).items():
        if value is not None:
            merged[key] = value
    return merged


def _enrich(candidate):
    specs = extract.extract_specs(candidate)
    candidate["specs"] = _apply_background_specs(candidate, specs)
    return candidate


def _has_strong(ranked):
    """True when at least one ranked option is a fully/partly verified fit (tier A or B)."""
    return any(c.get("tier") in ("A", "B") for c in ranked)


def web_fallback(query, ranked, cancel=None):
    """Return web-search link candidates, but only when the databases produced no
    strong (tier A/B) match. Returns [] otherwise or on any backend failure.

    These are unverified links (a from-scratch web search via the configured
    backend), never mixed into the ranked database results.
    """
    if _has_strong(ranked):
        return []
    return websearch.web_fallback(query, cancel=cancel)


def _partdb_candidates(query):
    """Stored crawl results as ranked-ready candidates ([] if partdb absent)."""
    if _partdb is None:
        return []
    try:
        fg = query.get("freq_ghz") or (None, None)
        cands = _partdb.query_candidates(
            category=query.get("category"), f_lo=fg[0], f_hi=fg[1],
            limit=DB_CANDIDATE_LIMIT)
    except Exception:
        return []
    exclude = {_norm_vendor(v) for v in (query.get("exclude_vendors") or [])}
    for c in cands:
        c.setdefault("category", query.get("category"))
        c["_partdb"] = True
    if exclude:
        # The vendor picker can now list DB-only vendors, so excluding one must
        # actually drop its stored parts (vendors_for only filters the registry).
        # Match on the normalized name so "Mini-Circuits" also drops the ingest's
        # "Mini Circuits" spelling.
        cands = [c for c in cands if _norm_vendor(c.get("vendor")) not in exclude]
    return cands


def db_vendors():
    """Distinct vendor names present in partdb ([] if partdb is unavailable)."""
    if _partdb is None:
        return []
    try:
        return _partdb.distinct_vendors()
    except Exception:
        return []


def vendor_choices():
    """Union of registry and database vendor names for the GUI picker.

    A DB vendor whose normalized name matches a registry vendor is folded into
    the registry's canonical spelling, so an ingest's "Mini Circuits" doesn't
    double up with the registry's "Mini-Circuits". Genuinely new vendors from the
    data (e.g. manufacturers pulled in by an everythingRF ingest) are appended.
    """
    reg = [v["name"] for v in load_vendors()]
    by_norm = {_norm_vendor(n): n for n in reg}
    out = list(reg)
    for n in db_vendors():
        if not n:
            continue
        key = _norm_vendor(n)
        if key not in by_norm:
            by_norm[key] = n
            out.append(n)
    return sorted(out, key=str.lower)


def _crawl_spec(query):
    """Map the GUI/CLI query dict onto the crawler's requirement-spec dict."""
    return {
        "category": query.get("category"),
        "freq_ghz": query.get("freq_ghz"),
        "nf_db_max": query.get("nf_db_max"),
        "gain_db_min": query.get("gain_db_min"),
        "attenuation_db": query.get("attenuation_db"),
        "connector": query.get("connector"),
        "space": bool(query.get("space")),
        "prefer": query.get("prefer_vendors"),
    }


def _start_background_crawl(query, cancel, sink, on_result=None):
    """Launch crawler.crawl on a daemon thread; each ingested part is converted
    to a candidate and appended to `sink` (a thread-safe list.append is enough).

    Parts that land before ranking join THIS run; everything else persists in
    partdb and appears instantly in the next search. Returns the Thread or None.
    """
    if _crawler is None or not CRAWL_IN_BACKGROUND:
        return None

    def on_part(part_id, mpn, vendor):
        try:
            c = _partdb.candidate_by_mpn(mpn, vendor)
            # Stream only parts matching this search's category (unknown is
            # allowed through so page-thin parts still show); off-category
            # finds are stored for their own future searches, not shown here.
            if c is not None and c.get("category") not in (
                    query.get("category"), None, ""):
                c = None
            if c is not None:
                c["_partdb"] = True
                c.setdefault("category", query.get("category"))
                sink.append(c)
                    # Live-stream into the GUI table IMMEDIATELY — including
                    # after ranking finishes. The crawl thread outlives
                    # run_search by design; without this, everything it found
                    # post-ranking was invisible until the next search, which
                    # read as "crawling well after N ranked options" with a
                    # table that never moved.
                if on_result:
                    try:
                        rank.evaluate(c, query)
                    except Exception:
                        pass
                    on_result(c)
        except Exception:
            pass

    def run():
        try:
            _crawler.crawl(_crawl_spec(query), on_part=on_part, cancel=cancel)
        except Exception as e:
            fetch.log(f"background crawl failed: {type(e).__name__}: {e}")

    t = threading.Thread(target=run, daemon=True, name="rfparts-crawl")
    t.start()
    return t


def run_search(query, progress=None, cancel=None, tick=None, on_result=None):
    """Core search: discover -> extract -> de-dupe -> rank. Returns (ranked, errors).

    Pure (no printing). `progress` is a status-string callback; `cancel` is an
    optional threading.Event that stops the run early; `tick` is callback(done,
    total) fired as extraction advances; `on_result` is callback(candidate) fired
    once per enriched candidate (cached or freshly mined) so a UI can stream rows
    into its table before the final ranking. Extracted specs are cached per vendor
    (specstore), so re-runs only fetch/mine URLs not seen before.
    """
    def emit(msg):
        if progress:
            progress(msg)

    def cancelled():
        return cancel is not None and cancel.is_set()

    timing.reset()
    _run_t0 = time.perf_counter()
    timing.tlog(f"=== search start: {query.get('category')} "
                f"(RFPARTS_PDF={'off' if not extract.PARSE_PDF else 'on'}) ===")

    # Pick up any fresh background-scanner results before overlaying them below.
    minicircuits_cache.reload_if_changed()

    # Local-database mode: no web discovery, no live extraction, no background
    # crawl. Parts come from partdb (previous crawls + ingested catalogs). The
    # ONLY online source still allowed is the DigiKey API (strategy 'api'); its
    # results remain an in-memory overlay and are never written to partdb.
    local_only = bool(query.get("local_only"))

    vendors = vendors_for(
        query["category"],
        prefer=query.get("prefer_vendors"),
        exclude=query.get("exclude_vendors"))
    if local_only:
        # Besides the stored DB, local mode allows only network-free sources:
        # 'catalog' (on-disk vendor catalogs like Mini-Circuits) and 'api'
        # (DigiKey, an in-memory overlay). No sitemap/search crawling, no live
        # page/datasheet extraction.
        vendors = [v for v in vendors if v.get("strategy") in ("api", "catalog")]
    if not vendors and not local_only:
        emit("no vendors carry that category")
        return [], []

    if local_only:
        extra = []
        if any(v.get("strategy") == "catalog" for v in vendors):
            extra.append("offline catalogs")
        if any(v.get("strategy") == "api" for v in vendors):
            extra.append("DigiKey API")
        emit("local database mode — stored parts"
             + (" + " + " + ".join(extra) if extra else "") + " (no crawling)")
    else:
        emit(f"searching {len(vendors)} vendors for {query['category']} ...")

    # 0) stored crawl results answer instantly, and a background crawl starts
    # hunting for new parts online while the registry vendors are processed.
    db_cands = _partdb_candidates(query)
    if db_cands:
        emit(f"{len(db_cands)} stored part(s) from previous crawls")
        if on_result:
            for c in db_cands:
                on_result(c)
    crawl_sink = []
    crawl_thread = None if local_only else _start_background_crawl(
        query, cancel, crawl_sink, on_result=on_result)
    if crawl_thread is not None:
        emit("background crawl started (new finds join this run for up to "
             f"{CRAWL_WAIT:.0f}s, then land in the database for next time)")

    # 1) discover candidate product pages per vendor (parallel across vendors)
    all_cands, errors = [], []
    _disc_t0 = time.perf_counter()
    ex = ThreadPoolExecutor(max_workers=MAX_WORKERS)
    dfut = {ex.submit(discover.candidates, v, query): v for v in vendors}
    try:
        for fut in as_completed(dfut):
            if cancelled():
                break
            v = dfut[fut]
            cands = fut.result()
            for c in cands:
                c["vendor"] = v["name"]
                c.setdefault("category", query.get("category"))
            timing.tlog(f"  discover {v['name']:<22} {len(cands):5d} candidates "
                        f"[{v.get('strategy', 'sitemap')}]")
            if cands:
                all_cands += cands
            else:
                errors.append(v["name"])
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    timing.tlog(f"  discovery total: {time.perf_counter() - _disc_t0:.1f}s")
    for v in vendors:
        if v.get("strategy") == "api" and v["name"] in errors:
            cid = os.environ.get("DIGIKEY_CLIENT_ID", "").strip()
            sec = os.environ.get("DIGIKEY_CLIENT_SECRET", "").strip()
            if not (cid and sec):
                emit(f"{v['name']}: skipped — DIGIKEY_CLIENT_ID/SECRET not visible "
                     "to THIS process (env vars set in another shell don't carry "
                     "over; set them before launching, or as User variables)")
            else:
                emit(f"{v['name']}: credentials present but 0 results — run "
                     "debug_digikey.py for the exact HTTP failure")
    if cancelled():
        emit("cancelled")
        return [], []

    # de-dupe by URL
    seen = set()
    all_cands = [c for c in all_cands if not (c["url"] in seen or seen.add(c["url"]))]
    total = len(all_cands)

    # 2) extract, using a per-vendor specs cache; only mine URLs not seen before
    stores = {}

    def store_for(name):
        if name not in stores:
            stores[name] = specstore.load(name)
        return stores[name]

    cached, todo = [], []
    for c in all_cands:
        hit = specstore.get(store_for(c.get("vendor", "?")), c["url"])
        if hit is not None:
            # Always overlay the current background cache after a specstore hit so
            # newly scanned Mini-Circuits S-parameter data is visible even when
            # the general spec cache entry predates it.
            c["specs"] = _apply_background_specs(c, hit)
            cached.append(c)
        else:
            todo.append(c)

    done = len(cached)
    # Breakdown of what actually has to be mined (this is what costs time): new
    # candidates split into offline catalog parts vs live parts that hit the
    # network (and possibly parse a datasheet PDF).
    from collections import Counter
    todo_by_vendor = Counter(c.get("vendor", "?") for c in todo)
    live_todo = sum(1 for c in todo if not c.get("_catalog"))
    timing.tlog(f"  to extract: {len(todo)} new ({live_todo} live/network, "
                f"{len(todo) - live_todo} catalog/offline), {len(cached)} cached")
    for v, n in todo_by_vendor.most_common():
        timing.tlog(f"    new {v:<22} {n}")
    emit(f"{total} candidate pages ({done} cached, {len(todo)} new); extracting ...")
    if tick and total:
        tick(done, total)
    if on_result:
        for c in cached:
            on_result(c)

    enriched = list(cached)
    _ext_t0 = time.perf_counter()
    ex = ThreadPoolExecutor(max_workers=MAX_WORKERS)
    efut = [ex.submit(_enrich, c) for c in todo]
    try:
        for fut in as_completed(efut):
            if cancelled():
                break
            c = fut.result()
            enriched.append(c)
            if not c["specs"].get("_error"):
                specstore.put(store_for(c.get("vendor", "?")), c["url"], c["specs"])
                if on_result:
                    on_result(c)
            done += 1
            if timing.enabled() and done % 100 == 0:
                rate = (time.perf_counter() - _ext_t0) / max(1, done - len(cached))
                timing.tlog(f"  ...extracted {done}/{total}  "
                            f"({rate * 1000:.0f} ms/new-part avg)")
            if tick:
                tick(done, total)
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    timing.tlog(f"  extraction total: {time.perf_counter() - _ext_t0:.1f}s "
                f"for {len(todo)} new parts")

    for name, st in stores.items():
        specstore.save(name, st)

    if cancelled():
        emit("cancelled")
        return [], []

    # In local mode the stored parts ARE the primary result set, so fold them in
    # now (before de-dup) rather than in the post-rank merge below — this lets a
    # DigiKey overlay row that duplicates a stored native part collapse into it.
    if local_only and db_cands:
        enriched = db_cands + enriched

    # Drop obvious sibling-subcategory mismatches (e.g. a high-power amplifier in
    # an LNA search) so a chosen subcategory actually narrows the results.
    enriched, sub_dropped = _subcategory_filter(enriched, query)
    if sub_dropped:
        emit(f"filtered {sub_dropped} part(s) belonging to a different {query.get('subcategory')} sibling")
        timing.tlog(f"  subcategory filter dropped {sub_dropped} sibling mismatch(es)")

    # De-dupe: drop DigiKey rows that merely re-list a native vendor's own part
    # (same manufacturer + part number). Keep the native listing.
    enriched, dk_dropped = _dedupe_digikey(enriched)
    if dk_dropped:
        emit(f"de-duped {dk_dropped} DigiKey row(s) already covered by a native vendor")

    # 2b) merge stored-crawl candidates, then give the background crawl a short
    # grace window so fast finds get ranked in this run. URL-level de-dupe keeps
    # a part that both the registry and the crawler found from appearing twice.
    if crawl_thread is not None and not cancelled():
        deadline = time.perf_counter() + CRAWL_WAIT
        while crawl_thread.is_alive() and time.perf_counter() < deadline:
            if cancelled():
                break
            time.sleep(0.5)
    have_urls = {c.get("url") for c in enriched}
    fresh = 0
    for c in (list(crawl_sink) if local_only else db_cands + list(crawl_sink)):
        if c.get("url") not in have_urls:
            have_urls.add(c.get("url"))
            enriched.append(c)
            fresh += 1
    if crawl_sink:
        emit(f"background crawl contributed {len(crawl_sink)} new part(s) this run")
    timing.tlog(f"  partdb/crawl merged {fresh} candidate(s)")

    # 3) rank
    ranked = rank.rank(enriched, query)
    timing.tlog(f"=== search done in {time.perf_counter() - _run_t0:.1f}s — "
                f"{len(ranked)} ranked options ===")
    timing.print_summary(delay=fetch.DEFAULT_DELAY)
    emit(f"done — {len(ranked)} ranked options")
    return ranked, errors


def persist(query, ranked, errors):
    """Save results.json (for `report`/`rfq`) and results.md. UTF-8 for ✓/✗ marks."""
    RESULTS.write_text(
        json.dumps({"spec": query, "results": ranked, "errors": errors}, indent=1),
        encoding="utf-8")
    md = rank.markdown(ranked, query, errors)
    (DATA / "results.md").write_text(md, encoding="utf-8")
    return md


def cmd_search(args):
    query = spec.build(args)
    if not query.get("category"):
        print("error: category is required for search", file=sys.stderr)
        return 2
    ranked, errors = run_search(query, progress=lambda m: print(m, file=sys.stderr))
    persist(query, ranked, errors)
    print(rank.markdown(ranked[: args.top], query, errors if args.show_errors else None))
    web = None if query.get("local_only") else web_fallback(query, ranked)
    if web:
        print("\nWeb results (unverified — no strong database match):")
        for c in web:
            tag = " [sNp]" if c.get("snp") else ""
            print(f"  {c['source']:<22} {c['title'][:70]}{tag}")
            print(f"    {c['url']}")
    print(f"\nfull report: {DATA / 'results.md'}  ({len(ranked)} ranked)", file=sys.stderr)
    return 0


def _load_results():
    if not RESULTS.exists():
        print("no saved results — run `rfparts search` first", file=sys.stderr)
        return None
    return json.loads(RESULTS.read_text(encoding="utf-8"))


def cmd_report(args):
    data = _load_results()
    if not data:
        return 1
    results = data["results"][: args.top] if args.top else data["results"]
    print(rank.markdown(results, data["spec"], data.get("errors")))
    return 0


def cmd_rfq(args):
    data = _load_results()
    if not data:
        return 1
    vendors = {v["name"]: v for v in load_vendors()}
    vendor = vendors.get(args.vendor)
    if not vendor:  # allow matching by domain too
        for v in vendors.values():
            if urlparse(v["url"]).netloc.removeprefix("www.") == args.vendor.removeprefix("www."):
                vendor = v
                break
    if not vendor:
        print(f"unknown vendor: {args.vendor}", file=sys.stderr)
        return 1
    parts = [c for c in data["results"] if c.get("vendor") == vendor["name"]][: args.parts]
    to, subject, body = rfq.draft(vendor, parts, data["spec"])
    print(f"To: {to}\nSubject: {subject}\n\n{body}")
    return 0


def cmd_vendors(args):
    vs = vendors_for(args.category) if args.category else load_vendors()
    for v in vs:
        cats = ",".join(v.get("categories", []))
        print(f"{v['name']:<24} {v.get('strategy', 'sitemap'):<8} {v['url']}  [{cats}]")
    return 0


def cmd_gui(args):
    try:
        from . import gui
    except Exception as e:  # tkinter missing on stripped-down builds
        print(f"could not start GUI: {e}", file=sys.stderr)
        print("This Python may lack tkinter. Use the CLI, or ask IT to enable tkinter.",
              file=sys.stderr)
        return 1
    return gui.main()


def build_parser():
    p = argparse.ArgumentParser(prog="rfparts", description="Curated RF parts finder")
    p.add_argument("-v", "--verbose", action="store_true", help="log fetch activity to stderr")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search", help="crawl vendors and rank options")
    s.add_argument("category", help="canonical category, e.g. attenuator, lna, filter")
    s.add_argument("--freq", help="passband in GHz, e.g. '4-8' or 'DC-18'")
    s.add_argument("--temp-k", dest="temp_k", type=float, help="operating temperature (K)")
    s.add_argument("--gain-db-min", dest="gain_db_min", type=float)
    s.add_argument("--noise-k-max", dest="noise_k_max", type=float)
    s.add_argument("--attenuation-db", dest="attenuation_db", type=float)
    s.add_argument("--impedance", help="characteristic impedance in ohms, e.g. 50, 75, 33")
    s.add_argument("--ports", type=int, help="number of ports/throws/ways, e.g. 2 for SPDT")
    s.add_argument("--connector", help="required connector family, e.g. SMA")
    s.add_argument("--mount", help="e.g. bulkhead")
    s.add_argument("--package", help="interface/package: connectorized, smt, die, flange")
    s.add_argument("--space", action="store_true",
                   help="require space qualification (ranks hi-rel/unknown as 'needs review')")
    s.add_argument("--local-only", "--local", dest="local_only", action="store_true",
                   help="search only the local database (+ DigiKey API); "
                        "no web discovery, extraction, or crawling")
    s.add_argument("--max-lead-weeks", dest="max_lead_weeks", type=float)
    s.add_argument("--prefer", nargs="+", help="vendor names to try first")
    s.add_argument("--exclude", nargs="+", help="vendor names to skip")
    s.add_argument("--other", nargs="+", help="freeform criteria (materials, non-magnetic, ...)")
    s.add_argument("--top", type=int, default=10, help="rows to print (full report always saved)")
    s.add_argument("--show-errors", action="store_true")
    s.set_defaults(func=cmd_search)

    r = sub.add_parser("report", help="re-render last saved results")
    r.add_argument("--top", type=int, default=0, help="0 = all")
    r.set_defaults(func=cmd_report)

    q = sub.add_parser("rfq", help="draft an RFQ email for a vendor")
    q.add_argument("vendor", help="vendor name (or domain) from the last results")
    q.add_argument("--parts", type=int, default=5, help="max parts to list")
    q.set_defaults(func=cmd_rfq)

    v = sub.add_parser("vendors", help="list registry vendors")
    v.add_argument("category", nargs="?", help="filter by category")
    v.set_defaults(func=cmd_vendors)

    g = sub.add_parser("gui", help="launch the desktop GUI")
    g.set_defaults(func=cmd_gui)
    return p


def main(argv=None):
    _force_utf8()
    args = build_parser().parse_args(argv)
    if getattr(args, "verbose", False):
        fetch.VERBOSE = True
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
