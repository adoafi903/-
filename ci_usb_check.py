"""Run the Windows iPhone import script on a machine with no phone: it must report that no phone was found."""
import sys
import tempfile
import threading

import iphone

sys.stdout.reconfigure(encoding="utf-8")

print("detect:", iphone.detect_usb())
try:
    iphone.import_usb(tempfile.mkdtemp(), lambda a, b: None, threading.Event(), on_wait=lambda t, s: print("wait:", s), wait=6)
    raise SystemExit("expected 'no device'")
except RuntimeError as e:
    print("ok:", e)
