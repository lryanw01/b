"""Vendor registry: load vendors.yaml (package file + optional user overlay),
define category synonyms, and select the vendor subset that carries a category.

The user overlay lets you extend/override the shipped list without editing the
package: drop a vendors.yaml (same schema) into $RFPARTS_HOME (default ~/.rfparts).
Entries are merged by "name".

Canonical category keys are stable lowercase snake_case identifiers (e.g.
``phase_shifter``). Human-readable and plural forms live in the synonym lists so
that spec._canon_category resolves a typed "Phase Shifters" to ``phase_shifter``,
catalog discovery can match a vendor's free-text group/label, and the GUI dropdown
(built from these keys) stays consistent with catalog.CODE_TO_CATEGORY.
"""
import os
import pathlib
import re

import yaml  # safe_load only — never yaml.load

# Where the tool keeps its cache, results, and optional user overlay.
DATA = pathlib.Path(os.environ.get("RFPARTS_HOME", "~/.rfparts")).expanduser()
DATA.mkdir(parents=True, exist_ok=True)

VENDORS_FILE = pathlib.Path(__file__).resolve().parent / "vendors.yaml"
USER_VENDORS = DATA / "vendors.yaml"  # optional overlay, merged by name

# Canonical category -> synonyms, used for vendor selection and URL/keyword
# filtering during discovery. Keys are snake_case; keep the human/plural forms in
# the value lists (they drive typed-input resolution and free-text matching).
# NOTE: avoid very short synonyms (e.g. a bare "ps") — during catalog discovery
# they substring-match unrelated text ("amps", "steps") and create false hits.
CATEGORY_SYNONYMS = {
    "amplifier": ["amplifier", "amp", "gain block", "power amplifier",
                  "high power amplifier", "lna", "low noise amplifier",
                  "low-noise amplifier", "driver amplifier", "buffer amplifier",
                  "variable gain amplifier", "if amplifier",
                  "limiting amplifier", "logarithmic amplifier", "log amplifier"],
    "attenuator": ["attenuator", "atten", "pad", "fixed attenuator",
                   "variable attenuator", "step attenuator", "digital step attenuator",
                   "programmable attenuator", "voltage variable attenuator",
                   "impedance matching pad", "matching pad", "min-loss pad",
                   "minimum loss pad"],
    "termination": ["termination", "terminator", "load"],
    "isolator": ["isolator"],
    "circulator": ["circulator"],
    "cable": ["cable", "coax", "coaxial", "jumper", "assembly"],
    "connector": ["connector", "adapter", "adaptor"],
    "feedthrough": ["feedthrough", "feed-through", "hermetic", "bulkhead"],
    "filter": ["filter", "bandpass", "lowpass", "highpass", "bpf", "lpf", "hpf"],
    "mixer": ["mixer", "frequency mixer"],
    "coupler": ["coupler", "directional coupler", "hybrid", "90/180 degree hybrid"],
    "divider": ["divider", "splitter", "combiner", "power divider",
                "power splitter", "power splitters / combiners"],
    "switch": ["switch", "spdt", "sp3t", "sp4t", "transfer switch"],
    "dc_block": ["dc block", "dc-block"],
    "bias_tee": ["bias tee", "bias-tee"],
    "detector": ["detector", "sdlva", "log detector"],
    "phase_shifter": ["phase shifter", "phase shifters"],
    "equalizer": ["equalizer", "equalizers", "gain equalizer"],
    "frequency_multiplier": ["frequency multiplier", "frequency multipliers",
                             "multiplier", "doubler", "tripler"],
    "limiter": ["limiter", "limiters"],
    "power_sensor": ["power sensor", "power sensors"],
    "power_detector": ["power detector", "power detectors"],
    "phase_detector": ["phase detector", "phase detectors"],
    "balun": ["balun", "baluns", "transformer", "transformers",
              "transformers / baluns"],
    "modulator": ["modulator", "modulators", "iq modulator"],
    "demodulator": ["demodulator", "demodulators", "iq demodulator"],
    "oscillator": ["oscillator", "oscillators", "vco"],
    "synthesizer": ["synthesizer", "synthesizers", "frequency synthesizer", "pll"],
    "mmic_die": ["mmic die", "mmic die parts", "bare die"],
    "waveguide": ["waveguide", "waveguides"],
}


def synonyms(category):
    """Synonym list for a category (falls back to the category itself)."""
    if not category:
        return []
    return CATEGORY_SYNONYMS.get(category, [category])


# --- GUI-facing category model -------------------------------------------
# For each user-facing category: a display label, the subcategories that refine
# a search (each with keyword/match synonyms), the ordered list of spec input
# fields worth showing (only fields the pipeline can actually match/rank), and
# the key parameters that matter for that category (shown as a hint and folded
# into the search keyword). `fields` values are gui form keys; the matchable set
# is: freq, temp_k, gain_db_min, noise_k_max, attenuation_db, ports, impedance,
# connector, package, space, max_lead_weeks.
#
# subcategory value = (display label, [synonyms]). The first synonym is used to
# specialize the DigiKey keyword (e.g. "RF low noise amplifier").
_COMMON_TAIL = ["connector", "package", "impedance", "space", "max_lead_weeks"]

CATEGORY_SPECS = {
    "amplifier": {
        "label": "Amplifiers",
        "subcategories": {
            "lna": ("LNA — low noise", ["low noise amplifier", "lna"]),
            "pa": ("PA — power", ["power amplifier", "pa"]),
            "hpa": ("HPA — high power", ["high power amplifier", "hpa"]),
            "driver": ("Driver / gain block", ["driver amplifier", "gain block"]),
            "vga": ("VGA — variable gain", ["variable gain amplifier", "vga"]),
            "if": ("IF amplifier", ["if amplifier"]),
            "buffer": ("Buffer", ["buffer amplifier"]),
            "log": ("Log / limiting", ["logarithmic amplifier", "limiting amplifier"]),
        },
        "fields": ["freq", "gain_db_min", "noise_k_max", "temp_k"] + _COMMON_TAIL,
        "key_params": ["gain", "noise figure", "P1dB", "OIP3"],
    },
    "attenuator": {
        "label": "Attenuators / pads",
        "subcategories": {
            "fixed": ("Fixed", ["fixed attenuator"]),
            "step": ("Step / programmable (DSA)", ["digital step attenuator", "step attenuator"]),
            "vva": ("Voltage-variable (VVA)", ["voltage variable attenuator", "vva"]),
            "minloss": ("Minimum-loss pad", ["min-loss pad", "minimum loss pad"]),
            "matching": ("Impedance matching pad", ["impedance matching pad", "matching pad"]),
        },
        "fields": ["freq", "attenuation_db"] + _COMMON_TAIL,
        "key_params": ["attenuation", "power handling", "VSWR", "flatness"],
    },
    "coupler": {
        "label": "Couplers",
        "subcategories": {
            "directional": ("Directional", ["directional coupler"]),
            "dual": ("Dual-directional", ["dual directional coupler"]),
            "bidirectional": ("Bi-directional", ["bi-directional coupler"]),
            "hybrid90": ("90° hybrid (quadrature)", ["90 degree hybrid", "quadrature hybrid"]),
            "hybrid180": ("180° hybrid (rat-race)", ["180 degree hybrid", "rat-race coupler"]),
        },
        "fields": ["freq", "ports"] + _COMMON_TAIL,
        "key_params": ["coupling", "directivity", "insertion loss", "isolation"],
    },
    "divider": {
        "label": "Dividers / combiners",
        "subcategories": {
            "wilkinson": ("Wilkinson", ["wilkinson power divider"]),
            "resistive": ("Resistive", ["resistive power divider"]),
            "reactive": ("Reactive", ["reactive power divider"]),
            "hybrid": ("Hybrid", ["hybrid power divider"]),
            "2way": ("2-way", ["2 way power divider"]),
            "4way": ("4-way", ["4 way power divider"]),
            "8way": ("8-way", ["8 way power divider"]),
            "combiner": ("Combiner", ["power combiner"]),
        },
        "fields": ["freq"] + _COMMON_TAIL,
        "key_params": ["number of ways", "isolation", "insertion loss", "amplitude/phase balance"],
    },
    "filter": {
        "label": "Filters",
        "subcategories": {
            "lpf": ("LPF — low-pass", ["low pass filter", "lowpass filter", "lpf"]),
            "hpf": ("HPF — high-pass", ["high pass filter", "highpass filter", "hpf"]),
            "bpf": ("BPF — band-pass", ["band pass filter", "bandpass filter", "bpf"]),
            "bsf": ("BSF — band-stop / notch", ["band stop filter", "bandstop filter",
                                                "band reject filter", "band-reject filter",
                                                "notch filter", "notch", "bsf"]),
            "diplexer": ("Diplexer", ["diplexer"]),
            "duplexer": ("Duplexer", ["duplexer"]),
            "cavity": ("Cavity", ["cavity filter"]),
            "ceramic": ("Ceramic", ["ceramic filter"]),
            "tunable": ("Tunable", ["tunable filter"]),
        },
        "fields": ["freq"] + _COMMON_TAIL,
        "key_params": ["passband", "cutoff", "insertion loss", "rejection", "bandwidth"],
    },
    "limiter": {
        "label": "Limiters",
        "subcategories": {
            "pin": ("PIN diode", ["pin diode limiter"]),
            "diode": ("Diode", ["diode limiter"]),
            "receiver": ("Receiver protector", ["receiver protector"]),
            "prelimiter": ("Pre-limiter", ["pre-limiter"]),
        },
        "fields": ["freq"] + _COMMON_TAIL,
        "key_params": ["threshold", "flat leakage", "max input power", "recovery time"],
    },
    "mixer": {
        "label": "Mixers",
        "subcategories": {
            "double": ("Double-balanced", ["double balanced mixer"]),
            "triple": ("Triple-balanced", ["triple balanced mixer"]),
            "single": ("Single-balanced", ["single balanced mixer"]),
            "iq": ("IQ / image-reject", ["iq mixer", "image reject mixer"]),
            "up": ("Upconverter", ["upconverter"]),
            "down": ("Downconverter", ["downconverter"]),
            "harmonic": ("Harmonic", ["harmonic mixer"]),
            "active": ("Active", ["active mixer"]),
        },
        "fields": ["freq"] + _COMMON_TAIL,
        "key_params": ["conversion loss", "LO drive", "isolation", "RF/LO/IF bands"],
    },
    "phase_shifter": {
        "label": "Phase shifters",
        "subcategories": {
            "analog": ("Analog", ["analog phase shifter"]),
            "digital": ("Digital", ["digital phase shifter"]),
            "voltage": ("Voltage-controlled", ["voltage controlled phase shifter"]),
            "reflection": ("Hybrid / reflection", ["reflection phase shifter"]),
            "vector": ("Vector modulator", ["vector modulator"]),
        },
        "fields": ["freq"] + _COMMON_TAIL,
        "key_params": ["phase range", "resolution / bits", "insertion loss", "phase error"],
    },
    "switch": {
        "label": "Switches",
        "subcategories": {
            # Subcategory for a switch is now its CONSTRUCTION type only. The throw count
        # (SPST/SPDT/SP4T...) moved to its own throw_config field, because they are
        # two independent facts -- an SP4T can be absorptive or reflective -- and
        # keeping them in one dropdown meant picking one made the other
        # unselectable.
        "absorptive": ("Absorptive", ["absorptive switch", "terminated switch",
                                      "non-reflective switch", "50 ohm terminated"]),
        "reflective": ("Reflective", ["reflective switch", "shunt switch"]),
        },
        "fields": ["freq"] + _COMMON_TAIL,
        "key_params": ["isolation", "insertion loss", "switching speed", "power handling"],
    },
    "termination": {
        "label": "Terminations / loads",
        "subcategories": {
            "coaxial": ("Coaxial load", ["coaxial termination"]),
            "chip": ("Chip / SMT", ["chip termination"]),
            "highpower": ("High-power", ["high power termination"]),
            "feedthrough": ("Feedthrough", ["feedthrough termination"]),
        },
        "fields": ["freq"] + _COMMON_TAIL,
        "key_params": ["power handling", "VSWR", "return loss"],
    },
}

# --- space-semiconductor categories --------------------------------------
# The space catalogs (ADI, TI) carry non-RF space-qualified parts (data
# converters, power management, clocks, interface, sensors) plus a few RF-system
# families (beamformers, transceivers). They're added here so those parts are
# browsable in the same GUI. Fields are minimal (space + package + lead) because
# the source sheets don't carry RF parametrics for them.
_SPACE_IC_SYNONYMS = {
    "data_converter": ["data converter", "adc", "dac", "analog to digital",
                       "digital to analog", "a/d converter", "d/a converter"],
    "power": ["power management", "regulator", "ldo", "dc-dc", "dc/dc",
              "voltage reference", "pmic", "point of load", "power"],
    "clock": ["clock", "timing", "clock and timing", "clock generator",
              "clock buffer", "clock distribution"],
    "sensor": ["sensor", "temperature sensor", "current sense"],
    "interface": ["interface", "transceiver ic", "rs-485", "rs-422", "lvds",
                  "digital isolator", "can transceiver"],
    "beamformer": ["beamformer", "beamforming", "beam former"],
    "transceiver": ["rf transceiver", "transceiver", "mxfe"],
    "ic": ["integrated circuit", "analog function", "logic", "asic"],
}
CATEGORY_SYNONYMS.update(_SPACE_IC_SYNONYMS)

# Baluns / RF transformers. "balun" was already a synonym entry but had no
# CATEGORY_SPECS record, so it never appeared in GUI_CATEGORIES and had no field
# list — balun listings ingested from everythingRF were unreachable in the GUI.
CATEGORY_SPECS.update({
    "balun": {
        "label": "Baluns / transformers",
        "subcategories": {
            "balun": ("Balun (unbal\u2192bal)", ["balun"]),
            "transformer": ("RF transformer", ["rf transformer", "impedance transformer"]),
            "active": ("Active balun", ["active balun"]),
            "passive": ("Passive balun", ["passive balun"]),
        },
        "fields": ["freq", "connector", "package", "impedance", "space",
                   "max_lead_weeks"],
        "key_params": ["insertion loss", "amplitude/phase balance",
                       "impedance ratio", "return loss"],
    },
})

_SPACE_IC_FIELDS = ["space", "package", "max_lead_weeks"]
_SPACE_IC_RF_FIELDS = ["freq", "space", "package", "max_lead_weeks"]
CATEGORY_SPECS.update({
    "data_converter": {"label": "Data converters (ADC/DAC)", "subcategories": {},
                       "fields": _SPACE_IC_FIELDS, "key_params": []},
    "power": {"label": "Power management", "subcategories": {},
              "fields": _SPACE_IC_FIELDS, "key_params": []},
    "clock": {"label": "Clock & timing", "subcategories": {},
              "fields": _SPACE_IC_FIELDS, "key_params": []},
    "sensor": {"label": "Sensors", "subcategories": {},
               "fields": _SPACE_IC_FIELDS, "key_params": []},
    "interface": {"label": "Interface / isolation", "subcategories": {},
                  "fields": _SPACE_IC_FIELDS, "key_params": []},
    "beamformer": {"label": "Beamformers", "subcategories": {},
                   "fields": _SPACE_IC_RF_FIELDS, "key_params": []},
    "transceiver": {"label": "RF transceivers / MxFE", "subcategories": {},
                    "fields": _SPACE_IC_RF_FIELDS, "key_params": []},
    "ic": {"label": "Other space ICs", "subcategories": {},
           "fields": _SPACE_IC_FIELDS, "key_params": []},
})


# --- categories seen in the vendor catalogues -----------------------------
# Walking Qorvo, MACOM, Skyworks and Marki turns up families the space-only
# dataset never contained. Added additively so nothing existing shifts, with
# minimal field lists: the vendor listings give frequency and package reliably,
# and little else in common across families.
_VENDOR_CAT_SYNONYMS = {
    "modulator": ["modulator", "demodulator", "iq modulator",
                  "vector modulator", "modulators & demodulators"],
    "synthesizer": ["synthesizer", "synthesiser", "pll", "pll synthesizer",
                    "frequency synthesizer"],
    "detector": ["detector", "power detector", "rf detector",
                 "successive detection log video amplifier"],
    "multiplier": ["multiplier", "frequency multiplier", "doubler", "tripler"],
    "transistor": ["transistor", "hemt", "phemt", "gan hemt", "gaas phemt",
                   "rf transistor", "ldmos"],
    "diode": ["diode", "pin diode", "schottky diode", "varactor",
              "limiter diode"],
    "oscillator": ["oscillator", "vco", "dro", "ocxo", "tcxo", "xo",
                   "voltage controlled oscillator"],
    "isolator": ["isolator", "rf isolator"],
    "circulator": ["circulator", "rf circulator"],
    "equalizer": ["equalizer", "equaliser", "gain equalizer",
                  "cable equalizer"],
    "bias_tee": ["bias tee", "bias-tee", "bias tees"],
}
CATEGORY_SYNONYMS.update(_VENDOR_CAT_SYNONYMS)

_VC_FIELDS = ["freq", "package", "space", "max_lead_weeks"]
_VENDOR_CAT_SYNONYMS["op_amp"] = [
    "op amp", "op-amp", "operational amplifier", "precision amplifier",
    "instrumentation amplifier", "difference amplifier", "current sense amplifier",
    "precision amplifiers",
]
CATEGORY_SYNONYMS.update({"op_amp": _VENDOR_CAT_SYNONYMS["op_amp"]})

_VC_LABELS = {
    "modulator": "Modulators / demodulators",
    "synthesizer": "Synthesizers & PLLs",
    "detector": "Detectors",
    "multiplier": "Frequency multipliers",
    "transistor": "Transistors (GaN / GaAs)",
    "diode": "Diodes (PIN / Schottky / varactor)",
    "oscillator": "Oscillators & VCOs",
    "isolator": "Isolators",
    "circulator": "Circulators",
    "equalizer": "Equalizers",
    "bias_tee": "Bias tees",
    # A precision op-amp is not an RF amplifier: it has no RF gain, noise figure,
    # OIP3 or P1dB, so it must not be asked for them.
    "op_amp": "Op-amps / precision amplifiers",
}
CATEGORY_SPECS.update({
    key: {"label": label, "subcategories": {}, "fields": list(_VC_FIELDS),
          "key_params": []}
    for key, label in _VC_LABELS.items()
})


# Ordered list of the user-facing categories (canonical keys) for the GUI.
GUI_CATEGORIES = list(CATEGORY_SPECS.keys())


# --- optional parametric specs (user-enterable, extracted + ranked) --------
# Each entry is a search criterion the user can enter for the categories that
# expose it. `key` (dict key) is where extract stores the mined value on a
# candidate's specs; `spec_key` is where the requirement lives on the query;
# `kind` sets the comparison (min: have>=want, max: have<=want, approx: within
# `tol`); `crit` is the short label shown in the results grid. `patterns` are
# regexes (first capture group = number) mined from candidate text when a
# structured value isn't already present. These are best-effort: when nothing is
# found the criterion is "unknown", not a hard miss.
# --------------------------------------------------------------- throw config
# A switch's pole/throw arrangement (SPDT, SP4T, transfer). everythingRF exposes
# it as the "Configuration" attribute; vendor catalogues and datasheets only ever
# write it into the title or description, so it needs a text parser too.
#
# Written long-hand as well as short: "Single Pole Four Throw", "1P4T", "SP-4T".
_THROW_WORDS = {"single": 1, "one": 1, "double": 2, "two": 2, "dual": 2,
                "triple": 3, "three": 3, "quad": 4, "four": 4, "five": 5,
                "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
                "twelve": 12, "sixteen": 16}
_THROW_COMPACT = re.compile(
    r"\b(?:SP|1P|DP|2P|(\d)P)\s*-?\s*(\d{1,2}|S|D|T|Q)\s*T\b", re.I)
_THROW_LONG = re.compile(
    r"\b(single|double|dual|two|one|three|triple|four|quad)\s+pole\s+"
    r"(single|double|dual|two|one|three|triple|four|quad|five|six|seven|eight|"
    r"nine|ten|twelve|sixteen)\s+throw\b", re.I)
_TRANSFER_RE = re.compile(r"\btransfer\s+switch\b|\bDPDT\s*transfer\b", re.I)
_THROW_LETTER = {"s": 1, "d": 2, "t": 3, "q": 4}


def throw_config(*texts):
    """Canonical pole/throw string ('SPDT', 'SP4T', 'DPDT', 'TRANSFER') or "".

    Checked in order of reliability: an explicit compact form, then the long-hand
    wording, then 'transfer switch'. Returns "" rather than guessing, because a
    wrong throw count is worse than none -- it silently excludes the right part
    from a filtered search."""
    for raw in texts:
        s = str(raw or "")
        if not s:
            continue
        m = _THROW_COMPACT.search(s)
        if m:
            poles = m.group(1)
            head = s[m.start():m.start() + 2].upper()
            npole = int(poles) if poles else (2 if head.startswith(("DP", "2P"))
                                              else 1)
            throw_raw = m.group(2).lower()
            nthrow = _THROW_LETTER.get(throw_raw)
            if nthrow is None:
                try:
                    nthrow = int(throw_raw)
                except ValueError:
                    continue
            if not (1 <= npole <= 4 and 1 <= nthrow <= 32):
                continue
            return _throw_str(npole, nthrow)
        m = _THROW_LONG.search(s)
        if m:
            npole = _THROW_WORDS.get(m.group(1).lower())
            nthrow = _THROW_WORDS.get(m.group(2).lower())
            if npole and nthrow:
                return _throw_str(npole, nthrow)
        if _TRANSFER_RE.search(s):
            return "TRANSFER"
    return ""


def _throw_str(npole, nthrow):
    pole = {1: "SP", 2: "DP", 3: "3P", 4: "4P"}.get(npole, f"{npole}P")
    throw = {1: "ST", 2: "DT", 3: "3T"}.get(nthrow, f"{nthrow}T")
    return pole + throw


# Switch throw configurations, in the order a dropdown should show them. Also the
# vocabulary the parsers normalise to (see specmatch.parse_throw_config).
THROW_CONFIG_CHOICES = ["SPST", "SPDT", "SP3T", "SP4T", "SP5T", "SP6T", "SP8T",
                        "SP10T", "SP12T", "SP16T", "DPDT", "DP3T", "DP4T",
                        "4P4T", "transfer"]

SWITCH_TYPE_CHOICES = ["absorptive", "reflective"]

# Params the GUI should render as a dropdown rather than a free-text box, so a new
# enumerated spec only has to be declared here to get the right widget.
FIELD_CHOICES = {
    "throw_config": THROW_CONFIG_CHOICES,
}


PARAM_SPECS = {
    "nf_db": {
        "spec_key": "nf_db_max", "label": "Max noise figure (dB)", "kind": "max",
        "crit": "nf", "tol": 0.0,
        "patterns": [r"noise\s*figure[^.\n\r]{0,40}?(\d+(?:\.\d+)?)\s*dB",
                     r"\bNF\b[^.\n\r]{0,20}?(\d+(?:\.\d+)?)\s*dB"]},
    "p1db_dbm": {
        "spec_key": "p1db_dbm_min", "label": "Min P1dB (dBm)", "kind": "min",
        "crit": "p1db", "tol": 0.0,
        # "output power" is deliberately NOT here. It means saturated output
        # power (Psat/Pout), not the 1 dB compression point, and matching it
        # made P1dB come out equal to Psat on any part that quoted only an
        # output power -- two specs that differ by several dB on a real device.
        "patterns": [r"P\s*1\s*-?\s*dB[^.\n\r]{0,30}?([+-]?\d+(?:\.\d+)?)\s*dBm",
                     r"1\s*-?\s*dB\s*compression[^.\n\r]{0,30}?"
                     r"([+-]?\d+(?:\.\d+)?)\s*dBm",
                     r"output\s*P1\s*-?\s*dB[^.\n\r]{0,25}?"
                     r"([+-]?\d+(?:\.\d+)?)\s*dBm"]},
    "oip3_dbm": {
        "spec_key": "oip3_dbm_min", "label": "Min OIP3 (dBm)", "kind": "min",
        "crit": "oip3", "tol": 0.0,
        "patterns": [r"OIP3[^.\n\r]{0,30}?([+-]?\d+(?:\.\d+)?)\s*dBm",
                     r"\bIP3\b[^.\n\r]{0,30}?([+-]?\d+(?:\.\d+)?)\s*dBm",
                     r"(?:third|3rd)[-\s]*order[^.\n\r]{0,30}?([+-]?\d+(?:\.\d+)?)\s*dBm"]},
    "psat_dbm": {
        "spec_key": "psat_dbm_min", "label": "Min Psat (dBm)", "kind": "min",
        "crit": "psat", "tol": 0.0,
        "patterns": [r"(?:saturated|sat\.?)\s*(?:output\s*)?power"
                     r"[^.\n\r]{0,25}?([+-]?\d+(?:\.\d+)?)\s*dBm",
                     r"\bPsat\b[^.\n\r]{0,25}?([+-]?\d+(?:\.\d+)?)\s*dBm",
                     r"\bPout\b[^.\n\r]{0,25}?([+-]?\d+(?:\.\d+)?)\s*dBm",
                     r"output\s*power[^.\n\r]{0,25}?"
                     r"([+-]?\d+(?:\.\d+)?)\s*dBm"]},
    "throw_config": {
        "spec_key": "throw_config", "label": "Config (SPDT/SP4T)",
        "kind": "text", "crit": "thr", "tol": 0.0,
        "patterns": []},
    "isolation_db": {
        "spec_key": "isolation_db_min", "label": "Min isolation (dB)", "kind": "min",
        "crit": "isol", "tol": 0.0,
        "patterns": [r"isolation[^.\n\r]{0,30}?(\d+(?:\.\d+)?)\s*dB"]},
    "conversion_loss_db": {
        "spec_key": "conversion_loss_db_max", "label": "Max conversion loss (dB)",
        "kind": "max", "crit": "cl", "tol": 0.0,
        "patterns": [r"conversion\s*loss[^.\n\r]{0,30}?(\d+(?:\.\d+)?)\s*dB"]},
    "coupling_db": {
        "spec_key": "coupling_db", "label": "Coupling (dB)", "kind": "approx",
        "crit": "cpl", "tol": 1.0,
        "patterns": [r"coupling[^.\n\r]{0,30}?(\d+(?:\.\d+)?)\s*dB"]},
    "switching_time_ns": {
        "spec_key": "switching_time_ns_max", "label": "Max switching time (ns)",
        "kind": "max", "crit": "tsw", "tol": 0.0,
        # Switching speed is quoted in ns, us or ms depending on the technology
        # (PIN diode tens of ns, electromechanical milliseconds), so the unit has
        # to be captured and normalised. Without unit_group every value would be
        # stored as a bare float and 2 ms would sort as faster than 50 ns.
        "unit_group": 2,
        "unit_scale": {"ns": 1.0, "nsec": 1.0, "nanosecond": 1.0,
                       "us": 1e3, "usec": 1e3, "\u00b5s": 1e3, "\u03bcs": 1e3,
                       "microsecond": 1e3,
                       "ms": 1e6, "msec": 1e6, "millisecond": 1e6,
                       "s": 1e9, "sec": 1e9, "second": 1e9},
        "patterns": [
            r"switching\s*(?:speed|time)[^.\n\r]{0,30}?(\d+(?:\.\d+)?)\s*"
            r"(ns|nsec|\u00b5s|\u03bcs|us|usec|ms|msec)\b",
            r"\bt(?:on|off|_?on|_?off)\b[^.\n\r]{0,20}?(\d+(?:\.\d+)?)\s*"
            r"(ns|nsec|\u00b5s|\u03bcs|us|usec|ms|msec)\b",
            r"(?:rise|fall)\s*(?:/\s*(?:fall|rise)\s*)?time[^.\n\r]{0,25}?"
            r"(\d+(?:\.\d+)?)\s*(ns|nsec|\u00b5s|\u03bcs|us|usec|ms|msec)\b",
        ]},
    "power_w": {
        "spec_key": "power_w_min", "label": "Min power handling (W)", "kind": "min",
        "crit": "pwr", "tol": 0.0,
        "patterns": [r"(?:power handling|input power|cw power|rated power)"
                     r"[^.\n\r]{0,25}?(\d+(?:\.\d+)?)\s*W\b",
                     r"(\d+(?:\.\d+)?)\s*(?:W|watt)"]},
}

# Which parametric specs each category exposes, in display order.
CATEGORY_PARAMS = {
    "amplifier": ["nf_db", "p1db_dbm", "oip3_dbm", "psat_dbm"],
    "attenuator": ["power_w"],
    "coupler": ["coupling_db", "isolation_db"],
    "divider": ["isolation_db"],
    "filter": [],
    "limiter": ["power_w"],
    "mixer": ["conversion_loss_db", "isolation_db", "oip3_dbm"],
    "phase_shifter": [],
    # throw_config is NOT here: it has its own dropdown directly under Category,
    # so listing it again would draw the widget twice.
    "switch": ["isolation_db", "switching_time_ns"],
    "termination": ["power_w"],
}


def category_params(category):
    """Parametric spec keys exposed by a category, in order."""
    return list(CATEGORY_PARAMS.get(category, []))


def param_spec(key):
    return PARAM_SPECS.get(key)


def category_label(category):
    spec = CATEGORY_SPECS.get(category)
    return spec["label"] if spec else category


def subcategories(category):
    """Ordered [(key, label, [synonyms])] for a category, or []."""
    spec = CATEGORY_SPECS.get(category)
    if not spec:
        return []
    return [(k, lbl, syns) for k, (lbl, syns) in spec["subcategories"].items()]


def subcategory_terms(category, subcat_key):
    """Synonym list for a subcategory, or []."""
    spec = CATEGORY_SPECS.get(category)
    if not spec:
        return []
    entry = spec["subcategories"].get(subcat_key)
    return list(entry[1]) if entry else []


def subcategory_exclusion_terms(category, subcat_key):
    """Distinctive synonyms of the *other* subcategories of a category.

    Used to drop obvious sibling mismatches (e.g. a high-power amplifier that
    slips into an LNA search). Only multi-word / length>=5 synonyms are returned:
    short abbreviations like 'pa', 'hpa', 'vga', 'if', 'lna' substring-match part
    numbers and prose, so they're unsafe for exclusion. Synonyms shared with the
    selected subcategory are never used to exclude.
    """
    spec = CATEGORY_SPECS.get(category)
    if not spec:
        return []
    own = {s.lower() for s in subcategory_terms(category, subcat_key)}
    terms = set()
    for key, (_lbl, syns) in spec["subcategories"].items():
        if key == subcat_key:
            continue
        for s in syns:
            sl = s.lower().strip()
            if sl and sl not in own and (" " in sl or len(sl) >= 5):
                terms.add(sl)
    return sorted(terms)


# --- amplifier subcategory classification -------------------------------
# Model-number signals are far more reliable than catalog free-text for
# Mini-Circuits amplifiers: HPA-/ZHL-/ZVA-/ZVE-/LZY- families and a wattage
# token ("...-2W+") mark power amps, an "LN" token marks low-noise amps, and
# DVGA/PVGA/MGVA mark voltage-variable-gain amps. (Derived from the full MC
# amplifier catalog: 306 power / 195 LNA / 19 VGA / 328 gain-block, with zero
# ZHL/ZVA/ZVE/HPA/LZY parts landing in the LNA bucket.)
_AMP_HP_PREFIX = ("ZHL", "ZVA", "ZVE", "HPA", "LZY", "TVA", "RFS", "RFE",
                  "ZVM", "ZQL", "WVA", "ZPUL", "ZHG")
_AMP_VGA_PREFIX = ("DVGA", "PVGA", "MGVA", "LVA", "RVA")
_AMP_WATT = re.compile(r"(?<![A-Z])\d+(?:\.\d+)?\s*W\b|-\d+(?:\.\d+)?W", re.I)


def classify_amplifier(model, specs=None):
    """Coarse amplifier class from model + specs:
    'lna' | 'high_power' | 'vga' | 'gain_block'."""
    u = str(model or "").upper()
    specs = specs or {}
    pre = re.match(r"([A-Z]+)", u)
    pre = pre.group(1) if pre else ""
    if pre in _AMP_HP_PREFIX or _AMP_WATT.search(u) or "HIGH POWER" in u:
        return "high_power"
    if "LN" in u or "LOW NOISE" in u:
        return "lna"
    if pre in _AMP_VGA_PREFIX or "VARIABLE GAIN" in u:
        return "vga"
    nf = specs.get("noise_nf_db")
    if nf is None:
        nf = specs.get("nf_db")
    if isinstance(nf, (int, float)) and nf <= 1.5:
        return "lna"
    p = specs.get("psat_dbm") or specs.get("p1db_dbm")
    if isinstance(p, (int, float)) and p >= 33:
        return "high_power"
    return "gain_block"


# Which coarse class each registry amplifier subcategory belongs to.
_AMP_SUBCAT_GROUP = {"lna": "lna", "pa": "power", "hpa": "power", "vga": "vga",
                     "driver": "gainblock", "buffer": "gainblock",
                     "if": "gainblock", "log": "gainblock"}
_AMP_COARSE_GROUP = {"lna": "lna", "high_power": "power", "vga": "vga",
                     "gain_block": "gainblock"}


def amp_subcat_conflict(requested_subcat, model, specs=None):
    """True when a part's model/specs put it in a *different strong* amplifier
    group than the requested subcategory (so an LNA search drops power/VGA parts,
    and vice-versa). Gain-block parts are never dropped (they're plausible)."""
    want = _AMP_SUBCAT_GROUP.get(requested_subcat)
    got = _AMP_COARSE_GROUP.get(classify_amplifier(model, specs))
    strong = {"lna", "power", "vga"}
    return bool(want and got and want in strong and got in strong and want != got)


def category_fields(category):
    spec = CATEGORY_SPECS.get(category)
    base = list(spec["fields"]) if spec else (["freq"] + _COMMON_TAIL)
    params = CATEGORY_PARAMS.get(category, [])
    if not params:
        return base
    # Place the parametric inputs after the category-specific fields but before
    # the common tail (connector/package/impedance/space/lead).
    idx = next((i for i, k in enumerate(base) if k in _COMMON_TAIL), len(base))
    return base[:idx] + params + base[idx:]


def category_key_params(category):
    spec = CATEGORY_SPECS.get(category)
    return list(spec.get("key_params", [])) if spec else []


def _merge(base, overlay):
    """Merge overlay entries into base, keyed by vendor name (overlay wins)."""
    by_name = {v["name"]: v for v in base}
    for v in overlay or []:
        by_name[v["name"]] = {**by_name.get(v["name"], {}), **v}
    return list(by_name.values())


def load_vendors():
    """Return the merged vendor list (package file + optional user overlay)."""
    vendors = yaml.safe_load(VENDORS_FILE.read_text()) or []
    if USER_VENDORS.exists():
        vendors = _merge(vendors, yaml.safe_load(USER_VENDORS.read_text()))
    return vendors


def vendors_for(category, prefer=None, exclude=None):
    """Vendors that carry `category` ("*" categories match anything).

    `prefer` names are sorted first; `exclude` names are dropped.
    """
    prefer = set(prefer or [])
    exclude = set(exclude or [])
    picked = []
    for v in load_vendors():
        if v["name"] in exclude:
            continue
        cats = v.get("categories", [])
        if category is None or category in cats or "*" in cats:
            picked.append(v)
    picked.sort(key=lambda v: (v["name"] not in prefer, v["name"].lower()))
    return picked
