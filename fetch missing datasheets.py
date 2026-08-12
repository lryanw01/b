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
    # ADI's datasheet URLs come from the parametric export, which states them
    # outright. Hittite is matched too: the parts were folded into ADI but the
    # vendor string on older rows still says Hittite.
    "adi": "%analog devices%",
    "hittite": "%hittite%",
    "marki":        "%marki%",
    "qorvo":        "%qorvo%",
    "macom":        "%macom%",
}
VENDOR_FOLDER = {
    "minicircuits": "Mini-Circuits", "marki": "Marki-Microwave",
    "qorvo": "Qorvo", "macom": "MACOM",
    "adi": "Analog-Devices", "hittite": "Analog-Devices",
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
def find_targets(vendors, limit, include_qualified=False, state=None,
                 retry_dead=False):
    """Parts worth fetching a datasheet for."""
    conn = partdb.db()
    dead = 0
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
            have = index.get(loose(r["mpn"])) if index else None
            if have is not None:
                # A file we cannot read is not a datasheet we have. The Qorvo
                # library is ~900 PDFs mangled by a text-mode save; they carry
                # the right names, so counting them as present is what made
                # "nothing to fetch" the answer for the vendor with the worst
                # coverage. Corrupt or empty files are treated as missing and
                # re-fetched, which also repairs them.
                bad = False
                try:
                    if dsmine and dsmine._sniff(have) == "corrupt":
                        bad = True
                    elif Path(have).stat().st_size < 1024:
                        bad = True
                except OSError:
                    bad = True
                if not bad:
                    continue
            if specs.get("datasheet_file"):
                p = Path(str(specs["datasheet_file"]))
                if p.is_file():
                    continue
            if state is not None and not retry_dead and is_dead(state,
                                                                r["mpn"], url):
                dead += 1
                continue
            seen.add(r["id"])
            out.append({"mpn": r["mpn"], "vendor": r["vendor"],
                        "vendor_key": key, "url": url,
                        "category": r["category"] or ""})
            if limit and len(out) >= limit:
                return out, dead
    return out, dead


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


_JS_FETCH = """
async (u) => {
  try {
    const r = await fetch(u, {credentials: 'include', redirect: 'follow'});
    const buf = await r.arrayBuffer();
    const bytes = new Uint8Array(buf);
    let bin = '';
    const CH = 0x8000;
    for (let i = 0; i < bytes.length; i += CH) {
      bin += String.fromCharCode.apply(null, bytes.subarray(i, i + CH));
    }
    return {status: r.status, data: btoa(bin)};
  } catch (e) {
    return {status: 0, data: null, error: String(e)};
  }
}
"""


# ------------------------------------------------------------------- state
# Outcomes are remembered PER PART, not as a position in the list. A high-water
# mark breaks the moment the target list changes -- one new part shifts every
# index after it, and the run either re-attempts hundreds of dead URLs or skips
# live ones it has never tried. Keyed on the part and its URL, the record stays
# correct however the list is reordered, and a URL that gets corrected is retried
# because the key changed with it.
#
# Only PERMANENT failures are remembered. A timeout or a dropped connection says
# nothing about the file, so those are tried again.
_PERMANENT = {404, 410, 451}


def state_path():
    try:
        from pythonrfparts.paths import CACHE_DIR
        return Path(CACHE_DIR) / "datasheet_fetch_state.json"
    except Exception:
        pass
    try:
        from rfparts.paths import CACHE_DIR
        return Path(CACHE_DIR) / "datasheet_fetch_state.json"
    except Exception:
        return Path(partdb.DATA) / "cache" / "datasheet_fetch_state.json"


def load_state():
    fp = state_path()
    if fp.is_file():
        try:
            import json
            data = json.loads(fp.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def save_state(state):
    fp = state_path()
    try:
        import json
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(json.dumps(state, indent=0), encoding="utf-8")
        return True
    except OSError as e:
        print(f"  ! could not save fetch state: {e}")
        return False


def state_key(mpn, url):
    return f"{loose(mpn)}|{url}"


def is_dead(state, mpn, url):
    rec = state.get(state_key(mpn, url))
    return isinstance(rec, dict) and rec.get("permanent") is True


def remember(state, mpn, url, status, why):
    state[state_key(mpn, url)] = {
        "status": status, "why": why[:60],
        "permanent": bool(status in _PERMANENT or why.startswith("tiny HTML")),
        "at": time.strftime("%Y-%m-%d %H:%M"),
    }


def origin_of(url):
    import urllib.parse
    u = urllib.parse.urlparse(url)
    return f"{u.scheme}://{u.netloc}" if u.scheme and u.netloc else ""


def fetch_via_page(page, url, debug=False, current_origin=None):
    """(bytes, status, reason) for a datasheet URL.

    Fetched from INSIDE the page with JavaScript rather than by navigating to it.
    Navigating to a PDF hands it to Chrome's built-in viewer, which consumes the
    response body -- resp.body() then fails and the only thing left to read is
    the viewer's own HTML wrapper. That wrapper is 536 bytes, which is exactly
    what every download turned into: the same tiny HTML for every part, from
    every vendor, because it was never the vendor's response at all.

    fetch() inside the page avoids the viewer completely, carries the session
    cookies, and returns the real bytes. It has to run same-origin or the browser
    blocks it, so the caller navigates to the vendor's root first -- once per
    origin, not once per file.
    """
    import base64
    try:
        res = page.evaluate(_JS_FETCH, url)
    except Exception as e:
        return None, None, f"{type(e).__name__}: {str(e)[:70]}"
    if not isinstance(res, dict):
        return None, None, "unexpected result from fetch"
    status = res.get("status") or 0
    if res.get("error"):
        return None, status, f"fetch blocked: {str(res['error'])[:60]}"
    if not res.get("data"):
        return None, status, f"HTTP {status}" if status else "empty response"
    try:
        blob = base64.b64decode(res["data"])
    except Exception as e:
        return None, status, f"decode failed: {type(e).__name__}"
    if status != 200:
        return None, status, f"HTTP {status}"
    return blob, status, "fetch"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--vendors", nargs="*", default=list(VENDOR_MATCH),
                    type=str.lower, choices=list(VENDOR_MATCH),
                    metavar="VENDOR",
                    help="any of: " + ", ".join(VENDOR_MATCH) + " (case-insensitive)")
    ap.add_argument("--limit", type=int, default=0, help="0 = no cap")
    ap.add_argument("--out", default=None,
                    help="datasheet library root (default: the one dsmine uses)")
    ap.add_argument("--delay", type=float, default=1.5,
                    help="base seconds between downloads (jitter added)")
    ap.add_argument("--endpoint", default="http://localhost:9222")
    ap.add_argument("--profile", default=None)
    ap.add_argument("--chrome", default=None)
    ap.add_argument("--retry-dead", action="store_true",
                    help="re-attempt URLs a previous run recorded as dead")
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

    state = load_state()
    targets, dead = find_targets(args.vendors, args.limit,
                                 args.include_qualified, state,
                                 args.retry_dead)
    if dead:
        print(f"\n{dead} part(s) skipped: a previous run proved the URL dead "
              f"(--retry-dead to try them again)")
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
    current_origin = None
    with sync_playwright() as p:
        browser, ctx, page = attach(
            p, args.endpoint,
            args.profile or (Path.home() / "rfparts-chrome"), args.chrome)
        print(f"  attached to Chrome ({page.url[:60] or 'about:blank'})\n")
        for i, t in enumerate(targets, 1):
            folder = out_root / VENDOR_FOLDER.get(t["vendor_key"], t["vendor"])
            folder.mkdir(parents=True, exist_ok=True)
            stem = safe_name(t["mpn"])
            # find_targets() has already decided this part needs a file --
            # including deciding that an unreadable one does not count. Re-testing
            # here with a plain existence check undid that and would have skipped
            # every corrupt Qorvo file all over again.
            existing = [f for f in folder.glob(f"{stem}.*")
                        if f.stat().st_size >= 1024
                        and not (dsmine and dsmine._sniff(f) == "corrupt")]
            if existing:
                skipped += 1
                continue
            # NAVIGATE, do not call the API context. ctx.request.get() issues a
            # bare HTTP call that never touches the page -- the address bar sits
            # on the start page, and over a CDP-attached browser it frequently
            # cannot reach the session at all, so every fetch fails. Driving the
            # page uses the whole browser stack, sends a real Referer, and lets
            # you watch it work.
            # One navigation per ORIGIN, not per file: fetch() must run
            # same-origin, and re-navigating for every part would be both slow
            # and needlessly noisy for the vendor.
            org = origin_of(t["url"])
            if org and org != current_origin:
                try:
                    page.goto(org + "/", wait_until="domcontentloaded",
                              timeout=45000)
                    current_origin = org
                    print(f"  -- on {org}")
                except Exception as e:
                    print(f"  ! could not open {org}: {type(e).__name__}")
                    current_origin = None
            blob, status, why = fetch_via_page(page, t["url"], args.debug)
            if args.debug:
                print(f"      {why}  status={status}  "
                      f"bytes={len(blob) if blob else 0}")
            if blob is None:
                failed += 1
                remember(state, t["mpn"], t["url"], status, why)
                print(f"  [{i}/{len(targets)}] {t['mpn']:<20} FAIL {why}")
                if failed % 25 == 0:
                    save_state(state)
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
                    remember(state, t["mpn"], t["url"], status,
                             f"tiny HTML {len(blob)}B")
                    print(f"  [{i}/{len(targets)}] {t['mpn']:<20} "
                          f"tiny HTML ({len(blob)} B) -- error page, not saved")
                    if failed % 25 == 0:
                        save_state(state)
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
            state.pop(state_key(t["mpn"], t["url"]), None)
            if saved % 25 == 0:
                save_state(state)
            print(f"  [{i}/{len(targets)}] {t['mpn']:<20} {len(blob):>9,} B "
                  f"-> {dest.name}")
            time.sleep(args.delay + random.uniform(0.2, 1.2))

    save_state(state)
    print(f"\nsaved {saved}, already had {skipped}, failed {failed}"
          + (f", {challenged} challenged" if challenged else ""))
    if saved:
        print("\nNext: delete Data\\cache\\datasheet_mining.json, then rebuild "
              "with Enrich ticked\nso these are mined for qualification wording.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        # Stopping is a normal way to use this; a stack trace is not an answer.
        # The state is saved as outcomes are learned, so what was proven dead
        # stays dead across the interruption.
        print("\nstopped. Saved files are kept, dead URLs are remembered, and "
              "a re-run picks up where this left off.")
        sys.exit(130)
