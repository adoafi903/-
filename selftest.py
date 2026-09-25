"""Build a small card image with deleted photos and check the engine gets them back byte for byte."""
import hashlib
import os
import shutil
import struct
import tempfile
import zlib

import fukugen as core


def png(w, h, rgb):
    raw = b"".join(b"\0" + bytes(rgb) * w for _ in range(h))
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + \
        chunk(b"tIME", struct.pack(">HBBBBB", 2019, 8, 3, 14, 22, 10)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def make_card_image(path):
    """Write a card image whose free space holds three deleted photos. Returns their bytes."""
    files = [png(640, 480, (200, 100, 50)), png(1024, 768, (20, 120, 220)), png(300, 300, (0, 0, 0))]
    img = bytearray(os.urandom(65536))
    for f in files:
        img += b"\0" * (-len(img) % 512) + f + os.urandom(7000)
    with open(path, "wb") as fh:
        fh.write(img)
    return files


def backup_test(tmp):
    """A fake iPhone backup: photos stored under hashed names, listed in Manifest.db."""
    import iphone
    import plistlib
    import sqlite3
    bk = os.path.join(tmp, "Backup", "00008030-TEST")
    os.makedirs(bk)
    with open(os.path.join(bk, "Info.plist"), "wb") as f:
        plistlib.dump({"Device Name": "テストのiPhone"}, f)
    with open(os.path.join(bk, "Manifest.plist"), "wb") as f:
        plistlib.dump({"IsEncrypted": False}, f)
    con = sqlite3.connect(os.path.join(bk, "Manifest.db"))
    con.execute("CREATE TABLE Files (fileID TEXT, domain TEXT, relativePath TEXT, flags INTEGER, file BLOB)")
    data = png(500, 400, (10, 200, 90))
    fid = hashlib.sha1(b"IMG_0042.PNG").hexdigest()
    os.makedirs(os.path.join(bk, fid[:2]))
    with open(os.path.join(bk, fid[:2], fid), "wb") as f:
        f.write(data)
    con.execute("INSERT INTO Files VALUES (?, 'CameraRollDomain', 'Media/DCIM/100APPLE/IMG_0042.PNG', 1, NULL)", (fid,))
    con.commit()
    con.close()
    backups, _ = iphone.find_backups([os.path.dirname(bk)])
    assert backups and backups[0]["device"] == "テストのiPhone", backups
    targets = iphone.backup_targets(bk)
    assert [t["orig"] for t in targets] == ["IMG_0042.PNG"], targets
    src = core.Source(targets[0]["path"])
    found = list(core.iter_scan(src))
    src.close()
    assert len(found) == 1 and found[0]["e"] == len(data), found
    print("self-test ok: iPhone backup read with original names")


def main():
    tmp = tempfile.mkdtemp()
    try:
        path = os.path.join(tmp, "card.img")
        files = make_card_image(path)
        src = core.Source(path)
        out = os.path.join(tmp, "out")
        got = {hashlib.sha1(open(core.write_record(src, r, out), "rb").read()).hexdigest() for r in core.iter_scan(src)}
        src.close()
        want = {hashlib.sha1(f).hexdigest() for f in files}
        assert want <= got, f"recovered {len(want & got)} of {len(want)}"
        assert os.path.isdir(os.path.join(out, "2019年", "08月")), "year/month folders missing"
        print(f"self-test ok: {len(want)} deleted files recovered")
        backup_test(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
