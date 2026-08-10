"""Desktop GUI for rfparts (Tkinter — ships with standard Python, no extra deps).

All specs are entered in the form: category, frequency, temperature, gain,
noise, attenuation, connector, mount, package/interface, space qualification,
lead time, preferred/excluded vendors, and freeform criteria. The search runs
on a background thread (network I/O is rate-limited), streaming candidate-page
extraction progress to a determinate bar, and can be cancelled mid-run. Results
land in a sortable table; from there you can open a part page, save the markdown
report, or draft an RFQ email.

Nothing here executes remote content or downloads binaries: it drives the same
polite, robots.txt-obeying fetch path as the CLI. Launch with `rfparts gui`,
`rfparts-gui`, or `python -m rfparts.gui`.
"""
import sys
import json
import time
from pathlib import Path
import queue
import re
import threading
import webbrowser
from types import SimpleNamespace

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import cli, rank, rfq, spec, space_dataset, marketplaces, partdb
from . import vendor_catalogs
from . import registry
from .registry import DATA, GUI_CATEGORIES, load_vendors
from .spec import PACKAGE_SYNONYMS
from .paths import ADI_PARAMETRICS, DATA_ROOT, EVERYTHING_RF, NEW_SOURCES

# Only the user-facing categories, shown by their display label.
CATEGORIES = [registry.category_label(k) for k in GUI_CATEGORIES]
PACKAGES = [""] + sorted(PACKAGE_SYNONYMS.keys())
IMPEDANCES = ["", "50 Ω", "75 Ω", "33 Ω"]
PREFS = DATA / "gui_prefs.json"


def _norm_name(value):
    """Loose vendor-name key for matching results to a supplier."""
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _load_prefs():
    try:
        return json.loads(PREFS.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_prefs(d):
    try:
        PREFS.write_text(json.dumps(d), encoding="utf-8")
    except OSError:
        pass

# Cell colours by whether a value meets the entered requirement.
CELL_MET = "#c8e6c9"       # green  — meets requirement
CELL_MISS = "#f4c7c3"      # red    — fails requirement
CELL_UNKNOWN = "#ffe6a3"   # amber  — required but value unknown
CELL_NEUTRAL = "#ffffff"   # white  — not a requirement / informational
CELL_NA = "#eeeeee"        # grey   — no value at all
HEAD_BG = "#37474f"
HEAD_FG = "#ffffff"
SEL_BORDER = "#1565c0"
TIER_BG = {"A": "#e8f6ec", "B": "#fff8e1", "C": "#fdecea"}

# Fixed leading + trailing columns of the datasheet grid.
LEAD_COLS = [
    ("score", "Score", 66),
    ("vendor", "Vendor", 130),
    ("part", "Part", 190),
    # Subcategory lives on the part ROW, not in specs, so it never appeared
    # among the spec columns. For switches it carries absorptive/reflective --
    # exactly what you want beside the part number.
    ("subtype", "Subtype", 96),
    ("interface", "Interface", 100),
    ("space", "Space qual", 100),
    # The mined score sits BESIDE the stated qualification rather than
    # replacing it. It matters most where Space qual is blank -- the case it
    # exists for -- so side by side lets a stated and an inferred one be told
    # apart at a glance.
    ("spacepct", "Space %", 78),
]
TAIL_COLS = [("notes", "Notes", 260)]

# Candidate spec columns that can appear between the lead and tail columns, each
# mapped to the rank criterion that decides its colour. Only columns with a
# value on at least one result are shown, so the grid reads like a datasheet for
# whatever category was searched. (RF port counting is intentionally gone.)
#   (col id, heading, spec key, criterion name, formatter)
def _f_freq(v):
    """Format a frequency band without assuming both endpoints exist.

    Rebuilt datasets may legitimately contain only a lower or upper edge while
    vendor-specific min/max rows are being combined.  Those values used to
    reach ``:g`` as None and crash the entire Tkinter results callback.
    """
    def _num(x):
        if isinstance(x, bool) or not isinstance(x, (int, float)):
            return None
        try:
            return f"{x:g}"
        except (TypeError, ValueError):
            return None

    if isinstance(v, (list, tuple)) and len(v) == 2:
        lo, hi = _num(v[0]), _num(v[1])
        if lo is not None and hi is not None:
            return f"{lo}–{hi}"
        if lo is not None:
            return f"≥{lo}"
        if hi is not None:
            return f"≤{hi}"
        return ""

    single = _num(v)
    return single or ""


_SPACE_PCT_BG = [(70, "#d7ecd9"), (40, "#f2f0d8"), (0, "#f4f4f4")]


def _space_pct_cell(c):
    """(text, background) for the mined space-qualification score.

    Blank when nothing was mined: an absent score and a score of zero mean
    different things, and showing "0%" for "never assessed" would be a claim
    the pipeline never made. Shading is deliberately gentle -- this is an
    inference from datasheet wording, not a qualification."""
    specs = c.get("specs") or {}
    pct = specs.get("space_score_pct")
    if pct is None:
        return ("", CELL_NEUTRAL)
    try:
        v = float(pct)
    except (TypeError, ValueError):
        return ("", CELL_NEUTRAL)
    bg = next(col for lo, col in _SPACE_PCT_BG if v >= lo)
    return (f"{v:.0f}%", bg)


def _subtype_str(c):
    """Kind of part within its category: absorptive/reflective for a switch.

    Falls back to a stated switch_type spec when the part row carries no
    subcategory, so a value parsed from a datasheet still shows up."""
    sub = (c.get("subcategory") or "").strip()
    if not sub:
        specs = c.get("specs") or {}
        for k in ("switch_type", "subtype", "configuration"):
            v = specs.get(k)
            if isinstance(v, str) and v.strip():
                sub = v.strip()
                break
    return sub.replace("_", " ")[:22]


def _f_text(v):
    return str(v) if isinstance(v, str) and v else ""


def _f_num(v):
    return f"{v:g}" if isinstance(v, (int, float)) else ""


def _f_time_ns(v):
    """Switching speed, stored canonically in ns but shown in whatever unit reads
    naturally: 45 ns, 2.5 us, 10 ms. A PIN diode switch and an electromechanical
    one differ by five orders of magnitude, so a single unit makes the column
    unreadable."""
    if not isinstance(v, (int, float)):
        return ""
    if v < 1000:
        return f"{v:g} ns"
    if v < 1e6:
        return f"{v / 1e3:g} \u00b5s"
    return f"{v / 1e6:g} ms"


SPEC_COLS = [
    ("freq", "Freq (GHz)", "freq_ghz", "freq", _f_freq),
    ("gain", "Gain (dB)", "gain_db", "gain", _f_num),
    ("nf", "NF (dB)", "noise_nf_db", "nf", _f_num),
    ("p1db", "P1dB (dBm)", "p1db_dbm", "p1db", _f_num),
    ("oip3", "OIP3 (dBm)", "oip3_dbm", "oip3", _f_num),
    ("psat", "Psat (dBm)", "psat_dbm", None, _f_num),
    ("il", "Ins.loss (dB)", "insertion_loss_db", None, _f_num),
    ("cl", "Conv.loss (dB)", "conversion_loss_db", "cl", _f_num),
    # A mixer has three ports and three ranges. freq_ghz alone cannot express
    # that, and these were being stored and then never shown.
    ("rff", "RF (GHz)", "rf_freq_ghz", "rff", _f_freq),
    ("lof", "LO (GHz)", "lo_freq_ghz", "lof", _f_freq),
    ("iff", "IF (GHz)", "if_freq_ghz", "iff", _f_freq),
    ("isol", "Isolation (dB)", "isolation_db", "isol", _f_num),
    ("tsw", "Switching", "switching_time_ns", "tsw", _f_time_ns),
    ("thr", "Config", "throw_config", "thr", _f_text),
    ("atten", "Atten (dB)", "attenuation_db", "atten", _f_num),
    ("pwr", "Power (W)", "power_w", "pwr", _f_num),
    ("tid", "TID (kRad)", "tid_krad", None, _f_num),
    ("sel", "SEL (MeV)", "sel_mev", None, _f_num),
]



# --- spec filter expressions ------------------------------------------------
# The left panel's per-spec boxes accept a small expression language rather than
# a bare number, because "isolation" wants a floor, "insertion loss" wants a
# ceiling and "frequency" wants a window -- one input type cannot serve all
# three, and forcing separate min/max boxes for 14 specs would swamp the panel.
#     40        at least 40           (>=)
#     <2        at most 2
#     >30       more than 30
#     4-8       between 4 and 8 inclusive
#     4-        4 and up
#
# There is deliberately no "-8" shorthand for "up to 8": gain, P1dB and OIP3 are
# routinely negative, so "-8" has to keep meaning the number minus eight. Use
# "<8" for a ceiling.
_FILT_RANGE = re.compile(r"^\s*([-+]?\d+(?:\.\d+)?)\s*(?:-|to|\u2013)\s*"
                         r"([-+]?\d+(?:\.\d+)?)\s*$", re.I)
_FILT_CMP = re.compile(r"^\s*(<=|>=|<|>|=)?\s*([-+]?\d+(?:\.\d+)?)\s*$")
_FILT_OPEN_MAX = re.compile(r"^\s*([-+]?\d+(?:\.\d+)?)\s*(?:-|to)\s*$", re.I)


def parse_spec_filter(text):
    """Turn a filter box into (lo, hi), either bound possibly None.

    Returns None when the text is not a usable expression, so a half-typed entry
    silently does nothing instead of hiding every row."""
    s = (text or "").strip()
    if not s:
        return None
    m = _FILT_RANGE.match(s)
    if m:
        lo, hi = float(m.group(1)), float(m.group(2))
        return (min(lo, hi), max(lo, hi))
    m = _FILT_CMP.match(s)               # checked before "4-" so "-8" stays -8
    if m:
        pass
    else:
        m2 = _FILT_OPEN_MAX.match(s)     # "4-" = 4 and up
        if m2:
            return (float(m2.group(1)), None)
    m = _FILT_CMP.match(s)
    if m:
        op, val = m.group(1) or ">=", float(m.group(2))
        if op in (">=", ">"):
            return (val, None)
        if op in ("<=", "<"):
            return (None, val)
        return (val, val)
    return None


def spec_text_matches(value, needle):
    return needle.strip().lower() in str(value or "").lower()


def spec_value_in_range(value, lo, hi):
    """Does a stored spec value satisfy the bounds?

    A frequency spec is a [lo, hi] band, and the useful question for a band is
    'does it cover what I asked for', not 'is its first number in range' -- so
    bands are tested for overlap/containment rather than compared as scalars."""
    if isinstance(value, (list, tuple)):
        if len(value) != 2:
            return False
        vlo, vhi = value
        if not isinstance(vlo, (int, float)) or not isinstance(vhi, (int, float)):
            return False
        if lo is not None and hi is not None:
            return vlo <= lo and vhi >= hi        # band must cover the window
        if lo is not None:
            return vhi >= lo
        if hi is not None:
            return vlo <= hi
        return True
    if not isinstance(value, (int, float)):
        return False
    if lo is not None and value < lo:
        return False
    if hi is not None and value > hi:
        return False
    return True



# Specs that are text, not numbers -- filtered by case-insensitive substring.
_TEXT_SPEC_HINTS = ("package", "connector", "configuration", "subtype", "mount",
                    "grade", "variant", "process", "form", "technology", "band",
                    "application", "lifecycle", "status", "pulsed", "text",
                    "description", "series", "polarity", "type")


def spec_is_text(key, value=None):
    if isinstance(value, str):
        return True
    k = (key or "").lower()
    return any(h in k for h in _TEXT_SPEC_HINTS)


_SPEC_LABEL_UNITS = [("_ghz", " (GHz)"), ("_dbm", " (dBm)"), ("_db", " (dB)"),
                     ("_ns", " (ns)"), ("_ohm", " (\u03a9)"), ("_ma", " (mA)"),
                     ("_mhz", " (MHz)"), ("_pct", " (%)"), ("_w", " (W)"),
                     ("_v", " (V)"), ("_c", " (\u00b0C)"), ("_krad", " (kRad)"),
                     ("_mev", " (MeV)")]


def spec_label(key):
    """A readable label for any spec key, including ones with no column.

    Built from the key rather than a hand-maintained table, because the whole
    point is to cover specs that were never enumerated anywhere -- a fixed table
    would silently omit exactly the ones this feature exists to expose."""
    for cid, heading, k, _crit, _fmt in SPEC_COLS:
        if k == key:
            return heading
    meta = registry.PARAM_SPECS.get(key)
    if meta:
        return meta["label"]
    base, unit = key, ""
    for suffix, u in _SPEC_LABEL_UNITS:
        if base.endswith(suffix):
            base, unit = base[: -len(suffix)], u
            break
    pretty = base.replace("_", " ").strip()
    # Keep RF acronyms upper-case; "Vswr" and "Oip3" read like typos.
    _ACRONYMS = {"vswr": "VSWR", "oip3": "OIP3", "iip3": "IIP3", "nf": "NF",
                 "p1db": "P1dB", "psat": "Psat", "tid": "TID", "sel": "SEL",
                 "rf": "RF", "dc": "DC", "il": "IL", "sma": "SMA",
                 "absmax": "abs-max", "temp": "Temp", "pae": "PAE"}
    words = [_ACRONYMS.get(w.lower(), w.capitalize()) for w in pretty.split()]
    return " ".join(words) + unit


# Categories where a throw configuration is a real property of the part, so the
# Config dropdown is shown between Category and Subcategory.
CONFIG_CATEGORIES = ("switch",)


class CheckList(ttk.Menubutton):
    """A dropdown of checkboxes: pick any number of values, not just one.

    tkinter has no multi-select combobox, so this is a Menubutton whose menu
    holds one Checkbutton per choice plus Select all / Clear all. The button
    text summarises the selection ("(any)", "SMT", "3 selected") so the panel
    stays readable when several are ticked.

    It keeps a StringVar in sync holding a comma-separated selection, so callers
    that already read `self.vars[key].get()` keep working: an empty string or
    "(any)" still means no constraint, and a single choice still reads as that
    one value. That is what lets the existing query/filter code stay untouched.
    """

    ANY = "(any)"

    def __init__(self, master, textvariable=None, values=(), width=22,
                 on_change=None, **kw):
        super().__init__(master, width=width, style="TMenubutton", **kw)
        self._var = textvariable if textvariable is not None else tk.StringVar()
        self._on_change = on_change
        self._checks = {}
        self._menu = tk.Menu(self, tearoff=False)
        self.configure(menu=self._menu, direction="below")
        self.set_values(values)

    # ---- public API mirroring the bits of Combobox that callers used --------
    def set_values(self, values):
        """Rebuild the menu. Ticked values that still exist stay ticked."""
        previously = set(self.selection())
        self._menu.delete(0, "end")
        self._checks = {}
        self._menu.add_command(label="Select all", command=self.select_all)
        self._menu.add_command(label="Clear all", command=self.clear_all)
        self._menu.add_separator()
        for v in values:
            if v == self.ANY:
                continue          # "(any)" is what an empty selection MEANS
            bv = tk.BooleanVar(value=(v in previously))
            self._checks[v] = bv
            self._menu.add_checkbutton(label=v, variable=bv,
                                       onvalue=True, offvalue=False,
                                       command=self._sync)
        self._sync()

    def config(self, **kw):
        if "values" in kw:
            self.set_values(kw.pop("values"))
        if kw:
            super().config(**kw)
    configure = config

    def selection(self):
        """The ticked values, in menu order."""
        return [v for v, bv in self._checks.items() if bv.get()]

    def select_all(self):
        for bv in self._checks.values():
            bv.set(True)
        self._sync()

    def clear_all(self):
        for bv in self._checks.values():
            bv.set(False)
        self._sync()

    def set_selection(self, values):
        wanted = {str(v).strip() for v in values if str(v).strip()}
        for v, bv in self._checks.items():
            bv.set(v in wanted)
        self._sync()

    def _sync(self):
        chosen = self.selection()
        # Everything ticked means the same as nothing ticked: no constraint.
        if not chosen or len(chosen) == len(self._checks):
            self._var.set("")
            self.configure(text=self.ANY)
        elif len(chosen) == 1:
            self._var.set(chosen[0])
            self.configure(text=chosen[0][:26])
        else:
            self._var.set(", ".join(chosen))
            self.configure(text=f"{len(chosen)} selected")
        if self._on_change:
            try:
                self._on_change()
            except Exception:
                pass


def selected_values(raw):
    """A CheckList variable's text -> list of chosen values ([] means any)."""
    s = str(raw or "").strip()
    if not s or s == CheckList.ANY:
        return []
    return [v.strip() for v in s.split(",") if v.strip()
            and v.strip() != CheckList.ANY]


class BrowseWindow(tk.Toplevel):
    """Browse the catalogue: every part in the chosen categories and vendors.

    The main search answers "what fits this spec". This answers "what do we have",
    which is a different question and was previously only answerable by writing
    SQL. Both selectors are multi-select, because the useful queries are things
    like "every switch and mixer from MACOM or Qorvo".
    """

    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.title("Browse catalogue")
        self.geometry("1500x780")
        self._rows = []
        self._sort_col = None
        self._sort_desc = False

        bar = ttk.Frame(self, padding=8)
        bar.pack(fill="x")
        ttk.Label(bar, text="Categories").grid(row=0, column=0, sticky="w")
        self.cat_var = tk.StringVar()
        self.cat_pick = CheckList(bar, textvariable=self.cat_var,
                                  values=self._categories(), width=26)
        self.cat_pick.grid(row=0, column=1, sticky="w", padx=(4, 14))

        ttk.Label(bar, text="Vendors").grid(row=0, column=2, sticky="w")
        self.ven_var = tk.StringVar()
        self.ven_pick = CheckList(bar, textvariable=self.ven_var,
                                  values=self._vendors(), width=30)
        self.ven_pick.grid(row=0, column=3, sticky="w", padx=(4, 14))

        ttk.Label(bar, text="Max rows").grid(row=0, column=4, sticky="w")
        self.limit_var = tk.StringVar(value="400")
        ttk.Entry(bar, textvariable=self.limit_var, width=7).grid(
            row=0, column=5, sticky="w", padx=(4, 14))

        ttk.Button(bar, text="Show", command=self.refresh).grid(row=0, column=6)
        ttk.Button(bar, text="Export CSV", command=self.export).grid(
            row=0, column=7, padx=6)
        self.status = ttk.Label(bar, text="", foreground="#555")
        self.status.grid(row=1, column=0, columnspan=8, sticky="w", pady=(6, 0))

        wrap = ttk.Frame(self, padding=(8, 0, 8, 8))
        wrap.pack(fill="both", expand=True)
        self.table = DatasheetTable(wrap, on_sort=self._on_sort)
        self.table.pack(fill="both", expand=True)
        # Deliberately NOT refreshing on open: with nothing selected that means
        # "every part", and building the entire catalogue before the window is
        # even visible is what made it look hung.
        self.table.set_columns(["Vendor", "Part", "Category"], [150, 190, 110])
        self.status.configure(
            text="Choose categories and/or vendors, then press Show. "
                 "Leave both empty to list everything (slower).")

    # ---------------------------------------------------------------- data
    def _categories(self):
        try:
            rows = partdb.db().execute(
                "SELECT category AS c, COUNT(*) AS n FROM parts "
                "WHERE category IS NOT NULL AND category != '' "
                "GROUP BY category ORDER BY n DESC").fetchall()
            return [f"{r['c']}  ({r['n']})" for r in rows]
        except Exception:
            return []

    def _vendors(self):
        """Vendors as the CANONICAL name, so one entry per manufacturer rather
        than one per spelling."""
        try:
            rows = partdb.db().execute(
                "SELECT vendor AS v, COUNT(*) AS n FROM parts "
                "WHERE vendor IS NOT NULL AND vendor != '' GROUP BY vendor"
            ).fetchall()
        except Exception:
            return []
        merged = {}
        for r in rows:
            merged[partdb.canonical_vendor(r["v"])] = \
                merged.get(partdb.canonical_vendor(r["v"]), 0) + r["n"]
        return [f"{v}  ({n})" for v, n in
                sorted(merged.items(), key=lambda kv: -kv[1])]

    @staticmethod
    def _strip_count(label):
        return re.sub(r"\s*\(\d+\)\s*$", "", str(label)).strip()

    def refresh(self):
        cats = [self._strip_count(v) for v in selected_values(self.cat_var.get())]
        vens = [self._strip_count(v) for v in selected_values(self.ven_var.get())]
        try:
            limit = max(1, int(self.limit_var.get()))
        except ValueError:
            limit = 2000
        try:
            rows = self._query(cats, vens, limit)
        except Exception as e:
            self.status.configure(text=f"query failed: {type(e).__name__}: {e}")
            return
        self._rows = rows
        self._render(rows)
        cat_txt = ", ".join(cats) if cats else "all categories"
        ven_txt = ", ".join(vens) if vens else "all vendors"
        self.status.configure(
            text=f"{len(rows)} part(s) — {cat_txt} — {ven_txt}"
                 + ("   (limit reached; raise Max rows to see more)"
                    if len(rows) >= limit else ""))

    def _vendor_spellings(self, canon_names):
        """Raw vendor strings in the database for these canonical names.

        Vendors are canonicalised when a candidate is BUILT, so filtering has to
        happen on the raw column that SQL can see -- otherwise the query cannot
        use an index and every row has to be assembled before it can be rejected.
        """
        if not canon_names:
            return []
        want = {c.lower() for c in canon_names}
        out = []
        try:
            for r in partdb.db().execute(
                    "SELECT DISTINCT vendor FROM parts "
                    "WHERE vendor IS NOT NULL AND vendor != ''"):
                if partdb.canonical_vendor(r["vendor"]).lower() in want:
                    out.append(r["vendor"])
        except Exception:
            pass
        return out

    def _query(self, cats, vendors, limit):
        """Ask SQL for the matching part ids, then build only those.

        The first version fetched query_candidates(limit*3) -- which itself
        over-fetches threefold -- and filtered in Python, so choosing one vendor
        still assembled the specs of ~18000 parts. That is the whole database on
        every refresh AND every sort, which is why it stopped responding.
        """
        where, args = [], []
        if cats:
            where.append("lower(category) IN (%s)" % ",".join("?" * len(cats)))
            args += [c.lower() for c in cats]
        if vendors:
            spellings = self._vendor_spellings(vendors)
            if not spellings:
                return []
            where.append("vendor IN (%s)" % ",".join("?" * len(spellings)))
            args += spellings
        sql = "SELECT id FROM parts"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY vendor, mpn LIMIT ?"
        args.append(limit)
        ids = [r["id"] for r in partdb.db().execute(sql, args)]
        if not ids:
            return []
        return partdb.query_candidates(ids=ids, limit=len(ids))

    # -------------------------------------------------------------- render
    def _visible_cols(self, rows):
        present = set()
        for c in rows:
            s = c.get("specs") or {}
            for cid, _h, key, _cr, _f in SPEC_COLS:
                if s.get(key) not in (None, "", []):
                    present.add(cid)
        return [col for col in SPEC_COLS if col[0] in present]

    def _render(self, rows):
        spec_cols = self._visible_cols(rows)
        headings = ["Vendor", "Part", "Category", "Subtype", "Space"] + \
                   [h for _c, h, _k, _cr, _f in spec_cols] + ["Package"]
        widths = [150, 190, 110, 96, 100] + \
                 [110] * len(spec_cols) + [150]
        self.table.set_columns(headings, widths)
        self.table.clear_rows() if hasattr(self.table, "clear_rows") else None
        for c in rows:
            specs = c.get("specs") or {}
            cells = [(c.get("vendor", ""), CELL_NEUTRAL),
                     (c.get("model") or c.get("title", ""), CELL_NEUTRAL),
                     (c.get("category") or "", CELL_NEUTRAL),
                     (_subtype_str(c), CELL_NEUTRAL),
                     (rank._space_str(c), CELL_NEUTRAL)]
            for _cid, _h, key, _cr, fmt in spec_cols:
                cells.append((fmt(specs.get(key)), CELL_NEUTRAL))
            cells.append((str(specs.get("package") or "")[:28], CELL_NEUTRAL))
            self.table.add_row(cells, c)
        self._spec_cols = spec_cols

    def _on_sort(self, col):
        if self._sort_col == col:
            self._sort_desc = not self._sort_desc
        else:
            self._sort_col, self._sort_desc = col, False
        spec_cols = getattr(self, "_spec_cols", [])
        keys = [lambda c: (c.get("vendor") or "").lower(),
                lambda c: (c.get("model") or c.get("title") or "").lower(),
                lambda c: (c.get("category") or "").lower(),
                lambda c: _subtype_str(c).lower(),
                lambda c: rank._space_str(c).lower()]
        for _cid, _h, key, _cr, _f in spec_cols:
            keys.append(lambda c, k=key: (c.get("specs") or {}).get(k))
        keys.append(lambda c: str((c.get("specs") or {}).get("package") or "").lower())
        if not (0 <= col < len(keys)):
            return
        self._rows = App._sorted_by(self._rows, keys[col], self._sort_desc)
        self._render(self._rows)
        self.table.note_sort(col, self._sort_desc)

    def export(self):
        if not self._rows:
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".csv", filetypes=[("CSV", "*.csv")],
            initialfile="catalogue.csv")
        if not path:
            return
        import csv as _csv
        spec_keys = sorted({k for c in self._rows for k in (c.get("specs") or {})})
        try:
            with open(path, "w", newline="", encoding="utf-8") as fh:
                w = _csv.writer(fh)
                w.writerow(["vendor", "mpn", "category", "subcategory",
                            "description", "url"] + spec_keys)
                for c in self._rows:
                    s = c.get("specs") or {}
                    w.writerow([c.get("vendor", ""), c.get("model", ""),
                                c.get("category", ""), c.get("subcategory", ""),
                                (c.get("description") or "")[:200],
                                c.get("url", "")]
                               + [s.get(k, "") for k in spec_keys])
            self.status.configure(text=f"exported {len(self._rows)} row(s) to {path}")
        except OSError as e:
            self.status.configure(text=f"export failed: {e}")


class App(ttk.Frame):
    # Class-level defaults so a re-layout triggered before the filter panel is
    # built (a category trace can fire during __init__) cannot raise.
    _spec_filter_row = 0
    _spec_filter_toggle = None
    _spec_filter_box = None
    _sf_inner = None
    _sf_canvas = None
    _spec_filter_hint = None
    _spec_filter_keys = ()
    _sort_col = None
    _sort_desc = False

    def __init__(self, master):
        super().__init__(master, padding=8)
        self.grid(sticky="nsew")
        master.columnconfigure(0, weight=1)
        master.rowconfigure(0, weight=1)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(1, weight=1)

        # Bounded: an unbounded queue let a fast local ingest (1000+ parts a
        # second) outrun the 40-per-tick UI drain and grow without limit. When
        # full, the producer drops progress text rather than stalling the walk --
        # parts and milestones are never dropped.
        self.q = queue.Queue(maxsize=4000)
        self.selected_candidate = None
        self.last_query = None
        self.last_ranked = []
        self.last_errors = []
        self.cancel_event = threading.Event()

        prefs = _load_prefs()
        saved_prefer = set(prefs.get("prefer", []))
        saved_exclude = set(prefs.get("exclude", []))
        vendor_names = self._all_vendor_names()
        self.prefer_vars = {name: tk.BooleanVar(value=name in saved_prefer)
                            for name in vendor_names}
        self.exclude_vars = {name: tk.BooleanVar(value=name in saved_exclude)
                             for name in vendor_names}

        self._build_form()
        self._build_results()
        self._build_statusbar()

    # ---- form -----------------------------------------------------------
    def _build_form(self):
        f = ttk.LabelFrame(self, text="Requirement", padding=8)
        f.grid(row=0, column=0, rowspan=3, sticky="nsw", padx=(0, 8))
        f.columnconfigure(1, weight=1)
        self._form = f
        self.vars = {}

        # category label <-> canonical key
        self._cat_label_to_key = {registry.category_label(k): k for k in GUI_CATEGORIES}

        self.vars["category"] = tk.StringVar()
        self._cat_label = ttk.Label(f, text="Category *")
        self._cat_combo = ttk.Combobox(f, textvariable=self.vars["category"],
                                       values=CATEGORIES, width=22, state="readonly")
        self.vars["category"].trace_add("write", self._on_category_change)

        self.vars["subcategory"] = tk.StringVar(value="(any)")
        self._sub_label = ttk.Label(f, text="Subcategory")
        # Multi-select: a search for "absorptive OR reflective" is a normal thing
        # to want, and a single-choice dropdown made it two searches.
        self._sub_combo = CheckList(f, textvariable=self.vars["subcategory"],
                                    values=[], width=22)
        self._sub_label_to = {"(any)": None}
        # A filter's response type (LPF/HPF/BPF/BSF) changes the frequency input
        # and key specs, so relayout when the subcategory changes too.
        self.vars["subcategory"].trace_add("write", self._on_subcategory_change)

        # Throw configuration gets its own dropdown directly under Category and
        # above Subcategory. It used to be mixed INTO the subcategory list, which
        # meant choosing SP4T made absorptive/reflective unselectable even though
        # they are independent facts about the same part.
        self.vars["throw_config"] = tk.StringVar(value="(any)")
        self._cfg_label = ttk.Label(f, text="Config")
        self._cfg_combo = CheckList(
            f, textvariable=self.vars["throw_config"],
            values=list(registry.THROW_CONFIG_CHOICES), width=22)

        self._keyparams_label = ttk.Label(f, text="", foreground="#777", wraplength=210)

        # --- all optional spec-input rows, created once, shown per category ---
        self.vars["freq"] = tk.StringVar()
        self.vars["temp_k"] = tk.StringVar()
        self.vars["gain_db_min"] = tk.StringVar()
        self.vars["noise_k_max"] = tk.StringVar()
        self.vars["attenuation_db"] = tk.StringVar()
        self.vars["connector"] = tk.StringVar()
        self.vars["mount"] = tk.StringVar()   # kept for spec.build; not shown
        self.vars["package"] = tk.StringVar()
        self.vars["impedance"] = tk.StringVar()
        self.vars["ports"] = tk.StringVar()
        self.vars["max_lead_weeks"] = tk.StringVar()
        self.vars["flt_cutoff"] = tk.StringVar()   # filter LPF/HPF cutoff (GHz)

        self.impedance_combo = ttk.Combobox(
            f, textvariable=self.vars["impedance"], values=IMPEDANCES, width=22,
            state="disabled")
        self.vars["package"].trace_add("write", self._on_package_change)

        def mkhelp(text):
            return ttk.Label(f, text=text, foreground="#777")

        # Filter LPF/HPF use a single cutoff (Fc); BPF/BSF reuse the freq range
        # row (relabelled). Created once; shown by _relayout for filters only.
        self._flt_cutoff_lbl = ttk.Label(f, text="Cutoff Fc (GHz)")
        self._flt_cutoff_entry = ttk.Entry(f, textvariable=self.vars["flt_cutoff"])
        self._flt_cutoff_help = mkhelp("single-sided cutoff frequency")

        # key -> (label widget, main widget, help widget or None)
        self._field_rows = {
            "freq": (ttk.Label(f, text="Frequency (GHz)"),
                     ttk.Entry(f, textvariable=self.vars["freq"]),
                     mkhelp("e.g. 4-8 or .1-18")),
            "gain_db_min": (ttk.Label(f, text="Min gain (dB)"),
                            ttk.Entry(f, textvariable=self.vars["gain_db_min"]), None),
            "noise_k_max": (ttk.Label(f, text="Max noise (K)"),
                            ttk.Entry(f, textvariable=self.vars["noise_k_max"]), None),
            "temp_k": (ttk.Label(f, text="Temperature (K)"),
                       ttk.Entry(f, textvariable=self.vars["temp_k"]),
                       mkhelp("≤120 K flags a cryo need")),
            "attenuation_db": (ttk.Label(f, text="Attenuation (dB)"),
                               ttk.Entry(f, textvariable=self.vars["attenuation_db"]), None),
            "ports": (ttk.Label(f, text="Ports"),
                      ttk.Entry(f, textvariable=self.vars["ports"]),
                      mkhelp("e.g. 2 (SPDT), 4-way")),
            "impedance": (ttk.Label(f, text="Impedance (Ω)"), self.impedance_combo, None),
            "connector": (ttk.Label(f, text="Connector"),
                          ttk.Entry(f, textvariable=self.vars["connector"]), None),
            "package": (ttk.Label(f, text="Package/interface"),
                        ttk.Combobox(f, textvariable=self.vars["package"],
                                     values=PACKAGES, width=22), None),
            "max_lead_weeks": (ttk.Label(f, text="Max lead (weeks)"),
                               ttk.Entry(f, textvariable=self.vars["max_lead_weeks"]), None),
        }
        # Optional parametric inputs (NF, P1dB, OIP3, isolation, ...): one entry
        # per PARAM_SPECS key, shown for the categories that expose them.
        for pkey, meta in registry.PARAM_SPECS.items():
            if pkey in self.vars:
                # Already has a dedicated widget (the Config dropdown). Creating a
                # second StringVar here silently orphaned the combobox: it kept
                # writing to the old variable while every reader looked at the new
                # empty one, so choosing SPDT did nothing at all.
                continue
            self.vars[pkey] = tk.StringVar()
            choices = registry.FIELD_CHOICES.get(pkey)
            widget = (ttk.Combobox(f, textvariable=self.vars[pkey], width=22,
                                   state="readonly",
                                   values=["(any)"] + list(choices))
                      if choices else
                      ttk.Entry(f, textvariable=self.vars[pkey]))
            self._field_rows[pkey] = (
                ttk.Label(f, text=meta["label"]), widget, None)

        self.vars["space"] = tk.BooleanVar()
        self._space_check = ttk.Checkbutton(
            f, text="Require space qualification", variable=self.vars["space"])

        # This build is offline-only: results come solely from the local dataset
        # (everythingRF + ingested space catalogs). No web discovery, extraction,
        # DigiKey API, or background crawling. The toggle is on and disabled,
        # kept only to make the offline behaviour explicit.
        self.vars["local_only"] = tk.BooleanVar(value=True)
        self._local_check = ttk.Checkbutton(
            f, text="Local dataset (offline space catalogs)",
            variable=self.vars["local_only"], state="disabled")
        self.vars["space"].set(True)   # space-qualified focus by default

        # --- footer (always shown) ---
        self._prefer_lbl = ttk.Label(f, text="Prefer vendors")
        self.prefer_btn = ttk.Button(f, text=self._prefer_label(),
                                     command=self.open_prefer_dialog)
        self._exclude_lbl = ttk.Label(f, text="Exclude vendors")
        self.exclude_btn = ttk.Button(f, text=self._exclude_label(),
                                      command=self.open_exclude_dialog)
        self.vars["other"] = tk.StringVar()
        self._other_lbl = ttk.Label(f, text="Other criteria")
        self._other_entry = ttk.Entry(f, textvariable=self.vars["other"])
        self._other_help = mkhelp("comma-separated")
        self.vars["top"] = tk.StringVar(value="250")
        self._top_lbl = ttk.Label(f, text="Show top N")
        self._top_entry = ttk.Entry(f, textvariable=self.vars["top"], width=8)

        self._btns = ttk.Frame(f)
        self.search_btn = ttk.Button(self._btns, text="Search", command=self.on_search)
        self.search_btn.pack(side="left")
        self.cancel_btn = ttk.Button(self._btns, text="Cancel", command=self.on_cancel,
                                     state="disabled")
        self.cancel_btn.pack(side="left", padx=4)
        ttk.Button(self._btns, text="Clear", command=self.on_clear).pack(side="left")

        self._btns2 = ttk.Frame(f)
        self.rebuild_btn = ttk.Button(self._btns2, text="Rebuild dataset…",
                                      command=self.on_rebuild)
        self.rebuild_btn.pack(side="left")
        ttk.Button(self._btns2, text="Recommended suppliers…",
                   command=self.open_suppliers).pack(side="left", padx=4)
        ttk.Button(self._btns2, text="Dataset health…",
                   command=self.show_health).pack(side="left")
        # "What do we have" is a different question from "what fits this spec",
        # and until now it could only be answered by writing SQL.
        ttk.Button(self._btns2, text="Browse catalogue…",
                   command=self.open_browse).pack(side="left", padx=4)

        self._build_spec_filters(f)

        self._on_category_change()   # initial layout (no category yet)

    # ---- per-spec result filters ----------------------------------------
    def _build_spec_filters(self, parent):
        """Scrollable filter panel, populated from the specs the RESULTS carry.

        It was previously built once from the 14 spec COLUMNS, which meant every
        other stored spec -- VSWR, impedance, supply, directivity, return loss,
        the absolute-maximum values -- was invisible to filtering even though the
        part detail pane showed it. The rows are now rebuilt after each search
        from the union of keys actually present, so anything a part reports can be
        filtered on."""
        self._spec_filter_vars = {}
        self._spec_filter_rows = {}
        self._spec_filters_open = tk.BooleanVar(value=False)
        self._spec_filter_toggle = ttk.Checkbutton(
            parent, text="Filter results by spec\u2026",
            variable=self._spec_filters_open,
            command=self._toggle_spec_filters)
        self._spec_filter_box = ttk.Labelframe(parent, text="Spec filters",
                                               padding=(6, 4))
        self._spec_filter_box.columnconfigure(0, weight=1)
        ttk.Label(self._spec_filter_box,
                  text="40 \u2265 40    <2 \u2264 2    4-8 range    text: substring",
                  foreground="#666", font=("", 8)).grid(row=0, column=0,
                                                        sticky="w", pady=(0, 3))
        # A scrollable inner area: a full result set can carry 30+ distinct specs,
        # which would otherwise push the Search button off the panel.
        holder = ttk.Frame(self._spec_filter_box)
        holder.grid(row=1, column=0, sticky="nsew")
        self._sf_canvas = tk.Canvas(holder, height=200, highlightthickness=0,
                                    background="#ffffff")
        self._sf_canvas.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(holder, orient="vertical",
                           command=self._sf_canvas.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self._sf_canvas.configure(yscrollcommand=sb.set)
        holder.columnconfigure(0, weight=1)
        self._sf_inner = tk.Frame(self._sf_canvas, background="#ffffff")
        self._sf_canvas.create_window((0, 0), window=self._sf_inner, anchor="nw")
        self._sf_inner.bind(
            "<Configure>",
            lambda e: self._sf_canvas.configure(
                scrollregion=self._sf_canvas.bbox("all")))
        self._sf_inner.columnconfigure(1, weight=1)

        btns = ttk.Frame(self._spec_filter_box)
        btns.grid(row=2, column=0, sticky="ew", pady=(4, 0))
        ttk.Button(btns, text="Apply", width=7,
                   command=self._reapply_filters).pack(side="left")
        ttk.Button(btns, text="Clear", width=7,
                   command=self._clear_spec_filters).pack(side="left", padx=3)
        self._spec_filter_hint = ttk.Label(btns, text="", foreground="#666",
                                           font=("", 8))
        self._spec_filter_hint.pack(side="left", padx=4)
        self._refresh_spec_filters([])

    def _spec_keys_in(self, ranked):
        """Every spec key present across the results, column specs first so the
        familiar ones stay at the top, then the rest alphabetically."""
        seen = {}
        for c in ranked or []:
            for k, v in (c.get("specs") or {}).items():
                if v in (None, "", []):
                    continue
                seen.setdefault(k, v)
        ordered = [k for _cid, _h, k, _cr, _f in SPEC_COLS if k in seen]
        cat = self._current_category_key()
        if cat:
            for k in registry.category_params(cat):
                if k not in ordered:
                    ordered.append(k)
        ordered += sorted(k for k in seen if k not in ordered)
        return ordered, seen

    def _refresh_spec_filters(self, ranked):
        """Rebuild the rows for the specs these results actually have.

        Existing entries are kept, so a value you typed survives a re-search."""
        if getattr(self, "_sf_inner", None) is None:
            return
        keys, sample = self._spec_keys_in(ranked)
        if not keys:
            keys = [k for _c, _h, k, _cr, _f in SPEC_COLS]
            sample = {}
        for w in self._sf_inner.winfo_children():
            w.grid_remove()
        for i, key in enumerate(keys):
            var = self._spec_filter_vars.get(key)
            if var is None:
                var = tk.StringVar()
                self._spec_filter_vars[key] = var
            row = self._spec_filter_rows.get(key)
            if row is None:
                is_text = spec_is_text(key, sample.get(key))
                lbl = tk.Label(self._sf_inner, text=spec_label(key), bg="#ffffff",
                               anchor="w", font=("", 8))
                ent = ttk.Entry(self._sf_inner, textvariable=var, width=11)
                ent.bind("<Return>", lambda e: self._reapply_filters())
                row = (lbl, ent, is_text)
                self._spec_filter_rows[key] = row
            lbl, ent, _is_text = row
            lbl.grid(row=i, column=0, sticky="w", padx=(2, 4), pady=1)
            ent.grid(row=i, column=1, sticky="ew", pady=1)
        self._spec_filter_keys = keys
        if getattr(self, "_spec_filter_hint", None) is not None:
            self._spec_filter_hint.configure(text=f"{len(keys)} spec(s)")

    def _toggle_spec_filters(self):
        if self._spec_filter_box is None:
            return
        if self._spec_filters_open.get():
            self._spec_filter_box.grid(row=self._spec_filter_row, column=0,
                                       columnspan=2, sticky="ew", pady=(2, 4))
        else:
            self._spec_filter_box.grid_remove()

    def _clear_spec_filters(self):
        for var in self._spec_filter_vars.values():
            var.set("")
        self._reapply_filters()

    def _reapply_filters(self):
        """Re-filter the results already in hand. No new search is run, so this
        stays instant even on a few thousand rows."""
        if self.last_ranked:
            self._show_results(self.last_ranked, self.last_errors, keep_sort=True)

    def _active_spec_filters(self):
        """[(key, kind, bounds_or_text)] for every box with usable text in it."""
        out = []
        # The Config dropdown filters like a text spec. Routing it through the
        # same path as the spec-filter boxes means one implementation rather than
        # two, and it works whether the value came from a catalog column, the
        # description, or a mined datasheet.
        # NOTE: the Config dropdown deliberately does NOT hard-filter here. It is
        # passed into the query instead, so rank marks matches met and mismatches
        # miss and the score reflects it -- other configurations stay visible but
        # sink. Use the Config box in this panel when exclusion is what you want.
        for key, var in getattr(self, "_spec_filter_vars", {}).items():
            try:
                raw = var.get().strip()
            except tk.TclError:
                continue
            if not raw:
                continue
            bounds = parse_spec_filter(raw)
            if bounds:
                out.append((key, "num", bounds))
            else:
                # Not a number expression, so treat it as a substring. That makes
                # package/connector/configuration filterable with the same box.
                out.append((key, "text", raw))
        return out

    def _apply_spec_filters(self, ranked):
        active = self._active_spec_filters()
        if not active:
            return ranked
        kept = []
        for c in ranked:
            specs = c.get("specs") or {}
            for key, kind, arg in active:
                val = specs.get(key)
                if kind == "anyof":
                    # Several ticked: match any of them.
                    if str(val or "").strip().lower() not in [
                            str(a).strip().lower() for a in arg]:
                        break
                elif kind == "exact":
                    # A dropdown choice is an exact match: picking SP4T must not
                    # also return SP4T-ish neighbours, and substring matching
                    # would make "SPST" match nothing while "SP1T" matched
                    # "SP12T".
                    if str(val or "").strip().lower() != str(arg).strip().lower():
                        break
                elif kind == "text":
                    if not spec_text_matches(val, arg):
                        break
                elif not spec_value_in_range(val, arg[0], arg[1]):
                    break
            else:
                kept.append(c)
        return kept

    def _chosen_subcategory(self):
        """The selected subcategory as (key, synonyms), or None for "any".

        Several may be ticked. The downstream query takes one, so the keys are
        merged and the synonym lists concatenated -- searching absorptive OR
        reflective then matches either, rather than silently using whichever
        happened to be first.
        """
        picks = [self._sub_label_to.get(v)
                 for v in selected_values(self.vars["subcategory"].get())]
        picks = [p for p in picks if p]
        if not picks:
            return None
        if len(picks) == 1:
            return picks[0]
        keys = "|".join(k for k, _s in picks)
        syns = [s for _k, sl in picks for s in (sl or [])]
        return (keys, syns)

    def _current_category_key(self):
        return self._cat_label_to_key.get(self.vars["category"].get().strip())

    # --- filter response-type awareness ---------------------------------
    _FILTER_RESPONSE = {"lpf": "lowpass", "hpf": "highpass",
                        "bpf": "bandpass", "bsf": "bandstop"}
    _FILTER_KEYPARAMS = {
        "lowpass":  ["cutoff Fc", "insertion loss", "stopband rejection", "return loss"],
        "highpass": ["cutoff Fc", "insertion loss", "stopband rejection", "return loss"],
        "bandpass": ["passband", "center / bandwidth", "insertion loss",
                     "rejection", "return loss"],
        "bandstop": ["stopband", "notch depth / rejection", "insertion loss",
                     "return loss"],
    }
    # frequency input (label, help) per filter response.
    _FILTER_FREQ_UI = {
        "lowpass":  ("Cutoff Fc (GHz)", "passband DC–Fc, e.g. 6"),
        "highpass": ("Cutoff Fc (GHz)", "passband Fc and above, e.g. 6"),
        "bandpass": ("Passband f\u2081–f\u2082 (GHz)", "e.g. 3.7-4.2"),
        "bandstop": ("Stopband f\u2081–f\u2082 (GHz)", "band to reject, e.g. 2.3-2.5"),
    }

    def _filter_response(self):
        """'lowpass'|'highpass'|'bandpass'|'bandstop' for the current filter
        subcategory, else None (not a filter, or a construction-only subtype
        such as cavity/ceramic/tunable/diplexer/duplexer)."""
        if self._current_category_key() != "filter":
            return None
        chosen = self._chosen_subcategory()
        return self._FILTER_RESPONSE.get(chosen[0] if chosen else None)

    def _apply_keyparams(self, cat_key):
        if cat_key == "filter":
            kp = (self._FILTER_KEYPARAMS.get(self._filter_response())
                  or registry.category_key_params("filter"))
        else:
            kp = registry.category_key_params(cat_key)
        self._keyparams_label.config(text=("Key specs: " + ", ".join(kp)) if kp else "")

    def _on_subcategory_change(self, *_):
        # For filters the response type changes the frequency input + key specs.
        if self._current_category_key() == "filter":
            self._apply_keyparams("filter")
            self._relayout("filter")

    def _place_freq(self, cat_key, r):
        """Grid the frequency input for this category, returning the next row.

        Filters get a type-aware control: a single cutoff (Fc) for LPF/HPF, a
        relabelled range for BPF/BSF. Every other category (and construction-only
        filter subtypes) keeps the generic 'Frequency (GHz)' range."""
        lbl, entry, hlp = self._field_rows["freq"]
        resp = self._filter_response() if cat_key == "filter" else None
        if resp in ("lowpass", "highpass"):
            self.vars["freq"].set("")        # clear range so it can't leak in
            text, help_text = self._FILTER_FREQ_UI[resp]
            self._flt_cutoff_lbl.config(text=text)
            self._flt_cutoff_help.config(text=help_text)
            self._flt_cutoff_lbl.grid(row=r, column=0, sticky="w", pady=2)
            self._flt_cutoff_entry.grid(row=r, column=1, sticky="ew", pady=2)
            r += 1
            self._flt_cutoff_help.grid(row=r, column=1, sticky="w")
            return r + 1
        self.vars["flt_cutoff"].set("")       # clear cutoff when using a range
        if resp in ("bandpass", "bandstop"):
            text, help_text = self._FILTER_FREQ_UI[resp]
        else:
            text, help_text = "Frequency (GHz)", "e.g. 4-8 or DC-18"
        lbl.config(text=text)
        if hlp is not None:
            hlp.config(text=help_text)
        lbl.grid(row=r, column=0, sticky="w", pady=2)
        entry.grid(row=r, column=1, sticky="ew", pady=2)
        r += 1
        if hlp is not None:
            hlp.grid(row=r, column=1, sticky="w")
            r += 1
        return r

    def _on_category_change(self, *_):
        key = self._current_category_key()
        # repopulate subcategory choices
        self._sub_label_to = {"(any)": None}
        values = ["(any)"]
        for k, lbl, syns in registry.subcategories(key):
            values.append(lbl)
            self._sub_label_to[lbl] = (k, syns)
        self._sub_combo.config(values=values)
        # CheckList keeps ticks that still exist and drops the rest, so nothing
        # needs resetting here.
        self._apply_keyparams(key)
        # Don't let a value typed under one category leak into a search for
        # another that hides that field (e.g. gain set for an amp, then Switches).
        shown = set(registry.category_fields(key))
        for fkey in self._field_rows:
            if fkey not in shown:
                self.vars[fkey].set("")
        if key not in CONFIG_CATEGORIES:
            self.vars["throw_config"].set("(any)")
        self._relayout(key)

    def _relayout(self, cat_key):
        # hide all optional widgets, then re-place the ones this category uses
        for lbl, w, hlp in self._field_rows.values():
            lbl.grid_remove()
            w.grid_remove()
            if hlp is not None:
                hlp.grid_remove()
        self._space_check.grid_remove()
        for w in (self._flt_cutoff_lbl, self._flt_cutoff_entry, self._flt_cutoff_help):
            w.grid_remove()

        r = 0
        self._cat_label.grid(row=r, column=0, sticky="w", pady=2)
        self._cat_combo.grid(row=r, column=1, sticky="ew", pady=2)
        r += 1
        # Config sits between Category and Subcategory, and only for categories
        # that actually have a throw configuration.
        if cat_key in CONFIG_CATEGORIES:
            self._cfg_label.grid(row=r, column=0, sticky="w", pady=2)
            self._cfg_combo.grid(row=r, column=1, sticky="ew", pady=2)
            r += 1
        else:
            self._cfg_label.grid_remove()
            self._cfg_combo.grid_remove()
        self._sub_label.grid(row=r, column=0, sticky="w", pady=2)
        self._sub_combo.grid(row=r, column=1, sticky="ew", pady=2)
        r += 1
        if self._keyparams_label.cget("text"):
            self._keyparams_label.grid(row=r, column=0, columnspan=2, sticky="w", pady=(0, 4))
            r += 1

        for field in registry.category_fields(cat_key):
            if field == "space":
                self._space_check.grid(row=r, column=0, columnspan=2, sticky="w", pady=2)
                r += 1
                continue
            if field == "freq":
                r = self._place_freq(cat_key, r)
                continue
            row = self._field_rows.get(field)
            if not row:
                continue
            lbl, w, hlp = row
            lbl.grid(row=r, column=0, sticky="w", pady=2)
            w.grid(row=r, column=1, sticky="ew", pady=2)
            r += 1
            if hlp is not None:
                hlp.grid(row=r, column=1, sticky="w")
                r += 1

        # space toggle sits with the always-shown footer if a category omits it
        if "space" not in registry.category_fields(cat_key):
            self._space_check.grid(row=r, column=0, columnspan=2, sticky="w", pady=2)
            r += 1
        # local-database toggle is always available, regardless of category
        self._local_check.grid(row=r, column=0, columnspan=2, sticky="w", pady=2)
        r += 1
        for lbl, w in ((self._prefer_lbl, self.prefer_btn),
                       (self._exclude_lbl, self.exclude_btn)):
            lbl.grid(row=r, column=0, sticky="w", pady=2)
            w.grid(row=r, column=1, sticky="ew", pady=2)
            r += 1
        self._other_lbl.grid(row=r, column=0, sticky="w", pady=2)
        self._other_entry.grid(row=r, column=1, sticky="ew", pady=2)
        r += 1
        self._other_help.grid(row=r, column=1, sticky="w")
        r += 1
        # Guarded: a category trace can fire _relayout during __init__, before
        # the filter panel exists.
        if getattr(self, "_spec_filter_toggle", None) is not None:
            self._spec_filter_toggle.grid(row=r, column=0, columnspan=2,
                                          sticky="w", pady=(6, 0))
            r += 1
            self._spec_filter_row = r
            if self._spec_filters_open.get():
                self._spec_filter_box.grid(row=r, column=0, columnspan=2,
                                           sticky="ew", pady=(2, 4))
            else:
                self._spec_filter_box.grid_remove()
            r += 1
        self._top_lbl.grid(row=r, column=0, sticky="w", pady=2)
        self._top_entry.grid(row=r, column=1, sticky="w", pady=2)
        r += 1
        self._btns.grid(row=r, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        r += 1
        self._btns2.grid(row=r, column=0, columnspan=2, sticky="ew", pady=(4, 0))

    # ---- results table --------------------------------------------------
    def _build_results(self):
        wrap = ttk.Frame(self)
        wrap.grid(row=0, column=1, rowspan=2, sticky="nsew")
        wrap.columnconfigure(0, weight=1)
        wrap.rowconfigure(0, weight=1)

        # legend
        legend = ttk.Frame(wrap)
        legend.grid(row=0, column=0, sticky="w", pady=(0, 4))
        ttk.Label(legend, text="Cell colour:").pack(side="left")
        for txt, col in (("meets", CELL_MET), ("fails", CELL_MISS),
                         ("unknown", CELL_UNKNOWN), ("n/a", CELL_NA)):
            sw = tk.Label(legend, text=f" {txt} ", bg=col, relief="groove", bd=1)
            sw.pack(side="left", padx=3)

        self.table = DatasheetTable(wrap, on_sort=self._on_sort_col,
                                    on_select=self._on_row_select,
                                    on_open=self.open_url)
        self.table.grid(row=1, column=0, sticky="nsew")
        wrap.rowconfigure(1, weight=1)

        ttk.Label(wrap, text="Selected part:").grid(row=2, column=0, sticky="w",
                                                    pady=(6, 2))
        self.notes_text = tk.Text(wrap, height=5, wrap="word", padx=6, pady=5)
        self.notes_text.grid(row=3, column=0, sticky="ew")
        self.notes_text.insert("1.0", "Select a result to see its qualification and specs.")
        self.notes_text.config(state="disabled")

        bar = ttk.Frame(self)
        bar.grid(row=2, column=1, sticky="ew", pady=(6, 0))
        ttk.Button(bar, text="Open part page", command=self.open_url).pack(side="left")
        ttk.Button(bar, text="Copy URL", command=self.copy_url).pack(side="left", padx=4)
        ttk.Button(bar, text="Draft RFQ…", command=self.draft_rfq).pack(side="left")
        ttk.Button(bar, text="Save report…", command=self.save_report).pack(side="left", padx=4)
        ttk.Button(bar, text="Recommended suppliers…",
                   command=self.open_suppliers).pack(side="left", padx=4)
        ttk.Button(bar, text="Show family…", command=self.show_family).pack(side="left")
        ttk.Button(bar, text="Debug selected…", command=self.debug_selected).pack(side="right")
        # Post-rebuild verification: for parts with missing specs, go back to the
        # file each was parsed from and check whether the value was actually there.
        ttk.Button(bar, text="Audit missing specs…",
                   command=self.audit_missing_specs).pack(side="right", padx=4)

    def _build_statusbar(self):
        self.status = tk.StringVar(value="Enter a category and press Search.")
        sb = ttk.Frame(self)
        sb.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        sb.columnconfigure(0, weight=1)
        ttk.Label(sb, textvariable=self.status, anchor="w").grid(row=0, column=0, sticky="ew")
        self.progress = ttk.Progressbar(sb, mode="indeterminate", length=160)
        self.progress.grid(row=0, column=1, sticky="e")

    # ---- spec assembly --------------------------------------------------
    def _num(self, key):
        raw = self.vars[key].get().strip()
        if not raw:
            return None
        try:
            return float(raw)
        except ValueError:
            raise ValueError(f"{key.replace('_', ' ')}: '{raw}' is not a number")

    def _list(self, key):
        raw = self.vars[key].get().strip()
        if not raw:
            return None
        return [p.strip() for p in raw.replace(";", ",").split(",") if p.strip()]

    def _on_package_change(self, *_):
        connectorized = self.vars["package"].get().strip() == "connectorized"
        self.impedance_combo.config(state="readonly" if connectorized else "disabled")
        if not connectorized:
            self.vars["impedance"].set("")

    def build_query(self):
        """Assemble a spec dict from the form, reusing spec.build()."""
        pkg = self.vars["package"].get().strip() or None
        imp = self.vars["impedance"].get().strip() if pkg == "connectorized" else ""
        cat_key = self._current_category_key()
        sub_key, sub_terms = None, None
        chosen = self._chosen_subcategory()
        if chosen:
            sub_key, sub_terms = chosen
        ns = SimpleNamespace(
            category=cat_key,
            subcategory=sub_key,
            subcategory_terms=sub_terms,
            freq=self.vars["freq"].get().strip() or None,
            temp_k=self._num("temp_k"),
            gain_db_min=self._num("gain_db_min"),
            noise_k_max=self._num("noise_k_max"),
            attenuation_db=self._num("attenuation_db"),
            ports=self._num("ports"),
            max_lead_weeks=self._num("max_lead_weeks"),
            impedance=imp or None,
            connector=self.vars["connector"].get().strip() or None,
            mount=self.vars["mount"].get().strip() or None,
            package=pkg,
            space=bool(self.vars["space"].get()),
            local_only=bool(self.vars["local_only"].get()),
            prefer=self._prefer_selected() or None,
            exclude=self._exclude_selected() or None,
            other=self._list("other"),
        )
        q = spec.build(ns)
        for pkey, meta in registry.PARAM_SPECS.items():
            var = self.vars.get(pkey)
            raw = var.get().strip() if var is not None else ""
            if not raw or raw == "(any)":
                continue
            if meta.get("kind") == "text":
                picks = selected_values(raw)
                if picks:
                    q[meta["spec_key"]] = picks if len(picks) > 1 else picks[0]
                    continue
                # Enumerated specs go through as strings so rank scores them as a
                # criterion. Passing them to float() silently dropped them, which
                # is why choosing SPDT changed nothing.
                q[meta["spec_key"]] = raw
                continue
            try:
                q[meta["spec_key"]] = float(raw)
            except ValueError:
                pass

        # Filter frequency is response-specific. LPF/HPF carry a single cutoff
        # (Fc) from the cutoff field; BPF/BSF use the passband/stopband range
        # that spec.build already parsed. Everything downstream still consumes
        # freq_ghz = [lo, hi]; the extra keys are recorded for display/future use.
        if cat_key == "filter":
            resp = self._filter_response()
            if resp in ("lowpass", "highpass"):
                fc = self._num("flt_cutoff")
                # Cutoff is a TARGET: results are ranked by closeness to it rather
                # than hard-filtered by band coverage, so carry cutoff_ghz and drop
                # any freq_ghz band.
                q.pop("freq_ghz", None)
                if fc is not None:
                    q["cutoff_ghz"] = fc
            if resp:
                q["filter_response"] = resp
                if resp == "bandstop" and q.get("freq_ghz"):
                    q["stopband_ghz"] = q["freq_ghz"]
        return q

    # ---- search (threaded) ----------------------------------------------
    def _prefer_selected(self):
        return [name for name, var in self.prefer_vars.items() if var.get()]

    def _prefer_label(self):
        n = sum(1 for var in self.prefer_vars.values() if var.get())
        return f"{n} selected…" if n else "Choose…"

    def _exclude_selected(self):
        return [name for name, var in self.exclude_vars.items() if var.get()]

    def _exclude_label(self):
        n = sum(1 for var in self.exclude_vars.values() if var.get())
        return f"{n} selected…" if n else "Choose…"

    def _all_vendor_names(self):
        """Registry vendors plus any vendor present in the local database, so
        the picker reflects the current dataset (e.g. manufacturers pulled in by
        an everythingRF/SATNow ingest that aren't in vendors.yaml). Aliases like
        'Mini Circuits' are folded into the registry's canonical spelling."""
        return cli.vendor_choices()

    def _sync_vendor_vars(self):
        """Add BooleanVars for vendors newly present in the DB since startup,
        preserving existing selections. Keeps the picker in step with the data."""
        for name in self._all_vendor_names():
            self.prefer_vars.setdefault(name, tk.BooleanVar())
            self.exclude_vars.setdefault(name, tk.BooleanVar())

    def open_prefer_dialog(self):
        self._sync_vendor_vars()
        VendorSelectDialog(self.winfo_toplevel(), self, mode="prefer")

    def open_exclude_dialog(self):
        self._sync_vendor_vars()
        VendorSelectDialog(self.winfo_toplevel(), self, mode="exclude")

    def _vendor_selection_changed(self):
        self.prefer_btn.config(text=self._prefer_label())
        self.exclude_btn.config(text=self._exclude_label())
        _save_prefs({"prefer": self._prefer_selected(),
                     "exclude": self._exclude_selected()})

    # ---- search (local, instant) ---------------------------------------
    # ---- search (local, instant) ---------------------------------------
    def on_search(self):
        try:
            query = self.build_query()
        except ValueError as e:
            messagebox.showerror("Invalid input", str(e))
            return
        if not query.get("category"):
            messagebox.showerror("Missing category", "Category is required.")
            return
        query["local_only"] = True
        self.last_query = query
        _save_prefs({"prefer": self._prefer_selected(), "exclude": self._exclude_selected()})
        self.table.clear()
        self.selected_candidate = None
        self._set_notes("Searching…")
        self.cancel_event.clear()
        self.search_btn.config(state="disabled")
        self.progress.config(mode="indeterminate", value=0)
        self.progress.start(12)
        self.status.set("Searching local dataset…")

        def worker():
            try:
                ranked, errors = cli.run_search(
                    query, progress=lambda m: self.q.put(("status", m)),
                    cancel=self.cancel_event)
                self.q.put(("done", (ranked, errors)))
            except Exception as e:  # noqa: BLE001
                self.q.put(("error", str(e)))

        threading.Thread(target=worker, daemon=True).start()
        self.after(80, self._poll)

    def on_cancel(self):
        self.cancel_event.set()
        self.status.set("Cancelling…")

    def _poll(self):
        try:
            # Never drain an unbounded stream in one Tk callback. A fast
            # producer can otherwise starve Tkinter and make every window look
            # frozen even though the rebuild is running on a worker thread.
            for _ in range(300):
                kind, payload = self.q.get_nowait()
                if kind == "status":
                    self.status.set(payload)
                    if getattr(self, "build_window", None):
                        self.build_window.add_message(payload)
                elif kind == "part":
                    if getattr(self, "build_window", None):
                        self.build_window.add_part(payload)
                elif kind == "build_event":
                    if getattr(self, "build_window", None):
                        self.build_window.handle_event(payload)
                elif kind == "error":
                    self._finish()
                    messagebox.showerror("Search failed", payload)
                    self.status.set("Search failed.")
                elif kind == "done":
                    ranked, errors = payload
                    self._show_results(ranked, errors)
                    self._finish()
        except queue.Empty:
            pass
        if str(self.search_btn["state"]) == "disabled":
            self.after(80, self._poll)

    def _finish(self):
        self.progress.stop()
        self.progress.config(mode="determinate", value=0)
        self.search_btn.config(state="normal")

    # ---- results (datasheet table) -------------------------------------
    @staticmethod
    def _cell_color(state, has_value):
        if state == "met":
            return CELL_MET
        if state == "miss":
            return CELL_MISS
        if state == "unknown":
            return CELL_UNKNOWN
        return CELL_NEUTRAL if has_value else CELL_NA

    @staticmethod
    def _interface_str(c):
        specs = c.get("specs", {})
        return (specs.get("mount_type") or specs.get("package") or "—")[:22]

    def _visible_spec_cols(self, ranked):
        """Spec columns to show: any column some result has a value for, PLUS the
        specs the searched category is defined by.

        The category's own specs are shown even when every value is blank. A
        switch search that hid the Switching column entirely looked like the
        feature was missing, when the honest message is 'this spec exists for
        switches and the dataset has no values for it yet' -- which is what an
        empty column says."""
        present = set()
        for c in ranked:
            s = c.get("specs", {})
            for cid, _h, key, _crit, _fmt in SPEC_COLS:
                if s.get(key) not in (None, "", []):
                    present.add(cid)
        cat = self._current_category_key()
        if cat:
            wanted = set(registry.category_params(cat))
            wanted |= set(registry.category_key_params(cat) or [])
            for cid, _h, key, _crit, _fmt in SPEC_COLS:
                if key in wanted:
                    present.add(cid)
        return [col for col in SPEC_COLS if col[0] in present]

    # ---- sorting --------------------------------------------------------
    def _sort_keys_for(self, spec_cols):
        """One key function per displayed column, matching the cell order built
        in _render_rows.

        Sorting on the *displayed string* would be wrong for every numeric
        column ("9" > "10") and meaningless for the frequency column, so each
        key pulls the underlying value instead."""
        keys = [lambda c: (c.get("tier", "?"), -(c.get("fit_score") or 0)),
                lambda c: (c.get("vendor") or "").lower(),
                lambda c: (c.get("model") or c.get("title") or "").lower(),
                lambda c: _subtype_str(c).lower(),
                lambda c: self._interface_str(c).lower(),
                lambda c: rank._space_str(c),
                lambda c: (c.get("specs") or {}).get("space_score_pct")]
        for _cid, _h, key, _cr, _fmt in spec_cols:
            def spec_key(c, k=key):
                v = (c.get("specs") or {}).get(k)
                if isinstance(v, (list, tuple)):
                    # Prefer the lower band edge, but a partial upper-only band
                    # is still a real sortable value rather than an exception or
                    # an automatic blank.
                    vals = [x for x in v if isinstance(x, (int, float))
                            and not isinstance(x, bool)]
                    v = vals[0] if vals else None
                return (v if isinstance(v, (int, float))
                        and not isinstance(v, bool) else None)
            keys.append(spec_key)
        keys.append(lambda c: self._note_summary(c).lower())
        return keys

    def _on_sort_col(self, col):
        if not self.last_ranked:
            return
        cur, desc = self.table.sort_state()
        # Same column again flips direction; a new column starts ascending,
        # except score/space where "best first" is the useful default.
        desc = (not desc) if cur == col else (col in (0, 4))
        self._sort_col, self._sort_desc = col, desc
        self.table.note_sort(col, desc)
        self._show_results(self.last_ranked, self.last_errors, keep_sort=True)

    @staticmethod
    def _sorted_by(rows, keyfn, desc):
        """Sort, always keeping rows with no value for that column at the bottom.

        A part with a blank cell is not 'the smallest' -- it is unknown, and
        letting it sort to the top of an ascending Psat column would bury the
        real answers under parts that never stated a value."""
        with_val, without = [], []
        for c in rows:
            v = keyfn(c)
            (without if v is None else with_val).append((v, c))
        try:
            with_val.sort(key=lambda pair: pair[0], reverse=desc)
        except TypeError:                     # mixed types: fall back to string
            with_val.sort(key=lambda pair: str(pair[0]), reverse=desc)
        return [c for _v, c in with_val] + [c for _v, c in without]

    def _show_results(self, ranked, errors, keep_sort=False):
        self.last_ranked = ranked
        self.last_errors = errors or []
        if not keep_sort:
            self._sort_col, self._sort_desc = None, False
            self.table.note_sort(None, False)
            # New result set: re-derive which specs are filterable from it.
            self._refresh_spec_filters(ranked)
        try:
            top_n = int(self.vars["top"].get())
        except (ValueError, tk.TclError):
            top_n = 250
        ranked = self._apply_spec_filters(ranked)
        shown = ranked[:top_n] if top_n else ranked
        spec_cols = self._visible_spec_cols(shown)

        headings = ([h for _c, h, _w in LEAD_COLS]
                    + [h for _cid, h, _k, _cr, _f in spec_cols]
                    + [h for _c, h, _w in TAIL_COLS])
        widths = ([w for _c, _h, w in LEAD_COLS]
                  + [92 for _ in spec_cols]
                  + [w for _c, _h, w in TAIL_COLS])
        self.table.set_columns(headings, widths)

        col = getattr(self, "_sort_col", None)
        if col is not None:
            keys = self._sort_keys_for(spec_cols)
            if col < len(keys):
                shown = self._sorted_by(shown, keys[col],
                                        getattr(self, "_sort_desc", False))

        for c in shown:
            crit = c.get("criteria", {})
            specs = c.get("specs", {})
            tier = c.get("tier", "?")
            cells = []
            # lead columns
            cells.append((f"{tier} {c.get('fit_score', 0)}", TIER_BG.get(tier, CELL_NEUTRAL)))
            cells.append((c.get("vendor", "?"), CELL_NEUTRAL))
            cells.append((c.get("model") or c.get("title", "?"), CELL_NEUTRAL))
            cells.append((_subtype_str(c), CELL_NEUTRAL))
            cells.append((self._interface_str(c),
                          self._cell_color(crit.get("pkg"),
                                           bool(specs.get("mount_type") or specs.get("package")))))
            cells.append((rank._space_str(c),
                          self._cell_color(crit.get("space"), True)))
            cells.append(_space_pct_cell(c))
            # spec columns
            for _cid, _h, key, cr, fmt in spec_cols:
                v = specs.get(key)
                txt = fmt(v)
                cells.append((txt, self._cell_color(crit.get(cr), bool(txt))))
            # notes
            cells.append((self._note_summary(c), CELL_NEUTRAL))
            self.table.add_row(cells, c)

        n = len(ranked)
        total = len(self.last_ranked)
        extra = f" (showing {len(shown)})" if len(shown) < n else ""
        filt = f" filtered from {total}" if n < total else ""
        self.status.set(f"{n} space part(s){filt}{extra}."
                        + ("  " + "; ".join(errors) if errors else ""))

    # ---- notes / selection ---------------------------------------------
    @staticmethod
    def _note_summary(c):
        crit = c.get("criteria", {})
        missing = [k for k, v in crit.items() if v == "unknown"]
        failed = [k for k, v in crit.items() if v == "miss"]
        if failed:
            return "✗ fails: " + ", ".join(failed[:4])
        if missing:
            return "⚠ unverified: " + ", ".join(missing[:4])
        return "✓ meets entered criteria"

    def _part_notes(self, c):
        specs = c.get("specs", {})
        lines = [f"{c.get('vendor', '?')}  —  {c.get('model') or c.get('title', '?')}"]
        if c.get("description"):
            lines.append(c["description"][:200])
        qual = specs.get("qual_level")
        variant = specs.get("space_variant")
        space_bits = []
        if variant:
            space_bits.append(variant.replace("_", " "))
        if qual:
            space_bits.append(qual)
        if specs.get("ti_suffix"):
            space_bits.append("TI -" + specs["ti_suffix"])
        if space_bits:
            lines.append("Space: " + " | ".join(space_bits))
        rad = []
        if isinstance(specs.get("tid_krad"), (int, float)):
            rad.append(f"TID {specs['tid_krad']:g} kRad")
        if isinstance(specs.get("sel_mev"), (int, float)):
            rad.append(f"SEL {specs['sel_mev']:g} MeV·cm²/mg")
        if rad:
            lines.append("Radiation: " + ", ".join(rad))
        if specs.get("orderable"):
            lines.append("Orderable: " + str(specs["orderable"])[:160])
        if c.get("qual_summary"):
            lines.append("Evidence: " + c["qual_summary"])
        crit = c.get("criteria", {})
        if crit:
            marks = {"met": "✓", "miss": "✗", "unknown": "?"}
            lines.append("Criteria: " + " ".join(
                f"{k}{marks.get(v, '')}" for k, v in crit.items()))
        return "\n".join(lines)

    def _on_row_select(self, candidate):
        self.selected_candidate = candidate
        self._set_notes(self._part_notes(candidate) if candidate else "")

    def _set_notes(self, text):
        self.notes_text.config(state="normal")
        self.notes_text.delete("1.0", "end")
        self.notes_text.insert("1.0", text)
        self.notes_text.config(state="disabled")

    def _selected_url(self):
        c = self.selected_candidate
        return c.get("url") if c else None

    def open_url(self):
        url = self._selected_url()
        if url:
            webbrowser.open(url)
        else:
            messagebox.showinfo("No URL", "Select a part with a product URL first.")

    def copy_url(self):
        url = self._selected_url()
        if not url:
            messagebox.showinfo("No URL", "Select a part first.")
            return
        self.clipboard_clear()
        self.clipboard_append(url)
        self.status.set("Product URL copied.")

    def audit_missing_specs(self):
        """Explain the gaps in the dataset rather than just counting them.

        For a sample of parts that lack expected specs, re-parse the source each
        was read from and decide, per spec: the parser gets it now (stale row),
        the source states it and we miss it (parser gap), or the source never
        stated it (nothing to fix). Writes a zip; touches nothing."""
        try:
            from . import nospec_audit
        except Exception as exc:
            messagebox.showerror("Unavailable", f"nospec_audit: {exc}",
                                 parent=self)
            return
        if not messagebox.askyesno(
                "Audit missing specs",
                "Check a few parts PER SOURCE that are missing expected specs "
                "against the file they were parsed from?\n\n"
                "Sources are the actual inputs — ADI Parametrics, ADI space "
                "qualified product list, MACOM, Marki, EverythingRF, Qorvo — "
                "not vendors, since one vendor can have several inputs.\n\n"
                "This re-reads local sources and cached pages. It can take a "
                "minute on a large dataset and writes nothing to the database.",
                parent=self):
            return
        log = []
        self.status.set("Auditing missing specs…")
        self.update_idletasks()
        try:
            out = nospec_audit.build_audit(per_source=3, progress=log.append)
        except Exception as exc:
            self.status.set("Audit failed.")
            messagebox.showerror("Audit failed",
                                 f"{type(exc).__name__}: {exc}\n\n"
                                 + "\n".join(log[-10:]), parent=self)
            return
        self.status.set("Audit complete.")
        if out is None:
            messagebox.showinfo(
                "Nothing to audit",
                "No parts are missing an expected spec for their category.",
                parent=self)
            return
        SampleBundleWindow(self, out, log)

    def debug_selected(self):
        c = self.selected_candidate
        if not c:
            messagebox.showinfo("No selection", "Select a result first.")
            return
        payload = {
            "vendor": c.get("vendor"), "model": c.get("model"),
            "title": c.get("title"), "product_url": c.get("url"),
            "category": c.get("category"), "subcategory": c.get("subcategory"),
            "description": c.get("description"),
            "specs": c.get("specs"), "criteria": c.get("criteria"),
            "qual_evidence": c.get("qual_evidence"),
            "rank": {"tier": c.get("tier"), "score": c.get("fit_score"),
                     "met": c.get("met"), "miss": c.get("miss"),
                     "unknown": c.get("unknown"), "pedigree": c.get("pedigree")},
        }
        win = tk.Toplevel(self.winfo_toplevel())
        win.title(f"Debug: {c.get('model') or c.get('title', 'part')}")
        win.geometry("860x640")
        win.rowconfigure(0, weight=1)
        win.columnconfigure(0, weight=1)
        text = tk.Text(win, wrap="none", padx=8, pady=8)
        text.grid(row=0, column=0, sticky="nsew")
        yb = ttk.Scrollbar(win, orient="vertical", command=text.yview)
        yb.grid(row=0, column=1, sticky="ns")
        text.configure(yscrollcommand=yb.set)
        text.insert("1.0", json.dumps(payload, indent=2, ensure_ascii=False, default=str))
        text.config(state="disabled")
        buttons = ttk.Frame(win, padding=8)
        buttons.grid(row=2, column=0, columnspan=2, sticky="ew")

        def copy_debug():
            win.clipboard_clear()
            win.clipboard_append(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
            self.status.set("Debug data copied.")

        ttk.Button(buttons, text="Copy debug data", command=copy_debug).pack(side="left")
        if c.get("url"):
            ttk.Button(buttons, text="Open source page",
                       command=lambda: webbrowser.open(c["url"])).pack(side="left", padx=4)
        ttk.Button(buttons, text="Close", command=win.destroy).pack(side="right")

    def save_report(self):
        if not self.last_ranked:
            messagebox.showinfo("Nothing to save", "Run a search first.")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".md", filetypes=[("Markdown", "*.md"), ("All files", "*.*")],
            initialfile="space_parts_report.md")
        if not path:
            return
        md = rank.markdown(self.last_ranked, self.last_query, self.last_errors)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(md)
        self.status.set(f"Saved report to {path}")

    def draft_rfq(self):
        if not self.last_ranked:
            messagebox.showinfo("No results", "Run a search first.")
            return
        vendors_in_results = sorted({c.get("vendor") for c in self.last_ranked if c.get("vendor")})
        if not vendors_in_results:
            return
        RfqDialog(self.winfo_toplevel(), self, vendors_in_results)

    # ---- dataset rebuild + suppliers -----------------------------------
    def on_rebuild(self):
        RebuildDialog(self.winfo_toplevel(), self)

    # --- worker-side queue helpers -------------------------------------
    # Text may be dropped under pressure; parts and events may not.
    def _put_status(self, msg):
        try:
            self.q.put_nowait(("status", msg))
        except queue.Full:
            pass

    def _put_part(self, row):
        self.q.put(("part", row))

    def _put_event(self, ev):
        self.q.put(("build_event", ev))

    def run_rebuild(self, erf_parent, source_dir, source_files, dedupe,
                    vendors=None, vendor_rate=1.0, adi_dir=None,
                    download_datasheets=True, use_cache=True, categories=None,
                    reset=False, resume=True, reset_vendors_only=False,
                    mine_datasheets=True, sources=None):
        """Called by RebuildDialog; ingests on a worker thread."""
        self.build_cancel = threading.Event()
        self.rebuild_btn.config(state="disabled")
        self.progress.config(mode="indeterminate", value=0)
        self.progress.start(12)
        self.status.set("Rebuilding dataset…")
        self.build_window = BuildProgressWindow(self.winfo_toplevel(),
                                                vendors or [], app=self)

        def worker():
            try:
                summary = space_dataset.rebuild(
                    erf_parent=erf_parent or None, source_dir=source_dir or None,
                    source_files=source_files or (), dedupe=dedupe,
                    progress=self._put_status,
                    vendors=vendors or None, vendor_rate=vendor_rate,
                    adi_dir=adi_dir,
                    download_datasheets=download_datasheets,
                    use_cache=use_cache, categories=categories, reset=reset,
                    resume=resume, reset_vendors_only=reset_vendors_only,
                    part=self._put_part, event=self._put_event,
                    cancel=self.build_cancel,
                    mine_datasheets=mine_datasheets, sources=sources)
                self.q.put(("rebuilt", summary))
            except Exception as e:  # noqa: BLE001
                self.q.put(("rebuild_error", str(e)))

        threading.Thread(target=worker, daemon=True).start()
        self.after(80, self._poll_rebuild)

    def _poll_rebuild(self):
        busy = str(self.rebuild_btn["state"]) == "disabled"
        try:
            # Keep Tk responsive even when scrapers emit hundreds of activity
            # messages in a burst. Remaining messages are handled next tick.
            for _ in range(300):
                kind, payload = self.q.get_nowait()
                if kind == "status":
                    self.status.set(payload)
                    if getattr(self, "build_window", None):
                        self.build_window.add_message(payload)
                elif kind == "part":
                    if getattr(self, "build_window", None):
                        self.build_window.add_part(payload)
                elif kind == "build_event":
                    if getattr(self, "build_window", None):
                        self.build_window.handle_event(payload)
                elif kind == "rebuild_error":
                    self.progress.stop()
                    self.rebuild_btn.config(state="normal")
                    if getattr(self, "build_window", None):
                        self.build_window.finish(False, payload)
                    messagebox.showerror("Rebuild failed", payload)
                    busy = False
                elif kind == "rebuilt":
                    self.progress.stop()
                    self.progress.config(mode="determinate", value=0)
                    self.rebuild_btn.config(state="normal")
                    s = payload["stats"]
                    self._sync_vendor_vars()
                    msg = (f"Dataset rebuilt: {s['parts']} parts, "
                           f"{s['qualified']} qualified, {s['grade']} grade, "
                           f"{s['vendors']} vendors.")
                    if payload.get("errors"):
                        msg += f"  ({len(payload['errors'])} source warning(s))"
                    self.status.set(msg)
                    if getattr(self, "build_window", None):
                        self.build_window.finish(True, msg)
                    messagebox.showinfo(
                        "Rebuild complete",
                        msg + ("\n\nWarnings:\n" + "\n".join(payload["errors"])
                               if payload.get("errors") else ""))
                    busy = False
        except queue.Empty:
            pass
        if busy:
            self.after(80, self._poll_rebuild)

    def open_suppliers(self):
        cat = self._current_category_key()
        SuppliersWindow(self.winfo_toplevel(), self, cat,
                        bool(self.vars["space"].get()))

    def show_family(self):
        c = self.selected_candidate
        if not c:
            messagebox.showinfo("No selection", "Select a result first.")
            return
        mpn = c.get("model") or c.get("mpn") or c.get("title")
        try:
            members = partdb.family_by_mpn(mpn, include_self=True)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Family lookup failed", str(e))
            return
        if len(members) <= 1:
            messagebox.showinfo(
                "No pedigree variants",
                f"{mpn} has no other pedigree variants in the dataset.\n\n"
                f"(Base part number: {partdb.family_key(mpn or '')})")
            return
        FamilyWindow(self.winfo_toplevel(), mpn, partdb.family_key(mpn), members)

    def open_browse(self):
        """Open the catalogue browser, reusing the existing window if it is up."""
        win = getattr(self, "_browse_win", None)
        try:
            if win is not None and win.winfo_exists():
                win.lift()
                win.refresh()
                return
        except Exception:
            pass
        try:
            self._browse_win = BrowseWindow(self.winfo_toplevel(), self)
        except Exception as e:
            messagebox.showerror("Browse catalogue",
                                 f"Could not open the browser:\n{e}")

    def show_health(self):
        try:
            health = partdb.dataset_health()
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Health report failed", str(e))
            return
        HealthWindow(self.winfo_toplevel(), health)

    def on_clear(self):
        for k, var in self.vars.items():
            if isinstance(var, tk.BooleanVar):
                var.set(k in ("local_only", "space"))
            else:
                var.set("")
        self.vars["top"].set("250")
        self.table.clear()
        self.selected_candidate = None
        self._set_notes("Select a result to see its qualification and specs.")
        self.status.set("Cleared.")


class VendorSelectDialog(tk.Toplevel):
    """Scrollable checkbox list used for preferred or excluded vendors."""

    def __init__(self, master, app, mode):
        super().__init__(master)
        self.app = app
        self.mode = mode
        is_exclude = mode == "exclude"
        self.title("Exclude vendors" if is_exclude else "Prefer vendors")
        self.geometry("340x560")
        self.minsize(300, 320)
        self.resizable(True, True)
        self.transient(master)

        prompt = "Vendors to skip:" if is_exclude else "Vendors to try first:"
        ttk.Label(self, text=prompt, padding=8).pack(anchor="w")

        # Reserve the button bar at the bottom FIRST so a long vendor list can't
        # push it off-screen (it previously packed after an expanding frame).
        bar = ttk.Frame(self, padding=8)
        bar.pack(side="bottom", fill="x")
        ttk.Button(bar, text="Clear all", command=self._clear).pack(side="left")
        ttk.Button(bar, text="Done", command=self._done).pack(side="right")

        wrap = ttk.Frame(self)
        wrap.pack(side="top", fill="both", expand=True, padx=8)
        canvas = tk.Canvas(wrap, highlightthickness=0)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        win = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        # Keep the scrollable region current, and stretch the inner frame to the
        # canvas width so rows fill it.
        inner.bind("<Configure>",
                   lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win, width=e.width))

        # Mouse-wheel scrolling (Windows/macOS <MouseWheel>, X11 Button-4/5),
        # active only while the pointer is over the list.
        def _on_wheel(e):
            if getattr(e, "num", 0) == 4:
                canvas.yview_scroll(-1, "units")
            elif getattr(e, "num", 0) == 5:
                canvas.yview_scroll(1, "units")
            elif e.delta:
                canvas.yview_scroll(-1 if e.delta > 0 else 1, "units")

        def _bind_wheel(_):
            for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                canvas.bind_all(seq, _on_wheel)

        def _unbind_wheel(_):
            for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                canvas.unbind_all(seq)

        canvas.bind("<Enter>", _bind_wheel)
        canvas.bind("<Leave>", _unbind_wheel)
        self.bind("<Destroy>", _unbind_wheel)

        variables = app.exclude_vars if is_exclude else app.prefer_vars
        for name in sorted(variables, key=str.lower):
            ttk.Checkbutton(inner, text=name, variable=variables[name]).pack(
                anchor="w", pady=1)

    def _clear(self):
        variables = self.app.exclude_vars if self.mode == "exclude" else self.app.prefer_vars
        for var in variables.values():
            var.set(False)

    def _done(self):
        self.app._vendor_selection_changed()
        self.destroy()


class RfqDialog(tk.Toplevel):
    """Pick a vendor from the results and show a drafted RFQ email."""

    def __init__(self, master, app, vendors_in_results):
        super().__init__(master)
        self.title("Draft RFQ")
        self.app = app
        self.geometry("640x480")
        self.transient(master)

        top = ttk.Frame(self, padding=8)
        top.pack(fill="x")
        ttk.Label(top, text="Vendor:").pack(side="left")
        self.vendor = tk.StringVar(value=vendors_in_results[0])
        ttk.Combobox(top, textvariable=self.vendor, values=vendors_in_results,
                     width=30, state="readonly").pack(side="left", padx=6)
        ttk.Button(top, text="Draft", command=self.render).pack(side="left")
        ttk.Button(top, text="Copy", command=self.copy).pack(side="left", padx=6)

        self.text = tk.Text(self, wrap="word", padx=8, pady=8)
        self.text.pack(fill="both", expand=True)
        self.render()

    def render(self):
        by_name = {v["name"]: v for v in load_vendors()}
        name = self.vendor.get()
        # DB-only vendors (from ingested catalogs) aren't in vendors.yaml — use a
        # synthetic entry so an RFQ can still be drafted.
        vendor = by_name.get(name) or {"name": name, "url": "", "rfq_email": ""}
        parts = [c for c in self.app.last_ranked if c.get("vendor") == vendor["name"]][:5]
        to, subject, body = rfq.draft(vendor, parts, self.app.last_query)
        self.text.delete("1.0", "end")
        self.text.insert("1.0", f"To: {to}\nSubject: {subject}\n\n{body}")

    def copy(self):
        self.clipboard_clear()
        self.clipboard_append(self.text.get("1.0", "end-1c"))


class DatasheetTable(ttk.Frame):
    """Scrollable grid of coloured cells — a datasheet-style results table.

    ttk.Treeview can only colour whole rows, so per-cell colouring (green =
    meets the entered requirement, red = fails, amber = required-but-unknown,
    grey = no value) is done with a grid of tk.Labels inside a scrollable
    Canvas. Row click selects (and calls on_select); double-click calls on_open.
    """

    def __init__(self, master, on_select=None, on_open=None, on_sort=None):
        super().__init__(master)
        self.on_select = on_select
        self.on_open = on_open
        self.on_sort = on_sort          # called with the clicked column index
        self._sort_col = None
        self._sort_desc = False
        self._head_labels = []
        self._warned_width = False
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        # Keep the header in a separate canvas so vertical scrolling moves only
        # the data rows.  The header and body are horizontally synchronized.
        self.header_canvas = tk.Canvas(
            self, highlightthickness=0, background=HEAD_BG, height=32
        )
        self.header_canvas.grid(row=0, column=0, sticky="ew")

        self.canvas = tk.Canvas(self, highlightthickness=0, background="#ffffff")
        self.canvas.grid(row=1, column=0, sticky="nsew")
        vs = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        vs.grid(row=1, column=1, sticky="ns")
        hs = ttk.Scrollbar(self, orient="horizontal", command=self._xview)
        hs.grid(row=2, column=0, sticky="ew")
        self.canvas.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)

        self.header_inner = tk.Frame(self.header_canvas, background=HEAD_BG)
        self._header_win = self.header_canvas.create_window(
            (0, 0), window=self.header_inner, anchor="nw"
        )
        self.header_inner.bind("<Configure>", self._sync_header_region)

        self.inner = tk.Frame(self.canvas, background="#ffffff")
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", self._sync_body_region)
        self.canvas.bind("<Enter>", lambda e: self._bind_wheel())
        self.canvas.bind("<Leave>", lambda e: self._unbind_wheel())

        self._headings = []
        self._widths = []
        self._row_frames = []      # per data row: list of cell labels
        self._candidates = []
        self._selected = None

    def _xview(self, *args):
        """Scroll the body and frozen header horizontally as one table."""
        self.canvas.xview(*args)
        self.header_canvas.xview(*args)

    def _sync_header_region(self, _event=None):
        bbox = self.header_canvas.bbox("all")
        if bbox:
            self.header_canvas.configure(scrollregion=bbox, height=max(1, bbox[3] - bbox[1]))

    def _sync_body_region(self, _event=None):
        bbox = self.canvas.bbox("all")
        if bbox:
            self.canvas.configure(scrollregion=bbox)

    def _bind_wheel(self):
        self.canvas.bind_all("<MouseWheel>",
                             lambda e: self.canvas.yview_scroll(int(-e.delta / 120), "units"))
        self.canvas.bind_all("<Button-4>", lambda e: self.canvas.yview_scroll(-1, "units"))
        self.canvas.bind_all("<Button-5>", lambda e: self.canvas.yview_scroll(1, "units"))

    def _unbind_wheel(self):
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.canvas.unbind_all(seq)

    def clear(self):
        for w in self.header_inner.winfo_children():
            w.destroy()
        for w in self.inner.winfo_children():
            w.destroy()
        self._row_frames = []
        self._candidates = []
        self._selected = None
        self._headings = []
        self._head_labels = []

    def set_columns(self, headings, widths):
        self.clear()
        self._headings = headings
        self._widths = widths
        self._head_labels = []
        for col, (h, w) in enumerate(zip(headings, widths)):
            text = h
            if self._sort_col == col:
                text = f"{h}  {'\u25bc' if self._sort_desc else '\u25b2'}"
            lbl = tk.Label(self.header_inner, text=text, bg=HEAD_BG, fg=HEAD_FG,
                           font=("TkDefaultFont", 9, "bold"), padx=6, pady=4,
                           borderwidth=1, relief="solid", anchor="center",
                           wraplength=max(w, 60),
                           cursor="hand2" if self.on_sort else "")
            lbl.grid(row=0, column=col, sticky="nsew")
            if self.on_sort:
                lbl.bind("<Button-1>", lambda e, c=col: self.on_sort(c))
            self.header_inner.columnconfigure(col, minsize=w)
            self.inner.columnconfigure(col, minsize=w)
            self._head_labels.append(lbl)
        self.update_idletasks()
        self._sync_header_region()

    def sort_state(self):
        return self._sort_col, self._sort_desc

    def note_sort(self, col, descending):
        """Record the sort so the next set_columns() draws the arrow."""
        self._sort_col = col
        self._sort_desc = descending

    def clear_rows(self):
        """Drop the data rows but keep the header, for a re-sort in place."""
        for labels in self._row_frames:
            for lbl in labels:
                lbl.destroy()
        self._row_frames = []
        self._candidates = []
        self._selected = None

    def add_row(self, cells, candidate):
        """Add one data row, tolerating a cell/column count mismatch.

        This used to index self._widths[col] directly, so a single row with one
        cell more than there are columns raised IndexError halfway through
        rendering and left a half-built grid on screen -- the table "breaking"
        while loading. A mismatch is a bug worth knowing about, but it must not
        destroy the view, so rows are padded or trimmed to the header width and
        the discrepancy is reported once.
        """
        ncols = len(self._headings) or len(cells)
        cells = list(cells)
        if len(cells) != ncols:
            if not self._warned_width:
                self._warned_width = True
                print(f"  ! table row had {len(cells)} cell(s) for {ncols} "
                      f"column(s); padding/trimming. Headings and cells are out "
                      f"of step -- check LEAD_COLS/TAIL_COLS against the cell "
                      f"builder.")
            if len(cells) < ncols:
                cells += [("", CELL_NEUTRAL)] * (ncols - len(cells))
            else:
                cells = cells[:ncols]
        r = len(self._row_frames) + 1
        labels = []
        for col, cell in enumerate(cells):
            try:
                text, bg = cell
            except (TypeError, ValueError):
                text, bg = cell, CELL_NEUTRAL
            width = self._widths[col] if col < len(self._widths) else 100
            anchor = "w" if col in (1, 2) or col == ncols - 1 else "center"
            lbl = tk.Label(self.inner, text="" if text is None else str(text),
                           bg=bg or CELL_NEUTRAL, padx=6, pady=3,
                           borderwidth=1, relief="solid", anchor=anchor,
                           wraplength=max(width, 60), justify="left")
            lbl.grid(row=r, column=col, sticky="nsew")
            lbl.bind("<Button-1>", lambda e, idx=r - 1: self._select(idx))
            lbl.bind("<Double-1>", lambda e: self.on_open() if self.on_open else None)
            labels.append(lbl)
        self._row_frames.append(labels)
        self._candidates.append(candidate)

    def _select(self, idx):
        if self._selected is not None and self._selected < len(self._row_frames):
            for lbl in self._row_frames[self._selected]:
                lbl.configure(highlightthickness=0, relief="solid", borderwidth=1)
        self._selected = idx
        for lbl in self._row_frames[idx]:
            lbl.configure(highlightbackground=SEL_BORDER, highlightthickness=2,
                          relief="solid", borderwidth=1)
        if self.on_select:
            self.on_select(self._candidates[idx])


class SampleBundleWindow(tk.Toplevel):
    """Shows where the bundle went and what went into it."""

    def __init__(self, master, path, log):
        super().__init__(master)
        self.title("Parse sample bundle")
        self.geometry("860x560")
        self.transient(master)
        self.path = Path(path)
        top = ttk.Frame(self, padding=10)
        top.pack(fill="x")
        ttk.Label(top, text="Bundle written:", font=("", 10, "bold")).pack(
            anchor="w")
        ttk.Label(top, text=str(self.path), foreground="#0645ad",
                  wraplength=800, justify="left").pack(anchor="w")
        size = self.path.stat().st_size / 1024 if self.path.exists() else 0
        ttk.Label(top, text=f"{size:.0f} kB — send this file as-is.",
                  foreground="#555").pack(anchor="w", pady=(2, 0))
        body = ttk.Frame(self, padding=(10, 0))
        body.pack(fill="both", expand=True)
        txt = tk.Text(body, wrap="none", padx=6, pady=4)
        txt.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(body, orient="vertical", command=txt.yview)
        sb.pack(side="right", fill="y")
        txt.configure(yscrollcommand=sb.set)
        txt.insert("1.0", "\n".join(log))
        try:
            import zipfile
            with zipfile.ZipFile(self.path) as z:
                names = z.namelist()
            txt.insert("end", "\n\ncontents\n" + "-" * 40 + "\n")
            for n in sorted(names):
                txt.insert("end", f"  {n}\n")
        except Exception:
            pass
        txt.config(state="disabled")
        bar = ttk.Frame(self, padding=10)
        bar.pack(fill="x")
        ttk.Button(bar, text="Open containing folder",
                   command=self._reveal).pack(side="left")
        ttk.Button(bar, text="Copy path", command=self._copy).pack(side="left",
                                                                  padx=6)
        ttk.Button(bar, text="Close", command=self.destroy).pack(side="right")

    def _copy(self):
        self.clipboard_clear()
        self.clipboard_append(str(self.path))

    def _reveal(self):
        import subprocess
        folder = str(self.path.parent)
        try:
            if sys.platform.startswith("win"):
                subprocess.Popen(["explorer", folder])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", folder])
            else:
                subprocess.Popen(["xdg-open", folder])
        except Exception as exc:
            messagebox.showinfo("Path", f"{folder}\n\n({exc})", parent=self)


class ParseInspectorWindow(tk.Toplevel):
    """See what the parsers actually extracted from a page.

    The build log says a page yielded 37 parts; it cannot tell you which columns
    were recognised, what a spec cell held before it became a number, or why the
    other rows were dropped. This shows all three, against a cached vendor page or
    any HTML file on disk, without touching the database."""

    def __init__(self, master):
        super().__init__(master)
        self.title("Parse inspector")
        self.geometry("1080x740")
        self.transient(master)

        top = ttk.Frame(self, padding=8)
        top.pack(fill="x")
        ttk.Label(top, text="Vendor:").pack(side="left")
        self.vendor_pick = tk.StringVar(value="(all)")
        self.vendor_combo = ttk.Combobox(top, textvariable=self.vendor_pick,
                                         width=16, state="readonly")
        self.vendor_combo.pack(side="left", padx=(4, 12))
        self.vendor_combo.bind("<<ComboboxSelected>>",
                               lambda e: self._load_list())
        ttk.Label(top, text="Source:").pack(side="left")
        self.choice = tk.StringVar()
        self.combo = ttk.Combobox(top, textvariable=self.choice, width=70,
                                  state="readonly")
        self.combo.pack(side="left", padx=6)
        ttk.Button(top, text="Browse…", command=self._browse).pack(side="left")
        ttk.Button(top, text="Inspect", command=self._run).pack(side="left",
                                                               padx=6)

        opts = ttk.Frame(self, padding=(8, 0))
        opts.pack(fill="x")
        ttk.Label(opts, text="force parser:").pack(side="left")
        self.vendor = tk.StringVar(value="(auto-detect)")
        ttk.Combobox(opts, textvariable=self.vendor, width=16, state="readonly",
                     values=["(auto-detect)", "qorvo", "macom", "skyworks",
                             "marki"]).pack(side="left", padx=6)
        ttk.Button(opts, text="Reload cache list",
                   command=self._load_list).pack(side="left", padx=6)
        self.count = tk.StringVar(value="")
        ttk.Label(opts, textvariable=self.count,
                  foreground="#555").pack(side="right")

        body = ttk.Frame(self, padding=8)
        body.pack(fill="both", expand=True)
        self.text = tk.Text(body, wrap="none", padx=6, pady=4)
        self.text.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(body, orient="vertical", command=self.text.yview)
        sb.pack(side="right", fill="y")
        self.text.configure(yscrollcommand=sb.set)

        bar = ttk.Frame(self, padding=8)
        bar.pack(fill="x")
        ttk.Button(bar, text="Copy", command=self._copy).pack(side="left")
        ttk.Button(bar, text="Save JSON…",
                   command=self._save_json).pack(side="left", padx=6)
        ttk.Button(bar, text="Close", command=self.destroy).pack(side="right")

        self._files = []
        self._report = None
        self._load_list()

    def _load_list(self):
        """Cached vendor pages AND local inputs (everythingRF HTML, ADI
        spreadsheets), grouped by vendor. The inspector previously saw only the
        vendor page cache, so the two sources that produce the most rows could
        not be inspected at all."""
        try:
            from . import parse_debug
            counts = parse_debug.vendors_available()
            choices = ["(all)"] + [f"{k} ({v})" for k, v in
                                   sorted(counts.items())]
            self.vendor_combo["values"] = choices
            picked = self.vendor_pick.get()
            vendor = None
            if picked and not picked.startswith("("):
                vendor = picked.split(" ")[0]
            items = parse_debug.all_sources(vendor)
        except Exception as exc:
            items = []
            self.count.set(f"could not list sources: {exc}")
        self._files = [f for _tag, f in items]
        self._tags = [t for t, _f in items]
        labels = [f"[{t}]  {f.name}" for t, f in items]
        self.combo["values"] = labels
        if labels:
            self.combo.current(0)
        self.count.set(f"{len(self._files)} source(s)"
                       + ("" if not self._files else
                          f"  ({len(set(self._tags))} vendor group(s))"))

    def _browse(self):
        f = filedialog.askopenfilename(
            parent=self, title="Choose an HTML file",
            filetypes=[("HTML", "*.html *.htm *.txt"), ("All files", "*.*")])
        if f:
            self._files.insert(0, Path(f))
            vals = [f"{Path(f).parent.name} / {Path(f).name}"] + \
                list(self.combo["values"])
            self.combo["values"] = vals
            self.combo.current(0)

    def _selected(self):
        idx = self.combo.current()
        if 0 <= idx < len(self._files):
            return self._files[idx]
        return None

    def _run(self):
        path = self._selected()
        if not path:
            messagebox.showinfo("Nothing selected",
                                "Pick a cached page or browse to a file.",
                                parent=self)
            return
        try:
            from . import parse_debug
            vendor = (None if self.vendor.get().startswith("(")
                      else self.vendor.get())
            # inspect_path handles .xlsx (ADI parametric / space portfolio) as
            # well as HTML, so local sources work here too
            self._report = parse_debug.inspect_path(Path(path), vendor=vendor)
            rendered = parse_debug.render(self._report, max_parts=40,
                                         show_rejects=40)
        except Exception as exc:
            rendered = f"Inspection failed: {type(exc).__name__}: {exc}"
            self._report = None
        self.text.delete("1.0", "end")
        self.text.insert("1.0", rendered)

    def _copy(self):
        self.clipboard_clear()
        self.clipboard_append(self.text.get("1.0", "end"))

    def _save_json(self):
        if not self._report:
            messagebox.showinfo("Nothing to save", "Run an inspection first.",
                                parent=self)
            return
        f = filedialog.asksaveasfilename(parent=self, defaultextension=".json",
                                         initialfile="parse_report.json")
        if f:
            Path(f).write_text(json.dumps(self._report, indent=2, default=str),
                               encoding="utf-8")


class BuildProgressWindow(tk.Toplevel):
    """Live rebuild view with newly parsed parts and a compact activity log."""

    _PART_COLUMNS = (
        ("vendor", "Vendor", 130), ("mpn", "Part number", 150),
        ("category", "Category", 110), ("subcategory", "Subcategory", 110),
        ("frequency", "Frequency", 110), ("gain", "Gain", 75),
        ("nf", "NF", 65), ("p1db", "P1dB", 70),
        ("oip3", "OIP3", 70), ("package", "Package", 110),
        # The ADI space portfolio's payload is radiation, temperature and package
        # construction -- it has no RF columns at all. Without somewhere to show
        # them, every portfolio part looked like it had nothing but a frequency.
        ("tid", "TID (kRad)", 85), ("sel", "SEL (MeV)", 85),
        ("temp", "Temp (°C)", 95), ("pkg_mat", "Pkg material", 105),
        ("space", "Space", 105), ("source", "Source", 180),
    )

    def __init__(self, master, vendors, app=None):
        super().__init__(master)
        self.title("Building dataset")
        self.geometry("1320x860")
        self.minsize(900, 560)
        self.minsize(980, 680)
        self.resizable(True, True)
        self.transient(master)
        self.protocol("WM_DELETE_WINDOW", lambda: None)
        self.current = tk.StringVar(value="Starting rebuild…")
        self.url_status = tk.StringVar(value="Waiting for the first source…")
        self._app = app
        self._part_items = {}
        self._message_count = 0
        self._counters = {}
        self._vendor_dirty = True
        self._follow_tail = True
        self._vendor_cache = {}

        ttk.Label(self, textvariable=self.current, padding=8,
                  font=("TkDefaultFont", 10, "bold")).pack(fill="x")

        scrape_box = ttk.LabelFrame(self, text="Currently being read or scraped", padding=8)
        scrape_box.pack(fill="x", padx=8, pady=(0, 6))
        ttk.Label(scrape_box, textvariable=self.url_status, justify="left",
                  wraplength=1250).pack(fill="x", anchor="w")

        # side="bottom" and packed before the expanding widgets, so this row can
        # never be squeezed off the window.
        bar = ttk.Frame(self)
        bar.pack(side="bottom", fill="x", padx=8, pady=(0, 8))
        self.stop_btn = ttk.Button(bar, text="Stop  (finish current request)",
                                   command=self._request_stop)
        self.stop_btn.pack(side="left")
        ttk.Button(bar, text="Dataset health…",
                   command=lambda: self._open_health(None)).pack(side="left",
                                                                padx=(6, 0))
        self.health_menu = ttk.Button(bar, text="Vendor health…",
                                      command=self._pick_vendor_health)
        self.health_menu.pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="Parse inspector…",
                   command=self._open_parse_inspector).pack(side="left",
                                                           padx=(6, 0))
        self.follow = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="follow newest", variable=self.follow,
                        command=lambda: setattr(self, "_follow_tail",
                                                self.follow.get())
                        ).pack(side="left", padx=(12, 0))
        self.done = ttk.Button(bar, text="Close", command=self.destroy,
                               state="disabled")
        self.done.pack(side="right")

        table_box = ttk.LabelFrame(
            self, text="Newly parsed parts (updates every page or 15-part batch)", padding=6)
        table_box.pack(fill="both", expand=True, padx=8, pady=(0, 6))
        table_box.rowconfigure(0, weight=1)
        table_box.columnconfigure(0, weight=1)
        cols = [c[0] for c in self._PART_COLUMNS]
        self.parts_table = ttk.Treeview(table_box, columns=cols, show="headings", height=14)
        for key, heading, width in self._PART_COLUMNS:
            self.parts_table.heading(key, text=heading)
            self.parts_table.column(key, width=width, minwidth=55, stretch=key in ("source", "package"))
        py = ttk.Scrollbar(table_box, orient="vertical", command=self.parts_table.yview)
        px = ttk.Scrollbar(table_box, orient="horizontal", command=self.parts_table.xview)
        self.parts_table.configure(yscrollcommand=py.set, xscrollcommand=px.set)
        self.parts_table.grid(row=0, column=0, sticky="nsew")
        py.grid(row=0, column=1, sticky="ns")
        px.grid(row=1, column=0, sticky="ew")

        log_box = ttk.LabelFrame(self, text="Source, cache, checkpoint, and skip status", padding=5)
        log_box.pack(fill="x", padx=8, pady=(0, 6))
        self.activity = tk.Text(log_box, height=4, wrap="none", state="disabled", padx=5, pady=3)
        log_y = ttk.Scrollbar(log_box, orient="vertical", command=self.activity.yview)
        self.activity.configure(yscrollcommand=log_y.set)
        self.activity.pack(side="left", fill="both", expand=True)
        log_y.pack(side="right", fill="y")

        # Scrollable: with every vendor plus everythingRF and both ADI sources
        # this list outgrew its fixed height and the lower rows were unreachable.
        vendor_box = ttk.LabelFrame(self, text="Current database by vendor",
                                    padding=6)
        vendor_box.pack(fill="x", padx=8, pady=(0, 6))
        self._vendor_canvas = tk.Canvas(vendor_box, height=96,
                                        highlightthickness=0)
        vsb = ttk.Scrollbar(vendor_box, orient="vertical",
                            command=self._vendor_canvas.yview)
        self._vendor_canvas.configure(yscrollcommand=vsb.set)
        self._vendor_canvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.vendor_frame = ttk.Frame(self._vendor_canvas)
        self._vendor_window = self._vendor_canvas.create_window(
            (0, 0), window=self.vendor_frame, anchor="nw")
        self.vendor_frame.bind(
            "<Configure>",
            lambda e: self._vendor_canvas.configure(
                scrollregion=self._vendor_canvas.bbox("all")))
        self._vendor_canvas.bind(
            "<Configure>",
            lambda e: self._vendor_canvas.itemconfigure(self._vendor_window,
                                                        width=e.width))
        # wheel scrolling only while the pointer is over the panel
        def _wheel(event):
            self._vendor_canvas.yview_scroll(
                int(-1 * (event.delta / 120)) or -1 if event.delta else 0,
                "units")
        self._vendor_canvas.bind(
            "<Enter>", lambda e: self._vendor_canvas.bind_all("<MouseWheel>",
                                                             _wheel))
        self._vendor_canvas.bind(
            "<Leave>", lambda e: self._vendor_canvas.unbind_all("<MouseWheel>"))

        self._tick()
        self._refresh_vendor_rows()

    @staticmethod
    def _plain_spec(specs, *keys):
        for key in keys:
            value = specs.get(key)
            if isinstance(value, (list, tuple)) and value:
                value = value[0]
            if value not in (None, "", []):
                return value
        return ""

    @classmethod
    def _row_values(cls, row):
        specs = row.get("specs") or {}
        fmin = cls._plain_spec(specs, "freq_min", "frequency_min_ghz")
        fmax = cls._plain_spec(specs, "freq_max", "frequency_max_ghz")
        freq = f"{fmin}-{fmax} GHz" if fmin != "" and fmax != "" else (f"{fmin or fmax} GHz" if fmin != "" or fmax != "" else "")
        def unit(value, suffix):
            return f"{value} {suffix}" if value not in (None, "") else ""

        # Noise figure first, then insertion loss, then conversion loss.
        # No "(IL)" marker: the column is the part's noise contribution in dB and
        # for a lossy network that IS its insertion loss.
        nf_plain = cls._plain_spec(specs, "nf_db", "noise_nf_db",
                                   "noise_figure_db")
        if nf_plain == "":
            nf_plain = cls._plain_spec(specs, "insertion_loss_db",
                                       "conversion_loss_db")
        return (
            row.get("vendor", ""), row.get("mpn", ""), row.get("category", ""),
            row.get("subcategory", ""), freq,
            unit(cls._plain_spec(specs, "gain_db", "gain"), "dB"),
            unit(nf_plain, "dB"),
            unit(cls._plain_spec(specs, "p1db_dbm", "p1db"), "dBm"),
            unit(cls._plain_spec(specs, "oip3_dbm", "oip3"), "dBm"),
            cls._plain_spec(specs, "package", "mount_type"),
            cls._plain_spec(specs, "tid_krad"),
            cls._plain_spec(specs, "sel_mev"),
            cls._temp_range(specs),
            cls._plain_spec(specs, "package_material", "lead_finish"),
            row.get("space", ""),
            row.get("source", "") or row.get("url", ""),
        )

    @staticmethod
    def _temp_range(specs):
        """-55 to 125 as one cell; the portfolio always gives both ends."""
        lo = specs.get("temp_min_c")
        hi = specs.get("temp_max_c")
        lo = lo[0] if isinstance(lo, (tuple, list)) and lo else lo
        hi = hi[0] if isinstance(hi, (tuple, list)) and hi else hi
        if lo is None and hi is None:
            return ""
        fmt = lambda v: ("" if v is None
                         else (f"{v:g}" if isinstance(v, (int, float)) else str(v)))
        if lo is not None and hi is not None:
            return f"{fmt(lo)} to {fmt(hi)}"
        return fmt(lo if lo is not None else hi)

    def _upsert_part(self, row):
        vendor = str(row.get("vendor", ""))
        mpn = str(row.get("mpn", ""))
        if not vendor and not mpn:
            return
        key = (vendor.lower(), mpn.upper())
        values = self._row_values(row)
        item = self._part_items.get(key)
        if item and self.parts_table.exists(item):
            self.parts_table.item(item, values=values)
            self.parts_table.move(item, "", "end")
        else:
            item = self.parts_table.insert("", "end", values=values)
            self._part_items[key] = item
        # Only auto-scroll when the user is already at the bottom, otherwise the
        # table yanks itself away while they are trying to read it.
        if self._follow_tail:
            self.parts_table.see(item)
        # Vendor counts are refreshed on a timer, not per part. Doing it here ran
        # a GROUP BY over the whole parts table and destroyed/recreated every
        # vendor label for each of ~1000 rows, on the Tk thread.
        self._vendor_dirty = True

    def _log(self, text):
        self.activity.config(state="normal")
        self.activity.insert("end", text + "\n")
        # Bound the small diagnostic box so it remains responsive.
        if int(self.activity.index("end-1c").split(".")[0]) > 500:
            self.activity.delete("1.0", "100.0")
        self.activity.see("end")
        self.activity.config(state="disabled")

    def add_message(self, message):
        """Progress TEXT only.

        Parts and milestones arrive through add_part() and handle_event(); this
        no longer has to recognise a JSON payload disguised as a log line."""
        text = str(message).strip()
        if not text:
            return
        self.current.set(text)
        if (text.startswith(("SCRAPE |", "SOURCE |", "FILE ", "RESUME |"))
                or "page(s)" in text or "walking " in text.lower()):
            self.url_status.set(text)
        log_tokens = ("SKIP", "CHECKPOINT", "CACHE", "DB BATCH", "DB WRITE",
                      "FILE DONE", "FAILED", "ERROR", "robots.txt",
                      "dataset now", "de-duplicating", "normalized JSON",
                      "RESET", "RESUME", "stop requested")
        if any(tok.lower() in text.lower() for tok in log_tokens):
            self._log(text)

    def add_part(self, row):
        """One parsed part, as a dict, straight from the ingest layer."""
        self._upsert_part(row)

    def handle_event(self, ev):
        """Structured milestones: page/product/datasheet/resume/vendor_done."""
        kind = ev.get("type")
        vendor = ev.get("vendor") or ""
        detail = ev.get("detail") or ""
        url = ev.get("url") or ""
        if kind in ("page", "product", "datasheet"):
            label = {"page": "catalogue page", "product": "product page",
                     "datasheet": "datasheet"}[kind]
            self.url_status.set(f"{vendor}  {label}: {detail}")
            if url:
                self.current.set(url)
            self._counters[vendor] = self._counters.get(vendor, 0) + 1
        elif kind == "resume":
            self._log(f"RESUME | {vendor}: skipped {ev.get('skipped', 0)} "
                      f"unit(s) already recorded")
        elif kind == "vendor_done":
            self._log(f"{vendor}: {ev.get('parts', 0)} part(s) in "
                      f"{ev.get('secs', 0)}s "
                      f"({ev.get('with_freq', 0)} with frequency)")
            self._vendor_dirty = True
        elif kind == "db_batch":
            self._vendor_dirty = True

    def _tick(self):
        """Refresh vendor counts at most once a second, and only when something
        actually changed."""
        if self._vendor_dirty:
            self._vendor_dirty = False
            self._refresh_vendor_rows()
        if self.winfo_exists():
            self.after(1000, self._tick)

    def _request_stop(self):
        app = getattr(self, "_app", None)
        ev = getattr(app, "build_cancel", None)
        if ev is not None:
            ev.set()
        self.stop_btn.config(state="disabled")
        self._log("*** stop requested; finishing the current request")
        self.url_status.set("stopping after the current request …")

    def _open_health(self, vendor):
        try:
            health = partdb.dataset_health(vendor)
        except Exception as exc:
            messagebox.showerror("Health failed", str(exc), parent=self)
            return
        HealthWindow(self, health)

    def _pick_vendor_health(self):
        try:
            counts = partdb.vendor_part_counts()
        except Exception as exc:
            messagebox.showerror("Health failed", str(exc), parent=self)
            return
        if not counts:
            messagebox.showinfo("No vendors", "No parts stored yet.",
                                parent=self)
            return
        menu = tk.Menu(self, tearoff=0)
        for vendor, n in counts.items():
            menu.add_command(label=f"{vendor}  ({n:,})",
                             command=lambda v=vendor: self._open_health(v))
        try:
            x = self.health_menu.winfo_rootx()
            y = self.health_menu.winfo_rooty() + self.health_menu.winfo_height()
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    def _open_parse_inspector(self):
        ParseInspectorWindow(self)


    def _refresh_vendor_rows(self):
        try:
            counts = partdb.vendor_part_counts()
        except Exception as exc:
            for w in self.vendor_frame.winfo_children():
                w.destroy()
            ttk.Label(self.vendor_frame,
                      text=f"Could not read database counts: {exc}").grid(
                row=0, column=0, sticky="w")
            return
        # Rebuild only when the set of vendors changes; otherwise just update the
        # existing label text. Recreating every widget each refresh is what made
        # the panel flicker and the window stutter.
        if set(counts) != set(self._vendor_cache):
            for w in self.vendor_frame.winfo_children():
                w.destroy()
            self._vendor_cache = {}
            for i, vendor in enumerate(counts):
                var = tk.StringVar()
                ttk.Label(self.vendor_frame, textvariable=var, width=30).grid(
                    row=i // 3, column=(i % 3) * 2, sticky="w", padx=(0, 4),
                    pady=1)
                ttk.Button(self.vendor_frame, text="health", width=7,
                           command=lambda v=vendor: self._open_health(v)).grid(
                    row=i // 3, column=(i % 3) * 2 + 1, sticky="w", padx=(0, 12))
                self._vendor_cache[vendor] = var
        for vendor, count in counts.items():
            var = self._vendor_cache.get(vendor)
            if var is not None:
                seen = self._counters.get(vendor, 0)
                var.set(f"{vendor}: {count:,}"
                        + (f"  (+{seen} fetches)" if seen else ""))
        return
        if not counts:
            ttk.Label(self.vendor_frame, text=f"No parts stored in {partdb.DB_PATH}").grid(row=0, column=0, sticky="w")
            return
        for i, (vendor, count) in enumerate(counts.items()):
            ttk.Label(self.vendor_frame, text=f"{vendor}: {count:,}", width=30).grid(
                row=i // 4, column=i % 4, sticky="w", padx=(0, 8), pady=1)

    def finish(self, ok, message):
        self.current.set(message)
        self.url_status.set(("COMPLETE: " if ok else "FAILED: ") + message)
        self._refresh_vendor_rows()
        self.done.config(state="normal")
        self.protocol("WM_DELETE_WINDOW", self.destroy)


class VendorHealthWindow(tk.Toplevel):
    def __init__(self, master, h):
        super().__init__(master)
        self.title(f"Dataset health — {h['vendor']}")
        self.geometry("680x640")
        txt = tk.Text(self, wrap="word", padx=10, pady=8)
        txt.pack(fill="both", expand=True)
        lines = [f"{h['vendor']} dataset health", "=" * 52,
                 f"Parts: {h['parts']}    Overall spec coverage: {h['coverage_pct']}%    Assessment: {h['grade']}",
                 "", "Parts by category"]
        for cat, n in h["by_category"]:
            lines.append(f"  {cat:<22} {n:>6}")
        lines.append("\nSpec coverage by category")
        for cc in h["category_coverage"]:
            lines.append(f"\n  {cc['category']} ({cc['count']})")
            for label, n, pct in cc["specs"]:
                lines.append(f"    {label:<18} {n:>5}  {pct:5.1f}%")
        txt.insert("1.0", "\n".join(lines))
        txt.config(state="disabled")
        ttk.Button(self, text="Close", command=self.destroy).pack(anchor="e", padx=8, pady=8)


class RebuildDialog(tk.Toplevel):
    """Pick the everythingRF parent folder and/or a folder of catalog files,
    then rebuild the dataset (dedupe on by default)."""

    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.title("Rebuild dataset")
        self.geometry("1000x850")
        self.minsize(820, 650)
        self.resizable(True, True)
        self.transient(master)
        self.erf = tk.StringVar(value=str(EVERYTHING_RF))
        self.src = tk.StringVar(value=str(NEW_SOURCES))
        self.dedupe = tk.BooleanVar(value=True)
        # One checkbox per walkable vendor catalogue, so a refresh can target a
        # single vendor instead of re-walking all of them.
        self.vendor_vars = {v: tk.BooleanVar(value=False)
                            for v in space_dataset.CATALOG_VENDORS}
        self.adi_dir = tk.StringVar(value=str(ADI_PARAMETRICS))
        self.rate = tk.StringVar(value="1.0")
        self.dl = tk.BooleanVar(value=True)
        # Datasheet enrichment: mining local datasheets for specs the catalog
        # listings do not carry. On by default, but it is the slowest step on a
        # cold run, so it needs to be switchable.
        self.enrich = tk.BooleanVar(value=True)
        # Resume and cache answer different questions, so they are separate
        # controls. Deriving one from the other is why the vendor walks never
        # resumed: use_cache saved the request, but nothing recorded which units
        # of work were finished.
        self.use_cache = tk.BooleanVar(value=True)
        self.resume = tk.BooleanVar(value=True)
        # Default FALSE: "Reset" that silently spares every vendor you did not
        # tick is not a reset. With this on by default, ticking Reset plus one
        # vendor left the rest of the dataset in place, which looked exactly like
        # the reset having done nothing.
        self.reset_vendors_only = tk.BooleanVar(value=False)
        self.mode = tk.StringVar(value="resume")
        self.category_vars = {k: tk.BooleanVar(value=False) for k in GUI_CATEGORIES}

        pad = {"padx": 8, "pady": 4}

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)
        canvas = tk.Canvas(body, highlightthickness=0)
        scroll = ttk.Scrollbar(body, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")

        frm = ttk.Frame(canvas, padding=10)
        self._rebuild_canvas_window = canvas.create_window(
            (0, 0), window=frm, anchor="nw")
        frm.columnconfigure(1, weight=1)
        frm.bind("<Configure>", lambda e: canvas.configure(
            scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(
            self._rebuild_canvas_window, width=e.width))

        def _wheel(event):
            # bind_all callbacks can outlive a destroyed dialog. Guard every
            # access and remove the global bindings when this window closes.
            try:
                if not self.winfo_exists() or not canvas.winfo_exists():
                    return
                if getattr(event, "num", None) == 4:
                    canvas.yview_scroll(-1, "units")
                elif getattr(event, "num", None) == 5:
                    canvas.yview_scroll(1, "units")
                elif getattr(event, "delta", 0):
                    canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
            except tk.TclError:
                return

        def _unbind_rebuild_wheel(_event=None):
            for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                try:
                    self.unbind_all(sequence)
                except tk.TclError:
                    pass

        canvas.bind_all("<MouseWheel>", _wheel)
        canvas.bind_all("<Button-4>", _wheel)
        canvas.bind_all("<Button-5>", _wheel)
        self.bind("<Destroy>", _unbind_rebuild_wheel, add="+")

        ttk.Label(frm, text="everythingRF parent folder\n(holds EverythingRFSpace* subfolders)",
                  justify="left").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(frm, textvariable=self.erf).grid(row=0, column=1, sticky="ew", **pad)
        ttk.Button(frm, text="Browse…",
                   command=lambda: self._pick(self.erf)).grid(row=0, column=2, **pad)

        ttk.Label(frm, text="New sources folder\n(ADI .xlsx, Qorvo/TI .pdf)",
                  justify="left").grid(row=1, column=0, sticky="w", **pad)
        ttk.Entry(frm, textvariable=self.src).grid(row=1, column=1, sticky="ew", **pad)
        ttk.Button(frm, text="Browse…",
                   command=lambda: self._pick(self.src)).grid(row=1, column=2, **pad)

        ttk.Checkbutton(frm, text="De-duplicate after ingest (keep the copy with more specs)",
                        variable=self.dedupe).grid(row=2, column=0, columnspan=3,
                                                   sticky="w", **pad)

        # ---- live vendor catalogues -------------------------------------
        vf = ttk.LabelFrame(frm, text="Vendor catalogues to refresh", padding=8)
        vf.grid(row=3, column=0, columnspan=3, sticky="ew", **pad)
        # SOURCES, not vendors. A Qorvo part can arrive from the everythingRF
        # pages, the aerospace brochure or the parametric tables, so "re-run
        # Qorvo" cannot say which of those to touch. Re-running a source leaves
        # every other source's parts exactly as they are.
        self.source_vars = {k: tk.BooleanVar(value=False)
                            for k in space_dataset.SOURCES}
        for i, (key, label) in enumerate(space_dataset.SOURCES.items()):
            r, c = divmod(i, 2)
            ttk.Checkbutton(vf, text=label,
                            variable=self.source_vars[key]).grid(
                row=r, column=c, sticky="w", padx=(0, 18))
        row = (len(space_dataset.SOURCES) + 1) // 2
        ttk.Button(vf, text="All", width=6,
                   command=lambda: [x.set(True) for x in
                                    self.source_vars.values()]).grid(
            row=row, column=0, sticky="w", pady=(6, 0))
        ttk.Button(vf, text="None", width=6,
                   command=lambda: [x.set(False) for x in
                                    self.source_vars.values()]).grid(
            row=row, column=1, sticky="w", pady=(6, 0))
        ttk.Label(vf, text="nothing ticked = every source",
                  foreground="#666").grid(row=row + 1, column=0, columnspan=2,
                                          sticky="w")
        ttk.Button(vf, text="Source folders\u2026",
                   command=self._open_folders).grid(row=row, column=1,
                                                    sticky="e", pady=(6, 0))

        of = ttk.Frame(frm)
        of.grid(row=4, column=0, columnspan=3, sticky="ew", **pad)
        ttk.Label(of, text="ADI parametric folder").grid(row=0, column=0, sticky="w")
        ttk.Entry(of, textvariable=self.adi_dir, width=44).grid(row=0, column=1,
                                                               sticky="ew", padx=6)
        ttk.Button(of, text="Browse…",
                   command=lambda: self._pick(self.adi_dir)).grid(row=0, column=2)
        ttk.Label(of, text="seconds per request").grid(row=1, column=0, sticky="w",
                                                      pady=(6, 0))
        ttk.Entry(of, textvariable=self.rate, width=8).grid(row=1, column=1,
                                                           sticky="w", padx=6,
                                                           pady=(6, 0))
        ttk.Checkbutton(of, text="Download datasheet files",
                        variable=self.dl).grid(row=2, column=0, columnspan=2,
                                               sticky="w")
        ttk.Checkbutton(of, text="Reuse cached pages (much faster on a re-run)",
                        variable=self.use_cache).grid(row=3, column=0,
                                                      columnspan=2, sticky="w")
        ttk.Checkbutton(of, text="Enrich from local datasheets (fills specs the "
                                 "listings omit; only new/changed parts)",
                        variable=self.enrich).grid(row=4, column=0, columnspan=2,
                                                   sticky="w")
        # Mode comes FIRST, because it changes what the rest of the dialog
        # means. "Normal" and "Reset first" were really one axis -- whether
        # existing data is kept -- so they are one choice of two.
        mf = ttk.LabelFrame(frm, text="1. Mode", padding=8)
        mf.grid(row=0, column=0, columnspan=3, sticky="ew", **pad)
        ttk.Radiobutton(mf, text="Refresh — keep what is there, add and update "
                                 "only what changed",
                        variable=self.mode, value="resume",
                        command=self._mode_changed).pack(anchor="w")
        ttk.Radiobutton(mf, text="Reset — delete the selected sources' parts, "
                                 "scrape state and caches, then re-read",
                        variable=self.mode, value="reset",
                        command=self._mode_changed).pack(anchor="w")
        self.mode_hint = ttk.Label(mf, text="", foreground="#666",
                                   wraplength=560, justify="left")
        self.mode_hint.pack(anchor="w", pady=(4, 0))
        self.reset_scope_cb = ttk.Checkbutton(
            mf, text="narrow the reset to only the sources ticked below",
            variable=self.reset_vendors_only, state="disabled")
        self.reset_scope_cb.pack(anchor="w", padx=(22, 0))
        self.resume_cb = ttk.Checkbutton(
            mf, text="Resume — skip pages already recorded as done",
            variable=self.resume)
        self.resume_cb.pack(anchor="w", pady=(6, 0))


        ttk.Label(frm, text="Local folders and vendor catalogues are optional "
                            "individually, but pick at least one of them.",
                  foreground="#555").grid(row=5, column=0, columnspan=3,
                                          sticky="w", **pad)
        ttk.Label(frm, text=f"Dataset, caches, datasheets, and normalized JSON: {DATA_ROOT}",
                  foreground="#555").grid(row=8, column=0, columnspan=3,
                                          sticky="w", **pad)

        btns = ttk.Frame(self, padding=(10, 8))
        btns.pack(fill="x", side="bottom")
        ttk.Button(btns, text="Rebuild", command=self._go).pack(side="right")
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(
            side="right", padx=6)
        # A peer action, not a rebuild option: it samples a couple of part
        # numbers per source and writes nothing to the database, so there is no
        # reason to sit through a full scrape to get one.
        ttk.Button(btns, text="Create parse sample bundle…",
                   command=self._make_sample_bundle).pack(side="left")
        ttk.Label(btns, text="  PNs per source").pack(side="left")
        self.sample_n = tk.StringVar(value="2")
        ttk.Entry(btns, textvariable=self.sample_n, width=4).pack(side="left",
                                                                 padx=4)
        self.sample_offline = tk.BooleanVar(value=True)
        ttk.Checkbutton(btns, text="cached pages only",
                        variable=self.sample_offline).pack(side="left",
                                                           padx=(6, 0))

    def _make_sample_bundle(self):
        """Build the shareable bundle. Deliberately separate from Rebuild: it
        parses only a couple of part numbers per vendor and writes nothing to the
        database."""
        try:
            n = max(1, int(self.sample_n.get()))
        except ValueError:
            n = 2
        vendors = [v for v, var in getattr(self, "source_vars", {}).items()
                   if var.get() and v in ("macom", "marki", "skyworks")]
        try:
            from . import parse_sample
        except Exception as exc:
            messagebox.showerror("Unavailable", f"parse_sample: {exc}",
                                 parent=self)
            return
        # map the rebuild vendor keys onto the sampler's, and always include the
        # local sources since those are the ones most often at fault
        keys = []
        for v in vendors:
            if v == "adi":
                keys += ["adi_parametric", "adi_space"]
            elif v in parse_sample.SAMPLE_VENDORS:
                keys.append(v)
        keys += ["everythingrf"]
        if not vendors:
            keys = None          # no vendor ticked -> sample everything
        log = []
        try:
            out = parse_sample.build_bundle(
                vendors=keys, per_vendor=n, progress=log.append,
                offline=self.sample_offline.get())
        except Exception as exc:
            messagebox.showerror("Bundle failed",
                                 f"{type(exc).__name__}: {exc}\n\n"
                                 + "\n".join(log[-12:]), parent=self)
            return
        SampleBundleWindow(self, out, log)

    def _mode_changed(self):
        """Reshape the dialog for the chosen mode.

        Reset wipes the scrape state, so Resume cannot mean anything afterwards
        -- leaving it tickable would be offering a promise the mode has already
        broken."""
        reset = self.mode.get() == "reset"
        self.reset_scope_cb.config(state="normal" if reset else "disabled")
        self.resume_cb.config(state="disabled" if reset else "normal")
        if reset:
            self.mode_hint.config(
                text="Deletes parts, scrape state, cached pages and downloaded "
                     "datasheets before re-reading. Tick sources below and the "
                     "narrow option to limit what is deleted; otherwise "
                     "EVERYTHING goes.")
        else:
            self.mode_hint.config(
                text="Nothing is deleted. Sources you do not tick are not "
                     "touched at all -- not re-read, and their parts left "
                     "exactly as they are. Tick nothing to refresh every "
                     "source.")

    def _open_folders(self):
        """Where each source is read from, in its own window.

        These paths are set once and then never touched, so they were pushing
        the choices that DO change on every run further down the dialog."""
        win = tk.Toplevel(self)
        win.title("Source folders")
        win.transient(self)
        frm = ttk.Frame(win, padding=10)
        frm.pack(fill="both", expand=True)
        rows = [("everythingRF parent folder", self.erf),
                ("Catalog files folder", self.src),
                ("ADI parametric folder", self.adi_dir)]
        for i, (label, var) in enumerate(rows):
            ttk.Label(frm, text=label).grid(row=i, column=0, sticky="w", pady=3)
            ttk.Entry(frm, textvariable=var, width=52).grid(row=i, column=1,
                                                            sticky="ew", padx=6)
            ttk.Button(frm, text="Browse\u2026", width=9,
                       command=lambda v=var: self._pick(v)).grid(row=i, column=2)
        frm.columnconfigure(1, weight=1)
        ttk.Label(frm, text="Paths are unchanged from before; this window only "
                            "moves them out of the way.",
                  foreground="#666").grid(row=len(rows), column=0, columnspan=3,
                                          sticky="w", pady=(8, 4))
        ttk.Button(frm, text="Close", command=win.destroy).grid(
            row=len(rows) + 1, column=2, sticky="e")

    def _pick(self, var):
        d = filedialog.askdirectory(parent=self)
        if d:
            var.set(d)

    def _go(self):
        erf, src = self.erf.get().strip(), self.src.get().strip()
        sources = [k for k, var in self.source_vars.items() if var.get()]
        vendors = None          # the source selection decides which walks run
        if not (erf or src or sources):
            messagebox.showwarning(
                "Nothing selected",
                "Choose an everythingRF folder, a sources folder, or at least "
                "one vendor catalogue.", parent=self)
            return
        try:
            rate = max(0.0, float(self.rate.get()))
        except ValueError:
            rate = 1.0
        categories = None      # category filtering removed from the rebuild
        reset = self.mode.get() == "reset"
        if reset:
            narrowed = bool(self.reset_vendors_only.get() and vendors)
            try:
                counts = partdb.vendor_part_counts()
            except Exception:
                counts = {}
            total = sum(counts.values())
            if narrowed:
                keep = total
                names = []
                for v in vendors:
                    nm = vendor_catalogs.VENDORS.get(v, {}).get("name", v)
                    names.append(nm)
                    keep -= counts.get(nm, 0)
                scope = (f"ONLY: {', '.join(names)}\n\n"
                         f"{total - keep} part(s) will be deleted and "
                         f"{keep} part(s) from other vendors will be KEPT.")
            else:
                scope = (f"the WHOLE dataset\n\n"
                         f"all {total} part(s) will be deleted.")
            if not messagebox.askyesno(
                    "Reset dataset",
                    f"Delete parts, scrape state, cached pages and downloaded "
                    f"datasheets for {scope}\n\n"
                    f"This also clears the everythingRF resume checkpoints, so "
                    f"local HTML will be re-parsed. It cannot be undone.",
                    parent=self):
                return
        self.destroy()
        self.app.run_rebuild(erf, src, (), self.dedupe.get(),
                             vendors=vendors, vendor_rate=rate,
                             sources=sources,
                             adi_dir=self.adi_dir.get().strip() or None,
                             download_datasheets=self.dl.get(),
                             use_cache=self.use_cache.get(),
                             categories=None, reset=reset,
                             resume=(self.resume.get() and not reset),
                             reset_vendors_only=self.reset_vendors_only.get(),
                             mine_datasheets=self.enrich.get())


class SuppliersWindow(tk.Toplevel):
    """Recommended suppliers for the current category — curated, offline, and
    knowledgeable. Select one to open its space page or draft an RFQ/email."""

    def __init__(self, master, app, category, space):
        super().__init__(master)
        self.app = app
        self.category = category
        self.space = space
        self.title("Recommended suppliers"
                   + (f" — {category.replace('_', ' ')}" if category else ""))
        self.geometry("980x560")
        self.transient(master)
        self.rows = marketplaces.suggest(category, space)

        ttk.Label(self, padding=8, justify="left", foreground="#555",
                  text=("Curated offline shortlist for this category — verify "
                        "against each supplier's current site. Manufacturers "
                        "first, then RF/hi-rel distributors, marketplaces, and "
                        "qualification authorities. Double-click to open; select "
                        "one and Draft RFQ/email to start an enquiry.")
                  ).pack(fill="x")

        wrap = ttk.Frame(self)
        wrap.pack(fill="both", expand=True, padx=8)
        cols = ("kind", "name", "cats", "quals", "contact")
        tree = ttk.Treeview(wrap, columns=cols, show="headings", selectmode="browse")
        for cid, h, w, stretch in (("kind", "Type", 96, False),
                                   ("name", "Supplier", 190, False),
                                   ("cats", "Categories", 210, False),
                                   ("quals", "Qualification", 260, True),
                                   ("contact", "Contact", 180, False)):
            tree.heading(cid, text=h)
            tree.column(cid, width=w, stretch=stretch)
        tree.pack(side="left", fill="both", expand=True)
        vs = ttk.Scrollbar(wrap, orient="vertical", command=tree.yview)
        vs.pack(side="right", fill="y")
        tree.configure(yscrollcommand=vs.set)

        self._by_iid = {}
        for r in self.rows:
            iid = tree.insert("", "end", values=(
                r["kind"], r["name"], r["cats"], r["quals"], r["contact"]))
            self._by_iid[iid] = r
        tree.bind("<Double-1>", lambda e: self._open())
        tree.bind("<<TreeviewSelect>>", lambda e: self._on_select())
        self._tree = tree

        # note pane for the selected supplier
        self.note = tk.Text(self, height=3, wrap="word", padx=6, pady=5)
        self.note.pack(fill="x", padx=8, pady=(6, 0))
        self.note.insert("1.0", "Select a supplier to see details.")
        self.note.config(state="disabled")

        bar = ttk.Frame(self, padding=8)
        bar.pack(fill="x")
        self.open_btn = ttk.Button(bar, text="Open website", command=self._open,
                                   state="disabled")
        self.open_btn.pack(side="left")
        self.rfq_btn = ttk.Button(bar, text="Draft RFQ / email…",
                                  command=self._draft, state="disabled")
        self.rfq_btn.pack(side="left", padx=6)
        ttk.Button(bar, text="Close", command=self.destroy).pack(side="right")

    def _selected(self):
        sel = self._tree.selection()
        return self._by_iid.get(sel[0]) if sel else None

    def _on_select(self):
        r = self._selected()
        state = "normal" if r else "disabled"
        self.open_btn.config(state=state)
        self.rfq_btn.config(state=state)
        if r:
            self.note.config(state="normal")
            self.note.delete("1.0", "end")
            self.note.insert("1.0", f"{r['name']} — {r['note']}")
            self.note.config(state="disabled")

    def _open(self):
        r = self._selected()
        if r and r.get("url"):
            webbrowser.open(r["url"])

    def _draft(self):
        r = self._selected()
        if not r:
            return
        query = dict(self.app.last_query or {})
        query.setdefault("category", self.category)
        query.setdefault("space", self.space)
        vendor = marketplaces.draft_context(r)
        parts = [c for c in (self.app.last_ranked or [])
                 if _norm_name(c.get("vendor")) == _norm_name(r["name"])]
        to, subject, body = rfq.draft(vendor, parts, query)
        DraftWindow(self, f"RFQ — {r['name']}", to, subject, body)


class DraftWindow(tk.Toplevel):
    """Read-only drafted email/RFQ with a Copy button (no sending)."""

    def __init__(self, master, title, to, subject, body):
        super().__init__(master)
        self.title(title)
        self.geometry("660x520")
        self.transient(master)
        head = ttk.Frame(self, padding=8)
        head.pack(fill="x")
        ttk.Label(head, text=f"To: {to}", wraplength=620,
                  justify="left").pack(anchor="w")
        if subject:
            ttk.Label(head, text=f"Subject: {subject}", wraplength=620,
                      justify="left").pack(anchor="w")
        self.text = tk.Text(self, wrap="word", padx=8, pady=8)
        self.text.pack(fill="both", expand=True)
        self.text.insert("1.0", body)
        bar = ttk.Frame(self, padding=8)
        bar.pack(fill="x")
        ttk.Button(bar, text="Copy", command=self._copy).pack(side="left")
        ttk.Button(bar, text="Close", command=self.destroy).pack(side="right")
        self._full = f"To: {to}\nSubject: {subject}\n\n{body}"

    def _copy(self):
        self.clipboard_clear()
        self.clipboard_append(self._full)


class FamilyWindow(tk.Toplevel):
    """A base part number and its pedigree variants (space-qualified, grade, …).
    Reminder: pedigree is informational — these are separate parts, shown so a
    base part can be seen next to its qualified siblings."""

    def __init__(self, master, mpn, base, members):
        super().__init__(master)
        self.title(f"Family — {base}")
        self.geometry("820x420")
        self.transient(master)
        ttk.Label(self, padding=8, justify="left", foreground="#555",
                  text=(f"Parts sharing base number '{base}', strongest pedigree "
                        f"first. Pedigree is informational and does not affect "
                        f"search ranking; these are distinct orderable parts.")
                  ).pack(fill="x")
        wrap = ttk.Frame(self)
        wrap.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        cols = ("mpn", "pedigree", "vendor", "category")
        tree = ttk.Treeview(wrap, columns=cols, show="headings", selectmode="browse")
        for cid, h, w, stretch in (("mpn", "Part", 220, False),
                                   ("pedigree", "Pedigree", 150, False),
                                   ("vendor", "Vendor", 200, True),
                                   ("category", "Category", 150, False)):
            tree.heading(cid, text=h)
            tree.column(cid, width=w, stretch=stretch)
        tree.pack(side="left", fill="both", expand=True)
        vs = ttk.Scrollbar(wrap, orient="vertical", command=tree.yview)
        vs.pack(side="right", fill="y")
        tree.configure(yscrollcommand=vs.set)
        tree.tag_configure("self", background="#e3f2fd")
        self._urls = {}
        for m in members:
            iid = tree.insert("", "end",
                              values=((m["mpn"] or "?") + (" (selected)" if m["is_self"] else ""),
                                      m["pedigree_label"], m["vendor"] or "?",
                                      m["category"] or ""),
                              tags=("self",) if m["is_self"] else ())
            self._urls[iid] = m.get("url")
        tree.bind("<Double-1>", lambda e: self._open(tree))
        bar = ttk.Frame(self, padding=8)
        bar.pack(fill="x")
        ttk.Label(bar, foreground="#777",
                  text="Double-click a part to open its page.").pack(side="left")
        ttk.Button(bar, text="Close", command=self.destroy).pack(side="right")

    def _open(self, tree):
        sel = tree.selection()
        if sel and self._urls.get(sel[0]):
            webbrowser.open(self._urls[sel[0]])


class HealthWindow(tk.Toplevel):
    """Dataset coverage / pedigree / family snapshot from partdb."""

    def __init__(self, master, h):
        super().__init__(master)
        self.title("Dataset health")
        self.geometry("620x640")
        self.transient(master)
        txt = tk.Text(self, wrap="word", padx=10, pady=8)
        txt.pack(fill="both", expand=True)
        yb = ttk.Scrollbar(self, orient="vertical", command=txt.yview)
        yb.place(relx=1.0, rely=0, relheight=1.0, anchor="ne")
        txt.configure(yscrollcommand=yb.set)

        parts = h["parts"] or 1
        L = []
        L.append(f"Dataset health\n{'=' * 46}")
        L.append(f"parts: {h['parts']}     vendors: {h['vendors']}     "
                 f"duplicate groups remaining: {h['duplicate_groups']}\n")

        so = h.get("source_overlap", {})
        L.append("Source overlap")
        L.append(f"  EverythingRF only     {so.get('everythingrf_only', 0):>5}")
        L.append(f"  Vendor site only      {so.get('vendor_only', 0):>5}")
        L.append(f"  Found in both         {so.get('both', 0):>5}")
        if so.get("other", 0):
            L.append(f"  Other/local sources   {so.get('other', 0):>5}")
        L.append("")

        L.append("Pedigree distribution  (informational — not scored)")
        for k in partdb.pedigree.LADDER:
            n = h["pedigree"].get(k, 0)
            L.append(f"  {h['pedigree_labels'][k]:<16} {n:>5}  {100.0*n/parts:5.1f}%")

        L.append("\nParts by category")
        for cat, n in h["by_category"]:
            L.append(f"  {cat:<20} {n:>5}")

        L.append("\nSpec coverage by category")
        L.append("  (% of parts in that category carrying a value — RF specs are")
        L.append("   only shown for the categories they apply to)")
        for cc in h["category_coverage"]:
            L.append(f"\n  {cc['category']} ({cc['count']})")
            for lbl, n, pct in cc["specs"]:
                bar = "\u2588" * int(pct / 5)
                L.append(f"    {lbl:<16} {n:>5}  {pct:5.1f}%  {bar}")

        L.append(f"\nRadiation data (TID/SEL): {h['radiation_parts']} parts "
                 f"({h['radiation_pct']}%)")

        f = h["families"]
        L.append(f"\nFamilies (base part + pedigree variants)")
        L.append(f"  {f['families']} base parts have variants "
                 f"({f['parts_in_families']} parts total)")
        L.append(f"  {f['multi_pedigree_families']} span more than one pedigree level")

        txt.insert("1.0", "\n".join(L))
        txt.config(state="disabled")
        bar = ttk.Frame(self, padding=8)
        bar.pack(fill="x")
        ttk.Button(bar, text="Copy", command=lambda: (
            self.clipboard_clear(), self.clipboard_append("\n".join(L)))).pack(side="left")
        ttk.Button(bar, text="Close", command=self.destroy).pack(side="right")


def main():
    root = tk.Tk()
    root.title("rfparts — space-qualified parts finder")
    root.geometry("1280x760")
    root.minsize(1040, 620)
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    main()
