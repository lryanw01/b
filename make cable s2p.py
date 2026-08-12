#!/usr/bin/env python3
"""Generate a Touchstone .s2p for a lossy, matched, reciprocal cable.

Insertion-loss model (result in dB, f in GHz, L in feet):

    IL = 0.06 + 0.0095*f + 0.005*sqrt(f)
         + L * (0.016 + 0.0049*f + 0.2035*sqrt(f))

S-parameters written:
    S11 = S22 = 0                                  (ideal match / infinite return loss)
    S21 = S12 = 10**(-IL/20) * exp(-j*2*pi*f*tau)   (loss magnitude + electrical delay)
    tau = L_meters / (vf * c)                        (group delay from length + velocity factor)

Reciprocal (S12=S21) and symmetric (S11=S22): correct for a plain passive cable.

Only numpy is required. scikit-rf is the recommended toolkit for the *cascade* step:
    import skrf as rf
    cable = rf.Network('cable_3ft.s2p')
    total = cable ** amp ** filt          # '**' cascades 2-ports
"""

import argparse
import math
import numpy as np

C = 299_792_458.0        # speed of light, m/s
FT_TO_M = 0.3048


def insertion_loss_db(f_ghz, L_ft):
    """Cable insertion loss in dB. f in GHz, L in feet. Vectorized over f."""
    f = f_ghz
    return (0.06 + 0.0095 * f + 0.005 * np.sqrt(f)
            + L_ft * (0.016 + 0.0049 * f + 0.2035 * np.sqrt(f)))


def build_s(f_ghz, L_ft, vf, use_delay):
    """Return (s, il_db, tau_s). s has shape (npts, 2, 2), complex."""
    il = insertion_loss_db(f_ghz, L_ft)
    mag = 10.0 ** (-il / 20.0)
    tau = (L_ft * FT_TO_M) / (vf * C) if use_delay else 0.0   # seconds
    phase = -2.0 * np.pi * (f_ghz * 1e9) * tau                # radians
    s21 = mag * np.exp(1j * phase)

    n = f_ghz.size
    s = np.zeros((n, 2, 2), dtype=complex)
    s[:, 1, 0] = s21     # S21
    s[:, 0, 1] = s21     # S12 (reciprocal)
    # S11 = S22 = 0 already (ideal match)
    return s, il, tau


def _pair(z, form, floor_db=-240.0):
    """Return the two numbers for one S entry in the requested Touchstone form."""
    if form == "ri":
        return z.real, z.imag
    mag = abs(z)
    ang = math.degrees(math.atan2(z.imag, z.real))
    if form == "ma":
        return mag, ang
    db = 20.0 * math.log10(mag) if mag > 0.0 else floor_db   # avoid -inf when S=0
    return db, ang


def write_touchstone(path, f_ghz, s, form, z0, header_lines):
    """Write a Touchstone v1 .s2p. 2-port column order is S11 S21 S12 S22."""
    form = form.lower()
    order = [(0, 0), (1, 0), (0, 1), (1, 1)]     # <-- the 2-port swap (S21 before S12)
    if form == "ri":
        legend = "! FREQ ReS11 ImS11 ReS21 ImS21 ReS12 ImS12 ReS22 ImS22"
    elif form == "ma":
        legend = "! FREQ MagS11 AngS11 MagS21 AngS21 MagS12 AngS12 MagS22 AngS22"
    else:
        legend = "! FREQ dBS11 AngS11 dBS21 AngS21 dBS12 AngS12 dBS22 AngS22"

    with open(path, "w", newline="\n") as fh:
        for line in header_lines:
            fh.write(f"! {line}\n")
        fh.write(f"# GHZ S {form.upper()} R {z0:g}\n")
        fh.write(legend + "\n")
        for k in range(f_ghz.size):
            parts = [f"{f_ghz[k]:.9g}"]
            for (i, j) in order:
                a, b = _pair(s[k, i, j], form)
                parts.append(f"{a:.9g}")
                parts.append(f"{b:.9g}")
            fh.write(" ".join(parts) + "\n")


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Generate an .s2p for a lossy, matched cable.")
    p.add_argument("-L", "--length", type=float, default=3.0,
                   help="cable length in feet (default 3.0)")
    p.add_argument("--start", type=float, default=0.01,
                   help="start frequency in GHz (default 0.01 = 10 MHz)")
    p.add_argument("--stop", type=float, default=40.0,
                   help="stop frequency in GHz (default 40)")
    p.add_argument("--step", type=float, default=0.01,
                   help="frequency step in GHz (default 0.01 = 10 MHz)")
    p.add_argument("--vf", type=float, default=0.70,
                   help="velocity factor for electrical delay (default 0.70)")
    p.add_argument("--no-delay", action="store_true",
                   help="zero the S21/S12 phase (magnitude-only cable)")
    p.add_argument("--z0", type=float, default=50.0,
                   help="reference impedance in ohms (default 50)")
    p.add_argument("--format", choices=["ri", "db", "ma"], default="ri",
                   help="Touchstone data format (default ri)")
    p.add_argument("-o", "--output", default=None,
                   help="output path (default cable_<L>ft.s2p)")
    a = p.parse_args(argv)

    npts = int(round((a.stop - a.start) / a.step)) + 1
    f = np.linspace(a.start, a.stop, npts)          # exact endpoints, uniform step
    s, il, tau = build_s(f, a.length, a.vf, not a.no_delay)

    out = a.output or f"cable_{a.length:g}ft.s2p"
    header = [
        "Synthetic cable S-parameters (matched, reciprocal, passive)",
        f"Length = {a.length:g} ft, velocity factor = {a.vf:g}, "
        f"group delay = {tau * 1e9:.4f} ns" + ("  [DELAY DISABLED]" if a.no_delay else ""),
        "IL(dB), f in GHz: 0.06 + 0.0095*f + 0.005*sqrt(f) "
        "+ L*(0.016 + 0.0049*f + 0.2035*sqrt(f))",
        f"S11=S22=0 (ideal match); S21=S12=10^(-IL/20)*exp(-j*2*pi*f*tau); Z0={a.z0:g} ohm",
        f"{npts} points, {a.start:g} to {a.stop:g} GHz, step {a.step:g} GHz",
    ]
    write_touchstone(out, f, s, a.format, a.z0, header)

    print(f"Wrote {out}: {npts} pts, {a.start:g}-{a.stop:g} GHz, format={a.format}")
    print(f"IL @ {a.start:g} GHz = {il[0]:.4f} dB   "
          f"IL @ {a.stop:g} GHz = {il[-1]:.4f} dB   "
          f"delay = {tau * 1e9:.4f} ns")


if __name__ == "__main__":
    main()
