"""Windows end-to-end check: files really deleted by Windows must come back byte for byte."""
import hashlib
import io
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")


def prepare(folder):
    from PIL import Image, ImageDraw
    os.makedirs(folder, exist_ok=True)
    want = {}
    for k, (date, col) in enumerate([("2015:03:14 09:26:53", (200, 60, 60)), ("2019:08:03 14:22:10", (60, 200, 90)), ("2024:12:31 23:59:00", (60, 90, 200))]):
        im = Image.new("RGB", (1600, 1200), col)
        ImageDraw.Draw(im).ellipse([200, 200, 1200, 1000], outline=(255, 255, 255), width=12)
        ex = Image.Exif()
        ex.get_ifd(0x8769)[0x9003] = date
        b = io.BytesIO()
        im.save(b, "JPEG", quality=92, exif=ex.tobytes())
        name = f"IMG_{k + 1:04d}.JPG"
        open(os.path.join(folder, name), "wb").write(b.getvalue())
        want[name] = hashlib.sha1(b.getvalue()).hexdigest()
    video = b"\x00\x00\x00\x18ftypmp42" + os.urandom(24 * 1024 * 1024)
    open(os.path.join(folder, "VID_0001.MP4"), "wb").write(video)
    want["VID_0001.MP4"] = hashlib.sha1(video).hexdigest()
    json.dump(want, open("expected.json", "w"))
    print("prepared", sorted(want))


def check(device, expected, fs):
    import fukugen as core
    import undelete
    want = json.load(open(expected))
    src = core.Source(device)
    print(f"{device}: {core.fmt_size(src.size)}, volumes: {undelete.volumes(src)}")
    found = {r["orig"]: r for r in undelete.scan(src)}
    ok = True
    for name, sha in sorted(want.items()):
        r = found.get(name)
        if not r:
            print(f"  {name}: NOT FOUND")
            ok = False
            continue
        same = hashlib.sha1(core.read_record(src, r)).hexdigest() == sha
        pieces = len([x for x in r["runs"] if x[0] is not None]) if r["runs"] else 0
        print(f"  {name}: {r['folder']}\\{name} pieces={pieces} date={r['date']} overwritten={r['overwritten']} zeroed={r['empty']} identical={same}")
        ok = ok and same
    src.close()
    print(f"{fs} {device}: {'PASS' if ok else 'FAIL'}")
    return ok


if __name__ == "__main__":
    if sys.argv[1] == "prepare":
        prepare(sys.argv[2])
    else:
        sys.exit(0 if check(sys.argv[2], sys.argv[3], sys.argv[4]) else 1)
