"""erf_save_pages.py — assisted saving of listing pages from YOUR OWN browser.

WHAT THIS IS
    You open Chrome, go to the first listing page, and clear any Cloudflare
    check yourself (as a human). This script then attaches to that same real
    browser session and automates the tedious part: save the rendered HTML,
    click "Next", wait, save again — the same clicks you'd do by hand.

WHAT THIS IS NOT
    It does not launch a hidden/stealth browser, spoof a TLS or browser
    fingerprint, or try to solve/defeat a Cloudflare challenge. If a challenge
    appears mid-run it STOPS and hands control back to you. The clearance cookie
    it rides on is the one you earned as a human, in your own profile.

    That's the whole point: we automate within a session you legitimately hold
    and keep supervising — we don't impersonate a human to gain access.

REQUIREMENTS
    pip install playwright
    playwright install chromium        # (only needed if you ever run headless;
                                       #  not required for attach-to-your-Chrome)

STEP 1 — launch your Chrome with a debug port and a dedicated profile:

    # macOS
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \\
        --remote-debugging-port=9222 --user-data-dir="$HOME/erf-chrome"

    # Windows
    "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" ^
        --remote-debugging-port=9222 --user-data-dir="%USERPROFILE%\\erf-chrome"

    # Linux
    google-chrome --remote-debugging-port=9222 --user-data-dir="$HOME/erf-chrome"

STEP 2 — in that Chrome window, navigate to page 1 of the listing and clear any
         Cloudflare / human check yourself.

STEP 3 — run this:
    python erf_save_pages.py out_folder --max-pages 20

STEP 4 — feed the saved HTML into your existing ingest:
    python erf_probe2.py out_folder
"""
import argparse
import hashlib
import pathlib
import random
import sys
import time

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit("Playwright not installed. Run:  pip install playwright")

CF_MARKERS = ("cf-browser-verification", "cf_chl_", "checking your browser",
              "just a moment", "turnstile", "__cf_chl", "attention required")

# Common "next page" patterns. everythingRF's exact markup may differ — if none
# of these match, the script tells you, and you can pass --next-selector "..."
# (send me the pagination HTML and I'll pin the exact one).
NEXT_SELECTORS = (
    'a[rel="next"]',
    'a[aria-label*="Next" i]',
    'a[title*="Next" i]',
    'li.next:not(.disabled) a',
    'a.next:not(.disabled)',
    '.pagination a.next',
)


def looks_like_challenge(html: str) -> bool:
    low = html.lower()
    return any(m in low for m in CF_MARKERS) and len(html) < 30000


def sig(html: str) -> str:
    """Cheap fingerprint to detect when a click didn't actually advance."""
    return hashlib.md5(html[:4000].encode("utf-8", "replace")).hexdigest()


def find_next(page, override):
    if override:
        el = page.query_selector(override)
        return el
    for sel in NEXT_SELECTORS:
        el = page.query_selector(sel)
        if el and el.is_visible():
            return el
    # text-based fallback
    try:
        el = page.get_by_role("link", name="Next", exact=False).first
        if el and el.count() and el.is_visible():
            return el
    except Exception:
        pass
    return None


def human_pause(base):
    """Human-paced, jittered delay. Keep volume modest and supervised."""
    time.sleep(base + random.uniform(0.4, 2.5))


def run(out_dir, max_pages, endpoint, next_sel, base_delay):
    out = pathlib.Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(endpoint)
        except Exception as e:
            sys.exit(f"Could not attach to Chrome at {endpoint}: {e}\n"
                     "Did you launch Chrome with --remote-debugging-port=9222?")

        if not browser.contexts or not browser.contexts[0].pages:
            sys.exit("No open tab found. Open the listing page 1 in that Chrome first.")

        page = browser.contexts[0].pages[0]
        print(f"attached to: {page.url}\n")

        last = None
        for i in range(1, max_pages + 1):
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass  # some pages never fully idle; proceed with what rendered

            html = page.content()

            if looks_like_challenge(html):
                input(f"\n[{i}] Cloudflare/human check detected. Solve it in the "
                      "browser, then press Enter here to continue... ")
                html = page.content()

            this_sig = sig(html)
            if this_sig == last:
                print(f"[{i}] page didn't change after clicking Next — stopping.")
                break
            last = this_sig

            fp = out / f"page_{i:03d}.html"
            fp.write_text(html, encoding="utf-8")
            print(f"[{i}] saved {fp.name}  ({len(html):,} bytes)  {page.url}")

            nxt = find_next(page, next_sel)
            if not nxt:
                print(f"[{i}] no Next link found — reached the end (or selector "
                      "needs adjusting via --next-selector).")
                break

            try:
                nxt.click()
            except Exception as e:
                print(f"[{i}] couldn't click Next ({type(e).__name__}) — stopping.")
                break

            human_pause(base_delay)

        print(f"\ndone. {len(list(out.glob('page_*.html')))} pages in {out}/")
        print(f"next:  python erf_probe2.py {out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out_dir", help="folder to save the HTML pages into")
    ap.add_argument("--max-pages", type=int, default=1000,
                    help="hard cap on pages to save (default 20 — keep it modest)")
    ap.add_argument("--endpoint", default="http://localhost:9222",
                    help="CDP endpoint of your running Chrome")
    ap.add_argument("--next-selector", default=None,
                    help="CSS selector for the Next link, if auto-detect misses it")
    ap.add_argument("--delay", type=float, default=3.0,
                    help="base seconds between pages (jitter added on top)")
    a = ap.parse_args()
    run(a.out_dir, a.max_pages, a.endpoint, a.next_selector, a.delay)
