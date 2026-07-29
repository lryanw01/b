Exception in Tkinter callback
Traceback (most recent call last):
  File "C:\EngTools\Python3128\Lib\tkinter\__init__.py", line 1968, in __call__
    return self.func(*args)
           ^^^^^^^^^^^^^^^^
  File "C:\Users\lane.white\Downloads\rfparts\rfparts\gui.py", line 894, in on_rebuild
    RebuildDialog(self.winfo_toplevel(), self)
  File "C:\Users\lane.white\Downloads\rfparts\rfparts\gui.py", line 1495, in __init__
    ttk.Label(frm, text=f"Dataset, caches, datasheets, and normalized JSON: {DATA_ROOT}",
                                                                             ^^^^^^^^^
NameError: name 'DATA_ROOT' is not defined
