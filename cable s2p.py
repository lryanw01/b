"""Generate Touchstone files for cables and multi-port active devices.

Two registries, both defined entirely in this file (no external data files):

  CABLES  -- passive cables: IL coefficients, VSWR bands, velocity factor.
  DEVICES -- N-port devices: one entry per SWITCH STATE, with per-path gain/loss,
             isolation, port VSWR, and noise figure.

WHY ONE STATE PER ENTRY: S-parameters describe a single linear time-invariant
network. A switch has two states, so it has two S-matrices. There is no way to
put "TX mode" and "RX mode" in one Touchstone file -- the file format has no
concept of a control line. So a SPDT front end is always at least two files.

S3P vs S2P for the switched front end:
  * The .s3p is the honest full description of one state: it carries the real
    3-port isolation terms, so a simulator can see leakage paths.
  * The .s2p is what most cascade tools actually want, AND it is the only
    Touchstone v1 file that can carry NOISE DATA -- the v1 noise block is
    defined for 2-ports only. A 20 dB NF cannot ride in an .s3p.
  * Extracting the 2x2 submatrix from the 3x3 is exact, not an approximation:
    S-parameters are already defined with all other ports terminated in Z0,
    which is exactly the 50 ohm term on the unused port.
This program writes both, so use the .s2p pair in the cascade and keep the
.s3p pair as the full-fidelity record.

PORT / NOTATION CONVENTION: Sij = response at port i for a drive at port j
(IEEE). So a path from port 3 into port 2 is S23, not S32. See the notes on
frontend_tx -- the source spec used the other label, and this file follows the
stated physical direction.

Only numpy is required. Cascading with scikit-rf:
    import skrf as rf
    rx = rf.Network('frontend_rx.s2p')   # noise data is read automatically
    chain = cable ** rx
    chain.nf(50.0)
"""

import argparse
import cmath
import math
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

C = 299_792_458.0          # m/s
FT_TO_M = 0.3048
FALLBACK_VF = 0.70         # solid-PTFE-ish; used only when a spec has vf=None
S_FLOOR_DB = -240.0        # printed instead of -inf when an S entry is exactly 0
T0 = 290.0                 # K, IEEE reference temperature for noise figure


# ----------------------------------------------------------------------------
# Frequency-dependence helpers -- every gain/loss field takes f_ghz -> dB
# ----------------------------------------------------------------------------

def const(x):
    """Flat value in dB."""
    return lambda f: np.full(np.shape(f), float(x), dtype=float)


def poly(a0, a_f=0.0, a_sqrt=0.0):
    """a0 + a_f*f + a_sqrt*sqrt(f), f in GHz, result in dB."""
    return lambda f: a0 + a_f * np.asarray(f) + a_sqrt * np.sqrt(np.asarray(f))


def interp(points):
    """Linear interpolation through [(f_ghz, dB), ...]; flat outside the range."""
    pts = sorted(points)
    xs = np.array([p[0] for p in pts], dtype=float)
    ys = np.array([p[1] for p in pts], dtype=float)
    return lambda f: np.interp(np.asarray(f, dtype=float), xs, ys)


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
    # [(f_start_ghz, f_stop_ghz, vswr), ...] ascending, stop inclusive
    vswr_bands: List[Tuple[float, float, float]] = field(default_factory=list)
    vf: Optional[float] = None               # velocity factor; None -> FALLBACK_VF
    delay_ns_per_ft: Optional[float] = None   # overrides vf if given
    phase_stability_ppm: Optional[float] = None   # total spec'd delay deviation
    f_max_ghz: Optional[float] = None         # rated max frequency, for warnings
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
        return vswr_piecewise(self.vswr_bands, f_ghz, extrap, self.key)

    def tau_s(self, L_ft):
        """One-way group delay in seconds, and the vf actually used."""
        if self.delay_ns_per_ft is not None:
            return self.delay_ns_per_ft * 1e-9 * L_ft, None
        vf = self.vf if self.vf is not None else FALLBACK_VF
        return (L_ft * FT_TO_M) / (vf * C), vf


def vswr_piecewise(bands, f_ghz, extrap="hold", who=""):
    """Piecewise VSWR vs frequency. extrap: hold | linear | error."""
    f = np.asarray(f_ghz, dtype=float)
    if not bands:
        return np.ones_like(f)
    bands = sorted(bands, key=lambda b: b[0])
    v = np.full(f.shape, np.nan)
    for lo, hi, vswr in bands:
        v = np.where((f >= lo) & (f <= hi) & np.isnan(v), vswr, v)
    v = np.where(np.isnan(v) & (f < bands[0][0]), bands[0][2], v)
    above = np.isnan(v)
    if np.any(above):
        if extrap == "error":
            raise ValueError(f"{who}: VSWR undefined above {bands[-1][1]:g} GHz; "
                             "use --vswr-extrap hold|linear or extend the bands")
        if extrap == "linear" and len(bands) >= 2:
            (l1, h1, v1), (l2, h2, v2) = bands[-2], bands[-1]
            x1, x2 = 0.5 * (l1 + h1), 0.5 * (l2 + h2)
            slope = (v2 - v1) / (x2 - x1) if x2 != x1 else 0.0
            v = np.where(above, np.maximum(v2 + slope * (f - x2), 1.0), v)
        else:
            v = np.where(above, bands[-1][2], v)
    return v


# ----------------------------------------------------------------------------
# Cable registry
# ----------------------------------------------------------------------------

CABLES: Dict[str, CableSpec] = {

    "cable_a": CableSpec(
        key="cable_a",
        description="Test-lab cable, user-supplied IL model and VSWR spec",
        il_fixed=(0.06, 0.0095, 0.005),
        il_per_ft=(0.016, 0.0049, 0.2035),
        vswr_bands=[(0.0, 2.0, 1.15), (2.0, 4.0, 1.20)],
        vf=None,                     # datasheet did not list velocity of propagation
        phase_stability_ppm=1300.0,  # datasheet "phase stability"; see notes
        f_max_ghz=4.0,               # highest frequency the VSWR spec covers
        default_length_ft=3.0,
        notes=("VSWR specified only to 4 GHz -- values above are extrapolated. "
               "Velocity factor unknown; FALLBACK_VF used unless --vf given. "
               "1300 ppm is treated as the TOTAL delay deviation allowed by the "
               "phase-stability spec (reference condition unknown). Applied via "
               "--phase-dev, a fraction in [-1, +1], for corner runs."),
    ),

    "ideal": CableSpec(
        key="ideal",
        description="Lossless, perfectly matched line (sanity check)",
        vf=1.0,
        notes="S11=S22=0, |S21|=1, delay only.",
    ),
}


# ----------------------------------------------------------------------------
# Device specification -- one entry per switch state
# ----------------------------------------------------------------------------

@dataclass
class PathSpec:
    """One directed path through the device: S[dst, src], ports are 1-based.

    gain_db > 0 is gain, < 0 is loss. Use const()/poly()/interp() for the value.
    reciprocal=True also fills S[src, dst] with the same value (passive paths).
    Set reciprocal=False for amplifiers and give reverse_db for the reverse
    isolation of that path; None falls back to the device isolation_db.
    """
    dst: int
    src: int
    gain_db: Callable[[np.ndarray], np.ndarray]
    delay_ns: float = 0.0
    reciprocal: bool = True
    reverse_db: Optional[float] = None


@dataclass
class DeviceSpec:
    """One switch state of an N-port device."""
    key: str
    description: str = ""
    nports: int = 3
    paths: List[PathSpec] = field(default_factory=list)
    isolation_db: float = 80.0       # applied to every path not listed above
    isolation_delay_ns: float = 0.0
    # port -> VSWR bands, e.g. {1: [(0.0, 40.0, 1.5)]}. Missing port = ideal match.
    port_vswr: Dict[int, List[Tuple[float, float, float]]] = field(default_factory=dict)
    nf_db: Optional[Callable[[np.ndarray], np.ndarray]] = None  # None -> passive
    # which ports become the .s2p, as (input_port, output_port)
    s2p_ports: Tuple[int, int] = (1, 2)
    gamma_opt: complex = 0j          # noise data: optimum source reflection
    rn_over_z0: float = 1.0          # noise data: normalized noise resistance
    f_max_ghz: Optional[float] = None
    z0: float = 50.0
    notes: str = ""


# ----------------------------------------------------------------------------
# Device registry
#
# Front end: port 1 = RX output, port 2 = common/antenna, port 3 = TX amp input.
# A SPDT at port 2 selects the internal 50 ohm term (TX) or the RX path (RX).
# ----------------------------------------------------------------------------

DEVICES: Dict[str, DeviceSpec] = {

    "frontend_tx": DeviceSpec(
        key="frontend_tx",
        description="SPDT front end, TX state (port 2 switched to 50 ohm term)",
        nports=3,
        paths=[
            # 57 dB gain, port 3 -> port 2. Amplifier: not reciprocal.
            PathSpec(dst=2, src=3, gain_db=const(57.0), delay_ns=0.0,
                     reciprocal=False, reverse_db=None),   # None -> isolation_db
        ],
        isolation_db=80.0,
        nf_db=const(20.0),
        s2p_ports=(3, 2),            # .s2p port1 = 3 (in), port2 = 2 (out)
        notes=("DIRECTION: the spec said 'S32 ... (From port 3 to port 2)'. Those "
               "disagree under IEEE convention, where Sij = out at i, in at j, so "
               "3 -> 2 is S23, and S32 would be the reverse. This entry follows "
               "the stated physical direction 3 -> 2, i.e. S23 = +57 dB. If the "
               "intent really was out-at-3, swap dst and src in the PathSpec.\n"
               "Port 2 is the 50 ohm load the amplifier drives -- which is what an "
               "S-parameter measurement already assumes at every port, so 'into "
               "the term' and 'out of port 2' are the same file. NF 20 dB rides in "
               "the .s2p noise block. Reverse (S32) sits at isolation_db; if the "
               "amp has its own reverse-isolation spec, set reverse_db."),
    ),

    "frontend_rx": DeviceSpec(
        key="frontend_rx",
        description="SPDT front end, RX state (port 2 switched to the RX path)",
        nports=3,
        paths=[
            # 0.5 dB insertion loss, port 2 -> port 1. Passive: reciprocal.
            PathSpec(dst=1, src=2, gain_db=const(-0.5), delay_ns=0.0,
                     reciprocal=True),
        ],
        isolation_db=80.0,
        nf_db=None,                  # passive path -> NF derived from the loss
        s2p_ports=(2, 1),            # .s2p port1 = 2 (in), port2 = 1 (out)
        notes=("S12 = S21 = -0.5 dB, modeled reciprocal because the path is a "
               "passive switch through. Port 3 is isolated by isolation_db. "
               "NF is auto-set equal to the insertion loss (passive, matched, "
               "at T0)."),
    ),
}


# ----------------------------------------------------------------------------
# Network construction -- cables
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


def build_cable_network(spec, f_ghz, L_ft, s11_model="ripple", vswr_extrap="hold",
                        vf_override=None, phase_dev=0.0):
    """Return (s, info); s is (n,2,2)."""
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
        s = line.copy()
        s[:, 0, 0] = gamma
        s[:, 1, 1] = gamma
        scale = np.sqrt(np.maximum(1.0 - gamma ** 2, 0.0))
        s[:, 1, 0] *= scale
        s[:, 0, 1] *= scale
    else:  # ripple
        rho = gamma / 2.0
        s = cascade(cascade(mismatch_2port(rho), line),
                    mismatch_2port(rho, flip=True))

    return s, {"il": il, "vswr": vswr, "gamma": gamma, "tau": tau,
               "tau_nom": tau_nom, "vf": vf_used, "s11_model": s11_model}


# ----------------------------------------------------------------------------
# Network construction -- devices
# ----------------------------------------------------------------------------

def _phasor(f_ghz, db, delay_ns):
    """Complex S entry from a dB value and a delay."""
    mag = 10.0 ** (np.asarray(db, dtype=float) / 20.0)
    return mag * np.exp(-2j * np.pi * f_ghz * 1e9 * delay_ns * 1e-9)


def build_device_network(spec, f_ghz, vswr_extrap="hold"):
    """Return (s, info); s is (n, nports, nports) for this switch state."""
    n = f_ghz.size
    N = spec.nports
    s = np.zeros((n, N, N), dtype=complex)

    # Every off-diagonal starts at the isolation floor.
    iso = _phasor(f_ghz, -abs(spec.isolation_db), spec.isolation_delay_ns)
    for i in range(N):
        for j in range(N):
            if i != j:
                s[:, i, j] = iso

    # Signal paths overwrite the floor.
    defined = []
    for p in spec.paths:
        g = np.asarray(p.gain_db(f_ghz), dtype=float)
        s[:, p.dst - 1, p.src - 1] = _phasor(f_ghz, g, p.delay_ns)
        defined.append((p.dst, p.src, g))
        if p.reciprocal:
            s[:, p.src - 1, p.dst - 1] = _phasor(f_ghz, g, p.delay_ns)
            defined.append((p.src, p.dst, g))
        elif p.reverse_db is not None:
            s[:, p.src - 1, p.dst - 1] = _phasor(
                f_ghz, -abs(p.reverse_db), p.delay_ns)
            defined.append((p.src, p.dst, -abs(p.reverse_db)))

    # Port reflections from VSWR (ideal match where unspecified).
    gammas = {}
    for port in range(1, N + 1):
        bands = spec.port_vswr.get(port)
        if bands:
            v = vswr_piecewise(bands, f_ghz, vswr_extrap, f"{spec.key} port {port}")
            g = (v - 1.0) / (v + 1.0)
        else:
            g = np.zeros(n)
        gammas[port] = g
        s[:, port - 1, port - 1] = g

    return s, {"defined": defined, "gammas": gammas}


def subnetwork(s, ports):
    """Exact 2-port (or k-port) reduction: the other ports are already Z0-terminated."""
    idx = [p - 1 for p in ports]
    return s[:, idx, :][:, :, idx]


def device_nf_db(spec, f_ghz, s):
    """NF vs frequency for the .s2p noise block. Passive paths derive from loss."""
    if spec.nf_db is not None:
        return np.asarray(spec.nf_db(f_ghz), dtype=float)
    # Passive, matched: NF equals the insertion loss between the s2p ports.
    src, dst = spec.s2p_ports
    gain_db = 20.0 * np.log10(np.maximum(np.abs(s[:, dst - 1, src - 1]), 1e-30))
    return -gain_db


# ----------------------------------------------------------------------------
# Touchstone output (N-port)
# ----------------------------------------------------------------------------

def _pair(z, form):
    if form == "ri":
        return z.real, z.imag
    mag, ang = abs(z), math.degrees(cmath.phase(z))
    if form == "ma":
        return mag, ang
    return (20.0 * math.log10(mag) if mag > 0.0 else S_FLOOR_DB), ang


def write_touchstone(path, f_ghz, s, form, z0, header_lines, noise=None):
    """Write a Touchstone v1 file for a 1-, 2-, or N-port network.

    2-port data order is S11 S21 S12 S22 on one line (the historic swap).
    N>2 is written row-major, one matrix row per line: S11 S12 S13 / S21 ...
    `noise`, if given, is (f_ghz, nf_db, gamma_opt, rn_over_z0) and is only
    legal for 2-port files in Touchstone v1.
    """
    form = form.lower()
    N = s.shape[1]
    ext_hint = {"ri": "RI", "ma": "MA", "db": "DB"}[form]

    with open(path, "w", newline="\n") as fh:
        for line in header_lines:
            fh.write(f"! {line}\n")
        fh.write(f"# GHZ S {ext_hint} R {z0:g}\n")

        if N == 2:
            fh.write({
                "ri": "! FREQ ReS11 ImS11 ReS21 ImS21 ReS12 ImS12 ReS22 ImS22",
                "ma": "! FREQ MagS11 AngS11 MagS21 AngS21 MagS12 AngS12 MagS22 AngS22",
                "db": "! FREQ dBS11 AngS11 dBS21 AngS21 dBS12 AngS12 dBS22 AngS22",
            }[form] + "\n")
        else:
            fh.write(f"! {N}-port, row-major: row i holds Si1..Si{N}\n")

        for k in range(f_ghz.size):
            if N == 2:
                parts = [f"{f_ghz[k]:.9g}"]
                for (i, j) in [(0, 0), (1, 0), (0, 1), (1, 1)]:
                    a, b = _pair(s[k, i, j], form)
                    parts += [f"{a:.9g}", f"{b:.9g}"]
                fh.write(" ".join(parts) + "\n")
            else:
                for i in range(N):
                    parts = [f"{f_ghz[k]:.9g}"] if i == 0 else [""]
                    for j in range(N):
                        a, b = _pair(s[k, i, j], form)
                        parts += [f"{a:.9g}", f"{b:.9g}"]
                    fh.write(" ".join(parts).strip() + "\n")

        if noise is not None:
            if N != 2:
                raise ValueError("Touchstone v1 noise data is 2-port only")
            nf_f, nf_db, gopt, rn = noise
            fh.write("! NOISE PARAMETERS\n")
            fh.write("! FREQ NFmin(dB) MagGopt AngGopt Rn/Z0\n")
            gm, ga = abs(gopt), math.degrees(cmath.phase(gopt))
            for k in range(nf_f.size):
                fh.write(f"{nf_f[k]:.9g} {nf_db[k]:.9g} {gm:.9g} {ga:.9g} {rn:.9g}\n")


# ----------------------------------------------------------------------------
# Frequency grid
# ----------------------------------------------------------------------------

def build_freq_grid(start, stop, step, step2, fbreak):
    """`step` from start to fbreak, then `step2` to stop. Endpoints land exactly."""
    if fbreak <= start or fbreak >= stop:
        n = int(round((stop - start) / step)) + 1
        return np.linspace(start, stop, n)
    n1 = int(round((fbreak - start) / step)) + 1
    n2 = int(round((stop - fbreak) / step2)) + 1
    return np.concatenate([np.linspace(start, fbreak, n1),
                           np.linspace(fbreak, stop, n2)[1:]])


# ----------------------------------------------------------------------------
# Emitters
# ----------------------------------------------------------------------------

def emit_cable(a, spec):
    L = spec.default_length_ft if a.length is None else a.length
    f = build_freq_grid(a.start, a.stop, a.step, a.step2, a.fbreak)
    s, info = build_cable_network(
        spec, f, L, s11_model=a.s11_model, vswr_extrap=a.vswr_extrap,
        vf_override=a.vf, phase_dev=a.phase_dev)
    if a.no_delay:
        s[:, 0, 1] = np.abs(s[:, 0, 1])
        s[:, 1, 0] = np.abs(s[:, 1, 0])
        info["tau"] = 0.0

    warns = []
    if spec.f_max_ghz is not None and a.stop > spec.f_max_ghz:
        warns.append(f"sweep reaches {a.stop:g} GHz but {spec.key} is specified only "
                     f"to {spec.f_max_ghz:g} GHz -- VSWR above that is "
                     f"'{a.vswr_extrap}' extrapolation, IL is formula extrapolation")
    if spec.vf is None and a.vf is None and spec.delay_ns_per_ft is None:
        warns.append(f"velocity factor not in spec; assumed {FALLBACK_VF:g} "
                     "(phase only, no effect on loss) -- override with --vf")

    stepdesc = (f"step {a.step:g} GHz below {a.fbreak:g} GHz then {a.step2:g} GHz above"
                if a.start < a.fbreak < a.stop else f"step {a.step:g} GHz")
    header = [
        f"Cable: {spec.key} -- {spec.description}",
        f"Length = {L:g} ft, Z0 = {spec.z0:g} ohm, {f.size} pts, "
        f"{a.start:g} to {a.stop:g} GHz, {stepdesc}",
        "IL(dB), f in GHz: (%g + %g*f + %g*sqrt(f)) + L*(%g + %g*f + %g*sqrt(f))"
        % (*spec.il_fixed, *spec.il_per_ft) if spec.il_func is None else "IL: custom",
        "VSWR bands (GHz:VSWR): " + (", ".join(
            f"{lo:g}-{hi:g}:{v:g}" for lo, hi, v in spec.vswr_bands) or "none (matched)"),
        f"Mismatch model = {a.s11_model}, VSWR extrapolation = {a.vswr_extrap}",
        f"Group delay = {info['tau'] * 1e9:.4f} ns"
        + (f" (vf = {info['vf']:g})" if info["vf"] else "")
        + (f", phase_dev = {a.phase_dev:+g} of {spec.phase_stability_ppm:g} ppm "
           f"({(info['tau'] - info['tau_nom']) * 1e12:+.2f} ps)"
           if a.phase_dev and spec.phase_stability_ppm else "")
        + (" [DELAY DISABLED]" if a.no_delay else ""),
        "Reciprocal (S12=S21), symmetric (S11=S22), passive",
    ] + ["WARNING: " + w for w in warns]

    out = a.output or f"{spec.key}_{L:g}ft.s2p"
    write_touchstone(out, f, s, a.format, spec.z0, header)
    il = info["il"]
    print(f"Wrote {out}: {f.size} pts, {a.start:g}-{a.stop:g} GHz")
    print(f"  IL   {il[0]:.4f} dB @ {a.start:g} GHz -> {il[-1]:.4f} dB @ {a.stop:g} GHz")
    print(f"  RL   worst {20 * np.log10(max(np.abs(s[:, 0, 0]).max(), 1e-30)):.2f} dB")
    print(f"  tau  {info['tau'] * 1e9:.4f} ns")
    for w in warns:
        print("  WARNING: " + w)


def emit_device(a, spec):
    f = build_freq_grid(a.start, a.stop, a.step, a.step2, a.fbreak)
    s, info = build_device_network(spec, f, vswr_extrap=a.vswr_extrap)
    nf = device_nf_db(spec, f, s)

    src, dst = spec.s2p_ports
    s2 = subnetwork(s, [src, dst])
    fwd = 20.0 * np.log10(np.maximum(np.abs(s2[:, 1, 0]), 1e-30))
    rev = 20.0 * np.log10(np.maximum(np.abs(s2[:, 0, 1]), 1e-30))

    warns = []
    loop = fwd.max() + rev.max()
    if loop >= 0.0:
        warns.append(f"forward gain {fwd.max():.1f} dB and reverse {rev.max():.1f} dB "
                     f"give loop gain {loop:+.1f} dB -- unstable as modeled")
    if not spec.port_vswr:
        warns.append("no port VSWR given; all ports modeled as ideal 50 ohm. "
                     "Set port_vswr in the DEVICES entry once you have the spec -- "
                     "port match drives ripple when this is cascaded")

    stepdesc = (f"step {a.step:g} GHz below {a.fbreak:g} GHz then {a.step2:g} GHz above"
                if a.start < a.fbreak < a.stop else f"step {a.step:g} GHz")
    base = [
        f"Device: {spec.key} -- {spec.description}",
        f"Z0 = {spec.z0:g} ohm, {f.size} pts, {a.start:g} to {a.stop:g} GHz, {stepdesc}",
        "Signal paths: " + ", ".join(
            f"S{d}{sc} = {g[0]:+.3f} dB" for d, sc, g in info["defined"]),
        f"All other paths at the isolation floor, {spec.isolation_db:g} dB",
        f"NF = {nf[0]:.3f} dB" + ("" if spec.nf_db is not None else " (derived from loss)"),
        "One switch state per file -- S-parameters cannot encode a control line",
    ] + ["WARNING: " + w for w in warns]

    base_name = a.output or spec.key
    written = []
    if a.emit in ("s3p", "both"):
        out3 = f"{base_name}.s3p"
        write_touchstone(out3, f, s, a.format, spec.z0, base + [
            f"Full {spec.nports}-port state. Noise data is NOT here: Touchstone v1 "
            "carries noise parameters for 2-ports only -- use the .s2p for NF."])
        written.append(out3)
    if a.emit in ("s2p", "both"):
        out2 = f"{base_name}.s2p"
        nf_f = f[::max(1, a.noise_decimate)]
        nf_v = nf[::max(1, a.noise_decimate)]
        write_touchstone(
            out2, f, s2, a.format, spec.z0,
            base + [f"2-port reduction: file port 1 = device port {src}, "
                    f"file port 2 = device port {dst}. Exact -- the unused port is "
                    "Z0-terminated, which is what the 50 ohm term is.",
                    f"Noise block: NFmin from spec, Gopt = {spec.gamma_opt}, "
                    f"Rn/Z0 = {spec.rn_over_z0:g} (at Gs=0 only NFmin matters)"],
            noise=(nf_f, nf_v, spec.gamma_opt, spec.rn_over_z0))
        written.append(out2)

    recip = any(p.reciprocal for p in spec.paths)
    print(f"Wrote {', '.join(written)}: {f.size} pts, {a.start:g}-{a.stop:g} GHz")
    print(f"  S{dst}{src} forward {fwd.min():+.3f} to {fwd.max():+.3f} dB")
    print(f"  S{src}{dst} reverse {rev.max():+.3f} dB "
          f"({'reciprocal' if recip else 'isolation'})")
    print(f"  NF {nf.min():.3f} to {nf.max():.3f} dB")
    for w in warns:
        print("  WARNING: " + w)


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def list_registries():
    print("CABLES")
    for k, c in CABLES.items():
        fm = f"{c.f_max_ghz:g} GHz" if c.f_max_ghz else "n/a"
        print(f"  {k:<16} {c.default_length_ft:>5g}ft  f_max {fm:<9} {c.description}")
    print("DEVICES")
    for k, d in DEVICES.items():
        print(f"  {k:<16} {d.nports}-port  s2p=({d.s2p_ports[0]}->"
              f"{d.s2p_ports[1]})  {d.description}")


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Generate Touchstone files from the CABLES and DEVICES registries.")
    p.add_argument("-c", "--cable", default=None, help="cable key")
    p.add_argument("-d", "--device", default=None, help="device key (switch state)")
    p.add_argument("--all-devices", action="store_true", help="emit every device state")
    p.add_argument("--list", action="store_true", help="list both registries and exit")
    p.add_argument("-L", "--length", type=float, default=None, help="cable length, ft")
    p.add_argument("--start", type=float, default=0.01, help="start GHz (default 0.01)")
    p.add_argument("--stop", type=float, default=40.0, help="stop GHz (default 40)")
    p.add_argument("--step", type=float, default=0.01,
                   help="step GHz below --break (default 0.01 = 10 MHz)")
    p.add_argument("--step2", type=float, default=0.1,
                   help="step GHz above --break (default 0.1 = 100 MHz)")
    p.add_argument("--break", dest="fbreak", type=float, default=10.0,
                   help="GHz where the step changes (default 10)")
    p.add_argument("--s11-model", choices=["ripple", "flat", "none"], default="ripple",
                   help="cable mismatch model (default ripple)")
    p.add_argument("--vswr-extrap", choices=["hold", "linear", "error"], default="hold",
                   help="VSWR behavior above the last band (default hold)")
    p.add_argument("--vf", type=float, default=None, help="override velocity factor")
    p.add_argument("--no-delay", action="store_true", help="zero the cable S21 phase")
    p.add_argument("--phase-dev", type=float, default=0.0,
                   help="fraction of the phase-stability spec, [-1,+1]")
    p.add_argument("--emit", choices=["s2p", "s3p", "both"], default="both",
                   help="device output type (default both)")
    p.add_argument("--noise-decimate", type=int, default=1,
                   help="write every Nth noise point (default 1)")
    p.add_argument("--format", choices=["ri", "db", "ma"], default="ri",
                   help="Touchstone data format (default ri)")
    p.add_argument("-o", "--output", default=None, help="output path or base name")
    a = p.parse_args(argv)

    if a.list:
        list_registries()
        return 0
    if a.all_devices:
        for key in DEVICES:
            emit_device(a, DEVICES[key])
        return 0
    if a.device:
        if a.device not in DEVICES:
            p.error(f"unknown device '{a.device}'. Known: {', '.join(DEVICES)}")
        emit_device(a, DEVICES[a.device])
        return 0
    key = a.cable or "cable_a"
    if key not in CABLES:
        p.error(f"unknown cable '{key}'. Known: {', '.join(CABLES)}")
    emit_cable(a, CABLES[key])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
