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


def main():
    files = [png(640, 480, (200, 100, 50)), png(1024, 768, (20, 120, 220)), png(300, 300, (0, 0, 0))]
    img = bytearray(os.urandom(65536))
    for f in files:
        img += b"\0" * (-len(img) % 512) + f + os.urandom(7000)
    tmp = tempfile.mkdtemp()
    try:
        path = os.path.join(tmp, "card.img")
        with open(path, "wb") as fh:
            fh.write(img)
        src = core.Source(path)
        out = os.path.join(tmp, "out")
        got = {hashlib.sha1(open(core.write_record(src, r, out), "rb").read()).hexdigest() for r in core.iter_scan(src)}
        src.close()
        want = {hashlib.sha1(f).hexdigest() for f in files}
        assert want <= got, f"recovered {len(want & got)} of {len(want)}"
        assert os.path.isdir(os.path.join(out, "2019年", "08月")), "year/month folders missing"
        print(f"self-test ok: {len(want)} deleted files recovered")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
