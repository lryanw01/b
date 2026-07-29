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
import json
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
from .paths import ADI_PARAMETRICS, EVERYTHING_RF, NEW_SOURCES

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
    ("interface", "Interface", 100),
    ("space", "Space qual", 100),
]
TAIL_COLS = [("notes", "Notes", 260)]

# Candidate spec columns that can appear between the lead and tail columns, each
# mapped to the rank criterion that decides its colour. Only columns with a
# value on at least one result are shown, so the grid reads like a datasheet for
# whatever category was searched. (RF port counting is intentionally gone.)
#   (col id, heading, spec key, criterion name, formatter)
def _f_freq(v):
    return f"{v[0]:g}–{v[1]:g}" if isinstance(v, (list, tuple)) and len(v) == 2 else ""


def _f_num(v):
    return f"{v:g}" if isinstance(v, (int, float)) else ""


SPEC_COLS = [
    ("freq", "Freq (GHz)", "freq_ghz", "freq", _f_freq),
    ("gain", "Gain (dB)", "gain_db", "gain", _f_num),
    ("nf", "NF (dB)", "noise_nf_db", "nf", _f_num),
    ("p1db", "P1dB (dBm)", "p1db_dbm", "p1db", _f_num),
    ("oip3", "OIP3 (dBm)", "oip3_dbm", "oip3", _f_num),
    ("psat", "Psat (dBm)", "psat_dbm", None, _f_num),
    ("il", "Ins.loss (dB)", "insertion_loss_db", None, _f_num),
    ("cl", "Conv.loss (dB)", "conversion_loss_db", "cl", _f_num),
    ("isol", "Isolation (dB)", "isolation_db", "isol", _f_num),
    ("atten", "Atten (dB)", "attenuation_db", "atten", _f_num),
    ("pwr", "Power (W)", "power_w", "pwr", _f_num),
    ("tid", "TID (kRad)", "tid_krad", None, _f_num),
    ("sel", "SEL (MeV)", "sel_mev", None, _f_num),
]


class App(ttk.Frame):
    def __init__(self, master):
        super().__init__(master, padding=8)
        self.grid(sticky="nsew")
        master.columnconfigure(0, weight=1)
        master.rowconfigure(0, weight=1)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(1, weight=1)

        self.q = queue.Queue()
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
        self._sub_combo = ttk.Combobox(f, textvariable=self.vars["subcategory"],
                                       values=["(any)"], width=22, state="readonly")
        self._sub_label_to = {"(any)": None}
        # A filter's response type (LPF/HPF/BPF/BSF) changes the frequency input
        # and key specs, so relayout when the subcategory changes too.
        self.vars["subcategory"].trace_add("write", self._on_subcategory_change)

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
                     mkhelp("e.g. 4-8 or DC-18")),
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
            self.vars[pkey] = tk.StringVar()
            self._field_rows[pkey] = (
                ttk.Label(f, text=meta["label"]),
                ttk.Entry(f, textvariable=self.vars[pkey]), None)

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

        self._on_category_change()   # initial layout (no category yet)

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
        chosen = self._sub_label_to.get(self.vars["subcategory"].get().strip())
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
        if self.vars["subcategory"].get() not in values:
            self.vars["subcategory"].set("(any)")
        self._apply_keyparams(key)
        # Don't let a value typed under one category leak into a search for
        # another that hides that field (e.g. gain set for an amp, then Switches).
        shown = set(registry.category_fields(key))
        for fkey in self._field_rows:
            if fkey not in shown:
                self.vars[fkey].set("")
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

        self.table = DatasheetTable(wrap, on_select=self._on_row_select,
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
        chosen = self._sub_label_to.get(self.vars["subcategory"].get().strip())
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
            if not raw:
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
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "status":
                    self.status.set(payload)
                    if getattr(self, "build_window", None):
                        self.build_window.add_message(payload)
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
        """Spec columns that at least one result has a value for (so the grid
        adapts to the searched category, datasheet-style)."""
        present = set()
        for c in ranked:
            s = c.get("specs", {})
            for cid, _h, key, _crit, _fmt in SPEC_COLS:
                if s.get(key) not in (None, "", []):
                    present.add(cid)
        return [col for col in SPEC_COLS if col[0] in present]

    def _show_results(self, ranked, errors):
        self.last_ranked = ranked
        self.last_errors = errors or []
        try:
            top_n = int(self.vars["top"].get())
        except (ValueError, tk.TclError):
            top_n = 250
        shown = ranked[:top_n] if top_n else ranked
        spec_cols = self._visible_spec_cols(shown)

        headings = ([h for _c, h, _w in LEAD_COLS]
                    + [h for _cid, h, _k, _cr, _f in spec_cols]
                    + [h for _c, h, _w in TAIL_COLS])
        widths = ([w for _c, _h, w in LEAD_COLS]
                  + [92 for _ in spec_cols]
                  + [w for _c, _h, w in TAIL_COLS])
        self.table.set_columns(headings, widths)

        for c in shown:
            crit = c.get("criteria", {})
            specs = c.get("specs", {})
            tier = c.get("tier", "?")
            cells = []
            # lead columns
            cells.append((f"{tier} {c.get('fit_score', 0)}", TIER_BG.get(tier, CELL_NEUTRAL)))
            cells.append((c.get("vendor", "?"), CELL_NEUTRAL))
            cells.append((c.get("model") or c.get("title", "?"), CELL_NEUTRAL))
            cells.append((self._interface_str(c),
                          self._cell_color(crit.get("pkg"),
                                           bool(specs.get("mount_type") or specs.get("package")))))
            cells.append((rank._space_str(c),
                          self._cell_color(crit.get("space"), True)))
            # spec columns
            for _cid, _h, key, cr, fmt in spec_cols:
                v = specs.get(key)
                txt = fmt(v)
                cells.append((txt, self._cell_color(crit.get(cr), bool(txt))))
            # notes
            cells.append((self._note_summary(c), CELL_NEUTRAL))
            self.table.add_row(cells, c)

        n = len(ranked)
        extra = f" (showing {len(shown)})" if len(shown) < n else ""
        self.status.set(f"{n} space part(s){extra}."
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

    def run_rebuild(self, erf_parent, source_dir, source_files, dedupe,
                    vendors=None, vendor_rate=1.0, adi_dir=None,
                    download_datasheets=True, use_cache=True, categories=None,
                    reset=False):
        """Called by RebuildDialog; ingests on a worker thread."""
        self.rebuild_btn.config(state="disabled")
        self.progress.config(mode="indeterminate", value=0)
        self.progress.start(12)
        self.status.set("Rebuilding dataset…")
        self.build_window = BuildProgressWindow(self.winfo_toplevel(), vendors or [])

        def worker():
            try:
                summary = space_dataset.rebuild(
                    erf_parent=erf_parent or None, source_dir=source_dir or None,
                    source_files=source_files or (), dedupe=dedupe,
                    progress=lambda m: self.q.put(("status", m)),
                    vendors=vendors or None, vendor_rate=vendor_rate,
                    adi_dir=adi_dir,
                    download_datasheets=download_datasheets,
                    use_cache=use_cache, categories=categories, reset=reset)
                self.q.put(("rebuilt", summary))
            except Exception as e:  # noqa: BLE001
                self.q.put(("rebuild_error", str(e)))

        threading.Thread(target=worker, daemon=True).start()
        self.after(80, self._poll_rebuild)

    def _poll_rebuild(self):
        busy = str(self.rebuild_btn["state"]) == "disabled"
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "status":
                    self.status.set(payload)
                    if getattr(self, "build_window", None):
                        self.build_window.add_message(payload)
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

    def __init__(self, master, on_select=None, on_open=None):
        super().__init__(master)
        self.on_select = on_select
        self.on_open = on_open
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        self.canvas = tk.Canvas(self, highlightthickness=0, background="#ffffff")
        self.canvas.grid(row=0, column=0, sticky="nsew")
        vs = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        vs.grid(row=0, column=1, sticky="ns")
        hs = ttk.Scrollbar(self, orient="horizontal", command=self.canvas.xview)
        hs.grid(row=1, column=0, sticky="ew")
        self.canvas.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)

        self.inner = tk.Frame(self.canvas, background="#ffffff")
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>",
                        lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Enter>", lambda e: self._bind_wheel())
        self.canvas.bind("<Leave>", lambda e: self._unbind_wheel())

        self._headings = []
        self._widths = []
        self._row_frames = []      # per data row: list of cell labels
        self._candidates = []
        self._selected = None

    def _bind_wheel(self):
        self.canvas.bind_all("<MouseWheel>",
                             lambda e: self.canvas.yview_scroll(int(-e.delta / 120), "units"))
        self.canvas.bind_all("<Button-4>", lambda e: self.canvas.yview_scroll(-1, "units"))
        self.canvas.bind_all("<Button-5>", lambda e: self.canvas.yview_scroll(1, "units"))

    def _unbind_wheel(self):
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.canvas.unbind_all(seq)

    def clear(self):
        for w in self.inner.winfo_children():
            w.destroy()
        self._row_frames = []
        self._candidates = []
        self._selected = None
        self._headings = []

    def set_columns(self, headings, widths):
        self.clear()
        self._headings = headings
        self._widths = widths
        for col, (h, w) in enumerate(zip(headings, widths)):
            lbl = tk.Label(self.inner, text=h, bg=HEAD_BG, fg=HEAD_FG,
                           font=("TkDefaultFont", 9, "bold"), padx=6, pady=4,
                           borderwidth=1, relief="solid", anchor="center",
                           wraplength=max(w, 60))
            lbl.grid(row=0, column=col, sticky="nsew")
            self.inner.columnconfigure(col, minsize=w)

    def add_row(self, cells, candidate):
        r = len(self._row_frames) + 1
        labels = []
        for col, (text, bg) in enumerate(cells):
            anchor = "w" if col in (1, 2) or col == len(cells) - 1 else "center"
            lbl = tk.Label(self.inner, text=text, bg=bg, padx=6, pady=3,
                           borderwidth=1, relief="solid", anchor=anchor,
                           wraplength=max(self._widths[col], 60),
                           justify="left")
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


class BuildProgressWindow(tk.Toplevel):
    """Focused rebuild status: current source/URL and most recent part."""

    def __init__(self, master, vendors):
        super().__init__(master)
        self.title("Building dataset")
        self.geometry("1200x820")
        self.minsize(900, 650)
        self.resizable(True, True)
        self.transient(master)
        self.protocol("WM_DELETE_WINDOW", lambda: None)
        self.current = tk.StringVar(value="Starting rebuild…")
        self.url_status = tk.StringVar(value="Waiting for the first source…")
        self.part_status = tk.StringVar(value="No part has been added yet.")

        ttk.Label(self, textvariable=self.current, padding=8,
                  font=("TkDefaultFont", 10, "bold")).pack(fill="x")

        scrape_box = ttk.LabelFrame(self, text="Currently being scraped", padding=10)
        scrape_box.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Label(scrape_box, textvariable=self.url_status, justify="left",
                  wraplength=1120).pack(fill="x", anchor="w")

        part_box = ttk.LabelFrame(self, text="Most recently added part", padding=10)
        part_box.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Label(part_box, textvariable=self.part_status, justify="left",
                  wraplength=1120).pack(fill="x", anchor="w")

        vendor_box = ttk.LabelFrame(self, text="Current dataset by vendor", padding=6)
        vendor_box.pack(fill="both", expand=True, padx=8, pady=6)
        vendor_box.rowconfigure(0, weight=1)
        vendor_box.columnconfigure(0, weight=1)
        self.vendor_canvas = tk.Canvas(vendor_box, highlightthickness=0)
        vendor_y = ttk.Scrollbar(vendor_box, orient="vertical",
                                 command=self.vendor_canvas.yview)
        self.vendor_canvas.configure(yscrollcommand=vendor_y.set)
        self.vendor_canvas.grid(row=0, column=0, sticky="nsew")
        vendor_y.grid(row=0, column=1, sticky="ns")
        self.vendor_frame = ttk.Frame(self.vendor_canvas, padding=4)
        self._vendor_window = self.vendor_canvas.create_window(
            (0, 0), window=self.vendor_frame, anchor="nw")
        self.vendor_frame.bind(
            "<Configure>",
            lambda e: self.vendor_canvas.configure(
                scrollregion=self.vendor_canvas.bbox("all")))
        self.vendor_canvas.bind(
            "<Configure>",
            lambda e: self.vendor_canvas.itemconfigure(
                self._vendor_window, width=e.width))

        def _vendor_wheel(event):
            if getattr(event, "num", None) == 4:
                self.vendor_canvas.yview_scroll(-1, "units")
            elif getattr(event, "num", None) == 5:
                self.vendor_canvas.yview_scroll(1, "units")
            elif getattr(event, "delta", 0):
                self.vendor_canvas.yview_scroll(
                    -1 if event.delta > 0 else 1, "units")
        self.vendor_canvas.bind("<MouseWheel>", _vendor_wheel)
        self.vendor_canvas.bind("<Button-4>", _vendor_wheel)
        self.vendor_canvas.bind("<Button-5>", _vendor_wheel)

        self.done = ttk.Button(self, text="Close", command=self.destroy,
                               state="disabled")
        self.done.pack(anchor="e", padx=8, pady=(0, 8))
        self._refresh_vendor_rows()

    def add_message(self, message):
        text = str(message).strip()
        if not text:
            return
        self.current.set(text)
        if text.startswith("ADDED |"):
            fields = {}
            for chunk in text.split("|")[1:]:
                if "=" in chunk:
                    key, value = chunk.split("=", 1)
                    fields[key.strip()] = value.strip()
            self.part_status.set(
                f"PN: {fields.get('pn', '?')}\n"
                f"Vendor: {fields.get('vendor', '?')}\n"
                f"Category: {fields.get('category', 'unknown')}\n"
                f"Space qualification: {fields.get('space', 'unknown')}\n"
                f"Specs: {fields.get('specs', 'no parsed specs')}")
        elif text.startswith(("SCRAPE |", "RESUME |", "SOURCE |")):
            self.url_status.set(text)
        elif "ingesting " in text.lower() or "vendor catalog ingest" in text.lower():
            self.url_status.set(text)
        self._refresh_vendor_rows()

    def _refresh_vendor_rows(self):
        for w in self.vendor_frame.winfo_children():
            w.destroy()
        try:
            counts = partdb.vendor_counts()
        except Exception:
            counts = {}
        if not counts:
            ttk.Label(self.vendor_frame, text="No parts currently stored.").grid(row=0, column=0, sticky="w")
            return
        for i, (vendor, count) in enumerate(sorted(counts.items(), key=lambda x: (-x[1], x[0].lower()))):
            ttk.Label(self.vendor_frame, text=f"{vendor}: {count} parts", width=34).grid(row=i//3, column=(i%3)*2, sticky="w")
            ttk.Button(self.vendor_frame, text="Health…",
                       command=lambda v=vendor: VendorHealthWindow(self, partdb.vendor_health(v))).grid(row=i//3, column=(i%3)*2+1, padx=(0, 10))

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
        self.use_cache = tk.BooleanVar(value=True)
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
            if getattr(event, "num", None) == 4:
                canvas.yview_scroll(-1, "units")
            elif getattr(event, "num", None) == 5:
                canvas.yview_scroll(1, "units")
            elif getattr(event, "delta", 0):
                canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
        canvas.bind_all("<MouseWheel>", _wheel)
        canvas.bind_all("<Button-4>", _wheel)
        canvas.bind_all("<Button-5>", _wheel)

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
        notes = {
            "adi": "parametric .xlsx you exported (never scraped)",
            "qorvo": "parametric tables incl. freq/gain/NF/OIP3",
            "macom": "product-detail pages + cdn datasheets",
            "skyworks": "category walk, then product page per part",
            "marki": "paged listings; datasheet text from HTML",
        }
        for i, v in enumerate(space_dataset.CATALOG_VENDORS):
            name = vendor_catalogs.VENDORS[v]["name"]
            ttk.Checkbutton(vf, text=name,
                            variable=self.vendor_vars[v]).grid(row=i, column=0,
                                                               sticky="w")
            ttk.Label(vf, text=notes.get(v, ""), foreground="#666").grid(
                row=i, column=1, sticky="w", padx=(10, 0))
        row = len(space_dataset.CATALOG_VENDORS)
        ttk.Button(vf, text="All", width=6,
                   command=lambda: [x.set(True) for x in
                                    self.vendor_vars.values()]).grid(row=row,
                                                                     column=0,
                                                                     sticky="w",
                                                                     pady=(6, 0))
        ttk.Button(vf, text="None", width=6,
                   command=lambda: [x.set(False) for x in
                                    self.vendor_vars.values()]).grid(row=row,
                                                                     column=1,
                                                                     sticky="w",
                                                                     pady=(6, 0))

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
        mf = ttk.LabelFrame(frm, text="Rebuild mode", padding=8)
        mf.grid(row=7, column=0, columnspan=3, sticky="ew", **pad)
        ttk.Radiobutton(mf, text="Resume scraping (reuse cache and skip network requests for cached pages)",
                        variable=self.mode, value="resume").pack(anchor="w")
        ttk.Radiobutton(mf, text="Reset dataset (delete all parts and scrape caches first)",
                        variable=self.mode, value="reset").pack(anchor="w")

        cf = ttk.LabelFrame(frm, text="Optional categories (none selected = all)", padding=8)
        cf.grid(row=6, column=0, columnspan=3, sticky="ew", **pad)
        for i, key in enumerate(GUI_CATEGORIES):
            ttk.Checkbutton(cf, text=registry.category_label(key),
                            variable=self.category_vars[key]).grid(row=i//4, column=i%4, sticky="w", padx=4)

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

    def _pick(self, var):
        d = filedialog.askdirectory(parent=self)
        if d:
            var.set(d)

    def _go(self):
        erf, src = self.erf.get().strip(), self.src.get().strip()
        vendors = [v for v, var in self.vendor_vars.items() if var.get()]
        if not (erf or src or vendors):
            messagebox.showwarning(
                "Nothing selected",
                "Choose an everythingRF folder, a sources folder, or at least "
                "one vendor catalogue.", parent=self)
            return
        try:
            rate = max(0.0, float(self.rate.get()))
        except ValueError:
            rate = 1.0
        categories = [k for k, var in self.category_vars.items() if var.get()]
        reset = self.mode.get() == "reset"
        if reset and not messagebox.askyesno(
                "Reset dataset",
                "Delete every existing part and all scrape caches before rebuilding?",
                parent=self):
            return
        self.destroy()
        self.app.run_rebuild(erf, src, (), self.dedupe.get(),
                             vendors=vendors, vendor_rate=rate,
                             adi_dir=self.adi_dir.get().strip() or None,
                             download_datasheets=self.dl.get(),
                             use_cache=(True if self.mode.get() == "resume" else self.use_cache.get()),
                             categories=categories, reset=reset)


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
