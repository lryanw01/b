PS C:\Users\lane.white> rfparts gui
Exception in Tkinter callback
Traceback (most recent call last):
  File "C:\EngTools\Python3128\Lib\tkinter\__init__.py", line 1968, in __call__
    return self.func(*args)
           ^^^^^^^^^^^^^^^^
  File "C:\Users\lane.white\Downloads\rfparts\rfparts\gui.py", line 893, in on_rebuild
    RebuildDialog(self.winfo_toplevel(), self)
  File "C:\Users\lane.white\Downloads\rfparts\rfparts\gui.py", line 1366, in __init__
    self.erf = tk.StringVar(value=str(EVERYTHING_RF))
                                      ^^^^^^^^^^^^^
NameError: name 'EVERYTHING_RF' is not defined
