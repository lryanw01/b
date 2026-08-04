r"""rfparts_save_pages.py — save listing/table pages from YOUR OWN browser.

One script for both sources that need a real browser:

    everythingRF   paginated listings behind a Cloudflare check
    Qorvo          parametric tables built client-side by a Next.js app, so a
                   plain HTTP fetch returns an app shell with no rows in it

WHAT THIS IS
    You drive Chrome. You clear any human check yourself. This attaches to that
    same real session over CDP and automates the tedious part: wait for the page
    to render, save the HTML, advance (click "Next" for everythingRF, go to the
    next table URL for Qorvo), repeat.

WHAT THIS IS NOT
    No headless/stealth browser, no TLS or UA fingerprint spoofing, no attempt to
    solve or bypass a challenge. If a challenge appears it STOPS and hands control
    back to you. Any clearance cookie in play is one you earned as a human in your
    own profile. Requests stay human-paced and supervised.

SETUP
    pip install playwright

    That is all. The script finds Chrome and launches it itself, with a debug
    port and a dedicated profile (~/rfparts-chrome), leaving your normal Chrome
    windows untouched. If a session is already listening it attaches to that
    instead.

    Only if auto-detection misses your install:
        python rfparts_save_pages.py --chrome "C:\Program Files\Google\Chrome\Application\chrome.exe"

    Or start it yourself. In PowerShell (note: & to run a quoted path, a backtick
    to continue a line, $env: not %VAR%):

        & "C:\Program Files\Google\Chrome\Application\chrome.exe" `
            --remote-debugging-port=9222 `
            --user-data-dir="$env:USERPROFILE\rfparts-chrome"

    In cmd.exe it is ^ for continuation and %USERPROFILE%:

        "C:\Program Files\Google\Chrome\Application\chrome.exe" ^
            --remote-debugging-port=9222 ^
            --user-data-dir="%USERPROFILE%\rfparts-chrome"

USAGE
    python rfparts_save_pages.py --source qorvo
    python rfparts_save_pages.py --source qorvo --only switches mixers

    No URLs to type, ever. Qorvo's six parametric tables are built in.
    everythingRF's listings are recovered from the pages already on disk: each
    saved page carries its own pagination links, so page 1 of the same filtered
    listing (space grade, global) is rebuilt from them and cached.

WHERE IT SAVES
    Qorvo -> <Sources>/QorvoParametric/<slug>.html
    ERF   -> <Sources>/EverythingRFSpaceQual/<folder>/page_NNN.html

    Both are exactly where the rebuild looks, so "Rebuild dataset" picks the
    pages up with no extra configuration.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import random
import re
import sys
import time
import urllib.parse
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit("Playwright not installed. Run:  pip install playwright")

# ---------------------------------------------------------------- destinations
def _sources_root():
    """Where the pipeline looks for local sources."""
    env = os.environ.get("RFPARTS_SOURCES", "").strip()
    if env:
        return Path(env)
    here = Path(__file__).resolve().parent
    for cand in (here / "Sources", here.parent / "Sources"):
        if cand.is_dir():
            return cand
    return here / "Sources"


# The six Qorvo parametric tables. Path-based URLs on the rebuilt (Vercel) site;
# the old /products/product-list?categoryID=N scheme 404s everywhere.
#
# robots.txt (checked 2026-08-04) disallows /products/c/, /products/i/,
# /products/s/, /products/sc/, /products/sca/ and /products/ai/. None of these
# URLs sit under those prefixes -- "/products/s/" does not match
# "/products/switches/", because the trailing slash is part of the rule.
QORVO_TABLES = [
    ("amplifiers-lnas-gain-blocks",
     "https://www.qorvo.com/products/amplifiers/lnas-gain-blocks/parametric-table"),
    ("amplifiers-power-amplifiers",
     "https://www.qorvo.com/products/amplifiers/power-amplifiers/parametric-table"),
    ("amplifiers-variable-gain-amplifiers",
     "https://www.qorvo.com/products/amplifiers/variable-gain-amplifiers/parametric-table"),
    ("switches-rf-switches",
     "https://www.qorvo.com/products/switches/rf-switches/parametric-table"),
    ("frequency-converters-mixers",
     "https://www.qorvo.com/products/frequency-converters/mixers/parametric-table"),
    ("frequency-converters-upconverters-downconverters",
     "https://www.qorvo.com/products/frequency-converters/upconverters-downconverters/parametric-table"),
]

CF_MARKERS = ("cf-browser-verification", "cf_chl_", "checking your browser",
              "just a moment", "turnstile", "__cf_chl", "attention required")

NEXT_SELECTORS = (
    'a[rel="next"]',
    'a[aria-label*="Next" i]',
    'a[title*="Next" i]',
    'li.next:not(.disabled) a',
    'a.next:not(.disabled)',
    '.pagination a.next',
)

# The grid only exists once the app has rendered. Saving before this appears is
# the whole failure mode we are working around, so it is waited for explicitly
# rather than trusting a fixed sleep.
QORVO_ROW_SELECTOR = "div.row.grid-row"
QORVO_READY_SELECTORS = ("div.table-body div.row.grid-row",
                         "div.table-head div.head-cell")



# ---------------------------------------------------------------- ERF URLs
# You should never have to type a URL. everythingRF listing pages carry their own
# pagination links, so the start URL for each category you already track is
# recoverable from the pages on disk:
#
#   <a class="navigate page-link" rel="next"
#      href="/search/microwave-rf-amplifiers/filters?page=4&country=global&sgrade=;Space;">
#
# Strip the page number and that is page 1 of the same filtered listing --
# including the space filter (sgrade=;Space;) that makes it the right listing.
# Discovered URLs are cached so later runs are instant even if the folder is
# emptied.
ERF_BASE = "https://www.everythingrf.com"
_ERF_NEXT_RE = re.compile(
    r'<a[^>]+class="[^"]*page-link[^"]*"[^>]+href="([^"]+)"[^>]*rel="next"', re.I)
_ERF_ANY_RE = re.compile(r'href="(/search/[^"]+/filters\?[^"]*)"', re.I)
URL_CACHE_NAME = ".rfparts_save_urls.json"


def _page1(href):
    """A listing href normalised to page 1, absolute."""
    import html as _h
    u = _h.unescape(href)
    if re.search(r"([?&])page=\d+", u):
        u = re.sub(r"([?&])page=\d+", r"\1page=1", u)
    else:
        u += ("&" if "?" in u else "?") + "page=1"
    return ERF_BASE + u if u.startswith("/") else u


def _score_listing(url):
    """Prefer the listing we actually track: space-filtered, global."""
    s = 0
    if "sgrade=;space;" in url.lower():
        s += 10
    if "country=global" in url.lower():
        s += 3
    return s


def discover_erf_urls(sources_root, erf_folder="EverythingRFSpaceQual",
                      verbose=True):
    """{subfolder: page-1 URL} read out of the pages already saved.

    One URL per existing subfolder, so re-running refreshes exactly the
    categories already tracked and nothing else."""
    root = Path(sources_root) / erf_folder
    found = {}
    if not root.is_dir():
        return found
    for sub in sorted(p for p in root.iterdir() if p.is_dir()):
        pages = sorted(sub.glob("page_*.html")) or sorted(sub.glob("*.html"))
        best = None
        for f in pages[:4]:            # the first few are enough
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            cands = [m.group(1) for m in _ERF_NEXT_RE.finditer(text)]
            cands += [m.group(1) for m in _ERF_ANY_RE.finditer(text)]
            for href in cands:
                u = _page1(href)
                if best is None or _score_listing(u) > _score_listing(best):
                    best = u
            if best and _score_listing(best) >= 13:
                break
        if best:
            found[sub.name] = best
            if verbose:
                print(f"    {sub.name:<34} {best[:78]}")
        elif verbose:
            print(f"    {sub.name:<34} ! no listing URL found in its saved pages")
    return found


def load_url_cache(sources_root):
    fp = Path(sources_root) / URL_CACHE_NAME
    if fp.is_file():
        try:
            import json
            data = json.loads(fp.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    return {}


def save_url_cache(sources_root, urls):
    fp = Path(sources_root) / URL_CACHE_NAME
    try:
        import json
        fp.write_text(json.dumps(urls, indent=1), encoding="utf-8")
    except OSError as e:
        print(f"    ! could not cache URLs: {e}")


def erf_targets(sources_root, erf_folder, verbose=True):
    """[(subfolder, url)] to walk: cached first, then discovered from disk."""
    cache = load_url_cache(sources_root)
    cached = cache.get(erf_folder) or {}
    if verbose:
        print(f"  everythingRF listings for {erf_folder}:")
    discovered = discover_erf_urls(sources_root, erf_folder, verbose=verbose)
    merged = dict(cached)
    merged.update(discovered)          # a fresh read of disk wins
    if merged != cached:
        cache[erf_folder] = merged
        save_url_cache(sources_root, cache)
    return sorted(merged.items())



# ---------------------------------------------------------------- Chrome
# Launching Chrome ourselves, rather than making you paste a command line.
#
# A dedicated --user-data-dir is not optional: Chrome ignores
# --remote-debugging-port when it hands the URL to an already-running instance of
# the same profile, so pointing at your everyday profile silently produces a
# browser with no debug port. A separate profile directory means a separate
# process, which is also why your normal Chrome windows are left alone.
#
# This is still your browser, visible, non-headless, with no stealth flags and no
# fingerprint tampering. You still clear any human check yourself.
CHROME_CANDIDATES = {
    "win32": [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ],
    "darwin": [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    ],
    "linux": ["google-chrome", "google-chrome-stable", "chromium",
              "chromium-browser", "microsoft-edge"],
}


def find_chrome(explicit=None):
    """Path to a Chrome/Chromium/Edge binary, or None."""
    import shutil
    if explicit:
        pth = Path(os.path.expandvars(explicit))
        return str(pth) if pth.exists() else None
    env = os.environ.get("RFPARTS_CHROME", "").strip()
    if env:
        pth = Path(os.path.expandvars(env))
        if pth.exists():
            return str(pth)
    for cand in CHROME_CANDIDATES.get(sys.platform, CHROME_CANDIDATES["linux"]):
        expanded = os.path.expandvars(cand)
        if os.path.sep in expanded or expanded.endswith(".exe"):
            if Path(expanded).exists():
                return expanded
        else:
            found = shutil.which(expanded)
            if found:
                return found
    for name in ("chrome", "google-chrome", "chromium", "msedge"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _port_open(endpoint, timeout=1.0):
    import socket
    parsed = urllib.parse.urlparse(endpoint)
    host, port = parsed.hostname or "localhost", parsed.port or 9222
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def launch_chrome(endpoint, profile_dir, chrome_path=None, wait=30):
    """Start Chrome with a debug port on a dedicated profile. Returns Popen."""
    import subprocess
    exe = find_chrome(chrome_path)
    if not exe:
        print("  ! could not find Chrome automatically.")
        print("    Pass --chrome \"C:\\Path\\To\\chrome.exe\" or set the "
              "RFPARTS_CHROME env var.")
        return None
    port = urllib.parse.urlparse(endpoint).port or 9222
    profile = Path(os.path.expandvars(str(profile_dir))).expanduser()
    profile.mkdir(parents=True, exist_ok=True)
    cmd = [exe, f"--remote-debugging-port={port}",
           f"--user-data-dir={profile}",
           "--no-first-run", "--no-default-browser-check",
           "about:blank"]
    print(f"  launching {Path(exe).name} on port {port}")
    print(f"    profile: {profile}")
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
    except Exception as e:
        print(f"  ! could not launch Chrome: {type(e).__name__}: {e}")
        return None
    for _ in range(int(wait * 2)):
        if _port_open(endpoint):
            print("    debug port is up")
            return proc
        time.sleep(0.5)
    print(f"  ! Chrome started but port {port} never opened within {wait}s.")
    return proc


def attach(p, endpoint, profile_dir=None, chrome_path=None, allow_launch=True):
    """Attach to Chrome, launching it first if nothing is listening."""
    launched = None
    if not _port_open(endpoint):
        if not allow_launch:
            sys.exit(f"Nothing is listening on {endpoint} and --no-launch was "
                     f"given.")
        print(f"  nothing on {endpoint} yet")
        launched = launch_chrome(endpoint, profile_dir or _default_profile(),
                                 chrome_path)
        if not _port_open(endpoint):
            sys.exit("Could not get a Chrome debug port. Start Chrome yourself "
                     "with:\n  " + powershell_hint(endpoint,
                                                    profile_dir or
                                                    _default_profile()))
    else:
        print(f"  found an existing Chrome debug session on {endpoint}")
    last = None
    for _ in range(10):
        try:
            browser = p.chromium.connect_over_cdp(endpoint)
            break
        except Exception as e:
            last = e
            time.sleep(1.0)
    else:
        sys.exit(f"Could not attach to Chrome at {endpoint}: {last}")
    ctx = browser.contexts[0] if browser.contexts else browser.new_context()
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    return browser, page, launched


def _default_profile():
    return Path.home() / "rfparts-chrome"


def powershell_hint(endpoint, profile):
    """The correct PowerShell incantation, for when auto-launch cannot work.

    Written out properly because the usual copy-paste failures are all avoidable:
    PowerShell needs the call operator & for a quoted path, uses a backtick (not
    ^) to continue a line, and expands $env:USERPROFILE rather than
    %USERPROFILE%.
    """
    port = urllib.parse.urlparse(endpoint).port or 9222
    exe = find_chrome() or (
        r"C:\Program Files\Google\Chrome\Application\chrome.exe"
        if sys.platform == "win32" else
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        if sys.platform == "darwin" else "google-chrome")
    return (f'& "{exe}" --remote-debugging-port={port} '
            f'--user-data-dir="{profile}"')


def looks_like_challenge(html: str) -> bool:
    low = html.lower()
    return any(m in low for m in CF_MARKERS) and len(html) < 30000


def sig(html: str) -> str:
    return hashlib.md5(html[:4000].encode("utf-8", "replace")).hexdigest()


def human_pause(base):
    time.sleep(base + random.uniform(0.4, 2.0))


def _wait_settled(page, timeout=20000):
    try:
        page.wait_for_load_state("networkidle", timeout=timeout)
    except Exception:
        pass          # some pages never fully idle; use what has rendered


def _clear_challenge(page, label):
    html = page.content()
    if looks_like_challenge(html):
        input(f"\n[{label}] human check detected. Clear it in the browser, "
              f"then press Enter here... ")
        _wait_settled(page)
        html = page.content()
    return html


# ---------------------------------------------------------------- Qorvo
def save_qorvo(page, out_root, delay, only=None, overwrite=False):
    out = Path(out_root) / "QorvoParametric"
    out.mkdir(parents=True, exist_ok=True)
    targets = [(s, u) for s, u in QORVO_TABLES
               if not only or any(o.lower() in s for o in only)]
    print(f"\n=== Qorvo: {len(targets)} parametric table(s) -> {out} ===")
    saved = skipped = empty = 0
    for i, (slug, url) in enumerate(targets, 1):
        dest = out / f"{slug}.html"
        if dest.exists() and not overwrite:
            print(f"[{i}/{len(targets)}] {slug}: already saved "
                  f"({dest.stat().st_size:,} bytes) -- use --overwrite to refresh")
            skipped += 1
            continue
        print(f"[{i}/{len(targets)}] {url}")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print(f"    ! navigation failed: {type(e).__name__}: {e}")
            continue
        _wait_settled(page)
        _clear_challenge(page, slug)
        # Wait for the grid itself, not just for the network to quiet down.
        ready = False
        for sel in QORVO_READY_SELECTORS:
            try:
                page.wait_for_selector(sel, timeout=25000)
                ready = True
                break
            except Exception:
                continue
        if not ready:
            print("    ! the parametric grid never appeared. The page may need a "
                  "moment more, or its markup changed.")
        # Rows can arrive in batches; wait until the count stops growing.
        last = -1
        for _ in range(12):
            try:
                n = page.locator(QORVO_ROW_SELECTOR).count()
            except Exception:
                n = 0
            if n and n == last:
                break
            last = n
            time.sleep(1.0)
        html = page.content()
        rows = (html.count('class="row grid-row"')
                or html.count("grid-row") or html.count("product-name"))
        if not rows:
            empty += 1
            print(f"    ! saved nothing useful: 0 grid rows in {len(html):,} "
                  f"bytes. Not writing the file.")
            continue
        dest.write_text(html, encoding="utf-8")
        saved += 1
        print(f"    saved {dest.name}  ({len(html):,} bytes, {rows} row(s))")
        human_pause(delay)
    print(f"\nQorvo: {saved} saved, {skipped} already present, {empty} empty")
    return saved


# ---------------------------------------------------------------- everythingRF
def save_erf_listing(page, out_dir, url, delay, max_pages, next_sel, overwrite,
                     label=""):
    """Save one paginated listing, starting from its page-1 URL."""
    out_dir.mkdir(parents=True, exist_ok=True)
    existing = sorted(out_dir.glob("page_*.html"))
    start_at = 1
    if existing and not overwrite:
        start_at = len(existing) + 1
        print(f"    {len(existing)} page(s) already here; continuing at "
              f"page_{start_at:03d}")
    target = url
    if start_at > 1:
        target = re.sub(r"([?&])page=\d+", rf"\g<1>page={start_at}", url)
    print(f"    opening {target[:96]}")
    try:
        page.goto(target, wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        print(f"    ! navigation failed: {type(e).__name__}: {e}")
        return 0
    last, saved = None, 0
    for i in range(start_at, max_pages + 1):
        _wait_settled(page)
        html = _clear_challenge(page, f"{label} page {i}")
        this = sig(html)
        if this == last:
            print(f"    [{i}] page did not change after Next -- stopping.")
            break
        last = this
        # Count on "product-box" alone, not class="product-box": the real pages
        # carry other classes in the same attribute, so the exact-match form
        # counted 0 on a perfectly good page and printed a false warning.
        boxes = html.count("product-box") or html.count("prod-title")
        dest = out_dir / f"page_{i:03d}.html"
        dest.write_text(html, encoding="utf-8")
        saved += 1
        print(f"    [{i}] saved {dest.name}  ({len(html):,} bytes, {boxes} box(es))")
        if boxes == 0:
            print("        ! no product boxes; listing may have ended.")
            break
        nxt = None
        if next_sel:
            nxt = page.query_selector(next_sel)
        else:
            for sel in NEXT_SELECTORS:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    nxt = el
                    break
            if nxt is None:
                try:
                    cand = page.get_by_role("link", name="Next",
                                            exact=False).first
                    if cand and cand.count() and cand.is_visible():
                        nxt = cand
                except Exception:
                    pass
        if not nxt:
            print(f"    [{i}] no Next link -- end of listing.")
            break
        try:
            nxt.click()
        except Exception as e:
            print(f"    [{i}] could not click Next ({type(e).__name__}).")
            break
        human_pause(delay)
    return saved


def save_erf(page, out_root, delay, erf_folder="EverythingRFSpaceQual",
             max_pages=1000, next_sel=None, overwrite=False, only=None):
    """Refresh every everythingRF listing already tracked on disk.

    No URL argument: the listings are the ones your saved pages came from,
    recovered from their own pagination links.
    """
    targets = erf_targets(out_root, erf_folder)
    if only:
        targets = [(s, u) for s, u in targets
                   if any(o.lower() in s.lower() for o in only)]
    if not targets:
        print(f"\n=== everythingRF: nothing to refresh ===")
        print(f"  No listing URL could be recovered from "
              f"{Path(out_root) / erf_folder}.")
        print(f"  That folder needs at least one previously saved page per")
        print(f"  category (the URL is read out of its pagination links).")
        return 0
    print(f"\n=== everythingRF: {len(targets)} listing(s) -> "
          f"{Path(out_root) / erf_folder} ===")
    total = 0
    for n, (sub, url) in enumerate(targets, 1):
        print(f"[{n}/{len(targets)}] {sub}")
        total += save_erf_listing(
            page, Path(out_root) / erf_folder / sub, url, delay, max_pages,
            next_sel, overwrite, label=sub)
        human_pause(delay)
    print(f"\neverythingRF: {total} page(s) saved")
    return total


MENU = """
==============================================================
  rfparts — save pages from your own browser
==============================================================
  sources root : {root}
  Chrome (CDP) : {endpoint}
--------------------------------------------------------------
  1) Qorvo          {qorvo}
  2) everythingRF   {erf}
  3) Both
  0) Quit
--------------------------------------------------------------"""


def _status(root):
    qdir = Path(root) / "QorvoParametric"
    n_q = len(list(qdir.glob("*.html"))) if qdir.is_dir() else 0
    qorvo = (f"{len(QORVO_TABLES)} table(s); {n_q} already saved"
             if n_q else f"{len(QORVO_TABLES)} table(s); none saved yet")
    edir = Path(root) / "EverythingRFSpaceQual"
    subs = [d for d in edir.iterdir() if d.is_dir()] if edir.is_dir() else []
    pages = sum(len(list(d.glob("page_*.html"))) for d in subs)
    erf = (f"{len(subs)} listing(s), {pages} page(s) saved"
           if subs else "no saved listings found yet")
    return qorvo, erf


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", choices=("erf", "qorvo", "both"), default=None,
                    help="skip the menu and go straight to this source")
    ap.add_argument("--out", default=None,
                    help="Sources root (default: RFPARTS_SOURCES, else "
                         "./Sources beside this file)")
    ap.add_argument("--erf-folder", default="EverythingRFSpaceQual")
    ap.add_argument("--max-pages", type=int, default=1000)
    ap.add_argument("--only", nargs="*",
                    help="limit to entries whose name contains these, e.g. "
                         "--only switches mixers")
    ap.add_argument("--next-selector", default=None)
    ap.add_argument("--endpoint", default="http://localhost:9222",
                    help="CDP endpoint (default http://localhost:9222)")
    ap.add_argument("--chrome", default=None,
                    help="path to chrome.exe if auto-detection misses it")
    ap.add_argument("--profile", default=None,
                    help="Chrome user-data-dir to launch with "
                         "(default ~/rfparts-chrome)")
    ap.add_argument("--no-launch", action="store_true",
                    help="do not start Chrome; require a session already "
                         "listening on --endpoint")
    ap.add_argument("--delay", type=float, default=3.0)
    ap.add_argument("--overwrite", action="store_true",
                    help="re-save pages that already exist")
    args = ap.parse_args(argv)

    out_root = Path(args.out) if args.out else _sources_root()
    out_root.mkdir(parents=True, exist_ok=True)

    choice = args.source
    if choice is None:
        qorvo, erf = _status(out_root)
        print(MENU.format(root=out_root, endpoint=args.endpoint,
                          qorvo=qorvo, erf=erf))
        try:
            pick = input("  choice> ").strip()
        except EOFError:
            return 0
        choice = {"1": "qorvo", "2": "erf", "3": "both",
                  "0": None, "": None, "q": None}.get(pick, "?")
        if choice == "?":
            print("  ? unknown choice")
            return 1
        if choice is None:
            return 0

    print(f"\nsources root: {out_root}")
    with sync_playwright() as p:
        browser, page, launched = attach(
            p, args.endpoint, profile_dir=args.profile,
            chrome_path=args.chrome, allow_launch=not args.no_launch)
        print(f"attached to Chrome: {page.url[:80] or 'about:blank'}")
        total = 0
        if choice in ("qorvo", "both"):
            total += save_qorvo(page, out_root, args.delay, only=args.only,
                                overwrite=args.overwrite)
        if choice in ("erf", "both"):
            total += save_erf(page, out_root, args.delay,
                              erf_folder=args.erf_folder,
                              max_pages=args.max_pages,
                              next_sel=args.next_selector,
                              overwrite=args.overwrite, only=args.only)
    print(f"\ndone: {total} page(s) saved.")
    print("Next: Rebuild dataset (Resume is fine -- unchanged files are skipped).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
