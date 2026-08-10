"""fetch_missing_datasheets.py — get datasheets we do not have yet.

    python fetch_missing_datasheets.py --dry-run
    python fetch_missing_datasheets.py
    python fetch_missing_datasheets.py --vendors marki qorvo --limit 200

WHAT IT DOES
    Finds parts that (a) come from Mini-Circuits, Marki, Qorvo or MACOM,
    (b) publish a datasheet URL, (c) have no local datasheet file yet, and
    (d) carry no space qualification or space grade -- then downloads those
    datasheets into the library so the enrichment pass can read them.

    The last filter is the point: a part already known to be space-qualified
    needs nothing from its datasheet. The parts worth fetching are the ones where
    nothing states a qualification, because their datasheets are the only place
    left that might say "MIL-PRF-38534", "hermetic", "screened to MIL-STD-883".

WHY A BROWSER
    Some of these vendors serve PDFs only to a real browser session. This
    attaches to YOUR Chrome over CDP and issues the request through it, so the
    download rides the session you already hold. It does not spoof a fingerprint,
    solve a challenge, or launch a hidden browser -- if a challenge appears it
    stops and hands control back to you.

    Files are written as BYTES, never as decoded text. Saving a PDF through a
    text path is what produced the ~900 corrupt Qorvo files already in the
    library, and those cannot be recovered by re-parsing -- only by re-download.

SETUP
    pip install playwright

    Chrome is launched automatically with a debug port and a dedicated profile.
    If one is already listening on the port, that session is used instead.
"""
from __future__ import annotations

import argparse
import os
import random
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from playwright.sync_api import sync_playwright
except ImportError:                                      # pragma: no cover
    sys.exit("Playwright not installed. Run:  pip install playwright")


def _opt(name):
    for pkg in ("pythonrfparts", "rfparts"):
        try:
            return __import__(f"{pkg}.{name}", fromlist=[name])
        except Exception:
            continue
    return None


partdb = _opt("partdb")
dsmine = _opt("dsmine")
if partdb is None:
    sys.exit("Could not import partdb. Run this from the folder that holds the "
             "package.")

# everythingRF is deliberately absent: it is an aggregator, its "datasheet"
# links point back at manufacturers, and its parts are covered by the vendors
# below wherever the manufacturer is one of them.
VENDOR_MATCH = {
    "minicircuits": "%mini%circuit%",
    "marki":        "%marki%",
    "qorvo":        "%qorvo%",
    "macom":        "%macom%",
}
VENDOR_FOLDER = {
    "minicircuits": "Mini-Circuits", "marki": "Marki-Microwave",
    "qorvo": "Qorvo", "macom": "MACOM",
}

CHROME_CANDIDATES = {
    "win32": [r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
              r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"],
    "darwin": ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"],
    "linux": ["google-chrome", "google-chrome-stable", "chromium"],
}


def loose(s):
    return re.sub(r"[^A-Z0-9]", "", str(s or "").upper())


def safe_name(mpn):
    """A filename dsmine will match back to this part.

    dsmine indexes on the loose form of the file STEM, so the stem has to stay
    recognisably the part number -- only characters a filesystem refuses are
    replaced."""
    return re.sub(r'[<>:"/\\|?*]', "_", str(mpn).strip())[:120]


# ------------------------------------------------------------------ targets
def find_targets(vendors, limit, include_qualified=False):
    """Parts worth fetching a datasheet for."""
    conn = partdb.db()
    index = dsmine.build_index() if dsmine else {}
    out, seen = [], set()
    for key in vendors:
        like = VENDOR_MATCH[key]
        rows = conn.execute(
            "SELECT p.id, p.mpn, p.vendor, p.category "
            "FROM parts p WHERE lower(p.vendor) LIKE ?", (like,)).fetchall()
        for r in rows:
            if r["id"] in seen:
                continue
            specs = {s["key"]: (s["value_text"] if s["value_text"] is not None
                                else s["value_typ"])
                     for s in conn.execute(
                         "SELECT key, value_text, value_typ FROM specs "
                         "WHERE part_id=?", (r["id"],))}
            # Already qualified: its datasheet cannot tell us anything we do not
            # already know, so it is not worth a request.
            if not include_qualified and (specs.get("space")
                                          or specs.get("space_variant")):
                continue
            url = str(specs.get("datasheet_url") or "").strip()
            if not url.startswith("http"):
                continue
            if index and loose(r["mpn"]) in index:
                continue                        # already have the file
            if specs.get("datasheet_file"):
                p = Path(str(specs["datasheet_file"]))
                if p.is_file():
                    continue
            seen.add(r["id"])
            out.append({"mpn": r["mpn"], "vendor": r["vendor"],
                        "vendor_key": key, "url": url,
                        "category": r["category"] or ""})
            if limit and len(out) >= limit:
                return out
    return out


# ------------------------------------------------------------------- chrome
def find_chrome(explicit=None):
    import shutil
    if explicit and Path(os.path.expandvars(explicit)).exists():
        return os.path.expandvars(explicit)
    env = os.environ.get("RFPARTS_CHROME", "").strip()
    if env and Path(os.path.expandvars(env)).exists():
        return os.path.expandvars(env)
    for cand in CHROME_CANDIDATES.get(sys.platform, CHROME_CANDIDATES["linux"]):
        expanded = os.path.expandvars(cand)
        if os.path.sep in expanded or expanded.endswith(".exe"):
            if Path(expanded).exists():
                return expanded
        elif shutil.which(expanded):
            return shutil.which(expanded)
    for name in ("chrome", "google-chrome", "chromium", "msedge"):
        if shutil.which(name):
            return shutil.which(name)
    return None


def port_open(endpoint, timeout=1.0):
    import socket
    import urllib.parse
    u = urllib.parse.urlparse(endpoint)
    try:
        with socket.create_connection((u.hostname or "localhost",
                                       u.port or 9222), timeout=timeout):
            return True
    except OSError:
        return False


def attach(p, endpoint, profile, chrome_path=None, wait=30):
    import subprocess
    import urllib.parse
    if not port_open(endpoint):
        exe = find_chrome(chrome_path)
        if not exe:
            sys.exit("Could not find Chrome. Pass --chrome with the full path.")
        port = urllib.parse.urlparse(endpoint).port or 9222
        prof = Path(os.path.expandvars(str(profile))).expanduser()
        prof.mkdir(parents=True, exist_ok=True)
        print(f"  launching {Path(exe).name} on port {port}")
        subprocess.Popen([exe, f"--remote-debugging-port={port}",
                          f"--user-data-dir={prof}", "--no-first-run",
                          "--no-default-browser-check", "about:blank"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(wait * 2):
            if port_open(endpoint):
                break
            time.sleep(0.5)
    browser = p.chromium.connect_over_cdp(endpoint)
    print(f"  CDP: {len(browser.contexts)} context(s)")
    ctx = browser.contexts[0] if browser.contexts else browser.new_context()
    try:
        print(f"       {len(ctx.pages)} page(s) in context 0: "
              + ", ".join((pg.url or "about:blank")[:40] for pg in ctx.pages[:3]))
    except Exception:
        pass
    # Without this a PDF link that Chrome decides to download is simply dropped.
    try:
        ctx.set_default_timeout(45000)
    except Exception:
        pass
    # Use a page we KNOW is live. Reusing whatever sits at index 0 can hand back
    # a tab that is closing or detached, and a goto on that silently does
    # nothing -- the address bar never moves and every fetch reports a failure.
    page = None
    for cand in list(ctx.pages):
        try:
            if not cand.is_closed():
                cand.bring_to_front()
                page = cand
                break
        except Exception:
            continue
    if page is None:
        page = ctx.new_page()
        print("       (opened a new tab)")
    return browser, ctx, page


CHALLENGE = ("cf-browser-verification", "just a moment", "checking your browser",
             "attention required", "captcha", "turnstile")


def looks_like_challenge(blob):
    head = blob[:4000].decode("utf-8", "replace").lower()
    return any(m in head for m in CHALLENGE)


def fetch_via_page(page, url, debug=False):
    """(bytes, status, reason) for a datasheet URL, driving the real page.

    Chrome handles a PDF link one of two ways: it renders it in the built-in
    viewer, or it downloads it and aborts the navigation. Both are normal, and a
    fetcher has to accept either -- treating the aborted navigation as an error
    is what makes every PDF look like a failure.
    """
    got = {}

    def _on_download(dl):
        try:
            got["path"] = dl.path()
            got["name"] = dl.suggested_filename
        except Exception as e:
            got["err"] = f"{type(e).__name__}: {e}"

    page.on("download", _on_download)
    resp = None
    nav_err = None
    try:
        resp = page.goto(url, wait_until="commit", timeout=45000)
    except Exception as e:
        nav_err = f"{type(e).__name__}: {str(e)[:80]}"
    # A download aborts the navigation, so give it a moment to land.
    for _ in range(20):
        if got.get("path"):
            break
        page.wait_for_timeout(150)
    try:
        page.remove_listener("download", _on_download)
    except Exception:
        pass

    if got.get("path"):
        try:
            return Path(got["path"]).read_bytes(), 200, "download"
        except OSError as e:
            return None, None, f"download unreadable: {e}"
    if resp is not None:
        status = resp.status
        try:
            body = resp.body()
        except Exception as e:
            # The viewer can consume the body; fall back to the rendered page.
            if debug:
                print(f"      body() failed ({type(e).__name__}); using content()")
            try:
                body = page.content().encode("utf-8", "replace")
            except Exception:
                body = b""
        if status != 200:
            return None, status, f"HTTP {status}"
        if not body:
            return None, status, "empty response"
        return body, status, "navigation"
    return None, None, nav_err or "no response"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--vendors", nargs="*", default=list(VENDOR_MATCH),
                    choices=list(VENDOR_MATCH))
    ap.add_argument("--limit", type=int, default=0, help="0 = no cap")
    ap.add_argument("--out", default=None,
                    help="datasheet library root (default: the one dsmine uses)")
    ap.add_argument("--delay", type=float, default=1.5,
                    help="base seconds between downloads (jitter added)")
    ap.add_argument("--endpoint", default="http://localhost:9222")
    ap.add_argument("--profile", default=None)
    ap.add_argument("--chrome", default=None)
    ap.add_argument("--test-url", default=None,
                    help="attach, navigate to this ONE url, report what happens")
    ap.add_argument("--debug", action="store_true",
                    help="print the reason for each failure")
    ap.add_argument("--dry-run", action="store_true",
                    help="list what would be fetched and stop")
    ap.add_argument("--include-qualified", action="store_true",
                    help="also fetch parts that already state a qualification")
    args = ap.parse_args(argv)

    roots = dsmine.default_roots() if dsmine else []
    out_root = Path(args.out) if args.out else (roots[0] if roots else
                                               Path("datasheets"))
    print(f"database : {partdb.DB_PATH}")
    print(f"library  : {out_root}")
    print(f"vendors  : {', '.join(args.vendors)}")

    if args.test_url:
        print(f"\nprobing one URL: {args.test_url}")
        with sync_playwright() as pw:
            _b, _c, page = attach(
                pw, args.endpoint,
                args.profile or (Path.home() / "rfparts-chrome"), args.chrome)
            print(f"  attached; page at {page.url[:70] or 'about:blank'}")
            blob, status, why = fetch_via_page(page, args.test_url, True)
            print(f"  result : {why}   status={status}   "
                  f"bytes={len(blob) if blob else 0}")
            try:
                print(f"  page is now at {page.url[:78]}")
            except Exception:
                pass
            if blob:
                print(f"  first bytes: {blob[:24]!r}")
        return 0

    targets = find_targets(args.vendors, args.limit, args.include_qualified)
    if not targets:
        print("\nNothing to fetch: every matching part either already has a "
              "datasheet\nor already states a qualification.")
        return 0
    from collections import Counter
    by_vendor = Counter(t["vendor_key"] for t in targets)
    print(f"\n{len(targets)} part(s) to fetch: "
          + ", ".join(f"{k} {n}" for k, n in by_vendor.most_common()))
    if args.dry_run:
        for t in targets[:25]:
            print(f"    {t['mpn']:<22} {t['vendor'][:16]:<16} {t['url'][:64]}")
        if len(targets) > 25:
            print(f"    ... {len(targets) - 25} more")
        print("\nDRY RUN -- nothing downloaded.")
        return 0

    saved = skipped = failed = challenged = 0
    with sync_playwright() as p:
        browser, ctx, page = attach(
            p, args.endpoint,
            args.profile or (Path.home() / "rfparts-chrome"), args.chrome)
        print(f"  attached to Chrome ({page.url[:60] or 'about:blank'})\n")
        for i, t in enumerate(targets, 1):
            folder = out_root / VENDOR_FOLDER.get(t["vendor_key"], t["vendor"])
            folder.mkdir(parents=True, exist_ok=True)
            stem = safe_name(t["mpn"])
            existing = list(folder.glob(f"{stem}.*"))
            if existing:
                skipped += 1
                continue
            # NAVIGATE, do not call the API context. ctx.request.get() issues a
            # bare HTTP call that never touches the page -- the address bar sits
            # on the start page, and over a CDP-attached browser it frequently
            # cannot reach the session at all, so every fetch fails. Driving the
            # page uses the whole browser stack, sends a real Referer, and lets
            # you watch it work.
            blob, status, why = fetch_via_page(page, t["url"], args.debug)
            if args.debug:
                try:
                    print(f"      page is now at {page.url[:78]}")
                except Exception:
                    pass
            if blob is None:
                failed += 1
                print(f"  [{i}/{len(targets)}] {t['mpn']:<20} FAIL {why}")
                time.sleep(args.delay)
                continue
            if looks_like_challenge(blob):
                challenged += 1
                print(f"  [{i}/{len(targets)}] {t['mpn']:<20} CHALLENGE PAGE")
                input("      Open the site in that Chrome window, clear the "
                      "check, then press Enter to carry on... ")
                continue
            # Trust the BYTES, not the URL or the content type: a .pdf link that
            # returns HTML is an error page, and writing it as a datasheet is how
            # the corrupt files already in the library got there.
            if blob[:5] == b"%PDF-":
                ext = ".pdf"
            elif blob[:4] in (b"PK\x03\x04",):
                ext = ".zip"
            elif b"<html" in blob[:1000].lower():
                # Marki really do serve HTML datasheets, so HTML cannot be
                # rejected outright -- but a few hundred bytes of it is an error
                # page, and writing that into the library would have enrichment
                # mine "404 Not Found" for qualification wording.
                if len(blob) < 2000:
                    failed += 1
                    print(f"  [{i}/{len(targets)}] {t['mpn']:<20} "
                          f"tiny HTML ({len(blob)} B) -- error page, not saved")
                    time.sleep(args.delay)
                    continue
                ext = ".html"
            else:
                ext = Path(t["url"]).suffix.lower() or ".bin"
            dest = folder / f"{stem}{ext}"
            try:
                dest.write_bytes(blob)          # bytes, never text
            except OSError as e:
                failed += 1
                print(f"  [{i}/{len(targets)}] {t['mpn']:<20} WRITE FAIL {e}")
                continue
            saved += 1
            print(f"  [{i}/{len(targets)}] {t['mpn']:<20} {len(blob):>9,} B "
                  f"-> {dest.name}")
            time.sleep(args.delay + random.uniform(0.2, 1.2))

    print(f"\nsaved {saved}, already had {skipped}, failed {failed}"
          + (f", {challenged} challenged" if challenged else ""))
    if saved:
        print("\nNext: delete Data\\cache\\datasheet_mining.json, then rebuild "
              "with Enrich ticked\nso these are mined for qualification wording.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
