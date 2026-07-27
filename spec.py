"""Build a structured requirement spec (a plain dict) from CLI options.

Spec keys (all optional except category):
  category        canonical category string (see registry.CATEGORY_SYNONYMS)
  freq_ghz        [lo, hi] required passband in GHz
  temp_k          operating temperature in kelvin (<=120 flags a cryo need)
  gain_db_min     minimum gain in dB
  noise_k_max     maximum noise temperature in kelvin
  attenuation_db  target attenuation in dB
  impedance_ohm   characteristic impedance in ohms (e.g. 50, 75, 33)
  connector       required connector family, e.g. "SMA"
  mount           e.g. "bulkhead"
  package         interface/package: connectorized | smt | die | flange
  space           True if the part must be space-qualified/qualifiable
  local_only      True to search only the local DB (+ DigiKey API); no crawling
  max_lead_weeks  acceptable lead time
  prefer_vendors  list of vendor names to try first
  exclude_vendors list of vendor names to skip
  other           freeform criteria (materials, "non-magnetic", ...)

Parsing is entirely local — no network, no LLM.
"""
import re

from .registry import CATEGORY_SYNONYMS

# Package/interface synonyms -> canonical label used by extract/rank.
PACKAGE_SYNONYMS = {
    "connectorized": ["connectorized", "connectorised", "coax", "coaxial", "connector"],
    "smt": ["smt", "surface-mount", "surface mount", "surfacemount", "drop-in", "drop in",
            "dropin", "qfn", "lga"],
    "die": ["die", "bare-die", "bare die", "mmic", "chip", "wirebond", "wire-bond"],
    "flange": ["flange", "flange-mount", "flange mount"],
}


def _canon_category(value):
    if not value:
        return None
    v = value.strip().lower()
    if v in CATEGORY_SYNONYMS:
        return v
    for canon, syns in CATEGORY_SYNONYMS.items():
        if v in [s.lower() for s in syns]:
            return canon
    return v  # unknown but usable as a token


def _canon_package(value):
    if not value:
        return None
    v = value.strip().lower()
    if v in PACKAGE_SYNONYMS:
        return v
    for canon, syns in PACKAGE_SYNONYMS.items():
        if v in syns:
            return canon
    return v  # unknown but usable


def _parse_range(value):
    if not value:
        return None
    parts = [p for p in value.replace("-", " ").replace(":", " ").split() if p]
    nums = []
    for p in parts:
        try:
            nums.append(float(p))
        except ValueError:
            pass
    if len(nums) >= 2:
        return [min(nums[:2]), max(nums[:2])]
    if len(nums) == 1:
        return [0.0, nums[0]]
    return None


def _parse_impedance(value):
    if value is None:
        return None
    m = re.search(r"\d+(?:\.\d+)?", str(value))
    return float(m.group()) if m else None


def build(args):
    """Construct a spec dict from an argparse namespace."""
    spec = {"category": _canon_category(getattr(args, "category", None))}
    if getattr(args, "freq", None):
        spec["freq_ghz"] = _parse_range(args.freq)
    for key in ("temp_k", "gain_db_min", "noise_k_max", "attenuation_db", "max_lead_weeks"):
        val = getattr(args, key, None)
        if val is not None:
            spec[key] = val
    if getattr(args, "impedance", None):
        z = _parse_impedance(args.impedance)
        if z is not None:
            spec["impedance_ohm"] = z
    if getattr(args, "ports", None) is not None:
        try:
            spec["ports"] = int(args.ports)
        except (TypeError, ValueError):
            pass
    if getattr(args, "connector", None):
        spec["connector"] = args.connector.upper().replace(" ", "")
    if getattr(args, "mount", None):
        spec["mount"] = args.mount.lower()
    if getattr(args, "package", None):
        spec["package"] = _canon_package(args.package)
    if getattr(args, "space", False):
        spec["space"] = True
    if getattr(args, "local_only", False):
        spec["local_only"] = True
    if getattr(args, "prefer", None):
        spec["prefer_vendors"] = args.prefer
    if getattr(args, "exclude", None):
        spec["exclude_vendors"] = args.exclude
    if getattr(args, "other", None):
        spec["other"] = args.other
    sub = getattr(args, "subcategory", None)
    if sub:
        spec["subcategory"] = sub
    terms = getattr(args, "subcategory_terms", None)
    if terms:
        spec["subcategory_terms"] = list(terms)
    return {k: v for k, v in spec.items() if v is not None}
