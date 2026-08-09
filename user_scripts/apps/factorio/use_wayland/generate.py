#!/usr/bin/env python3
"""Generate install_fix.py from the template + the real shim sources.

Injects EGLFIX_C and EXPORT_MAP as Python repr() strings so the embedding is
byte-exact (no manual transcription of the 43KB C source).

If the template is missing, it is reconstructed from the current
install_fix.py (the embedded repr strings are replaced with placeholders), so
the pipeline is self-healing: edit eglfix.c or install_fix.py logic, then run
this to produce a fresh install_fix.py.
"""
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, "install_fix.template.py")
DEST = os.path.join(HERE, "install_fix.py")
SRC = os.path.join(HERE, "..", "eglfix")

with open(os.path.join(SRC, "eglfix.c")) as f:
    eglfix_c = f.read()
with open(os.path.join(SRC, "export.map")) as f:
    export_map = f.read()

if not os.path.isfile(TEMPLATE):
    # Reconstruct the template from the current installer.
    with open(DEST) as f:
        body = f.read()
    body = re.sub(r"^EGLFIX_C = '.*?^EXPORT_MAP", "EGLFIX_C = @@EGLFIX_C@@\nEXPORT_MAP",
                  body, count=1, flags=re.M | re.S)
    body = re.sub(r"^EXPORT_MAP = '.*?^# Small GL/EGL smoke test",
                  "EXPORT_MAP = @@EXPORT_MAP@@\n# Small GL/EGL smoke test",
                  body, count=1, flags=re.M | re.S)
    with open(TEMPLATE, "w") as f:
        f.write(body)
    print("reconstructed template from install_fix.py")

with open(TEMPLATE) as f:
    template = f.read()

assert "@@EGLFIX_C@@" in template and "@@EXPORT_MAP@@" in template
out = template.replace("@@EGLFIX_C@@", repr(eglfix_c)).replace("@@EXPORT_MAP@@", repr(export_map))

with open(DEST, "w") as f:
    f.write(out)
print("wrote %s (%d bytes, eglfix.c=%d, export.map=%d)"
      % (DEST, len(out), len(eglfix_c), len(export_map)))
