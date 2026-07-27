"""Evaluate each candidate against the spec, tier by verification, sort, report.

evaluate(): tags each requested criterion met / miss / unknown.
tier():     A = every requested criterion met (fully verified fit)
            B = nothing missed, but something unverifiable (unknown)
            C = at least one hard miss
rank():     drop error pages, sort A>B>C, then by #met desc, then space-readiness
            desc (space-qualified > hi-rel > unknown when space is requested),
            then price asc (RFQ/no-price items sort after priced ones in a tier).
markdown(): a human-readable report grouped by tier, with Interface and Space
            columns so surface-mount vs connectorized and space-readiness are
            visible at a glance.
"""
import re

from .registry import PARAM_SPECS, synonyms

TIER_ORDER = {"A": 0, "B": 1, "C": 2}

_CRIT_ORDER = ["category", "freq", "cutoff", "cryo", "gain", "noise", "nf", "p1db", "oip3",
               "cl", "isol", "cpl", "pwr", "atten", "imp", "ports",
               "connector", "bulkhead", "pkg", "space", "lead"]
_MOUNT_LABEL = {"connectorized": "conn", "smt": "SMT", "die": "die", "flange": "flange"}
_SPACE_LABEL = {"qualified": "space-qual", "hi_rel": "hi-rel", "qualifiable": "upscreen?"}
_SPACE_SCORE = {"qualified": 6, "hi_rel": 4, "qualifiable": 1}
# everythingRF distinguishes space QUALIFIED from space GRADE (the ingester
# records which in space_variant). Grade is worth slightly less than qualified
# but still outranks hi-rel/upscreen/none, so a graded part beats an upscreen or
# unrated part on space readiness.
_VARIANT_SCORE = {"space_qualified": 6, "space_grade": 5}
_VARIANT_LABEL = {"space_qualified": "space-qual", "space_grade": "space-grade"}


def _space_score(specs):
    """Space-readiness points, preferring the qualified/grade distinction."""
    v = specs.get("space_variant")
    if v in _VARIANT_SCORE:
        return _VARIANT_SCORE[v]
    return _SPACE_SCORE.get(specs.get("space"), 0)


def _connector_match(have, want):
    return have and want and want.replace(" ", "").upper() in have.replace(" ", "").upper()


def evaluate(candidate, spec):
    """Return the candidate with 'criteria' {name: met|miss|unknown} and counts."""
    s = candidate.get("specs", {})
    crit = {}

    def check(name, ok, known):
        crit[name] = "met" if ok else ("miss" if known else "unknown")

    if spec.get("category"):
        want = spec["category"]
        name = (candidate.get("title", "") + " " + candidate.get("url", "")).lower()
        toks = [t.lower() for t in synonyms(want)]
        # Trust the category the catalog already assigned to the candidate; only
        # fall back to matching a synonym in the title/url for live (non-catalog)
        # candidates that carry no canonical category.
        matched = candidate.get("category") == want or any(t in name for t in toks)
        check("category", matched, bool(name.strip()) or bool(candidate.get("category")))

    if spec.get("freq_ghz"):
        want_lo, want_hi = spec["freq_ghz"]
        have = s.get("freq_ghz")
        check("freq", bool(have) and have[0] <= want_lo and have[1] >= want_hi, bool(have))

    # Filter cutoff: a low/high-pass search names a TARGET cutoff. Rather than
    # requiring exact band coverage, derive the part's cutoff (upper edge for a
    # low-pass, lower edge for a high-pass) and score by closeness. rank() then
    # orders by this distance so the nearest cutoff floats to the top.
    if spec.get("cutoff_ghz") is not None:
        target = spec["cutoff_ghz"]
        fg = s.get("freq_ghz")
        sub = candidate.get("subcategory") or spec.get("subcategory")
        resp = spec.get("filter_response")
        cutoff = None
        if fg:
            cutoff = fg[0] if (sub == "hpf" or resp == "highpass") else fg[1]
        candidate["part_cutoff_ghz"] = cutoff
        if cutoff is not None:
            delta = abs(cutoff - target)
            candidate["cutoff_delta"] = delta
            check("cutoff", delta <= max(0.1, 0.1 * target), True)  # within 10% counts as met
        else:
            candidate["cutoff_delta"] = float("inf")
            check("cutoff", False, False)

    if spec.get("temp_k") is not None and spec["temp_k"] <= 120:
        check("cryo", bool(s.get("cryo")), "cryo" in s)

    if spec.get("gain_db_min") is not None:
        have = s.get("gain_db")
        check("gain", have is not None and have >= spec["gain_db_min"], have is not None)

    if spec.get("noise_k_max") is not None:
        have = s.get("noise_k")
        check("noise", have is not None and have <= spec["noise_k_max"], have is not None)

    if spec.get("attenuation_db") is not None:
        have = s.get("attenuation_db")
        check("atten", have is not None and abs(have - spec["attenuation_db"]) < 0.6, have is not None)

    if spec.get("impedance_ohm") is not None:
        have = s.get("impedance_ohm")
        check("imp", have is not None and abs(have - spec["impedance_ohm"]) < 0.5, have is not None)

    if spec.get("ports") is not None:
        have = s.get("ports")
        check("ports", have is not None and have == spec["ports"], have is not None)

    if spec.get("connector"):
        have = s.get("connector")
        check("connector", _connector_match(have, spec["connector"]), have is not None)

    if spec.get("mount") == "bulkhead":
        check("bulkhead", bool(s.get("bulkhead")), "bulkhead" in s)

    if spec.get("package"):
        have = s.get("mount_type")
        known = have is not None and have != "unknown"
        check("pkg", known and have == spec["package"], known)

    # space qualification: met only if explicitly space-qualified; hi-rel or
    # no-signal -> unknown (needs human review). Never a hard miss, since a web
    # page can't prove a part is *un*-qualifiable.
    if spec.get("space"):
        sig = s.get("space")
        check("space", sig == "qualified", sig == "qualified")

    if spec.get("max_lead_weeks") is not None:
        have = s.get("lead_weeks")
        check("lead", have is not None and have <= spec["max_lead_weeks"], have is not None)

    # Optional parametric criteria (noise figure, P1dB, OIP3, isolation, ...).
    # Producers disagree on some key names: catalog.py and partdb emit
    # "noise_nf_db" while PARAM_SPECS calls it "nf_db", so NF-bearing catalog
    # candidates were ranked "unknown" ("nf couldn't be verified") with the
    # value sitting right in their specs. Alias lookup fixes every producer at
    # the single consumer instead of chasing each source.
    _ALIASES = {"nf_db": ("noise_nf_db",)}
    for key, meta in PARAM_SPECS.items():
        want = spec.get(meta["spec_key"])
        if want is None:
            continue
        have = s.get(key)
        if have is None:
            for alt in _ALIASES.get(key, ()):
                have = s.get(alt)
                if have is not None:
                    break
        known = have is not None
        if meta["kind"] == "min":
            ok = known and have >= want
        elif meta["kind"] == "max":
            ok = known and have <= want
        else:  # approx
            ok = known and abs(have - want) <= meta.get("tol", 0.0)
        check(meta["crit"], ok, known)

    candidate["criteria"] = crit
    candidate["met"] = sum(1 for v in crit.values() if v == "met")
    candidate["miss"] = sum(1 for v in crit.values() if v == "miss")
    candidate["unknown"] = sum(1 for v in crit.values() if v == "unknown")
    # A known value must outrank an otherwise-identical unknown value.  Unknown
    # criteria receive partial credit, hard misses receive none.  Explicit
    # hi-rel/space-qualified evidence adds a modest readiness bonus even when
    # space qualification was not entered as a hard search requirement.
    total = max(1, len(crit))
    evidence_points = candidate["met"] * 100 + candidate["unknown"] * 45
    # Reserve six points for reliability evidence so hi-rel parts visibly
    # score above otherwise-identical commercial parts.
    base_score = (evidence_points / total) * 0.94
    candidate["space_score"] = _space_score(s)
    candidate["fit_score"] = max(0, min(100, round(base_score + candidate["space_score"])))
    return candidate


def tier(candidate):
    if candidate["miss"] > 0:
        return "C"
    if candidate["unknown"] > 0:
        return "B"
    return "A"


def _price_key(candidate):
    p = candidate.get("specs", {}).get("price_usd")
    return (0, p) if isinstance(p, (int, float)) else (1, 0)  # priced first, RFQ last


_WAY_RE = re.compile(r"(\d+)\s*-?\s*way\b", re.I)


def _ways(candidate):
    """Number of output ways for a divider/combiner, or 0 if unknown.
    Prefers an explicit ways/ports spec, else parses 'N-way' from the name."""
    s = candidate.get("specs", {})
    n = s.get("no_of_ways")
    if isinstance(n, (int, float)) and not isinstance(n, bool) and n > 0:
        return int(n)
    p = s.get("ports")
    if isinstance(p, int) and not isinstance(p, bool) and p >= 3:
        return p - 1                      # an N-way divider has N+1 ports
    blob = (f"{candidate.get('title', '')} {candidate.get('description', '')} "
            f"{candidate.get('model', '')} {s.get('subcategory_hint', '')}")
    m = _WAY_RE.search(blob)
    return int(m.group(1)) if m else 0


def _ways_key(candidate):
    """Divider ordering: 2-way, 4-way, 8-way ascending; unknown ways sink within
    their score group. Zero (no effect) for every other category."""
    if candidate.get("category") == "divider":
        w = _ways(candidate)
        return w if w > 0 else 10 ** 6
    return 0


def rank(candidates, spec):
    scored = []
    for c in candidates:
        if c.get("specs", {}).get("_error"):
            continue
        evaluate(c, spec)
        c["tier"] = tier(c)
        scored.append(c)
    cutoff_search = spec.get("cutoff_ghz") is not None
    # Score first (tier, then fit); among equally-scored parts, order dividers by
    # number of ways so 2-/4-/8-way Wilkinsons group in order instead of by an
    # arbitrary tiebreak.
    scored.sort(key=lambda c: (
        (c.get("cutoff_delta", float("inf")) if cutoff_search else 0.0),
        TIER_ORDER[c["tier"]], -c.get("fit_score", 0), _ways_key(c),
        c.get("unknown", 0), -c.get("space_score", 0), -c.get("met", 0), _price_key(c)))
    return scored


# --- reporting ------------------------------------------------------------
def _lead_str(c):
    lw = c.get("specs", {}).get("lead_weeks")
    if lw == 0:
        return "in stock"
    if lw:
        return f"~{lw:g} wk"
    return "?"


def _price_str(c):
    p = c.get("specs", {}).get("price_usd")
    return f"${p:,.0f}" if isinstance(p, (int, float)) else "RFQ"


def _mount_str(c):
    return _MOUNT_LABEL.get(c.get("specs", {}).get("mount_type"), "?")


def _space_str(c):
    specs = c.get("specs", {})
    v = specs.get("space_variant")
    if v in _VARIANT_LABEL:
        return _VARIANT_LABEL[v]
    return _SPACE_LABEL.get(specs.get("space"), "—")


def _crit_str(c):
    marks = {"met": "✓", "miss": "✗", "unknown": "?"}
    return " ".join(
        f"{k}{marks[c['criteria'][k]]}" for k in _CRIT_ORDER if k in c.get("criteria", {}))


def markdown(results, spec, errors=None):
    lines = ["# RF parts search", ""]
    want = spec.get("category", "part")
    if spec.get("freq_ghz"):
        want += f", {spec['freq_ghz'][0]:g}–{spec['freq_ghz'][1]:g} GHz"
    if spec.get("cutoff_ghz") is not None:
        want += f", cutoff \u2248 {spec['cutoff_ghz']:g} GHz"
    if spec.get("temp_k") is not None:
        want += f", {spec['temp_k']:g} K"
    if spec.get("impedance_ohm") is not None:
        want += f", {spec['impedance_ohm']:g} Ω"
    if spec.get("package"):
        want += f", {spec['package']}"
    if spec.get("space"):
        want += ", space-qualified"
    lines += [f"**Requirement:** {want}", ""]
    labels = {"A": "Verified fit", "B": "Likely fit (unverified specs)", "C": "Partial / mismatch"}
    for t in ("A", "B", "C"):
        group = [c for c in results if c["tier"] == t]
        if not group:
            continue
        lines += [f"## {labels[t]} ({len(group)})", ""]
        lines += ["| Score | Vendor | Part | Interface | Space | Criteria | Price | Lead | URL |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for c in group:
            lines.append(
                f"| {c.get('tier', '?')} {c.get('fit_score', 0):d}/100 | {c.get('vendor', '?')} | {c.get('title', '?')[:44]} | {_mount_str(c)} "
                f"| {_space_str(c)} | {_crit_str(c)} | {_price_str(c)} | {_lead_str(c)} "
                f"| {c['url']} |")
        lines.append("")
    if errors:
        lines += ["## Vendors with no usable results", ""]
        lines += [f"- {v}" for v in errors] + [""]
    lines += ["---", "",
              "*Interface and Space columns are heuristic signals mined from vendor "
              "text/datasheets — confirm package and qualification status against the "
              "actual datasheet and screening flow before relying on them.*"]
    return "\n".join(lines)
