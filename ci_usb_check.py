"""Run the Windows iPhone import script on a machine with no phone: it must report that no phone was found."""
import tempfile
import threading

import iphone

print("detect:", iphone.detect_usb())
try:
    iphone.import_usb(tempfile.mkdtemp(), lambda a, b: None, threading.Event())
    raise SystemExit("expected 'no device'")
except RuntimeError as e:
    print("ok:", e)
