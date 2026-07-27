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

from . import cli, fetch, minicircuits_cache, rank, rfq, spec, specstore
from . import registry
from .registry import DATA, GUI_CATEGORIES, load_vendors
from .spec import PACKAGE_SYNONYMS

# Only the user-facing categories, shown by their display label.
CATEGORIES = [registry.category_label(k) for k in GUI_CATEGORIES]
PACKAGES = [""] + sorted(PACKAGE_SYNONYMS.keys())
IMPEDANCES = ["", "50 Ω", "75 Ω", "33 Ω"]
PREFS = DATA / "gui_prefs.json"


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

# Columns: (id, heading, width, anchor)
COLUMNS = [
    ("tier", "Tier / Score", 100, "center"),
    ("vendor", "Vendor", 130, "w"),
    ("title", "Part", 300, "w"),
    ("interface", "Interface", 80, "center"),
    ("snp", "sNp", 70, "center"),
    ("size", "Size (L×W×H)", 120, "center"),
    ("space", "Space", 90, "center"),
    ("criteria", "Criteria", 280, "w"),
    ("price", "Price", 80, "e"),
    ("lead", "Lead", 80, "center"),
    ("notes", "Notes", 230, "w"),
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
        self.row_url = {}       # treeview iid -> product URL
        self.row_candidate = {} # treeview iid -> ranked candidate
        self._stream_buf = []   # candidates streamed in during extraction
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

        # Watch the standalone Mini-Circuits background scanner cache. When it
        # changes on disk, refresh displayed Mini-Circuits rows in memory — no
        # network, no automatic re-crawl.
        self._minicircuits_cache_mtime = minicircuits_cache.cache_mtime_ns()
        self.after(60_000, self._poll_minicircuits_cache)

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

        # Local-database mode: results come only from the stored DB (previous
        # crawls + ingested catalogs) plus the DigiKey API — no web discovery,
        # extraction, or background crawling. Always shown (footer).
        self.vars["local_only"] = tk.BooleanVar()
        self._local_check = ttk.Checkbutton(
            f, text="Local database only (+ DigiKey API — no crawling)",
            variable=self.vars["local_only"])

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

    # ---- results table --------------------------------------------------
    def _build_results(self):
        wrap = ttk.Frame(self)
        wrap.grid(row=0, column=1, rowspan=2, sticky="nsew")
        wrap.columnconfigure(0, weight=1)
        wrap.rowconfigure(0, weight=1)
        wrap.rowconfigure(2, weight=0)

        cols = [c[0] for c in COLUMNS]
        self.tree = ttk.Treeview(wrap, columns=cols, show="headings", selectmode="browse")
        self._sort_state = {}
        for cid, heading, width, anchor in COLUMNS:
            self.tree.heading(cid, text=heading,
                              command=lambda c=cid: self._sort_by(c))
            self.tree.column(cid, width=width, anchor=anchor, stretch=(cid == "title"))
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.tree.tag_configure("A", background="#e8f6ec")
        self.tree.tag_configure("B", background="#fff8e1")
        self.tree.tag_configure("C", background="#fdecea")
        self.tree.bind("<Double-1>", lambda e: self.open_url())
        self.tree.bind("<<TreeviewSelect>>", self._show_selected_notes)

        vs = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        vs.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=vs.set)

        ttk.Label(wrap, text="Selected result notes:").grid(
            row=1, column=0, sticky="w", pady=(6, 2))
        self.notes_text = tk.Text(wrap, height=4, wrap="word", padx=6, pady=5)
        self.notes_text.grid(row=2, column=0, columnspan=2, sticky="ew")
        self.notes_text.insert("1.0", "Select a result to see why any values are unknown.")
        self.notes_text.config(state="disabled")

        ttk.Label(wrap, text="Web results (unverified — shown when no strong database match):").grid(
            row=3, column=0, sticky="w", pady=(8, 2))
        self.web_tree = ttk.Treeview(
            wrap, columns=("wsrc", "wtitle", "wsnp"), show="headings", height=5,
            selectmode="browse")
        for cid, heading, width, anchor, stretch in (
                ("wsrc", "Source", 160, "w", False),
                ("wtitle", "Result", 420, "w", True),
                ("wsnp", "sNp", 60, "center", False)):
            self.web_tree.heading(cid, text=heading)
            self.web_tree.column(cid, width=width, anchor=anchor, stretch=stretch)
        self.web_tree.grid(row=4, column=0, sticky="nsew")
        self.web_tree.bind("<Double-1>", lambda e: self._open_web_url())
        wvs = ttk.Scrollbar(wrap, orient="vertical", command=self.web_tree.yview)
        wvs.grid(row=4, column=1, sticky="ns")
        self.web_tree.configure(yscrollcommand=wvs.set)
        self.web_row_url = {}

        bar = ttk.Frame(self)
        bar.grid(row=2, column=1, sticky="ew", pady=(6, 0))
        ttk.Button(bar, text="Open part page", command=self.open_url).pack(side="left")
        ttk.Button(bar, text="Copy URL", command=self.copy_url).pack(side="left", padx=4)
        ttk.Button(bar, text="Draft RFQ…", command=self.draft_rfq).pack(side="left")
        ttk.Button(bar, text="Save report…", command=self.save_report).pack(side="left", padx=4)
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
                if fc is not None:
                    # lowpass passes DC..Fc; highpass passes Fc and up (modelled
                    # as an operating point at Fc so the band check still works).
                    q["freq_ghz"] = [0.0, fc] if resp == "lowpass" else [fc, fc]
                    q["cutoff_ghz"] = fc
                else:
                    q.pop("freq_ghz", None)
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

    def on_search(self):
        try:
            query = self.build_query()
        except ValueError as e:
            messagebox.showerror("Invalid input", str(e))
            return
        if not query.get("category"):
            messagebox.showerror("Missing category", "Category is required.")
            return

        self.last_query = query
        _save_prefs({"prefer": self._prefer_selected(), "exclude": self._exclude_selected()})
        self.tree.delete(*self.tree.get_children())
        self.row_url.clear()
        self.row_candidate.clear()
        self._stream_buf = []
        self.web_tree.delete(*self.web_tree.get_children())
        self.web_row_url.clear()
        self._set_notes("Select a result to see why any values are unknown.")
        self.cancel_event.clear()
        self.search_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        self.progress.config(mode="indeterminate", value=0)
        self.progress.start(12)
        self.status.set("Searching…")

        def worker():
            fetch.LOG_SINK = lambda m: self.q.put(("status", m))
            try:
                ranked, errors = cli.run_search(
                    query,
                    progress=lambda m: self.q.put(("status", m)),
                    cancel=self.cancel_event,
                    tick=lambda d, t: self.q.put(("tick", (d, t))),
                    on_result=lambda c: self.q.put(("result", c)))
                if self.cancel_event.is_set():
                    self.q.put(("cancelled", None))
                else:
                    web = ([] if query.get("local_only")
                           else cli.web_fallback(query, ranked, cancel=self.cancel_event))
                    self.q.put(("done", (ranked, errors, web)))
            except Exception as e:  # noqa: BLE001 - surface any failure in the UI
                self.q.put(("error", str(e)))
            finally:
                fetch.LOG_SINK = None

        threading.Thread(target=worker, daemon=True).start()
        self.after(100, self._poll)

    def on_cancel(self):
        self.cancel_event.set()
        self.cancel_btn.config(state="disabled")
        self.status.set("Cancelling… (finishing in-flight requests)")

    def _poll(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "status":
                    self._last_status = payload
                    self.status.set(payload)
                elif kind == "tick":
                    done, total = payload
                    if self.progress["mode"] != "determinate":
                        self.progress.stop()
                        self.progress.config(mode="determinate", maximum=max(total, 1))
                    self.progress["value"] = done
                    shown = len(self.tree.get_children())
                    self.status.set(f"Extracting specs… {done}/{total}"
                                    + (f" ({shown} shown)" if shown else ""))
                elif kind == "result":
                    # Stream a partial row so parts appear as they're extracted;
                    # flush in batches of ~10 to avoid thrashing the widget.
                    self._stream_buf.append(payload)
                    if len(self._stream_buf) >= 10:
                        self._stream_flush()
                elif kind == "error":
                    self._finish()
                    messagebox.showerror("Search failed", payload)
                    self.status.set("Search failed.")
                elif kind == "cancelled":
                    self._finish()
                    self.status.set("Search cancelled.")
                elif kind == "done":
                    ranked, errors, web = payload
                    self._stream_buf.clear()      # ranked set supersedes previews
                    self._show_results(ranked, errors)
                    self._show_web(web)
                    self._finish()
                    # DO NOT return: the background crawl keeps streaming
                    # "result" items after the search finishes, and returning
                    # here killed the poll loop — everything crawled after the
                    # EXTRACT TIMING SUMMARY was queued but never displayed.
        except queue.Empty:
            pass
        if self._stream_buf:
            self._stream_flush()                  # no more waiting for a batch of 10
        self.after(100, self._poll)

    def _stream_flush(self):
        """Insert any buffered streamed candidates as live rows.

        These are pre-ranking previews: each is evaluated against the current
        query so its tier/score/criteria are meaningful, but the final ranked
        order (and de-duped set) is applied when the run finishes in
        _show_results. Capped at the user's "Show top N" so the widget can't be
        flooded mid-run.
        """
        buf, self._stream_buf = self._stream_buf, []
        if not buf:
            return
        added = 0
        try:
            cap = int(float(self.vars["top"].get()))
        except ValueError:
            cap = 250
        for c in buf:
            try:
                rank.evaluate(c, self.last_query or {})
                c["tier"] = rank.tier(c)
            except Exception:
                c.setdefault("tier", "B")
                c.setdefault("fit_score", 0)
            # A part is streamed TWICE by design: once the moment it is
            # identified (fast, page specs only) and again when its datasheet
            # finishes parsing. The second arrival UPDATES the existing row in
            # place rather than being dropped, so the table fills immediately
            # and then sharpens.
            url = c.get("url")
            if url:
                existing = next((i for i, u in self.row_url.items() if u == url),
                                None)
                if existing is not None:
                    values, tag = self._row_values(c)
                    self.tree.item(existing, values=values, tags=(tag,))
                    self.row_candidate[existing] = c
                    continue
            at_cap = len(self.tree.get_children()) >= cap
            idx = self._stream_index(c)
            if at_cap:
                if idx == "end":
                    continue              # not top-tier: DB keeps it for next run
                last = self.tree.get_children()[-1]
                self.row_url.pop(last, None)
                self.row_candidate.pop(last, None)
                self.tree.delete(last)    # top-tier find replaces the worst row
            values, tag = self._row_values(c)
            iid = self.tree.insert("", idx, tags=(tag,), values=values)
            self.row_url[iid] = c.get("url")
            self.row_candidate[iid] = c
            added += 1
        if added:
            # The "N ranked options" line is printed once when the search
            # finishes; the background crawl keeps adding rows afterwards, so
            # the status line tracks the live table count.
            shown = len(self.tree.get_children())
            base = getattr(self, "_last_status", "")
            self.status.set(f"{base}  |  {shown} shown" if base
                            else f"{shown} shown")

    def _stream_rank_key(self, c):
        """Sort key matching rank.rank(): tier, then met count, then space
        readiness, then fit score. Lower sorts earlier."""
        tier = {"A": 0, "B": 1, "C": 2}.get(c.get("tier", "B"), 1)
        space = {"qualified": 0, "hi_rel": 1, "qualifiable": 2}.get(
            (c.get("specs") or {}).get("space"), 3)
        return (tier, -int(c.get("met", 0) or 0), space,
                -float(c.get("fit_score", 0) or 0))

    def _stream_index(self, c):
        """Where to insert a streamed candidate so the live table stays sorted.

        Top-tier finds (tier A, or any space-qualified part) are placed in
        rank order among the existing rows instead of being appended out of
        sight at the bottom — a crawl that turns up a space-qualified part
        mid-run should surface it immediately. Everything else appends, which
        keeps insertion cheap for the long tail.
        """
        specs = c.get("specs") or {}
        top = c.get("tier") == "A" or specs.get("space") == "qualified"
        if not top:
            return "end"
        key = self._stream_rank_key(c)
        for pos, iid in enumerate(self.tree.get_children()):
            other = self.row_candidate.get(iid)
            if other is None:
                continue
            if key < self._stream_rank_key(other):
                return pos
        return "end"

    @staticmethod
    def _sort_key(text):
        """Numeric-aware sort key: parse '$1,234', '85%', '2-18', '1.5 dB' as
        numbers; everything non-numeric sorts alphabetically after numbers;
        blanks/em-dashes sink to the bottom regardless of direction."""
        t = str(text).strip()
        if t in ("", "—", "?", "None"):
            return (2, 0.0, "")
        import re as _re
        m = _re.search(r"[-+]?\d+(?:[\d,]*\.?\d*)?", t.replace(",", ""))
        if m and (m.start() == 0 or t[0] in "$<>~"):
            try:
                return (0, float(m.group().replace(",", "")), t.lower())
            except ValueError:
                pass
        return (1, 0.0, t.lower())

    def _sort_by(self, col):
        """Sort the table by a column; click again to reverse. Non-numeric
        columns sort alphabetically."""
        reverse = self._sort_state.get(col, False)
        rows = [(self._sort_key(self.tree.set(iid, col)), iid)
                for iid in self.tree.get_children()]
        # blanks stay at the bottom in BOTH directions
        rows.sort(key=lambda x: (x[0][0] == 2, x[0]) if not reverse
                  else (x[0][0] == 2,), reverse=False)
        if reverse:
            filled = [r for r in rows if r[0][0] != 2]
            blanks = [r for r in rows if r[0][0] == 2]
            filled.sort(key=lambda x: x[0], reverse=True)
            rows = filled + blanks
        for pos, (_k, iid) in enumerate(rows):
            self.tree.move(iid, "", pos)
        self._sort_state = {col: not reverse}     # reset other columns' state

    def _finish(self):
        self.progress.stop()
        self.progress.config(mode="indeterminate", value=0)
        self.search_btn.config(state="normal")
        self.cancel_btn.config(state="disabled")

    @staticmethod
    def _size_str(specs):
        """Compact physical-size label from a mined dimensions dict, else '—'."""
        d = specs.get("dimensions") if isinstance(specs, dict) else None
        if not isinstance(d, dict):
            return "—"
        unit = d.get("unit", "")
        vals = [d.get("l"), d.get("w"), d.get("h")]
        vals = [f"{v:g}" for v in vals if isinstance(v, (int, float))]
        if vals:
            return "×".join(vals) + (f" {unit}" if unit else "")
        return d.get("raw", "—") or "—"

    def _row_values(self, c):
        """Build the (values tuple, tier tag) for one candidate row."""
        s = c.get("specs", {})
        iface = {"connectorized": "conn", "smt": "SMT", "die": "die",
                 "flange": "flange"}.get(s.get("mount_type"), "?")
        snp = self._snp_label(s)
        size = self._size_str(s)
        sp = {"qualified": "space-qual", "hi_rel": "hi-rel",
              "qualifiable": "upscreen?"}.get(s.get("space"), "—")
        pv = s.get("price_usd")
        if isinstance(pv, (int, float)):
            price = f"${pv:,.2f}" if pv < 10 else f"${pv:,.0f}"
        else:
            price = "RFQ"
        lw = s.get("lead_weeks")
        lead = "in stock" if lw == 0 else (f"~{lw:g} wk" if lw else "?")
        crit = self._crit_str(c)
        note_summary = self._note_summary(c)
        values = (f"{c.get('tier', '?')} {c.get('fit_score', 0):d}/100",
                  c.get("vendor", "?"), c.get("title", "?"), iface, snp, size, sp,
                  crit, price, lead, note_summary)
        return values, c.get("tier", "B")

    def _show_results(self, ranked, errors):
        self.last_ranked = ranked
        self.last_errors = errors
        # Final render replaces any rows streamed during extraction.
        self.tree.delete(*self.tree.get_children())
        self.row_url.clear()
        self.row_candidate.clear()
        try:
            top = int(float(self.vars["top"].get()))
        except ValueError:
            top = 250
        for c in ranked[:top]:
            values, tag = self._row_values(c)
            iid = self.tree.insert("", "end", tags=(tag,), values=values)
            self.row_url[iid] = c["url"]
            self.row_candidate[iid] = c
        n = len(ranked)
        msg = f"{n} ranked option(s); showing top {min(top, n)}."
        if errors:
            msg += f"  No results from: {', '.join(errors)}."
        self.status.set(msg if n else "No matches. Try loosening criteria.")

    @staticmethod
    def _snp_label(specs):
        """Column label showing whether the part has an associated S-parameter
        (.sNp) file. 'S4P' etc. when known, '—' when none is on record.

        Presence is decided by the background Mini-Circuits scan overlay: a
        sparams filename/url, or a port count sourced from the S-parameter scan.
        """
        if not isinstance(specs, dict):
            return "—"
        ports = specs.get("ports")
        src = specs.get("ports_source")
        ref = specs.get("sparams_filename") or specs.get("sparams_url") or ""
        has_snp = bool(ref) or src == "background_product_page_snp"
        if not has_snp:
            return "—"
        if isinstance(ports, int) and not isinstance(ports, bool) and ports > 0:
            return f"S{ports}P"
        m = re.search(r"[sS](\d{1,2})[pP]", str(ref))
        return f"S{int(m.group(1))}P" if m else "S?P"

    @staticmethod
    def _crit_str(c):
        order = ["category", "freq", "cryo", "gain", "noise", "atten", "imp", "ports",
                 "connector", "bulkhead", "pkg", "space", "lead"]
        marks = {"met": "✓", "miss": "✗", "unknown": "?"}
        crit = c.get("criteria", {})
        return " ".join(f"{k}{marks[crit[k]]}" for k in order if k in crit)


    @staticmethod
    def _missing_spec_notes(c):
        """Return human-readable reasons for unknown or unavailable specs."""
        specs = c.get("specs", {})
        criteria = c.get("criteria", {})
        vendor = c.get("vendor", "the vendor")
        notes = []

        reason_map = {
            "freq": "Frequency range was not found in structured catalog fields, product text, or the available datasheet.",
            "ports": "RF port count could not be inferred from topology, ways/throws fields, part-family naming, or RF-only documentation.",
            "pkg": "Interface type could not be identified as connectorized, SMT, die, or flange from package/interface metadata.",
            "connector": "RF connector type was not stated clearly in the product metadata or extracted product content.",
            "imp": "Impedance was not stated in the structured data, product description, part number, or datasheet text.",
            "gain": "Gain was not found in the available product specifications.",
            "noise": "Noise temperature was not found or could not be converted from the available data.",
            "atten": "Attenuation was not found in the available product specifications.",
            "cryo": "Cryogenic suitability was not explicitly stated, so it cannot be verified automatically.",
            "bulkhead": "Bulkhead mounting was not explicitly stated.",
            "space": "Space qualification was not explicitly stated; hi-rel evidence is only partial and does not prove full space qualification.",
            "lead": "Lead time or stock status was not published in a usable form.",
            "category": "The page title and URL did not provide enough evidence to verify the requested category.",
        }
        for key, state in criteria.items():
            if state == "unknown":
                notes.append(reason_map.get(key, f"{key} could not be verified from the available source data."))

        # Surface important displayed fields even when the user did not request them.
        if not specs.get("mount_type") or specs.get("mount_type") == "unknown":
            msg = reason_map["pkg"]
            if msg not in notes:
                notes.append(msg)
        if specs.get("ports") is None:
            msg = reason_map["ports"]
            if msg not in notes:
                notes.append(msg)
        if not specs.get("freq_ghz"):
            msg = reason_map["freq"]
            if msg not in notes:
                notes.append(msg)

        if specs.get("space") == "hi_rel":
            partial = "Partially missing: space. Hi-rel evidence was found, but explicit space qualification was not."
            if partial not in notes:
                notes.append(partial)

        ref = specs.get("sparams_filename") or specs.get("sparams_url")
        has_snp = bool(ref) or specs.get("ports_source") == "background_product_page_snp"

        if specs.get("_error"):
            notes.insert(0, f"Extraction error reported by {vendor}: {specs['_error']}")
        att = specs.get("attenuation_db")
        if isinstance(att, (int, float)):
            notes.append(f"Attenuation on record: {att:g} dB.")
        dims = specs.get("dimensions")
        if isinstance(dims, dict) and dims.get("raw"):
            notes.append(f"Physical size found: {dims['raw']}.")
        if not notes:
            notes.append("No missing requested specifications were detected. Verify critical values against the vendor datasheet.")
        if has_snp:
            ports = specs.get("ports")
            pstr = (f" ({ports}-port)"
                    if isinstance(ports, int) and not isinstance(ports, bool) and ports > 0
                    else "")
            notes.append(
                f"S-parameters on file{pstr}: {ref or 'available'} — from the background "
                f"Mini-Circuits scan.")
        return notes

    @classmethod
    def _note_summary(cls, c):
        notes = cls._missing_spec_notes(c)
        if notes and notes[0].startswith("No missing"):
            return "✓ Specs found"
        labels = []
        joined = " ".join(notes).lower()
        for label, token in (("frequency", "frequency"), ("RF ports", "port count"),
                             ("interface", "interface type"), ("connector", "connector type"),
                             ("impedance", "impedance"), ("lead", "lead time"),
                             ("space", "space qualification")):
            if token in joined:
                labels.append(label)
        if "partially missing: space" in joined:
            labels.append("space (partial)")
        # Preserve order while removing duplicates.
        labels = list(dict.fromkeys(labels))
        return "⚠ Missing: " + ", ".join(labels[:4]) if labels else "⚠ Review notes"

    def _set_notes(self, text):
        self.notes_text.config(state="normal")
        self.notes_text.delete("1.0", "end")
        self.notes_text.insert("1.0", text)
        self.notes_text.config(state="disabled")

    def _show_selected_notes(self, _event=None):
        sel = self.tree.selection()
        if not sel:
            return
        candidate = self.row_candidate.get(sel[0])
        if not candidate:
            return
        notes = self._missing_spec_notes(candidate)
        title = candidate.get("title", "Unknown part")
        detail = title + "\n\n" + "\n".join(f"• {note}" for note in notes)
        self._set_notes(detail)

    # ---- row actions ----------------------------------------------------
    def _selected_url(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("No selection", "Select a row first.")
            return None
        return self.row_url.get(sel[0])

    def open_url(self):
        url = self._selected_url()
        if url:
            webbrowser.open(url)  # opens the user's normal browser to a vendor page

    def _open_web_url(self):
        sel = self.web_tree.selection()
        if sel:
            url = self.web_row_url.get(sel[0])
            if url:
                webbrowser.open(url)

    def _show_web(self, web):
        """Render unverified web-search links (shown only when there was no strong
        database match). Double-clicking a row opens it in the browser."""
        self.web_tree.delete(*self.web_tree.get_children())
        self.web_row_url.clear()
        for c in web or []:
            iid = self.web_tree.insert(
                "", "end",
                values=(c.get("source", ""), c.get("title", ""),
                        "yes" if c.get("snp") else ""))
            self.web_row_url[iid] = c.get("url")
        if web:
            self.status.set(
                self.status.get() + f"  |  {len(web)} web link(s), unverified.")

    def copy_url(self):
        url = self._selected_url()
        if url:
            self.clipboard_clear()
            self.clipboard_append(url)
            self.status.set("URL copied to clipboard.")

    def debug_selected(self):
        """Show the selected candidate, cached database row, and source record."""
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("No selection", "Select a result first.")
            return
        candidate = self.row_candidate.get(sel[0])
        if not candidate:
            messagebox.showerror("Debug unavailable", "The selected result is no longer available.")
            return

        vendor = candidate.get("vendor", "?")
        url = candidate.get("url", "")
        store = specstore.load(vendor)
        cache_row = store.get(url)
        source_type = "local structured catalog" if candidate.get("_catalog") else "vendor product page / datasheet"
        payload = {
            "source_type": source_type,
            "vendor": vendor,
            "title": candidate.get("title"),
            "product_url": url,
            "datasheet_url": candidate.get("specs", {}).get("datasheet"),
            "catalog_data_file": candidate.get("_catalog_data_file"),
            "catalog_database_entry": candidate.get("_catalog_record"),
            "candidate_text_used_for_extraction": candidate.get("text"),
            "structured_specs_before_merge": candidate.get("_specs"),
            "final_extracted_specs": candidate.get("specs"),
            "ranking_criteria": candidate.get("criteria"),
            "rank": {
                "tier": candidate.get("tier"),
                "score": candidate.get("fit_score"),
                "met": candidate.get("met"),
                "miss": candidate.get("miss"),
                "unknown": candidate.get("unknown"),
            },
            "spec_cache_file": str(specstore._path(vendor)),
            "spec_cache_entry": cache_row,
        }
        if vendor == "Mini-Circuits":
            payload["minicircuits_background_cache"] = minicircuits_cache.cached_for(candidate)

        win = tk.Toplevel(self.winfo_toplevel())
        win.title(f"Debug: {candidate.get('title', 'selected part')}")
        win.geometry("920x680")
        win.transient(self.winfo_toplevel())
        win.rowconfigure(0, weight=1)
        win.columnconfigure(0, weight=1)

        text = tk.Text(win, wrap="none", padx=8, pady=8)
        text.grid(row=0, column=0, sticky="nsew")
        ybar = ttk.Scrollbar(win, orient="vertical", command=text.yview)
        ybar.grid(row=0, column=1, sticky="ns")
        xbar = ttk.Scrollbar(win, orient="horizontal", command=text.xview)
        xbar.grid(row=1, column=0, sticky="ew")
        text.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
        text.insert("1.0", json.dumps(payload, indent=2, ensure_ascii=False, default=str))
        text.config(state="disabled")

        buttons = ttk.Frame(win, padding=8)
        buttons.grid(row=2, column=0, columnspan=2, sticky="ew")

        def copy_debug():
            raw = json.dumps(payload, indent=2, ensure_ascii=False, default=str)
            win.clipboard_clear()
            win.clipboard_append(raw)
            self.status.set("Selected-part debug data copied.")

        ttk.Button(buttons, text="Copy debug data", command=copy_debug).pack(side="left")
        if url:
            ttk.Button(buttons, text="Open source page",
                       command=lambda: webbrowser.open(url)).pack(side="left", padx=4)
        datasheet = candidate.get("specs", {}).get("datasheet")
        if datasheet:
            ttk.Button(buttons, text="Open datasheet",
                       command=lambda: webbrowser.open(datasheet)).pack(side="left")
        ttk.Button(buttons, text="Close", command=win.destroy).pack(side="right")

    def save_report(self):
        if not self.last_ranked:
            messagebox.showinfo("Nothing to save", "Run a search first.")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".md", filetypes=[("Markdown", "*.md"), ("All files", "*.*")],
            initialfile="rf_parts_report.md")
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

    def on_clear(self):
        for k, var in self.vars.items():
            if isinstance(var, tk.BooleanVar):
                var.set(False)
            elif k == "top":
                var.set("250")
            else:
                var.set("")
        for var in self.prefer_vars.values():
            var.set(False)
        for var in self.exclude_vars.values():
            var.set(False)
        self.vars["subcategory"].set("(any)")
        self._on_category_change()
        self.prefer_btn.config(text=self._prefer_label())
        self.exclude_btn.config(text=self._exclude_label())
        _save_prefs({"prefer": [], "exclude": []})
        self.tree.delete(*self.tree.get_children())
        self.row_url.clear()
        self.row_candidate.clear()
        self.web_tree.delete(*self.web_tree.get_children())
        self.web_row_url.clear()
        self._set_notes("Select a result to see why any values are unknown.")
        self.status.set("Cleared.")

    # ---- Mini-Circuits background-cache polling -------------------------
    def _poll_minicircuits_cache(self):
        """Every minute, check whether the standalone Mini-Circuits S-parameter
        cache changed on disk and, if so, refresh the displayed rows.

        This never blocks the Tk event loop, never starts a network request or a
        new vendor crawl, and always reschedules itself.
        """
        try:
            current = minicircuits_cache.cache_mtime_ns()
            if current != self._minicircuits_cache_mtime:
                self._minicircuits_cache_mtime = current
                minicircuits_cache.reload_if_changed(force=True)
                self._refresh_minicircuits_results()
        except Exception:
            # Polling must never crash the UI; skip this tick and try again.
            pass
        finally:
            self.after(60_000, self._poll_minicircuits_cache)

    def _refresh_minicircuits_results(self):
        """Reapply the current background cache to already-displayed Mini-Circuits
        candidates and rerank, without re-running a search.

        If a search is in flight, or there is nothing to refresh, just leave a
        status message rather than touching the results.
        """
        searching = str(self.search_btn["state"]) == "disabled"
        if searching or not self.last_ranked or not self.last_query:
            self.status.set(
                "Mini-Circuits background data updated. Run Search to refresh results.")
            return

        updated = []
        changed = False
        for c in self.last_ranked:
            c = dict(c)
            if c.get("vendor") == "Mini-Circuits":
                specs = dict(c.get("specs") or {})
                for key in ("ports", "ports_source", "sparams_url", "sparams_filename"):
                    specs.pop(key, None)
                for key, value in minicircuits_cache.cached_for(c).items():
                    if value is not None:
                        specs[key] = value
                c["specs"] = specs
                changed = True
            updated.append(c)

        if not changed:
            self.status.set(
                "Mini-Circuits cache updated; no displayed Mini-Circuits rows needed refresh.")
            return

        ranked = rank.rank(updated, self.last_query)
        # Repopulate cleanly so no duplicate rows are created.
        self.tree.delete(*self.tree.get_children())
        self.row_url.clear()
        self.row_candidate.clear()
        self._show_results(ranked, self.last_errors)
        self.status.set("Mini-Circuits background data updated; results refreshed.")


class VendorSelectDialog(tk.Toplevel):
    """Scrollable checkbox list used for preferred or excluded vendors."""

    def __init__(self, master, app, mode):
        super().__init__(master)
        self.app = app
        self.mode = mode
        is_exclude = mode == "exclude"
        self.title("Exclude vendors" if is_exclude else "Prefer vendors")
        self.geometry("320x420")
        self.transient(master)

        prompt = "Vendors to skip:" if is_exclude else "Vendors to try first:"
        ttk.Label(self, text=prompt, padding=8).pack(anchor="w")

        wrap = ttk.Frame(self)
        wrap.pack(fill="both", expand=True, padx=8)
        canvas = tk.Canvas(wrap, highlightthickness=0)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind("<Configure>",
                   lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        variables = app.exclude_vars if is_exclude else app.prefer_vars
        for name in sorted(variables, key=str.lower):
            ttk.Checkbutton(inner, text=name, variable=variables[name]).pack(
                anchor="w", pady=1)

        bar = ttk.Frame(self, padding=8)
        bar.pack(fill="x")
        ttk.Button(bar, text="Clear all", command=self._clear).pack(side="left")
        ttk.Button(bar, text="Done", command=self._done).pack(side="right")

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
        vendor = by_name.get(self.vendor.get())
        if not vendor:
            return
        parts = [c for c in self.app.last_ranked if c.get("vendor") == vendor["name"]][:5]
        to, subject, body = rfq.draft(vendor, parts, self.app.last_query)
        self.text.delete("1.0", "end")
        self.text.insert("1.0", f"To: {to}\nSubject: {subject}\n\n{body}")

    def copy(self):
        self.clipboard_clear()
        self.clipboard_append(self.text.get("1.0", "end-1c"))


def main():
    root = tk.Tk()
    root.title("rfparts — RF/microwave/cryogenic parts finder")
    root.geometry("1180x720")
    root.minsize(980, 600)
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    main()
