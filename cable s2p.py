"""Generate Touchstone .s2p files for cables from a spec registry.

Each cable type lives in CABLES as a self-contained CableSpec: insertion-loss
coefficients, VSWR-vs-frequency bands, velocity factor, rated bandwidth, notes.
Add a cable by adding one dict entry -- no other code changes needed.

Insertion loss model (dB, f in GHz, L in feet):
    IL = (a0 + a_f*f + a_sqrt*sqrt(f)) + L*(b0 + b_f*f + b_sqrt*sqrt(f))
A cable may instead supply il_func(f_ghz, L_ft) -> dB for non-polynomial forms.

S-parameter model:
    Cable = [connector mismatch] -- [lossy delay line] -- [connector mismatch]
    Each connector gets |rho| = Gamma_spec / 2 so the round-trip sum hits the
    spec VSWR envelope at low frequency and rolls off with cable loss above it.
    S11/S22 therefore show physical mismatch ripple instead of a flat wall.
    S21 = S12 (reciprocal), S11 = S22 (symmetric).

Only numpy is required. For cascading the result, scikit-rf is the tool:
    import skrf as rf
    total = rf.Network('cable_a_3ft.s2p') ** amp ** filt
"""

import argparse
import cmath
import json
import math
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence, Tuple

import numpy as np

C = 299_792_458.0          # m/s
FT_TO_M = 0.3048
FALLBACK_VF = 0.70         # solid-PTFE-ish; used only when a spec has vf=None
S_FLOOR_DB = -240.0        # printed instead of -inf when an S entry is exactly 0


# ----------------------------------------------------------------------------
# Cable specification
# ----------------------------------------------------------------------------

@dataclass
class CableSpec:
    """Everything known about one cable type."""
    key: str
    description: str = ""
    # IL = fixed + L * per_ft, each a (const, *f, *sqrt(f)) triple, f in GHz
    il_fixed: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    il_per_ft: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    il_func: Optional[Callable[[np.ndarray, float], np.ndarray]] = None
    # [(f_start_ghz, f_stop_ghz, vswr), ...] ascending, stop is inclusive
    vswr_bands: List[Tuple[float, float, float]] = field(default_factory=list)
    vf: Optional[float] = None              # velocity factor; None -> FALLBACK_VF
    delay_ns_per_ft: Optional[float] = None  # overrides vf if given
    phase_stability_ppm: Optional[float] = None  # total spec'd delay deviation
    f_max_ghz: Optional[float] = None        # rated max frequency, for warnings
    default_length_ft: float = 3.0
    z0: float = 50.0
    notes: str = ""

    def insertion_loss_db(self, f_ghz, L_ft):
        if self.il_func is not None:
            return np.asarray(self.il_func(f_ghz, L_ft), dtype=float)
        a0, af, asq = self.il_fixed
        b0, bf, bsq = self.il_per_ft
        rt = np.sqrt(f_ghz)
        return (a0 + af * f_ghz + asq * rt) + L_ft * (b0 + bf * f_ghz + bsq * rt)

    def vswr_at(self, f_ghz, extrap="hold"):
        """Piecewise VSWR vs frequency. extrap: hold | linear | error."""
        f = np.asarray(f_ghz, dtype=float)
        if not self.vswr_bands:
            return np.ones_like(f)
        bands = sorted(self.vswr_bands, key=lambda b: b[0])
        v = np.full(f.shape, np.nan)
        for lo, hi, vswr in bands:
            v = np.where((f >= lo) & (f <= hi) & np.isnan(v), vswr, v)
        # below the first band
        v = np.where(np.isnan(v) & (f < bands[0][0]), bands[0][2], v)
        above = np.isnan(v)
        if np.any(above):
            if extrap == "error":
                raise ValueError(
                    f"{self.key}: VSWR undefined above {bands[-1][1]:g} GHz; "
                    "use --vswr-extrap hold|linear or extend vswr_bands")
            if extrap == "linear" and len(bands) >= 2:
                (l1, h1, v1), (l2, h2, v2) = bands[-2], bands[-1]
                x1, x2 = 0.5 * (l1 + h1), 0.5 * (l2 + h2)
                slope = (v2 - v1) / (x2 - x1) if x2 != x1 else 0.0
                v = np.where(above, v2 + slope * (f - x2), v)
                v = np.maximum(v, 1.0)
            else:
                v = np.where(above, bands[-1][2], v)
        return v

    def tau_s(self, L_ft):
        """One-way group delay in seconds, and the vf actually used."""
        if self.delay_ns_per_ft is not None:
            return self.delay_ns_per_ft * 1e-9 * L_ft, None
        vf = self.vf if self.vf is not None else FALLBACK_VF
        return (L_ft * FT_TO_M) / (vf * C), vf


# ----------------------------------------------------------------------------
# Cable registry -- add new cable types here
# ----------------------------------------------------------------------------

CABLES = {

    "cable_a": CableSpec(
        key="cable_a",
        description="Test-lab cable, user-supplied IL model and VSWR spec",
        il_fixed=(0.06, 0.0095, 0.005),
        il_per_ft=(0.016, 0.0049, 0.2035),
        vswr_bands=[(0.0, 2.0, 1.15), (2.0, 4.0, 1.20)],
        vf=None,                    # datasheet did not list velocity of propagation
        phase_stability_ppm=1300.0,  # datasheet "phase stability"; see notes
        f_max_ghz=4.0,              # highest frequency the VSWR spec covers
        default_length_ft=3.0,
        notes=("VSWR specified only to 4 GHz -- values above are extrapolated. "
               "Velocity factor unknown; FALLBACK_VF used unless --vf given. "
               "1300 ppm is treated as the TOTAL delay deviation allowed by the "
               "phase-stability spec (reference condition unknown -- temperature, "
               "flexure, or unit-to-unit). Applied only via --phase-dev, which "
               "takes a fraction of that deviation in [-1, +1] for corner runs."),
    ),

    # ---- TEMPLATE: copy, rename, and drop in real datasheet numbers ----
    "example_lowloss": CableSpec(
        key="example_lowloss",
        description="TEMPLATE ONLY -- placeholder numbers, not a real datasheet",
        il_fixed=(0.05, 0.0080, 0.004),
        il_per_ft=(0.012, 0.0040, 0.1500),
        vswr_bands=[(0.0, 8.0, 1.15), (8.0, 18.0, 1.25), (18.0, 40.0, 1.35)],
        vf=0.82,
        f_max_ghz=40.0,
        default_length_ft=3.0,
        notes="Illustrative registry entry. Replace every number before use.",
    ),

    # Zero-loss, perfectly matched line -- useful as a cascade sanity check.
    "ideal": CableSpec(
        key="ideal",
        description="Lossless, perfectly matched line (sanity check)",
        il_fixed=(0.0, 0.0, 0.0),
        il_per_ft=(0.0, 0.0, 0.0),
        vswr_bands=[],
        vf=1.0,
        default_length_ft=3.0,
        notes="S11=S22=0, |S21|=1, delay only.",
    ),
}


def load_cable_json(path):
    """Merge cable definitions from a JSON file into CABLES.

    JSON shape: {"key": {"il_fixed": [..], "il_per_ft": [..],
                         "vswr_bands": [[lo, hi, vswr], ...], "vf": 0.7, ...}}
    Coefficient-form cables only (il_func cannot be expressed in JSON).
    """
    with open(path) as fh:
        raw = json.load(fh)
    for key, d in raw.items():
        d = dict(d)
        d.pop("il_func", None)
        if "il_fixed" in d:
            d["il_fixed"] = tuple(d["il_fixed"])
        if "il_per_ft" in d:
            d["il_per_ft"] = tuple(d["il_per_ft"])
        if "vswr_bands" in d:
            d["vswr_bands"] = [tuple(b) for b in d["vswr_bands"]]
        d["key"] = key
        CABLES[key] = CableSpec(**d)
    return sorted(raw)


# ----------------------------------------------------------------------------
# Network construction
# ----------------------------------------------------------------------------

def cascade(A, B):
    """Cascade two (n,2,2) S-matrix stacks. Port 2 of A into port 1 of B."""
    a11, a12, a21, a22 = A[:, 0, 0], A[:, 0, 1], A[:, 1, 0], A[:, 1, 1]
    b11, b12, b21, b22 = B[:, 0, 0], B[:, 0, 1], B[:, 1, 0], B[:, 1, 1]
    d = 1.0 - a22 * b11
    out = np.empty_like(A)
    out[:, 0, 0] = a11 + a12 * a21 * b11 / d
    out[:, 1, 0] = a21 * b21 / d
    out[:, 0, 1] = a12 * b12 / d
    out[:, 1, 1] = b22 + b12 * b21 * a22 / d
    return out


def mismatch_2port(rho, flip=False):
    """Lossless reciprocal reflective junction with reflection coefficient rho."""
    n = rho.size
    t = np.sqrt(np.maximum(1.0 - rho ** 2, 0.0)).astype(complex)
    s = np.zeros((n, 2, 2), dtype=complex)
    sign = -1.0 if flip else 1.0
    s[:, 0, 0] = sign * rho
    s[:, 1, 1] = -sign * rho
    s[:, 0, 1] = t
    s[:, 1, 0] = t
    return s


def line_2port(f_ghz, il_db, tau_s):
    """Matched lossy delay line."""
    n = f_ghz.size
    t = 10.0 ** (-il_db / 20.0) * np.exp(-2j * np.pi * f_ghz * 1e9 * tau_s)
    s = np.zeros((n, 2, 2), dtype=complex)
    s[:, 0, 1] = t
    s[:, 1, 0] = t
    return s


def build_network(spec, f_ghz, L_ft, s11_model="ripple", vswr_extrap="hold",
                  vf_override=None, phase_dev=0.0):
    """Return (s, info) where s is (n,2,2) and info is a dict for the header."""
    il = spec.insertion_loss_db(f_ghz, L_ft)

    tau, vf_used = spec.tau_s(L_ft)
    if vf_override is not None:
        tau = (L_ft * FT_TO_M) / (vf_override * C)
        vf_used = vf_override
    tau_nom = tau
    if phase_dev and spec.phase_stability_ppm:
        tau *= (1.0 + phase_dev * spec.phase_stability_ppm * 1e-6)

    vswr = spec.vswr_at(f_ghz, extrap=vswr_extrap)
    gamma = (vswr - 1.0) / (vswr + 1.0)

    line = line_2port(f_ghz, il, tau)

    if s11_model == "none":
        s = line
    elif s11_model == "flat":
        # Worst-case wall: |S11| = |S22| = Gamma_spec, no ripple. Not physical,
        # but reproduces the spec envelope exactly at every frequency.
        s = line.copy()
        s[:, 0, 0] = gamma
        s[:, 1, 1] = gamma
        scale = np.sqrt(np.maximum(1.0 - gamma ** 2, 0.0))
        s[:, 1, 0] *= scale
        s[:, 0, 1] *= scale
    else:  # ripple
        rho = gamma / 2.0     # split between the two connectors
        s = cascade(cascade(mismatch_2port(rho), line),
                    mismatch_2port(rho, flip=True))

    info = {
        "il": il, "vswr": vswr, "gamma": gamma,
        "tau": tau, "tau_nom": tau_nom, "vf": vf_used, "s11_model": s11_model,
    }
    return s, info


# ----------------------------------------------------------------------------
# Touchstone output
# ----------------------------------------------------------------------------

def _pair(z, form):
    if form == "ri":
        return z.real, z.imag
    mag, ang = abs(z), math.degrees(cmath.phase(z))
    if form == "ma":
        return mag, ang
    return (20.0 * math.log10(mag) if mag > 0.0 else S_FLOOR_DB), ang


def write_touchstone(path, f_ghz, s, form, z0, header_lines):
    """Touchstone v1 .s2p. 2-port column order is S11 S21 S12 S22."""
    form = form.lower()
    legend = {
        "ri": "! FREQ ReS11 ImS11 ReS21 ImS21 ReS12 ImS12 ReS22 ImS22",
        "ma": "! FREQ MagS11 AngS11 MagS21 AngS21 MagS12 AngS12 MagS22 AngS22",
        "db": "! FREQ dBS11 AngS11 dBS21 AngS21 dBS12 AngS12 dBS22 AngS22",
    }[form]
    order = [(0, 0), (1, 0), (0, 1), (1, 1)]
    with open(path, "w", newline="\n") as fh:
        for line in header_lines:
            fh.write(f"! {line}\n")
        fh.write(f"# GHZ S {form.upper()} R {z0:g}\n")
        fh.write(legend + "\n")
        for k in range(f_ghz.size):
            parts = [f"{f_ghz[k]:.9g}"]
            for (i, j) in order:
                a, b = _pair(s[k, i, j], form)
                parts += [f"{a:.9g}", f"{b:.9g}"]
            fh.write(" ".join(parts) + "\n")


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def list_cables():
    print(f"{'key':<18} {'default L':>9}  {'f_max':>7}  description")
    print("-" * 78)
    for k, c in CABLES.items():
        fm = f"{c.f_max_ghz:g} GHz" if c.f_max_ghz else "n/a"
        print(f"{k:<18} {c.default_length_ft:>7g}ft  {fm:>7}  {c.description}")


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Generate .s2p files for cables defined in the CABLES registry.")
    p.add_argument("-c", "--cable", default="cable_a", help="cable key (default cable_a)")
    p.add_argument("--list", action="store_true", help="list registry and exit")
    p.add_argument("--cable-file", help="JSON file of extra cable definitions")
    p.add_argument("-L", "--length", type=float, default=None,
                   help="length in feet (default: the cable's own default)")
    p.add_argument("--start", type=float, default=0.01, help="start GHz (default 0.01)")
    p.add_argument("--stop", type=float, default=40.0, help="stop GHz (default 40)")
    p.add_argument("--step", type=float, default=0.01, help="step GHz (default 0.01)")
    p.add_argument("--s11-model", choices=["ripple", "flat", "none"], default="ripple",
                   help="mismatch model (default ripple)")
    p.add_argument("--vswr-extrap", choices=["hold", "linear", "error"], default="hold",
                   help="VSWR behavior above the last specified band (default hold)")
    p.add_argument("--vf", type=float, default=None, help="override velocity factor")
    p.add_argument("--no-delay", action="store_true", help="zero the S21/S12 phase")
    p.add_argument("--phase-dev", type=float, default=0.0,
                   help="fraction of the phase-stability spec to apply to the "
                        "delay, in [-1, +1], for phase corner runs (default 0)")
    p.add_argument("--format", choices=["ri", "db", "ma"], default="ri",
                   help="Touchstone data format (default ri)")
    p.add_argument("-o", "--output", default=None, help="output path")
    a = p.parse_args(argv)

    if a.cable_file:
        added = load_cable_json(a.cable_file)
        print(f"Loaded from {a.cable_file}: {', '.join(added)}")
    if a.list:
        list_cables()
        return 0
    if a.cable not in CABLES:
        p.error(f"unknown cable '{a.cable}'. Known: {', '.join(CABLES)}")

    spec = CABLES[a.cable]
    L = spec.default_length_ft if a.length is None else a.length

    npts = int(round((a.stop - a.start) / a.step)) + 1
    f = np.linspace(a.start, a.stop, npts)

    s, info = build_network(
        spec, f, L,
        s11_model=a.s11_model, vswr_extrap=a.vswr_extrap,
        vf_override=a.vf, phase_dev=a.phase_dev)
    if a.no_delay:
        for i, j in ((0, 1), (1, 0)):
            s[:, i, j] = np.abs(s[:, i, j])
        info["tau"] = 0.0

    warns = []
    if spec.f_max_ghz is not None and a.stop > spec.f_max_ghz:
        warns.append(f"sweep extends to {a.stop:g} GHz but {spec.key} is specified "
                     f"only to {spec.f_max_ghz:g} GHz -- VSWR above that is "
                     f"'{a.vswr_extrap}' extrapolation, IL is formula extrapolation")
    if spec.vf is None and a.vf is None and spec.delay_ns_per_ft is None:
        warns.append(f"velocity factor not in spec; assumed {FALLBACK_VF:g} "
                     "(phase only, no effect on loss) -- override with --vf")

    out = a.output or f"{spec.key}_{L:g}ft.s2p"
    header = [
        f"Cable: {spec.key} -- {spec.description}",
        f"Length = {L:g} ft, Z0 = {spec.z0:g} ohm, {npts} pts, "
        f"{a.start:g} to {a.stop:g} GHz, step {a.step:g} GHz",
        "IL(dB), f in GHz: (%g + %g*f + %g*sqrt(f)) + L*(%g + %g*f + %g*sqrt(f))"
        % (*spec.il_fixed, *spec.il_per_ft) if spec.il_func is None
        else "IL: custom il_func",
        "VSWR bands (GHz:VSWR): " + (", ".join(
            f"{lo:g}-{hi:g}:{v:g}" for lo, hi, v in spec.vswr_bands) or "none (matched)"),
        f"Mismatch model = {a.s11_model}, VSWR extrapolation = {a.vswr_extrap}",
        f"Group delay = {info['tau'] * 1e9:.4f} ns"
        + (f" (vf = {info['vf']:g})" if info["vf"] else "")
        + (f", phase_dev = {a.phase_dev:+g} of "
           f"{spec.phase_stability_ppm:g} ppm "
           f"({(info['tau'] - info['tau_nom']) * 1e12:+.2f} ps vs nominal)"
           if a.phase_dev and spec.phase_stability_ppm else "")
        + (" [DELAY DISABLED]" if a.no_delay else ""),
        "Reciprocal (S12=S21), symmetric (S11=S22), passive",
    ]
    for w in warns:
        header.append("WARNING: " + w)
    write_touchstone(out, f, s, a.format, spec.z0, header)

    il = info["il"]
    s11db = 20 * np.log10(np.maximum(np.abs(s[:, 0, 0]), 1e-30))
    print(f"Wrote {out}: cable={spec.key}, L={L:g} ft, {npts} pts, "
          f"{a.start:g}-{a.stop:g} GHz, format={a.format}")
    print(f"  IL   {il[0]:.4f} dB @ {a.start:g} GHz -> {il[-1]:.4f} dB @ {a.stop:g} GHz")
    print(f"  RL   worst {s11db.max():.2f} dB (|S11| max {np.abs(s[:, 0, 0]).max():.4f})")
    print(f"  tau  {info['tau'] * 1e9:.4f} ns")
    for w in warns:
        print("  WARNING: " + w)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
