"""dbg_which_db.py — you have two parts.db files. Which one is real?

    python dbg_which_db.py

partdb.py falls back to ~/.rfparts/parts.db while paths.py falls back to
<repo>/Data/parts.db, so with RFPARTS_HOME unset the writers and the readers can
land on different files. This prints what is in each so you can pick one.

Read-only. Nothing is written, moved or deleted.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def candidates():
    """Every parts.db this project could plausibly be using."""
    found, seen = [], set()

    def add(path, why):
        if not path:
            return
        p = Path(path)
        try:
            key = p.resolve()
        except OSError:
            key = p
        if key in seen:
            found[[f[1] for f in found].index(key)][2].append(why)
            return
        seen.add(key)
        found.append([p, key, [why]])

    env = os.environ.get("RFPARTS_HOME", "").strip()
    if env:
        add(Path(env) / "parts.db", "RFPARTS_HOME env var")
    add(Path.home() / ".rfparts" / "parts.db", "partdb.py fallback")
    here = Path(__file__).resolve().parent
    for root in (here, here.parent):
        add(root / "Data" / "parts.db", "paths.py fallback (<repo>/Data)")
    for pkg in ("pythonrfparts", "rfparts"):
        try:
            mod = __import__(f"{pkg}.partdb", fromlist=["partdb"])
            add(Path(mod.DB_PATH), f"{pkg}.partdb.DB_PATH (what WRITES go to)")
        except Exception:
            pass
        try:
            mod = __import__(f"{pkg}.paths", fromlist=["paths"])
            add(Path(mod.DB_PATH), f"{pkg}.paths.DB_PATH (what the rest uses)")
        except Exception:
            pass
    # Strays: only worth reporting a few, and only near the project. A wide
    # search turns up every scratch database ever made and buries the two that
    # actually matter.
    strays = 0
    for root in (here, here.parent, Path.home() / "Downloads" / "rfparts"):
        if strays >= 4 or not root.is_dir():
            continue
        try:
            for f in sorted(root.rglob("parts.db")):
                if strays >= 4:
                    break
                before = len(found)
                add(f, "also on disk")
                strays += len(found) - before
        except OSError:
            pass
    return found


def describe(path):
    """Counts and a few samples, without importing the package."""
    info = {"exists": path.is_file()}
    if not info["exists"]:
        return info
    st = path.stat()
    info["size_kb"] = st.st_size / 1024
    info["modified"] = time.strftime("%Y-%m-%d %H:%M",
                                     time.localtime(st.st_mtime))
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    except Exception as e:
        info["error"] = f"{type(e).__name__}: {e}"
        return info
    try:
        tables = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        info["tables"] = len(tables)
        for t in ("parts", "specs", "qual_evidence", "scrape_log"):
            if t in tables:
                info[t] = conn.execute(
                    f"SELECT COUNT(*) n FROM {t}").fetchone()["n"]
        if "parts" in tables:
            info["vendors"] = [
                (r["vendor"], r["n"]) for r in conn.execute(
                    "SELECT vendor, COUNT(*) n FROM parts GROUP BY vendor "
                    "ORDER BY n DESC LIMIT 6")]
            info["categories"] = [
                (r["c"] or "(none)", r["n"]) for r in conn.execute(
                    "SELECT category c, COUNT(*) n FROM parts GROUP BY category "
                    "ORDER BY n DESC LIMIT 8")]
            info["mc"] = conn.execute(
                "SELECT COUNT(*) n FROM parts WHERE lower(vendor) "
                "LIKE '%mini%circuit%'").fetchone()["n"]
            r = conn.execute("SELECT MAX(last_seen) m FROM parts").fetchone()
            if r and r["m"]:
                try:
                    info["newest_part"] = time.strftime(
                        "%Y-%m-%d %H:%M", time.localtime(float(r["m"])))
                except (TypeError, ValueError):
                    info["newest_part"] = str(r["m"])[:19]
    except Exception as e:
        info["error"] = f"{type(e).__name__}: {e}"
    finally:
        conn.close()
    return info


def main():
    print(f"RFPARTS_HOME = {os.environ.get('RFPARTS_HOME') or '(not set)'}")
    found = candidates()
    print(f"\n{len(found)} candidate database(s)\n")
    live = []
    for path, _key, whys in found:
        info = describe(path)
        print("=" * 74)
        print(f"  {path}")
        print(f"  used as: {'; '.join(sorted(set(whys)))}")
        if not info["exists"]:
            print("  DOES NOT EXIST")
            continue
        live.append((path, info))
        print(f"  {info['size_kb']:.0f} KB   modified {info['modified']}"
              + (f"   newest part {info.get('newest_part')}"
                 if info.get("newest_part") else ""))
        if info.get("error"):
            print(f"  ! {info['error']}")
            continue
        print(f"  parts={info.get('parts', 0)}  specs={info.get('specs', 0)}  "
              f"evidence={info.get('qual_evidence', 0)}  "
              f"Mini-Circuits={info.get('mc', 0)}")
        if info.get("vendors"):
            print("  vendors:    " + ", ".join(
                f"{v or '(blank)'} {n}" for v, n in info["vendors"]))
        if info.get("categories"):
            print("  categories: " + ", ".join(
                f"{c} {n}" for c, n in info["categories"]))
    if not live:
        print("\nNo database found. Has a rebuild been run?")
        return 1

    print("\n" + "=" * 74)
    print("  VERDICT")
    print("=" * 74)
    best = max(live, key=lambda kv: (kv[1].get("parts", 0),
                                     kv[1].get("specs", 0)))
    for path, info in live:
        tag = "  <-- most complete" if path == best[0] else ""
        print(f"  {info.get('parts', 0):>7} parts, {info.get('specs', 0):>7} "
              f"specs   {path}{tag}")
    print(f"""
  Keep the one that matches the rest of your tree -- the folder that already
  holds Sources, cache and datasheets -- so every part of the pipeline agrees.
  That is usually <repo>\\Data, not ~/.rfparts.

  Whichever you choose, set it explicitly so the two fallbacks stop disagreeing:

      setx RFPARTS_HOME "{best[0].parent}"

  Then reopen the terminal, re-run this, and confirm every candidate resolves to
  the same file. Archive the other rather than deleting it until you have.""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
