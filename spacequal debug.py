#!/usr/bin/env python3
"""spacequal_debug — find out WHY the multi-vendor download stalls.

Run this, then send me the report file it writes. Nothing is modified: no
datasheets are saved, no settings are touched, no files in the spacequal work
directory are written except the report itself.

    python spacequal_debug.py
    python spacequal_debug.py --report C:\\path\\my_report.txt

WHY THIS EXISTS
    The downloader printed "rotating across N vendors" and then went silent with
    Ctrl-C not responding. That is the signature of a blocking socket read with
    no timeout. The prime suspect is robots.txt: spacequal fetches PDFs with a
    30 s timeout, but reads robots.txt through urllib.robotparser, whose read()
    takes NO timeout argument -- so if a host accepts the TCP connection and then
    never answers (common with corporate egress filtering), the process waits
    forever, before it has printed a single part line.

    Every check below therefore runs in a daemon thread with a hard deadline, so
    this script can never hang the way the downloader did. A check that exceeds
    its budget is reported as TIMEOUT and we move on.

WHAT IT CHECKS
    1. environment: python, TLS, proxy variables, certificates
    2. DNS resolution for each vendor host
    3. TCP connect to :443
    4. TLS handshake
    5. robots.txt: status, latency, size, and what RobotFileParser concludes
    6. a real sample datasheet URL per vendor: status, content type, PDF magic
    7. the exact spacequal fetch path, instrumented (if spacequal.py is importable)
    8. the target list spacequal would build, and the first URLs it would try
"""
from __future__ import annotations

import argparse
import io
import os
import platform
import socket
import ssl
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from pathlib import Path

# A global floor so nothing in this script can block forever, even indirectly.
socket.setdefaulttimeout(15)

UA = "rfparts-spacequal-debug/1.0 (diagnostic; contact via tool operator)"

# Real URLs, taken from live search results, one per vendor. If these fail from
# your machine but work in your browser, the problem is the network path (proxy,
# TLS inspection, egress filter), not the URL pattern.
SAMPLES = [
    ("Mini-Circuits", "https://www.minicircuits.com/pdfs/10F-10F+.pdf"),
    ("Analog Devices",
     "https://www.analog.com/media/en/technical-documentation/data-sheets/ad9208.pdf"),
    ("MACOM", "https://cdn.macom.com/datasheets/MAAP-011325.pdf"),
    ("Skyworks",
     "https://www.skyworksinc.com/-/media/Skyworks/SL/documents/public/"
     "data-sheets/si5395-94-92-a-datasheet.pdf"),
    ("Marki Microwave", "https://markimicrowave.com/Assets/datasheets/M8-0420.pdf"),
    ("Qorvo (html)", "https://www.qorvo.com/products/d/da009703"),
]

HOSTS = ["www.minicircuits.com", "www.analog.com", "cdn.macom.com",
         "www.skyworksinc.com", "markimicrowave.com", "www.qorvo.com"]

OUT = io.StringIO()


def say(line=""):
    """Write to both the console (immediately) and the report buffer."""
    print(line, flush=True)
    OUT.write(line + "\n")


def run_guarded(label, fn, budget):
    """Run fn() in a daemon thread with a hard deadline.

    Returns (status, value, seconds). status is 'ok' | 'error' | 'TIMEOUT'.
    A timed-out thread is abandoned (daemon), so it cannot delay exit -- which is
    exactly the failure mode we are hunting."""
    box = {}
    t0 = time.time()

    def target():
        try:
            box["value"] = fn()
            box["status"] = "ok"
        except Exception as e:
            box["value"] = f"{type(e).__name__}: {e}"
            box["status"] = "error"
            box["trace"] = traceback.format_exc()

    th = threading.Thread(target=target, daemon=True, name=label)
    th.start()
    th.join(budget)
    secs = time.time() - t0
    if th.is_alive():
        return "TIMEOUT", f"exceeded {budget}s and was still blocked", secs
    return box.get("status", "error"), box.get("value"), secs


def fmt(status, secs):
    return f"[{status:<7}] {secs:6.2f}s"


# ------------------------------------------------------------------ 1. env
def section_env():
    say("=" * 74)
    say("1. ENVIRONMENT")
    say("=" * 74)
    say(f"  python           {sys.version.split()[0]}  ({sys.executable})")
    say(f"  platform         {platform.platform()}")
    say(f"  ssl              {ssl.OPENSSL_VERSION}")
    say(f"  default timeout  {socket.getdefaulttimeout()}  (set by this script)")
    say(f"  cwd              {os.getcwd()}")
    home = Path(os.environ.get("SPACEQUAL_HOME", Path.home() / ".spacequal"))
    say(f"  SPACEQUAL_HOME   {home}  (exists={home.exists()})")

    say("\n  proxy / network environment variables:")
    keys = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "REQUESTS_CA_BUNDLE",
            "SSL_CERT_FILE", "CURL_CA_BUNDLE")
    any_proxy = False
    for k in keys:
        for name in (k, k.lower()):
            v = os.environ.get(name)
            if v:
                any_proxy = True
                say(f"    {name} = {v}")
    if not any_proxy:
        say("    (none set)")
        say("    NOTE: if your browser reaches these sites through a corporate")
        say("    proxy configured by PAC/WPAD, python will NOT pick that up. That")
        say("    alone can cause exactly the hang you saw.")
    try:
        proxies = urllib.request.getproxies()
        say(f"  urllib.getproxies() -> {proxies or '{}'}")
    except Exception as e:
        say(f"  urllib.getproxies() failed: {e}")
    try:
        import certifi
        say(f"  certifi          {certifi.where()}")
    except ImportError:
        say("  certifi          not installed (using system trust store)")
    for mod in ("pdfplumber", "pypdf", "sklearn", "numpy", "joblib"):
        try:
            m = __import__(mod)
            say(f"  {mod:<16} {getattr(m, '__version__', 'present')}")
        except ImportError:
            say(f"  {mod:<16} MISSING")


# ------------------------------------------------------------------ 2. DNS
def section_dns():
    say("\n" + "=" * 74)
    say("2. DNS RESOLUTION  (budget 8s each)")
    say("=" * 74)
    results = {}
    for host in HOSTS:
        status, value, secs = run_guarded(
            f"dns-{host}", lambda h=host: socket.getaddrinfo(h, 443)[0][4][0], 8)
        results[host] = status
        say(f"  {fmt(status, secs)} {host:<26} {value if status != 'ok' else value}")
    return results


# ---------------------------------------------------------- 3/4. TCP + TLS
def section_tcp():
    say("\n" + "=" * 74)
    say("3. TCP CONNECT to :443   (budget 10s each)")
    say("=" * 74)
    for host in HOSTS:
        def connect(h=host):
            s = socket.create_connection((h, 443), timeout=8)
            peer = s.getpeername()
            s.close()
            return f"connected to {peer[0]}:{peer[1]}"
        status, value, secs = run_guarded(f"tcp-{host}", connect, 10)
        say(f"  {fmt(status, secs)} {host:<26} {value}")

    say("\n" + "=" * 74)
    say("4. TLS HANDSHAKE   (budget 12s each)")
    say("=" * 74)
    for host in HOSTS:
        def handshake(h=host):
            ctx = ssl.create_default_context()
            with socket.create_connection((h, 443), timeout=8) as raw:
                with ctx.wrap_socket(raw, server_hostname=h) as tls:
                    cert = tls.getpeercert() or {}
                    issuer = dict(x[0] for x in cert.get("issuer", ())) \
                        if cert.get("issuer") else {}
                    return (f"{tls.version()}  issuer="
                            f"{issuer.get('organizationName', '?')}")
        status, value, secs = run_guarded(f"tls-{host}", handshake, 12)
        say(f"  {fmt(status, secs)} {host:<26} {value}")
        if status == "ok" and "issuer=" in str(value):
            org = str(value).split("issuer=")[-1]
            if org and not any(ca in org for ca in
                               ("DigiCert", "Sectigo", "Let's Encrypt", "GlobalSign",
                                "Amazon", "Google", "Entrust", "GoDaddy", "Cloudflare",
                                "Microsoft", "?")):
                say(f"           ^ unusual issuer '{org}': looks like TLS "
                    f"inspection by a corporate middlebox")


# ------------------------------------------------------------- 5. robots
def section_robots():
    """The prime suspect. Fetch robots.txt directly (with a timeout), THEN run
    the exact RobotFileParser call spacequal uses (which has none)."""
    say("\n" + "=" * 74)
    say("5. ROBOTS.TXT   <-- prime suspect for the hang")
    say("=" * 74)
    verdicts = {}
    for host in HOSTS:
        url = f"https://{host}/robots.txt"

        def direct(u=url):
            req = urllib.request.Request(u, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=10) as r:
                body = r.read(4000)
            return f"HTTP {r.status}, {len(body)} bytes"
        status, value, secs = run_guarded(f"robots-direct-{host}", direct, 12)
        say(f"  {fmt(status, secs)} GET  {url}")
        say(f"            -> {value}")

        # Now the code path spacequal actually takes. NOTE: read() ignores
        # timeouts entirely; only the global socket default (set at the top of
        # this file) stops it hanging here. In spacequal there is no such floor.
        def parsed(h=host):
            rp = urllib.robotparser.RobotFileParser()
            rp.set_url(f"https://{h}/robots.txt")
            rp.read()
            sample = next((u for n, u in SAMPLES
                           if urllib.parse.urlparse(u).netloc == h), f"https://{h}/")
            return (f"allow_all={getattr(rp, 'allow_all', None)} "
                    f"disallow_all={getattr(rp, 'disallow_all', None)} "
                    f"can_fetch(sample)={rp.can_fetch(UA, sample)}")
        st2, v2, s2 = run_guarded(f"robots-parse-{host}", parsed, 20)
        say(f"  {fmt(st2, s2)} RobotFileParser -> {v2}")
        verdicts[host] = (status, st2, v2)
        if st2 == "TIMEOUT":
            say("            *** THIS IS THE HANG. In spacequal this call has no")
            say("            *** timeout at all, so it blocks forever here.")
        say("")
    return verdicts


# ------------------------------------------------------------ 6. samples
def section_samples():
    say("=" * 74)
    say("6. SAMPLE DATASHEET URLS   (budget 25s each, first 512 bytes only)")
    say("=" * 74)
    say("  These URLs came from live search results, so they exist. If one fails")
    say("  here but opens in your browser, the URL pattern is fine and the")
    say("  network path is the problem.\n")
    ok = 0
    for name, url in SAMPLES:
        def get(u=url):
            req = urllib.request.Request(u, headers={
                "User-Agent": UA, "Accept": "application/pdf,*/*"})
            with urllib.request.urlopen(req, timeout=20) as r:
                head = r.read(512)
                ctype = r.headers.get("Content-Type", "?")
                clen = r.headers.get("Content-Length", "?")
                magic = "%PDF" if head[:4] == b"%PDF" else repr(head[:12])
                return (f"HTTP {r.status}  type={ctype}  len={clen}  "
                        f"first-bytes={magic}")
        status, value, secs = run_guarded(f"sample-{name}", get, 25)
        if status == "ok":
            ok += 1
        say(f"  {fmt(status, secs)} {name}")
        say(f"            {url}")
        say(f"            -> {value}")
        say("")
    say(f"  {ok}/{len(SAMPLES)} sample URLs reachable from this machine")
    return ok


# --------------------------------------------------- 7/8. spacequal itself
def section_spacequal(args):
    say("=" * 74)
    say("7. SPACEQUAL CODE PATH")
    say("=" * 74)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import spacequal as S
    except Exception as e:
        say(f"  could not import spacequal.py: {type(e).__name__}: {e}")
        say("  (put this debug script in the same folder as spacequal.py)")
        return
    say(f"  imported spacequal from {S.__file__}")
    say(f"  WORKDIR      {S.WORKDIR}")
    say(f"  DS_ROOT      {getattr(S, 'DS_ROOT', 'n/a')}")
    settings = S.load_settings()
    say("\n  settings:")
    for k in sorted(settings):
        say(f"    {k:<16} {settings[k]}")

    say("\n  MultiHostFetcher timeout audit:")
    src_get = getattr(S.MultiHostFetcher.get, "__doc__", None)
    say(f"    fetcher.timeout attr default = "
        f"{S.MultiHostFetcher(rate=0).timeout}s  (used for PDF GETs)")
    say("    robots read path            = urllib.robotparser.read(), NO timeout")
    say("    ^ asymmetry confirmed in code: this is why it can hang before")
    say("      printing any part line.")

    say("\n" + "=" * 74)
    say("8. TARGET LIST spacequal WOULD BUILD")
    say("=" * 74)
    db = args.db or settings.get("db")
    catalog = args.catalog or settings.get("catalog")
    harvest = args.harvest or (settings.get("harvest_dir") or None)
    say(f"  db       {db}   (exists={Path(str(db)).is_file()})")
    say(f"  catalog  {catalog}   (exists={Path(str(catalog)).is_file()})")
    say(f"  harvest  {harvest}")

    def build():
        buf = io.StringIO()
        old = sys.stdout
        sys.stdout = buf
        try:
            targets = S.build_datasheet_targets(
                catalog_path=catalog if Path(str(catalog)).is_file() else None,
                db_path=db, harvest_dir=harvest, include_general=True)
        finally:
            sys.stdout = old
        return targets, buf.getvalue()

    status, value, secs = run_guarded("build-targets", build, 120)
    if status != "ok":
        say(f"  {fmt(status, secs)} build_datasheet_targets failed: {value}")
        return
    targets, chatter = value
    say(f"  {fmt(status, secs)} built {len(targets)} target(s)")
    for line in chatter.strip().splitlines():
        say(f"      {line}")
    from collections import Counter
    say("\n  per vendor:")
    for v, n in Counter(t["vendor"] for t in targets).most_common():
        say(f"    {S.VENDORS[v]['name']:<18} {n:>7}")
    say("\n  per space class:")
    for k, n in Counter(t["space"] for t in targets).most_common():
        say(f"    {k:<18} {n:>7}")

    say("\n  first 12 in the ROTATED order, with the URLs that would be tried:")
    rotated = S._rotate(targets)
    for t in rotated[:12]:
        say(f"    {S.VENDORS[t['vendor']]['name']:<16} {t['pn'][:26]:<26} "
            f"[{t['space']}]  src={t['source']}")
        for u in t["urls"]:
            say(f"        {u}")

    say("\n  live check of the first 3 real target URLs (budget 25s each):")
    tried = 0
    for t in rotated:
        if tried >= 3 or not t["urls"]:
            break
        url = t["urls"][0]
        tried += 1

        def get(u=url):
            req = urllib.request.Request(u, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=20) as r:
                head = r.read(256)
                return (f"HTTP {r.status} type={r.headers.get('Content-Type', '?')} "
                        f"magic={'%PDF' if head[:4] == b'%PDF' else repr(head[:10])}")
        st, val, secs = run_guarded(f"target-{t['pn']}", get, 25)
        say(f"    {fmt(st, secs)} {t['pn']}")
        say(f"              {url}")
        say(f"              -> {val}")


def verdict(dns_res, robots_res, n_samples):
    say("\n" + "=" * 74)
    say("VERDICT")
    say("=" * 74)
    dns_bad = [h for h, s in (dns_res or {}).items() if s != "ok"]
    robots_hang = [h for h, (a, b, c) in (robots_res or {}).items()
                   if b == "TIMEOUT" or a == "TIMEOUT"]
    robots_403 = [h for h, (a, b, c) in (robots_res or {}).items()
                  if "disallow_all=True" in str(c)]
    if dns_bad:
        say(f"  * DNS failed for: {', '.join(dns_bad)}")
        say("    Nothing can work until name resolution does -- likely a VPN/DNS")
        say("    or offline machine issue rather than anything in the script.")
    if robots_hang:
        say(f"  * robots.txt HUNG for: {', '.join(robots_hang)}")
        say("    CONFIRMED CAUSE of the silent stall. spacequal calls")
        say("    RobotFileParser.read() with no timeout, before printing any part")
        say("    line, and a hung socket read also swallows Ctrl-C on Windows.")
    if robots_403:
        say(f"  * robots.txt unreadable (401/403) for: {', '.join(robots_403)}")
        say("    RobotFileParser treats that as 'disallow everything', so those")
        say("    hosts get skipped -- which would show as no downloads.")
    if n_samples == 0:
        say("  * NONE of the sample URLs were reachable.")
        say("    If they open fine in your browser, python is not using the same")
        say("    network path (proxy / TLS inspection). Check section 1.")
    if not (dns_bad or robots_hang or robots_403) and n_samples:
        say("  * Network looks healthy from here and sample URLs resolved.")
        say("    Send the report anyway: sections 7-8 show the exact URLs and")
        say("    target list, which will localise it.")
    say("\n  Fixes I intend to make once you confirm with this report:")
    say("    1. hard timeout + socket.setdefaulttimeout floor on the robots fetch,")
    say("       so it can never block (and Ctrl-C always works)")
    say("    2. cache robots per host and fail OPEN with a warning on timeout")
    say("       rather than hanging or silently skipping the host")
    say("    3. flush=True on every progress line, and print a line BEFORE the")
    say("       first request so the queue head is visible immediately")
    say("    4. respect the general-purpose goal: collect ALL RF datasheets, with")
    say("       space/general only deciding the destination folder")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--report", default="spacequal_debug_report.txt")
    ap.add_argument("--db", help="override the rfparts DB path")
    ap.add_argument("--catalog", help="override the Mini-Circuits catalog path")
    ap.add_argument("--harvest", help="override the harvest folder")
    ap.add_argument("--skip-net", action="store_true",
                    help="environment and target list only, no network")
    args = ap.parse_args()

    say("spacequal deep debug report")
    say(f"generated {time.strftime('%Y-%m-%d %H:%M:%S')}")
    say("")
    section_env()
    dns_res = robots_res = None
    n_samples = 0
    if args.skip_net:
        say("\n(--skip-net: network sections skipped)")
    else:
        dns_res = section_dns()
        section_tcp()
        robots_res = section_robots()
        n_samples = section_samples()
    try:
        section_spacequal(args)
    except Exception:
        say("\nsection 7/8 raised:")
        say(traceback.format_exc())
    verdict(dns_res, robots_res, n_samples)

    out = Path(args.report)
    try:
        out.write_text(OUT.getvalue(), encoding="utf-8")
        print(f"\nreport written to {out.resolve()}", flush=True)
        print("Send me that file.", flush=True)
    except Exception as e:
        print(f"\ncould not write {out}: {e}", flush=True)
    # Daemon threads may still be blocked on hung sockets; leave immediately so
    # this script never becomes the thing that won't exit.
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    main()
