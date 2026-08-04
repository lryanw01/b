"""dbg_qorvo_fetch — why are all Qorvo category pages failing?

Run it, paste the output back. Nothing here writes to the database or the vendor
cache; it only reads, and it makes a handful of requests.

    python dbg_qorvo_fetch.py
    python dbg_qorvo_fetch.py --ids 1141 1142 --rate 2.0
    python dbg_qorvo_fetch.py --allow-browser-ua      # see the policy note below

WHY A DEBUG SCRIPT INSTEAD OF A FIX
-----------------------------------
There are several causes that all look identical from the outside ("HTTP error on
every page"), and they need opposite responses:

  403 because of the User-Agent   a WAF now rejects non-browser clients
  403 because of headers          UA is fine, but the request looks non-browser
  404                            Qorvo changed the URL scheme; the walk is
                                 asking for a page that no longer exists
  429                            rate limiting, made worse by a cold full walk
  robots.txt disallow            we are being told not to, and should not
  always failing, newly visible  the Qorvo walk used to swallow fetch errors
                                 silently (`except Exception: continue`) and now
                                 reports them, so this may not be new at all

Guessing wrong wastes a day. Each probe below isolates ONE variable.

A NOTE ON THE USER-AGENT PROBE
------------------------------
This project's crawling policy is deliberate: honest user-agent, robots.txt
respected, no anti-bot circumvention or fingerprint spoofing. Probe E sends a
browser User-Agent, which conflicts with that policy, so it is OFF unless you
pass --allow-browser-ua. It is included because it is diagnostically decisive --
if E succeeds and B/C/D fail, the cause is UA filtering and you then get to make
a policy decision knowingly. It is a diagnosis, not a recommended fix.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from pathlib import Path

socket.setdefaulttimeout(30)

HONEST_UA = ("rfparts/2.0 (RF parts sourcing research; "
             "contact: set RFPARTS_CONTACT env var)")
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# The ids the walk actually uses; a few is enough to tell a pattern from a fluke.
DEFAULT_IDS = [1141, 1142, 1143, 1108]

URL_FORMS = [
    ("current (what the walk uses)",
     "https://www.qorvo.com/products/product-list?categoryID={cid}"),
    ("trailing slash on path",
     "https://www.qorvo.com/products/product-list/?categoryID={cid}"),
    ("no www",
     "https://qorvo.com/products/product-list?categoryID={cid}"),
    ("newer /products/search form",
     "https://www.qorvo.com/products/search?categoryID={cid}"),
]

TABLE_RE = re.compile(r"<table", re.I)
TITLE_RE = re.compile(r"(?is)<title[^>]*>(.*?)</title>")


def _hdr(name):
    print("\n" + "=" * 74)
    print("  " + name)
    print("=" * 74)


def _try(url, headers, label, timeout=30):
    """One request. Returns a dict describing what happened -- never raises."""
    req = urllib.request.Request(url, headers=headers)
    out = {"label": label, "url": url}
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            blob = r.read(4_000_000)
            out.update(status=r.status, final_url=r.geturl(),
                       bytes=len(blob),
                       server=r.headers.get("Server", ""),
                       ctype=r.headers.get("Content-Type", ""))
            text = blob.decode("utf-8", "replace")
            out["tables"] = len(TABLE_RE.findall(text))
            m = TITLE_RE.search(text)
            out["title"] = (m.group(1).strip()[:70] if m else "")
            # WAF challenge pages return 200 with an interstitial, which is a
            # failure that looks like a success unless you check the body.
            low = text[:6000].lower()
            out["waf_hint"] = next(
                (s for s in ("just a moment", "checking your browser",
                             "access denied", "cf-browser-verification",
                             "captcha", "incapsula", "akamai", "bot detection",
                             "request unsuccessful")
                 if s in low), "")
    except urllib.error.HTTPError as e:
        body = b""
        try:
            body = e.read(4000)
        except Exception:
            pass
        out.update(status=e.code, error=f"HTTPError {e.code} {e.reason}",
                   server=e.headers.get("Server", "") if e.headers else "",
                   bytes=len(body))
        low = body.decode("utf-8", "replace").lower()
        out["waf_hint"] = next(
            (s for s in ("access denied", "captcha", "cloudflare", "incapsula",
                         "akamai", "bot", "forbidden")
             if s in low), "")
    except urllib.error.URLError as e:
        out.update(status=None, error=f"URLError {e.reason}")
    except Exception as e:
        out.update(status=None, error=f"{type(e).__name__}: {e}")
    out["secs"] = round(time.time() - t0, 2)
    return out


def _show(r):
    bits = [f"status={r.get('status')}"]
    if r.get("error"):
        bits.append(r["error"])
    if r.get("bytes") is not None:
        bits.append(f"{r.get('bytes')}B")
    if r.get("tables") is not None:
        bits.append(f"tables={r['tables']}")
    if r.get("server"):
        bits.append(f"server={r['server']}")
    if r.get("waf_hint"):
        bits.append(f"!! WAF/challenge hint: {r['waf_hint']!r}")
    print(f"    {r['label']:<38} " + "  ".join(str(b) for b in bits))
    if r.get("final_url") and r["final_url"] != r["url"]:
        print(f"      redirected -> {r['final_url'][:100]}")
    if r.get("title"):
        print(f"      title: {r['title']}")
    return r


# ---------------------------------------------------------------- probes
def probe_network():
    _hdr("0. is anything between you and the internet?")
    import os
    keys = ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy",
            "https_proxy", "no_proxy", "REQUESTS_CA_BUNDLE", "SSL_CERT_FILE")
    found = {k: os.environ.get(k) for k in keys if os.environ.get(k)}
    print(f"    proxy/TLS env vars: {found or 'none set'}")
    try:
        handlers = urllib.request.getproxies()
        print(f"    urllib.getproxies(): {handlers or 'none'}")
    except Exception as e:
        print(f"    urllib.getproxies() failed: {e}")
    # A control host. If a neutral site also fails identically, the problem is
    # not Qorvo at all.
    for host in ("https://example.com", "https://www.python.org"):
        r = _try(host, {"User-Agent": HONEST_UA, "Accept": "text/html,*/*"},
                 f"control: {host}")
        _show(r)
    print("    Reading: if the CONTROL hosts fail too, this is your network or")
    print("    an outbound proxy, not Qorvo. On a corporate network that is the")
    print("    single most likely cause of 'it used to work'.")


def probe_robots(rate):
    _hdr("A. robots.txt -- are we being told not to?")
    url = "https://www.qorvo.com/robots.txt"
    r = _try(url, {"User-Agent": HONEST_UA}, "fetch robots.txt")
    _show(r)
    if r.get("status") != 200:
        print("    (could not read robots.txt; the fetcher treats that as "
              "allowed)")
        return
    try:
        with urllib.request.urlopen(
                urllib.request.Request(url, headers={"User-Agent": HONEST_UA}),
                timeout=15) as resp:
            text = resp.read(200_000).decode("utf-8", "replace")
    except Exception as e:
        print(f"    re-read failed: {e}")
        return
    rp = urllib.robotparser.RobotFileParser()
    rp.parse(text.splitlines())
    for cid in (DEFAULT_IDS[0],):
        target = f"https://www.qorvo.com/products/product-list?categoryID={cid}"
        for ua in (HONEST_UA, "rfparts", "*"):
            print(f"    can_fetch({ua[:28]!r:<30}) -> {rp.can_fetch(ua, target)}")
    interesting = [ln for ln in text.splitlines()
                   if re.search(r"disallow|user-agent|crawl-delay", ln, re.I)]
    print(f"    robots.txt has {len(interesting)} relevant line(s); "
          f"showing any that mention 'product':")
    for ln in interesting:
        if "product" in ln.lower() or ln.lower().startswith("user-agent"):
            print(f"      {ln.strip()[:90]}")


def probe_headers(cid, allow_browser_ua, rate):
    _hdr(f"B-E. same URL, different request shapes (categoryID={cid})")
    url = f"https://www.qorvo.com/products/product-list?categoryID={cid}"
    results = []

    # B: exactly what the fetcher sends today
    results.append(_show(_try(url, {
        "User-Agent": HONEST_UA,
        "Accept": "text/html,*/*"}, "B current fetcher headers")))
    time.sleep(rate)

    # C: honest UA, but the ordinary headers any HTTP client sends
    results.append(_show(_try(url, {
        "User-Agent": HONEST_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "identity",
        "Connection": "keep-alive"}, "C honest UA + normal headers")))
    time.sleep(rate)

    # D: honest UA, full browser-ish header set MINUS the UA itself. If D works
    # and B does not, the UA is fine and the header shape was the problem.
    results.append(_show(_try(url, {
        "User-Agent": HONEST_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                  "image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "identity",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Cache-Control": "no-cache"}, "D honest UA + browser headers")))
    time.sleep(rate)

    if allow_browser_ua:
        results.append(_show(_try(url, {
            "User-Agent": BROWSER_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                      "image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "identity",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none"},
            "E browser UA (policy: opt-in)")))
    else:
        print("    E browser UA                          SKIPPED "
              "(pass --allow-browser-ua; see the note at the top)")
    return results


def probe_url_forms(cid, rate):
    _hdr(f"F. is the URL scheme still right? (categoryID={cid})")
    out = []
    for label, tmpl in URL_FORMS:
        out.append(_show(_try(tmpl.format(cid=cid),
                              {"User-Agent": HONEST_UA,
                               "Accept": "text/html,*/*"}, label)))
        time.sleep(rate)
    # And the plain product landing page: if THIS fails too, it is not about
    # category URLs at all.
    out.append(_show(_try("https://www.qorvo.com/products",
                          {"User-Agent": HONEST_UA,
                           "Accept": "text/html,*/*"}, "plain /products page")))
    return out


def probe_cache():
    _hdr("G. did it ever work? (on-disk vendor cache)")
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from pythonrfparts import partdb
        cache_root = Path(partdb.DATA) / "vendor_cache"
    except Exception as e:
        print(f"    could not locate the cache via the package ({e});")
        home = Path.home() / "Downloads" / "rfparts" / "Data" / "vendor_cache"
        cache_root = home
    print(f"    cache root: {cache_root}")
    host_dir = cache_root / "www.qorvo.com"
    if not host_dir.is_dir():
        print("    no cached Qorvo responses at all -- so there is no evidence "
              "here that\n    these pages ever fetched successfully on this "
              "machine.")
        return
    files = sorted(host_dir.glob("*.html"))
    print(f"    {len(files)} cached Qorvo page(s)")
    if not files:
        return
    sizes = sorted(f.stat().st_size for f in files)
    newest = max(files, key=lambda f: f.stat().st_mtime)
    oldest = min(files, key=lambda f: f.stat().st_mtime)
    print(f"      sizes  min={sizes[0]} median={sizes[len(sizes)//2]} "
          f"max={sizes[-1]}")
    print(f"      oldest {time.strftime('%Y-%m-%d %H:%M', time.localtime(oldest.stat().st_mtime))}"
          f"   newest {time.strftime('%Y-%m-%d %H:%M', time.localtime(newest.stat().st_mtime))}")
    withtab = 0
    for f in files[:40]:
        try:
            if TABLE_RE.search(f.read_text(encoding="utf-8", errors="replace")):
                withtab += 1
        except OSError:
            pass
    print(f"      of the first {min(40, len(files))}, {withtab} contain a <table>")
    print("    Reading: cached pages WITH tables prove fetching used to work and")
    print("    the parser had real input. Cached pages with none, or tiny sizes,")
    print("    point at responses that were already challenge/blocked pages.")


def probe_cached_parse():
    _hdr("H. is the PARSER still fine on a page that used to work?")
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from pythonrfparts import partdb, vendor_catalogs as vc
    except Exception as e:
        print(f"    could not import the package: {e}")
        return
    host_dir = Path(partdb.DATA) / "vendor_cache" / "www.qorvo.com"
    files = sorted(host_dir.glob("*.html")) if host_dir.is_dir() else []
    picked = None
    for f in files:
        try:
            txt = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if TABLE_RE.search(txt):
            picked = (f, txt)
            break
    if not picked:
        print("    no cached page with a table to test against.")
        return
    f, txt = picked
    tables = vc.parse_tables(txt)
    print(f"    {f.name}: {len(txt)} chars, parse_tables -> {len(tables)} table(s)")
    if tables:
        headers, rows = tables[0]
        print(f"      first table: {len(rows)} row(s), headers={headers[:6]}")
        print(f"      map_headers -> {vc.map_headers(headers)}")
    print("    If this yields tables, the parser is healthy and the problem is")
    print("    purely the fetch. If it yields none, the page shape changed too.")


def verdict(header_results, form_results):
    _hdr("VERDICT")
    codes = {r["label"]: r.get("status") for r in header_results}
    ok = [k for k, v in codes.items() if v == 200]
    b = codes.get("B current fetcher headers")
    print(f"    current fetcher shape (B): status={b}")
    if b == 200:
        print("    -> Fetching WORKS right now. The failures you saw were either")
        print("       transient/rate limiting, or they predate this run. Re-run")
        print("       the walk with --rate 2.0 and watch the error tally.")
    elif any(k.startswith(("C", "D")) for k in ok):
        print("    -> The User-Agent is NOT the problem; the request SHAPE was.")
        print("       Adding ordinary headers (Accept-Language, Accept-Encoding,")
        print("       Sec-Fetch-*) is a legitimate fix that keeps the honest UA.")
    elif any(k.startswith("E") for k in ok):
        print("    -> Only the browser UA got through. That is UA filtering, and")
        print("       working around it conflicts with this project's stated")
        print("       crawling policy. That is your call to make, not a bug to")
        print("       fix silently. Consider an official data feed or an")
        print("       RFPARTS_UA override you set deliberately.")
    elif b == 404:
        print("    -> 404: the URL scheme is gone. Check section F for a form")
        print("       that returns 200 and repoint the walk at it.")
    elif b == 429:
        print("    -> 429: rate limited. Raise the rate limit and resume rather")
        print("       than cold-walking all 199 ids in one go.")
    elif b == 403:
        print("    -> 403 on every shape including the browser-ish headers:")
        print("       likely an IP/WAF block rather than UA filtering. A VPN or")
        print("       a different network will tell you which.")
    else:
        print("    -> No shape succeeded and it is not a plain 403/404/429.")
        print("       Check section A (robots) and the server/WAF hints above.")
    sizes = {r.get("bytes") for r in header_results if r.get("bytes")}
    if len(sizes) == 1 and next(iter(sizes)) < 1500:
        print(f"    NOTE: every response was the same tiny size "
              f"({next(iter(sizes))}B). Identical short bodies across different")
        print("       request shapes come from an INTERMEDIARY (proxy, firewall,")
        print("       WAF), not from Qorvo -- the origin would vary its response.")
        print("       Check section 0 and whether the control hosts also failed.")
    good_forms = [r["label"] for r in form_results if r.get("status") == 200]
    if good_forms:
        print(f"    URL forms that returned 200: {good_forms}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--ids", nargs="*", type=int, default=DEFAULT_IDS)
    ap.add_argument("--rate", type=float, default=1.5,
                    help="seconds between requests (default 1.5, be polite)")
    ap.add_argument("--allow-browser-ua", action="store_true",
                    help="also probe with a browser User-Agent (see the note "
                         "in the docstring; conflicts with the project's "
                         "crawling policy)")
    args = ap.parse_args(argv)

    print("dbg_qorvo_fetch: isolating why the Qorvo walk gets HTTP errors")
    print(f"  honest UA : {HONEST_UA}")
    print(f"  ids       : {args.ids}")
    print(f"  rate      : {args.rate}s between requests")

    probe_network()
    probe_cache()
    probe_cached_parse()
    probe_robots(args.rate)
    hres = probe_headers(args.ids[0], args.allow_browser_ua, args.rate)
    fres = probe_url_forms(args.ids[0], args.rate)

    if len(args.ids) > 1:
        _hdr("I. is it every id, or only some?")
        for cid in args.ids[1:]:
            _show(_try(
                f"https://www.qorvo.com/products/product-list?categoryID={cid}",
                {"User-Agent": HONEST_UA, "Accept": "text/html,*/*"},
                f"categoryID={cid}"))
            time.sleep(args.rate)

    verdict(hres, fres)
    print("\n  Paste this whole output back and I will make the matching fix.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
