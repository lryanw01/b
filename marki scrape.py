#!/usr/bin/env python3
"""marki_scrape — Marki Microwave datasheets, saved to Downloads.

    python marki_scrape.py                 # everything, no flags needed
    python marki_scrape.py --forms connectorized --max-parts 20
    python marki_scrape.py --list-only     # enumerate parts, download nothing

NAVIGATION (your URL note)
    https://markimicrowave.com/products/<form>/<category>/[?page=N]
      form     connectorized | surface-mount | bare-die | waveguide
      category discovered from /products/<form>/ (mixers, amplifiers, ...)
    Listings are PAGED, so each category is walked ?page=1,2,3... until a page
    stops producing new part numbers. The previous version read page 1 only, so
    its "97 parts" was a first page, not a total.

WHY NOT THE PDFs
    Marki's per-part PDFs are Chrome print renderings (producer=Skia/PDF, zero
    font references, images only) -- no text layer at all, so nothing short of
    OCR can read them. The /datasheet/ PAGE carries the same content as HTML,
    which is cleaner anyway. We take the HTML.

DIRECT vs BROWSER
    Each page is fetched over plain HTTP first. That reliably returns the
    description and features, but the Electrical Specifications table is built by
    JavaScript and is missing. When a browser is available the page is re-fetched
    through it to pick the table up.

    Crucially, if the browser attempt fails the DIRECT TEXT IS STILL KEPT and
    saved as partial. The previous version discarded it, which is why only the
    hi-rel page ever got written.

The Space & Hi-Rel page is deliberately NOT collected: it describes Marki's
qualification process and contains no part data.

Chrome is opened and closed by this script; nothing to set up. Ctrl-C is safe --
saved files and the manifest are written as it goes.
"""
from __future__ import annotations

import argparse
import hashlib
import html as htmllib
import io
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

socket.setdefaulttimeout(25)
UA = "rfparts-marki-scrape/1.0 (RF parts sourcing research)"
BASE = "https://markimicrowave.com"
FORMS = ["connectorized", "surface-mount", "bare-die", "waveguide"]
# fallback if category discovery comes up empty
KNOWN_CATEGORIES = ["mixers", "amplifiers", "filters", "couplers",
                    "power-dividers", "baluns", "bias-tees", "equalizers",
                    "limiters", "multipliers", "detectors", "attenuators",
                    "switches", "phase-shifters", "diplexers", "terminations"]

_DEFAULT_OUTDIR = Path.home() / "Downloads" / "marki_datasheets"
OUTDIR = _DEFAULT_OUTDIR
OUT = io.StringIO()
SAVED = {"html": 0, "txt": 0, "bytes": 0}

CUES = ("hermetic", "ceramic", "GaAs", "GaN", "alumina", "bare die", "MIL-",
        "screened", "laser weld", "plastic", "epoxy", "-55", "kovar",
        "glass-to-metal", "hi-rel", "LTCC", "thin film", "space")


def say(line=""):
    print(line, flush=True)
    OUT.write(line + "\n")


def guarded(label, fn, budget):
    box = {}
    t0 = time.time()

    def target():
        try:
            box["v"] = fn()
            box["s"] = "ok"
        except urllib.error.HTTPError as e:
            box["v"] = f"HTTP {e.code}"
            box["s"] = "error"
        except Exception as e:
            box["v"] = f"{type(e).__name__}: {str(e)[:90]}"
            box["s"] = "error"
    th = threading.Thread(target=target, daemon=True, name=label)
    th.start()
    th.join(budget)
    if th.is_alive():
        return "TIMEOUT", f"blocked past {budget}s", time.time() - t0
    return box.get("s", "error"), box.get("v"), time.time() - t0


def http_get_text(url, timeout=25):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "text/html,*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(4_000_000).decode("utf-8", "replace")


# =========================================================== html -> text
_DROP = re.compile(r"<(script|style|noscript|svg|head)\b.*?</\1>", re.S | re.I)
_BLOCK = re.compile(r"</(p|div|li|tr|h[1-6]|table|section|br)\s*/?>", re.I)
_CELL = re.compile(r"</(td|th)\s*>", re.I)
_TAG = re.compile(r"<[^>]+>")
_HREF = re.compile(r'href\s*=\s*["\']([^"\']+)["\']', re.I)
_SPEC = re.compile(r"electrical specification|recommended operating|"
                   r"guaranteed from", re.I)
_START = ("device overview", "general description")
_END = ("stay informed", "privacy policy", "be the first to know", "copyright ©")


def html_to_text(raw):
    s = _DROP.sub(" ", raw)
    s = _CELL.sub(" | ", s)
    s = _BLOCK.sub("\n", s)
    s = _TAG.sub(" ", s)
    s = htmllib.unescape(s)
    s = re.sub(r"[ \t\xa0]+", " ", s)
    lines = [ln.strip(" |").strip() for ln in s.split("\n")]
    lines = [ln for ln in lines if ln]
    lo = 0
    for i, ln in enumerate(lines):
        if any(h in ln.lower() for h in _START):
            lo = i
            break
    hi = len(lines)
    for i in range(lo + 1, len(lines)):
        if any(h in lines[i].lower() for h in _END):
            hi = i
            break
    body = lines[lo:hi] or lines
    seen, out = set(), []
    for ln in body:
        k = re.sub(r"\s+", " ", ln.lower())
        if len(k) > 12 and k in seen:
            continue
        seen.add(k)
        out.append(ln)
    return "\n".join(out).strip()


def found_cues(text):
    return [c for c in CUES if re.search(re.escape(c), text, re.I)]


_WORDY = re.compile(r"^[A-Za-z]{4,}$")


def looks_like_pn(s):
    s = (s or "").strip().strip("-_")
    if not (3 < len(s) <= 40) or not any(c.isdigit() for c in s):
        return False
    return not any(_WORDY.match(seg) for seg in re.split(r"[-_]", s)[1:])


# ============================================================ managed Chrome
CHROME_PORT = 9222
CHROME_PROFILE = Path.home() / "sq-chrome"
_CHROME = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    str(Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" /
        "Application" / "chrome.exe"),
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]
_CHALLENGE = ("just a moment", "checking your browser", "attention required",
              "cf-browser-verification", "enable javascript and cookies",
              "verify you are human")


def find_chrome():
    import shutil
    for c in _CHROME:
        if c and Path(c).is_file():
            return c
    for n in ("google-chrome", "google-chrome-stable", "chromium",
              "chromium-browser", "microsoft-edge"):
        f = shutil.which(n)
        if f:
            return f
    return None


def port_open(port, timeout=0.6):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except Exception:
        return False


class Browser:
    """Visible Chrome with its own profile. Opened and closed by this script; a
    browser already on the port is attached to and left alone."""

    def __init__(self, port=CHROME_PORT, profile=CHROME_PROFILE):
        self.port, self.profile = port, Path(profile)
        self.proc = None
        self.we_launched = False
        self.ok = False
        self._pw = self._browser = None
        self.page = None
        self.paused_for_human = False

    def start(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            say("  Playwright not installed (pip install playwright); "
                "continuing without a browser -- spec tables will be missing")
            return False
        if port_open(self.port):
            say(f"  attaching to the browser already on port {self.port}")
        else:
            exe = find_chrome()
            if not exe:
                say("  no Chrome/Edge found; continuing without a browser")
                return False
            self.profile.mkdir(parents=True, exist_ok=True)
            say(f"  launching {Path(exe).name} (profile {self.profile})")
            import subprocess
            self.proc = subprocess.Popen(
                [exe, f"--remote-debugging-port={self.port}",
                 f"--user-data-dir={self.profile}", "--no-first-run",
                 "--no-default-browser-check", BASE + "/products/"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.we_launched = True
            for _ in range(40):
                if port_open(self.port):
                    break
                time.sleep(0.5)
            else:
                say("  Chrome never opened the debug port; carrying on without it")
                return False
        try:
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.connect_over_cdp(
                f"http://localhost:{self.port}", timeout=20000)
            ctx = (self._browser.contexts[0] if self._browser.contexts
                   else self._browser.new_context())
            self.page = ctx.pages[0] if ctx.pages else ctx.new_page()
            self.ok = True
            say("  browser ready")
        except Exception as e:
            say(f"  could not attach over CDP: {str(e).splitlines()[0][:90]}")
            self.ok = False
        return self.ok

    def close(self):
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self._pw = None
        if self.proc and self.we_launched:
            say("  closing the Chrome window we opened")
            try:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=8)
                except Exception:
                    self.proc.kill()
            except Exception:
                pass
        self.proc = None

    def challenged(self):
        try:
            blob = ((self.page.title() or "") + " " +
                    self.page.content()[:4000]).lower()
        except Exception:
            return False
        return any(m in blob for m in _CHALLENGE)

    def get_html(self, url, wait_text=None, timeout=60):
        self.page.goto(url, wait_until="domcontentloaded",
                       timeout=int(timeout * 1000))
        if self.challenged():
            # A human check appeared. We never try to solve it -- same rule as
            # erf_save_pages.py: hand control back and wait.
            say("\n  *** Cloudflare check on markimicrowave.com.")
            say("  *** Clear it in the Chrome window, then press Enter here.")
            try:
                input("  *** > ")
                self.paused_for_human = True
            except EOFError:
                say("  *** no console; continuing")
            self.page.goto(url, wait_until="domcontentloaded",
                           timeout=int(timeout * 1000))
        try:
            self.page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        if wait_text:
            try:
                self.page.wait_for_function(
                    "t => document.body && document.body.innerText"
                    ".toLowerCase().includes(t)",
                    arg=wait_text.lower(), timeout=15000)
            except Exception:
                pass
        return self.page.content()


# ================================================================ enumeration
def discover_categories(form, rate):
    """Category slugs under /products/<form>/."""
    url = f"{BASE}/products/{form}/"
    st, val, _ = guarded("cats", lambda: http_get_text(url), 40)
    if st != "ok":
        say(f"    {form}: listing failed ({val}); using the known category list")
        return list(KNOWN_CATEGORIES)
    cats = []
    for href in _HREF.findall(val):
        m = re.match(rf"^(?:{re.escape(BASE)})?/products/{re.escape(form)}/"
                     rf"([a-z0-9-]+)/?$", href.strip(), re.I)
        if m:
            c = m.group(1).lower()
            if c not in cats and not looks_like_pn(c):
                cats.append(c)
    time.sleep(rate)
    return cats or list(KNOWN_CATEGORIES)


def parts_on_page(html):
    """(pn -> product url) for /products/<form>/<cat>/<pn>/ links."""
    out = {}
    for href in _HREF.findall(html):
        m = re.match(r"^(?:https?://[^/]+)?(/products/[a-z-]+/[a-z0-9-]+/"
                     r"([A-Za-z0-9][A-Za-z0-9._-]*)/?)$", href.strip(), re.I)
        if m and looks_like_pn(m.group(2)):
            out.setdefault(m.group(2).upper(), urllib.parse.urljoin(BASE, m.group(1)))
    return out


def walk_category(form, cat, rate, max_pages=40):
    """Page through ?page=N until a page yields no new part numbers."""
    found, page, empties = {}, 1, 0
    while page <= max_pages:
        url = (f"{BASE}/products/{form}/{cat}/" if page == 1
               else f"{BASE}/products/{form}/{cat}/?page={page}")
        st, val, _ = guarded("cat", lambda u=url: http_get_text(u), 40)
        if st != "ok":
            if page == 1:
                say(f"    {form}/{cat:<16} page {page}: {val}")
            break
        got = parts_on_page(val)
        new = {k: v for k, v in got.items() if k not in found}
        if not new:
            empties += 1
            if empties >= 1:            # a page with nothing new = the end
                break
        found.update(new)
        page += 1
        time.sleep(rate)
    if found:
        say(f"    {form}/{cat:<16} {len(found):>4} part(s) over "
            f"{page - 1} page(s)")
    return found


# ================================================================== saving
def _safe(name):
    return re.sub(r"[^A-Za-z0-9._+-]", "_", str(name))[:80] or "part"


def save_artifact(mpn, kind, data, source_url, extra=None):
    folder = OUTDIR / "Marki-Microwave"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (_safe(mpn) + {"html": ".html", "txt": ".txt"}[kind])
    blob = data if isinstance(data, bytes) else data.encode("utf-8", "replace")
    path.write_bytes(blob)
    SAVED[kind] += 1
    SAVED["bytes"] += len(blob)
    rec = {"mpn": mpn, "vendor": "Marki Microwave", "kind": kind,
           "local_path": str(path.relative_to(OUTDIR)), "bytes": len(blob),
           "sha256": hashlib.sha256(blob).hexdigest(),
           "source_url": source_url,
           "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    if extra:
        rec.update(extra)
    OUTDIR.mkdir(parents=True, exist_ok=True)
    with (OUTDIR / "manifest.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")
    return path


# ================================================================ datasheets
def fetch_datasheet(ds_url, browser, rate):
    """(text, raw_html, how, has_specs).

    Direct HTTP gives description + features. The Electrical Specifications table
    is JS-built, so the browser is used to pick it up when available -- but if the
    browser fails, THE DIRECT TEXT IS KEPT. Discarding it is what made the last
    run save nothing."""
    direct_text = direct_raw = ""
    note = ""
    try:
        direct_raw = http_get_text(ds_url)
        direct_text = html_to_text(direct_raw)
    except urllib.error.HTTPError as e:
        note = f"direct HTTP {e.code}"
    except Exception as e:
        note = f"direct {type(e).__name__}"

    if direct_text and _SPEC.search(direct_text):
        return direct_text, direct_raw, "direct +specs", True

    if browser and browser.ok:
        try:
            raw = browser.get_html(ds_url, wait_text="electrical specification")
            text = html_to_text(raw)
            if text and _SPEC.search(text):
                return text, raw, "browser +specs", True
            if len(text) > len(direct_text):
                return text, raw, "browser (no spec table)", False
        except Exception as e:
            note = (note + "; " if note else "") + \
                   f"browser {type(e).__name__}"
        time.sleep(rate)

    if direct_text:
        # Partial is still worth keeping: description, features, ordering table.
        return direct_text, direct_raw, f"direct only, PARTIAL ({note or 'no specs'})", False
    return "", "", note or "nothing retrieved", False


def main():
    global OUTDIR
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--forms", nargs="*", default=FORMS,
                    help=f"form factors to walk (default: {' '.join(FORMS)})")
    ap.add_argument("--categories", nargs="*",
                    help="restrict to these category slugs")
    ap.add_argument("--max-parts", type=int, default=0,
                    help="stop after this many datasheets (0 = all)")
    ap.add_argument("--max-pages", type=int, default=40)
    ap.add_argument("--rate", type=float, default=1.0,
                    help="seconds between requests")
    ap.add_argument("--outdir", default=str(_DEFAULT_OUTDIR))
    ap.add_argument("--report", default="marki_scrape_report.txt")
    ap.add_argument("--list-only", action="store_true",
                    help="enumerate parts and stop")
    ap.add_argument("--no-browser", action="store_true",
                    help="direct HTTP only (spec tables will be missing)")
    args = ap.parse_args()
    OUTDIR = Path(args.outdir)

    say("marki_scrape -- Marki Microwave datasheets")
    say(f"generated {time.strftime('%Y-%m-%d %H:%M:%S')}")
    say(f"saving into {OUTDIR / 'Marki-Microwave'}")
    say("")

    browser = None
    interrupted = False
    try:
        say("=" * 74)
        say("1. ENUMERATE")
        say("=" * 74)
        parts = {}
        for form in args.forms:
            cats = args.categories or discover_categories(form, args.rate)
            say(f"  {form}: {len(cats)} category slug(s)")
            for cat in cats:
                parts.update(walk_category(form, cat, args.rate, args.max_pages))
        say(f"\n  {len(parts)} distinct part(s) total")
        if not parts:
            say("  nothing found -- check the URL format or your network")
            return 0
        if args.list_only:
            for pn in sorted(parts):
                say(f"    {pn:<22} {parts[pn]}")
            return 0

        if not args.no_browser:
            say("\n" + "=" * 74)
            say("2. BROWSER (for the JS-built specification tables)")
            say("=" * 74)
            browser = Browser()
            browser.start()

        say("\n" + "=" * 74)
        say("3. DATASHEETS")
        say("=" * 74)
        todo = sorted(parts)[: args.max_parts] if args.max_parts else sorted(parts)
        full = partial = failed = 0
        for i, pn in enumerate(todo, 1):
            ds = parts[pn].rstrip("/") + "/datasheet/"
            text, raw, how, has_specs = fetch_datasheet(ds, browser, args.rate)
            if not text:
                failed += 1
                say(f"  [{i}/{len(todo)}] {pn:<20} FAILED  {how}")
                continue
            cues = found_cues(text)
            meta = {"words": len(text.split()), "cues": cues,
                    "has_spec_table": has_specs, "fetch_path": how}
            if raw:
                save_artifact(pn, "html", raw, ds, meta)
            save_artifact(pn, "txt", text, ds, meta)
            if has_specs:
                full += 1
            else:
                partial += 1
            say(f"  [{i}/{len(todo)}] {pn:<20} {len(text.split()):>5} words  "
                f"specs={'Y' if has_specs else 'n'}  {how}")
            if cues:
                say(f"      cues: {', '.join(cues)}")
            time.sleep(args.rate)
        say(f"\n  {full} with spec tables, {partial} partial, {failed} failed")
    except KeyboardInterrupt:
        interrupted = True
        say("\n\n  Ctrl-C -- stopping. Files already saved are kept.")
    finally:
        if browser:
            browser.close()

    say("\n" + "=" * 74)
    say("SAVED")
    say("=" * 74)
    if SAVED["txt"] or SAVED["html"]:
        say(f"  {SAVED['txt']} text file(s), {SAVED['html']} html file(s), "
            f"{SAVED['bytes'] / (1024 * 1024):.1f} MB")
        say(f"  folder:   {OUTDIR / 'Marki-Microwave'}")
        say(f"  manifest: {OUTDIR / 'manifest.jsonl'}")
    else:
        say("  nothing saved")
    if interrupted:
        say("\n  (interrupted, so counts are partial)")
    try:
        Path(args.report).write_text(OUT.getvalue(), encoding="utf-8")
        print(f"\nreport written to {Path(args.report).resolve()}", flush=True)
    except Exception as e:
        print(f"\ncould not write the report: {e}", flush=True)
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    main()
