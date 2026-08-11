#!/usr/bin/env python3
"""
noise_match.py -- find the LNA input matching network for a *noise* match.

What it does
------------
The LNA hits NFmin only when its input sees Gamma_opt. Left alone, it sees the
roofing filter's output impedance instead. This tool:
  1. reads Gamma_opt from the LNA .s2p noise block  -> target impedance Z_opt,
  2. reads the filter's output reflection (S22, input port terminated in Z0)
     -> the source impedance the LNA actually sees, Z_source,
  3. designs the lossless 2-element L-network that transforms Z_source so the
     LNA looks back and sees Z_opt, and reports L/C values at the design freq.

Both L topologies are solved; each yields up to two real solutions. A back-
substitution residual is printed so you can confirm the network actually lands
on Z_opt (should be ~0).

Key assumptions (change with flags if your files differ)
  * Filter input port is terminated in Z0, so source refl = filter S22.
    Use --filter-port 11 if your filter file has output on port 1.
  * Narrowband lumped L-match. For wideband, iterate the topology per freq.

Usage
  python noise_match.py LNA.s2p FILTER.s2p [--freq 10e9] [--filter-port 22]
If --freq is omitted, the centre of the LNA noise-frequency range is used.
"""

import argparse
import cmath
import math
import numpy as np


def parse_touchstone(path):
    """Return dict: f(Hz), z0, s(dict of Nx complex for '11','21','12','22'),
    and if present nf: dict with f, nfmin_db, gopt(complex), rn_norm."""
    unit = 1e9      # GHZ default
    fmt = "MA"      # default per spec
    z0 = 50.0
    s_rows = []     # (f, 8 numbers)
    n_rows = []     # (f, nfmin_db, gmag, gang_deg, rn_norm)
    umap = {"HZ": 1.0, "KHZ": 1e3, "MHZ": 1e6, "GHZ": 1e9}

    with open(path) as fh:
        for raw in fh:
            line = raw.split("!", 1)[0].strip()
            if not line:
                continue
            if line.startswith("#"):
                toks = line[1:].upper().split()
                i = 0
                while i < len(toks):
                    t = toks[i]
                    if t in umap:
                        unit = umap[t]
                    elif t in ("MA", "DB", "RI"):
                        fmt = t
                    elif t == "R" and i + 1 < len(toks):
                        z0 = float(toks[i + 1]); i += 1
                    i += 1
                continue
            nums = [float(x) for x in line.split()]
            if len(nums) == 9:            # 2-port S-parameter row
                s_rows.append(nums)
            elif len(nums) == 5:          # noise row
                n_rows.append(nums)
            # anything else: silently skip (wrapped lines etc. not supported)

    if not s_rows:
        raise ValueError(f"{path}: no 2-port S-parameter rows found")

    s_rows = np.array(s_rows)
    f = s_rows[:, 0] * unit

    def to_complex(a, b):
        if fmt == "MA":
            return a * np.exp(1j * np.deg2rad(b))
        if fmt == "DB":
            return (10 ** (a / 20.0)) * np.exp(1j * np.deg2rad(b))
        return a + 1j * b  # RI

    # Touchstone 2-port order: f S11 S21 S12 S22
    s = {
        "11": to_complex(s_rows[:, 1], s_rows[:, 2]),
        "21": to_complex(s_rows[:, 3], s_rows[:, 4]),
        "12": to_complex(s_rows[:, 5], s_rows[:, 6]),
        "22": to_complex(s_rows[:, 7], s_rows[:, 8]),
    }

    nf = None
    if n_rows:
        n_rows = np.array(n_rows)
        nf = {
            "f": n_rows[:, 0] * unit,
            "nfmin_db": n_rows[:, 1],
            # noise-block gamma is always magnitude / angle(deg)
            "gopt": n_rows[:, 2] * np.exp(1j * np.deg2rad(n_rows[:, 3])),
            "rn_norm": n_rows[:, 4],
        }
    return {"f": f, "z0": z0, "s": s, "nf": nf}


def interp_c(fgrid, vals, f):
    order = np.argsort(fgrid)
    fg = fgrid[order]
    v = np.asarray(vals)[order]
    if f < fg[0] or f > fg[-1]:
        raise ValueError(f"design freq {f:.4g} Hz outside data span "
                         f"{fg[0]:.4g}..{fg[-1]:.4g} Hz")
    re = np.interp(f, fg, v.real)
    im = np.interp(f, fg, v.imag)
    return re + 1j * im


def g_to_z(g, z0):
    return z0 * (1 + g) / (1 - g)


def elem_series(X, w):
    if abs(X) < 1e-12:
        return ("short (none)", 0.0)
    if X > 0:
        return ("series L", X / w)          # henries
    return ("series C", -1.0 / (w * X))     # farads


def elem_shunt(B, w):
    if abs(B) < 1e-12:
        return ("open (none)", 0.0)
    if B > 0:
        return ("shunt  C", B / w)          # farads
    return ("shunt  L", -1.0 / (w * B))     # henries


def design_l(zs, zl, w):
    """Transform source impedance zs into zl seen at the LNA plane.
    Returns list of solutions with element values + residual check."""
    sols = []
    Rl, Xl = zl.real, zl.imag
    Rs, Xs = zs.real, zs.imag
    Ys = 1.0 / zs; Gs, Bs = Ys.real, Ys.imag
    Yl = 1.0 / zl; Gl, Bl = Yl.real, Yl.imag

    # Topology 1: series (at LNA) then shunt to ground, then source.
    disc1 = Gs / Rl - Gs ** 2
    if disc1 >= 0:
        root = math.sqrt(disc1)
        for sgn in (+1, -1):
            B = -Bs + sgn * root
            X = Xl + (Bs + B) * Rl / Gs
            zpar = 1.0 / (Ys + 1j * B)
            zA = 1j * X + zpar
            sols.append(("T1 series->shunt", elem_series(X, w),
                         elem_shunt(B, w), abs(zA - zl)))

    # Topology 2: shunt (at LNA) to ground then series toward source.
    disc2 = Rs / Gl - Rs ** 2
    if disc2 >= 0:
        root = math.sqrt(disc2)
        for sgn in (+1, -1):
            X = -Xs + sgn * root
            B = Bl + (Xs + X) * Gl / Rs
            zser = 1j * X + zs
            yA = 1j * B + 1.0 / zser
            sols.append(("T2 shunt->series", elem_series(X, w),
                         elem_shunt(B, w), abs(1.0 / yA - zl)))
    return sols


def fmt_val(kind, val):
    if "none" in kind:
        return f"{kind}"
    if kind.endswith("L"):
        return f"{kind} = {val*1e9:9.4f} nH"
    return f"{kind} = {val*1e12:9.4f} pF"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("lna", help="LNA .s2p")
    ap.add_argument("filt", help="roofing filter .s2p")
    ap.add_argument("--freq", type=float, default=None,
                    help="design frequency in Hz (e.g. 1.45e9)")
    ap.add_argument("--filter-port", choices=["11", "22"], default="22",
                    help="filter output-port S-param (default 22)")
    # target impedance the LNA should see -- one of these, else noise block
    ap.add_argument("--gamma-opt", type=float, nargs=2, metavar=("MAG", "ANGdeg"),
                    help="target reflection coeff from datasheet noise table")
    ap.add_argument("--z-opt", type=float, nargs=2, metavar=("R", "X"),
                    help="target impedance in ohms (overrides everything)")
    ap.add_argument("--to-z0", action="store_true",
                    help="fallback with no noise data: match filter output to Z0")
    args = ap.parse_args()

    lna = parse_touchstone(args.lna)
    flt = parse_touchstone(args.filt)
    z0 = lna["z0"]

    # ---- design frequency ----
    if args.freq:
        f = args.freq
    elif lna["nf"] is not None:
        f = 0.5 * (lna["nf"]["f"].min() + lna["nf"]["f"].max())
    else:
        f = 0.5 * (flt["f"].min() + flt["f"].max())
    w = 2 * math.pi * f

    # ---- resolve target impedance the LNA should see ----
    nfmin = None
    if args.z_opt is not None:
        z_opt = complex(args.z_opt[0], args.z_opt[1]); tsrc = "user --z-opt"
    elif args.gamma_opt is not None:
        g = args.gamma_opt[0] * cmath.exp(1j * math.radians(args.gamma_opt[1]))
        z_opt = g_to_z(g, z0); tsrc = "user --gamma-opt (datasheet noise table)"
    elif lna["nf"] is not None:
        nf = lna["nf"]; order = np.argsort(nf["f"])
        gopt = interp_c(nf["f"], nf["gopt"], f)
        nfmin = float(np.interp(f, nf["f"][order], nf["nfmin_db"][order]))
        z_opt = g_to_z(gopt, z0); tsrc = "LNA noise block"
    elif args.to_z0:
        z_opt = complex(z0, 0.0)
        tsrc = f"fallback: match filter output to {z0:.0f} ohm (no noise data)"
    else:
        raise SystemExit(
            "This LNA .s2p has no noise block, so Gamma_opt is unknown.\n"
            "A 50-ohm NF-vs-freq curve does NOT contain it:\n"
            "  F = Fmin + (Rn/Gs)*|Ys - Yopt|^2  -> 4 unknowns, 1 scalar per freq.\n"
            "Supply the target one of these ways:\n"
            "  --gamma-opt MAG ANGdeg   from the datasheet noise-parameter table\n"
            "  --z-opt R X              target impedance in ohms\n"
            "  --to-z0                  fallback: restore the characterized 50-ohm NF")

    gsrc = interp_c(flt["f"], flt["s"][args.filter_port], f)
    z_src = g_to_z(gsrc, flt["z0"])

    print(f"Design frequency        : {f/1e9:.4f} GHz")
    print(f"Target source           : {tsrc}")
    if nfmin is not None:
        print(f"LNA  NFmin              : {nfmin:.3f} dB")
    print(f"Target Z_opt (LNA wants): {z_opt.real:8.3f} {z_opt.imag:+8.3f} j  ohm")
    print(f"Filter S{args.filter_port} (LNA sees)   : {abs(gsrc):.4f} /_{math.degrees(cmath.phase(gsrc)):+.2f} deg")
    print(f"Source Z (LNA sees)     : {z_src.real:8.3f} {z_src.imag:+8.3f} j  ohm")
    print()

    sols = design_l(z_src, z_opt, w)
    if not sols:
        print("No real 2-element L solution. Try a 3-element (Pi/T) network.")
        return
    print("L-network solutions (source-side -> LNA plane):")
    for topo, se, sh, resid in sols:
        print(f"  {topo:18s} | {fmt_val(*se):22s} | {fmt_val(*sh):22s} "
              f"| residual={resid:.2e} ohm")


if __name__ == "__main__":
    main()
