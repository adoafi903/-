#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
消えた写真・動画の復元ツール（無料）

ドライブを先頭から最後まで（空き領域も含めて）読み、削除されたあとも残っている
写真・動画のデータを探して、撮影した年・月ごとのフォルダに書き出します。
元のドライブには一切書き込みません（読み取り専用で開きます）。

使い方:
  Windows : start_windows.bat をダブルクリック
  Mac     : start_mac.command をダブルクリック
  直接    : python3 fukugen.py [ドライブまたはイメージ] [保存先フォルダ]
"""
import argparse
import datetime as dt
import hashlib
import html
import json
import os
import re
import struct
import subprocess
import sys
import time

VERSION = "1.0"
IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"
MB = 1 << 20
ALIGN = 4096          # raw devices need sector-aligned reads
BLOCK = 8 * MB        # read cache block
CHUNK = 32 * MB       # scan step

# ---------------------------------------------------------------- helpers

def u16(b, o):
    return (b[o] << 8) | b[o + 1]


def u32(b, o):
    return int.from_bytes(b[o:o + 4], "big")


def u64(b, o):
    return int.from_bytes(b[o:o + 8], "big")


def fmt_size(n):
    for unit, s in (("GB", 1 << 30), ("MB", MB), ("KB", 1024)):
        if n >= s:
            return f"{n / s:.1f} {unit}"
    return f"{n} B"


def fmt_eta(sec):
    if sec is None or sec != sec or sec > 360000:
        return "計算中"
    sec = int(sec)
    if sec < 60:
        return f"{sec} 秒"
    if sec < 3600:
        return f"{sec // 60} 分"
    return f"{sec // 3600} 時間 {sec % 3600 // 60} 分"


def valid_date(y, mo, d, h=0, mi=0, s=0):
    try:
        if not (1990 <= y <= dt.date.today().year + 1):
            return None
        return dt.datetime(y, mo, d, h, mi, s)
    except ValueError:
        return None


DATE_RE = re.compile(r"^(\d{4})[:\-](\d{2})[:\-](\d{2})[ T](\d{2}):(\d{2}):?(\d{2})?")


def parse_date(s):
    m = DATE_RE.match(s or "")
    if not m:
        return None
    return valid_date(*(int(x or 0) for x in m.groups()))


def parse_tiff(b, t, lim):
    """Read DateTimeOriginal and camera model from an EXIF TIFF block."""
    lim = min(lim, len(b))
    if t + 8 > lim:
        return {}
    if b[t:t + 2] == b"II":
        e = "<"
    elif b[t:t + 2] == b"MM":
        e = ">"
    else:
        return {}

    def r16(o):
        return struct.unpack_from(e + "H", b, o)[0]

    def r32(o):
        return struct.unpack_from(e + "I", b, o)[0]

    def ifd(off, into):
        if off < 8 or t + off + 2 > lim:
            return
        base = t + off
        n = r16(base)
        if n > 300:
            return
        for k in range(n):
            en = base + 2 + k * 12
            if en + 12 > lim:
                break
            tag, typ, cnt = r16(en), r16(en + 2), r32(en + 4)
            if typ == 2:
                vo = en + 8 if cnt <= 4 else t + r32(en + 8)
                if vo + cnt <= lim:
                    into[tag] = b[vo:vo + min(cnt, 64)].split(b"\0")[0].decode("ascii", "replace").strip()
            elif typ == 4:
                into[tag] = r32(en + 8)
            elif typ == 3:
                into[tag] = r16(en + 8)

    t0, ex = {}, {}
    try:
        ifd(r32(t + 4), t0)
        if isinstance(t0.get(0x8769), int):
            ifd(t0[0x8769], ex)
    except struct.error:
        pass
    date = parse_date(ex.get(0x9003)) or parse_date(ex.get(0x9004)) or parse_date(t0.get(0x0132))
    make, model = t0.get(0x010F) or "", t0.get(0x0110) or ""
    if make and model.lower().startswith(make.split()[0].lower()):
        make = ""
    return {"date": date, "model": " ".join(x for x in (make, model) if x)}


# ---------------------------------------------------------------- source

def _win_size(f):
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class LengthInfo(ctypes.Structure):
        _fields_ = [("Length", ctypes.c_longlong)]

    out, ret = LengthInfo(), wintypes.DWORD()
    ok = ctypes.windll.kernel32.DeviceIoControl(
        wintypes.HANDLE(msvcrt.get_osfhandle(f.fileno())), 0x7405C, None, 0,
        ctypes.byref(out), ctypes.sizeof(out), ctypes.byref(ret), None)
    return out.Length if ok else 0


def _mac_size(f):
    import fcntl
    try:
        cnt = struct.unpack("Q", fcntl.ioctl(f.fileno(), 0x40086419, b"\0" * 8))[0]
        bs = struct.unpack("I", fcntl.ioctl(f.fileno(), 0x40046418, b"\0" * 4))[0]
        return cnt * bs
    except OSError:
        return 0


class Source:
    """Read-only, sector-aligned random access to a drive or image file."""

    def __init__(self, path):
        self.path = path
        self.f = open(path, "rb", buffering=0)
        self.size = self._size()
        self.bad = 0
        self._ca, self._cb = -1, b""

    def _size(self):
        if os.path.isfile(self.path):
            return os.path.getsize(self.path)
        s = 0
        try:
            s = _win_size(self.f) if IS_WIN else _mac_size(self.f) if IS_MAC else 0
        except Exception:
            s = 0
        if not s:
            try:
                s = self.f.seek(0, 2)
            except OSError:
                s = 0
        return s

    def _raw(self, a, n):
        try:
            self.f.seek(a)
            out = bytearray()
            while len(out) < n:
                d = self.f.read(n - len(out))
                if not d:
                    break
                out += d
            return bytes(out)
        except OSError:
            out = bytearray()
            for p in range(a, a + n, 512):
                k = min(512, a + n - p)
                try:
                    self.f.seek(p)
                    d = self.f.read(k) or b""
                except OSError:
                    d = b""
                    self.bad += 1
                out += d.ljust(k, b"\0")
            return bytes(out)

    def read_at(self, off, n):
        if off >= self.size or n <= 0:
            return b""
        n = min(n, self.size - off)
        if self._ca >= 0 and off >= self._ca and off + n <= self._ca + len(self._cb):
            s = off - self._ca
            return self._cb[s:s + n]
        a = off // ALIGN * ALIGN
        e = min(self.size, -(-(off + max(n, BLOCK)) // ALIGN) * ALIGN)
        self._ca, self._cb = a, self._raw(a, e - a)
        s = off - a
        return self._cb[s:s + n]

    def close(self):
        self.f.close()


# ---------------------------------------------------------------- carving

MARKER = re.compile(rb"\xff[^\x00\xd0-\xd7\xff]")
SEGMENT_MARKERS = {0xC4, 0xDA, 0xDB, 0xDD, 0xFE} | set(range(0xE0, 0xF0))
MP4_TOP = {b"ftyp", b"moov", b"mdat", b"free", b"skip", b"wide", b"uuid", b"pnot", b"meta",
           b"moof", b"mfra", b"sidx", b"styp", b"pdin", b"prft", b"emsg", b"junk", b"PICT", b"idat"}
VIDEO_BRANDS = re.compile(rb"^(isom|iso[2-9]|mp4[12]|avc1|qt  |M4V |3gp[4-6]|3g2a|MSNV|XAVC|mmp4|dash|f4v |NDAS|CAEP|niko|MPPI|kddi|FACE|caqv)")
HEIF_BRANDS = re.compile(rb"^(heic|heix|heim|heis|hevc|mif1|msf1|avif|avis)")


def jpeg_header(b):
    """Walk JPEG markers up to SOS. Returns dict or None."""
    p, sof, meta = 2, None, {}
    for _ in range(2000):
        if p >= len(b) or b[p] != 0xFF:
            return None
        while p < len(b) and b[p] == 0xFF:
            p += 1
        if p + 3 > len(b):
            return None
        m = b[p]
        p += 1
        if m == 0xD8 or m == 0x01 or 0xD0 <= m <= 0xD7:
            continue
        if m in (0x00, 0xD9):
            return None
        ln = u16(b, p)
        if ln < 2 or p + ln > len(b):
            return None
        if 0xC0 <= m <= 0xCF and m not in (0xC4, 0xC8, 0xCC) and ln >= 8:
            sof = (u16(b, p + 5), u16(b, p + 3))
        if m == 0xE1 and b[p + 2:p + 6] == b"Exif" and not meta:
            meta = parse_tiff(b, p + 8, p + ln)
        p += ln
        if m == 0xDA:
            if not sof or sof[0] < 16 or sof[1] < 16:
                return None
            return {"sos": p, "w": sof[0], "h": sof[1], **meta}
    return None


def jpeg_end(src, p, limit):
    """Find the EOI marker after the scan data. Returns (end, truncated)."""
    limit = min(limit, src.size)
    while p < limit:
        b = src.read_at(p, 4 * MB)
        if len(b) < 2:
            break
        m = MARKER.search(b)
        if not m:
            p += len(b) - 1
            continue
        i = m.start()
        mk, ai = b[i + 1], p + i
        if mk == 0xD9:
            return ai + 2, False
        if mk in SEGMENT_MARKERS:
            if i + 4 > len(b):
                p = ai
                if len(b) < 8:
                    break
                continue
            p = ai + 2 + u16(b, i + 2)
            continue
        return ai, True
    return min(p, limit), True


def carve_jpeg(src, s):
    hdr = jpeg_header(src.read_at(s, 512 * 1024))
    if not hdr:
        return None
    end, trunc = jpeg_end(src, s + hdr["sos"], s + 256 * MB)
    if end - s < 2048:
        return None
    return {"kind": "image", "ext": "jpg", "type": "JPEG", "s": s, "e": end, "trunc": trunc,
            "w": hdr["w"], "h": hdr["h"], "date": hdr.get("date"), "model": hdr.get("model", "")}


def carve_png(src, s):
    h = src.read_at(s, 33)
    if len(h) < 33 or h[12:16] != b"IHDR":
        return None
    w, hh = u32(h, 16), u32(h, 20)
    if not (0 < w <= 65535 and 0 < hh <= 65535):
        return None
    p, date, trunc = s + 8, None, True
    for _ in range(500000):
        c = src.read_at(p, 8)
        if len(c) < 8:
            break
        ln, t = u32(c, 0), c[4:8]
        if not t.isalpha() or ln > 0x7FFFFFFF or p + 12 + ln > src.size or p + 12 + ln - s > 512 * MB:
            break
        if t == b"tIME" and ln == 7:
            d = src.read_at(p + 8, 7)
            date = valid_date(u16(d, 0), d[2], d[3], d[4], d[5], d[6])
        elif t == b"eXIf" and ln < 65536 and not date:
            date = parse_tiff(src.read_at(p + 8, ln), 0, ln).get("date")
        p += 12 + ln
        if t == b"IEND":
            trunc = False
            break
    if p - s < 128:
        return None
    return {"kind": "image", "ext": "png", "type": "PNG", "s": s, "e": p, "trunc": trunc, "w": w, "h": hh, "date": date}


def carve_gif(src, s):
    b = src.read_at(s, 32 * MB)
    if len(b) < 13:
        return None
    w, h, f = b[6] | b[7] << 8, b[8] | b[9] << 8, b[10]
    p, frames = 13, 0
    if f & 0x80:
        p += 3 * (2 << (f & 7))

    def sub(p):
        while p < len(b):
            n = b[p]
            p += 1
            if n == 0:
                return p
            p += n
        return -1

    end, trunc = None, True
    for _ in range(200000):
        if p >= len(b):
            break
        x = b[p]
        if x == 0x3B:
            end, trunc = p + 1, False
            break
        if x == 0x21:
            q = sub(p + 2)
        elif x == 0x2C:
            if p + 10 > len(b):
                break
            lf = b[p + 9]
            q = p + 10 + (3 * (2 << (lf & 7)) if lf & 0x80 else 0) + 1
            q = sub(q)
            frames += 1
        else:
            break
        if q < 0:
            break
        p = q
    if not frames or not w or not h:
        return None
    return {"kind": "image", "ext": "gif", "type": "GIF", "s": s, "e": s + (end or p), "trunc": trunc, "w": w, "h": h, "date": None}


def mvhd_date(mb):
    i = mb.find(b"mvhd")
    if i < 0 or i + 16 > len(mb):
        return None
    sec = u64(mb, i + 8) if mb[i + 4] == 1 else u32(mb, i + 8)
    if not sec:
        return None
    try:
        d = dt.datetime.fromtimestamp(sec - 2082844800)
    except (OverflowError, OSError, ValueError):
        return None
    return d if 1995 <= d.year <= dt.date.today().year + 1 else None


def heif_meta(b):
    w = h = 0
    i = b.find(b"ispe")
    while i >= 0 and i + 16 <= len(b):
        W, H = u32(b, i + 8), u32(b, i + 12)
        if 0 < W < 100000 and 0 < H < 100000 and W * H > w * h:
            w, h = W, H
        i = b.find(b"ispe", i + 4)
    date = None
    for pat in (b"MM\x00*", b"II*\x00"):
        x = b.find(pat)
        while x >= 0 and not date:
            date = parse_tiff(b, x, len(b)).get("date")
            x = b.find(pat, x + 4)
    return w, h, date


def carve_isobmff(src, s):
    h = src.read_at(s, 16)
    if len(h) < 16 or h[4:8] != b"ftyp":
        return None
    brand = h[8:12]
    heif = bool(HEIF_BRANDS.match(brand))
    if not heif and not VIDEO_BRANDS.match(brand):
        return None
    if not 16 <= u32(h, 0) <= 512:
        return None
    p, boxes, trunc = s, {}, False
    for _ in range(2000):
        c = src.read_at(p, 16)
        if len(c) < 8:
            break
        bs, t, hd = u32(c, 0), c[4:8], 8
        if t not in MP4_TOP:
            break
        if bs == 1 and len(c) >= 16:
            bs, hd = u64(c, 8), 16
        if bs < hd:
            break
        if p + bs > src.size:
            if t == b"mdat" and b"moov" in boxes:
                boxes[t] = (p, src.size - p)
                p, trunc = src.size, True
            break
        boxes.setdefault(t, (p, bs))
        p += bs
    if heif:
        if b"meta" not in boxes:
            return None
        mp, ms = boxes[b"meta"]
        w, hh, _ = heif_meta(src.read_at(mp, min(ms, 4 * MB)))
        _, _, date = heif_meta(src.read_at(s, min(p - s, 4 * MB)))
        return {"kind": "image", "ext": "heic", "type": "HEIC", "s": s, "e": p, "trunc": trunc, "w": w, "h": hh, "date": date}
    if b"mdat" not in boxes:
        return None
    ext = "mov" if brand == b"qt  " else "mp4"
    if b"moov" not in boxes:
        return {"kind": "noindex", "ext": ext, "type": "MP4/MOV", "s": s, "e": p, "trunc": True, "w": 0, "h": 0, "date": None}
    mp, ms = boxes[b"moov"]
    date = mvhd_date(src.read_at(mp, min(ms, 32 * MB)))
    return {"kind": "video", "ext": ext, "type": "MP4/MOV", "s": s, "e": p, "trunc": trunc, "w": 0, "h": 0, "date": date}


SIGNATURES = [
    (b"\xff\xd8\xff", 0, carve_jpeg),
    (b"\x89PNG\r\n\x1a\n", 0, carve_png),
    (b"GIF87a", 0, carve_gif),
    (b"GIF89a", 0, carve_gif),
    (b"ftyp", 4, carve_isobmff),
]


# ---------------------------------------------------------------- output

def record_folder(root, r):
    d = r.get("date")
    if r["kind"] == "noindex":
        return os.path.join(root, "索引が無い動画")
    if d:
        return os.path.join(root, f"{d.year}年", f"{d.month:02d}月")
    return os.path.join(root, "日付不明", "動画" if r["kind"] == "video" else "写真・画像")


def record_name(r):
    d = r.get("date")
    name = (d.strftime("%Y-%m-%d_%H%M%S_") if d else "") + f"{r['s']:012X}"
    if r["trunc"] and r["kind"] != "noindex":
        name += "_一部欠損"
    return f"{name}.{r['ext']}"


def record_tail(r):
    """Bytes appended to a truncated file so viewers can open what is left."""
    if not r["trunc"]:
        return b""
    return {"jpg": b"\xff\xd9", "png": b"\0\0\0\0IEND\xaeB`\x82", "gif": b"\x3b"}.get(r["ext"], b"")


def write_record(src, r, root, on_bytes=None):
    """Copy one recovered item out of the drive into root/<year>/<month>/. Returns the path."""
    folder = record_folder(root, r)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, record_name(r))
    with open(path, "wb") as f:
        p = r["s"]
        while p < r["e"]:
            n = min(BLOCK, r["e"] - p)
            f.write(src.read_at(p, n))
            p += n
            if on_bytes:
                on_bytes(n)
        f.write(record_tail(r))
    return path


def record_key(src, r):
    size = r["e"] - r["s"]
    return (size, hashlib.sha1(src.read_at(r["s"], min(size, MB))).hexdigest())


class Output:
    def __init__(self, root, keep_small):
        self.root = root
        self.keep_small = keep_small
        self.records = []
        self.seen = set()
        self.counts = {"image": 0, "video": 0, "noindex": 0, "small": 0, "dup": 0, "partial": 0}

    def save(self, src, r):
        size = r["e"] - r["s"]
        if r["kind"] == "image" and r.get("w") and max(r["w"], r["h"]) < 256 and not self.keep_small:
            self.counts["small"] += 1
            return
        if r["kind"] == "noindex" and size < MB:
            return
        key = record_key(src, r)
        if key in self.seen:
            self.counts["dup"] += 1
            return
        self.seen.add(key)
        d = r.get("date")
        path = write_record(src, r, self.root)
        self.counts[r["kind"]] += 1
        if r["trunc"] and r["kind"] != "noindex":
            self.counts["partial"] += 1
        self.records.append({"path": os.path.relpath(path, self.root), "kind": r["kind"], "type": r["type"],
                             "date": d.isoformat(" ") if d else "", "w": r.get("w") or 0, "h": r.get("h") or 0,
                             "size": size, "offset": r["s"], "partial": bool(r["trunc"]), "model": r.get("model", "")})

    def write_index(self, source_label, scanned, total, seconds, bad):
        recs = sorted(self.records, key=lambda x: x["date"] or "0", reverse=True)
        groups = {}
        for r in recs:
            if r["kind"] == "noindex":
                g = "索引が無い動画"
            else:
                g = r["date"][:7].replace("-", "年") + "月" if r["date"] else "日付不明"
            groups.setdefault(g, []).append(r)
        rows = []
        for g, items in groups.items():
            tiles = []
            for r in items:
                src = html.escape(r["path"].replace(os.sep, "/"))
                cap = html.escape((r["date"][:16] if r["date"] else "日付不明") + " · " + fmt_size(r["size"]) + (" · 一部欠損" if r["partial"] else ""))
                if r["kind"] == "image" and r["type"] != "HEIC":
                    media = f'<img loading="lazy" src="{src}" alt="">'
                elif r["kind"] == "video":
                    media = f'<video preload="metadata" src="{src}#t=0.5" muted></video>'
                else:
                    media = f'<span>{html.escape(r["type"])}</span>'
                tiles.append(f'<a class="t" href="{src}" target="_blank">{media}<small>{cap}</small></a>')
            rows.append(f'<h2>{html.escape(g)} <small>{len(items)} 件</small></h2><div class="g">{"".join(tiles)}</div>')
        page = f"""<!doctype html><html lang="ja"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>復元結果</title><style>
body{{font-family:system-ui,"Hiragino Sans","Yu Gothic UI",Meiryo,sans-serif;background:#EEF2F4;color:#14212B;margin:0;padding:24px 16px}}
main{{max-width:1100px;margin:0 auto}} h1{{margin:0 0 4px}} p{{color:#556672;margin:0 0 20px}}
h2{{font-size:20px;border-bottom:1px solid #D5DEE3;padding-bottom:4px;margin:28px 0 10px}} h2 small{{font-size:13px;color:#556672;font-weight:400}}
.g{{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px}}
.t{{background:#fff;border-radius:8px;overflow:hidden;text-decoration:none;color:#556672;box-shadow:0 0 0 1px #D5DEE3;display:flex;flex-direction:column}}
.t img,.t video,.t span{{width:100%;aspect-ratio:1;object-fit:cover;display:flex;align-items:center;justify-content:center;background:#E3E9EC}}
.t small{{padding:4px 6px;font-size:11px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
</style><main><h1>復元結果</h1>
<p>{html.escape(source_label)} · {fmt_size(scanned)} / {fmt_size(total)} を {int(seconds)} 秒で調べました ·
写真・画像 {self.counts['image']} 件、動画 {self.counts['video']} 件{f"、読めない場所 {bad} か所" if bad else ""}</p>
{"".join(rows) or "<p>見つかりませんでした。</p>"}</main></html>"""
        with open(os.path.join(self.root, "復元結果を見る.html"), "w", encoding="utf-8") as f:
            f.write(page)
        with open(os.path.join(self.root, "復元リスト.json"), "w", encoding="utf-8") as f:
            json.dump(recs, f, ensure_ascii=False, indent=1)


# ---------------------------------------------------------------- scan

def iter_scan(src, all_offsets=False, stop=None, on_progress=None):
    """Walk the whole drive and yield every photo/video found (dicts with s, e, kind, ext, date...)."""
    pos, next_free = 0, 0
    while pos < src.size and not (stop and stop.is_set()):
        buf = src.read_at(pos, CHUNK + 16)
        if not buf:
            break
        hits = []
        lim = min(CHUNK, len(buf))
        for sig, back, fn in SIGNATURES:
            i = buf.find(sig)
            while 0 <= i < lim:
                s = pos + i - back
                if s >= 0 and (all_offsets or s % 512 == 0):
                    hits.append((s, fn))
                i = buf.find(sig, i + 1)
        hits.sort(key=lambda x: x[0])
        for s, fn in hits:
            if s < next_free or (stop and stop.is_set()):
                continue
            try:
                r = fn(src, s)
            except Exception:
                r = None
            if r and r["e"] > s:
                next_free = max(next_free, r["e"])
                yield r
        pos += lim
        if next_free > pos:
            pos = next_free // 512 * 512
        if on_progress:
            on_progress(min(pos, src.size))


def scan(src, out, all_offsets, label):
    t0, last = time.time(), 0
    state = {"pos": 0}
    stopped = False

    def progress(final=False):
        el = time.time() - t0
        done = min(state["pos"], src.size)
        speed = done / el if el > 0 else 0
        eta = (src.size - done) / speed if speed else None
        pct = done / src.size * 100 if src.size else 0
        c = out.counts
        line = (f"  {pct:5.1f}%  {fmt_size(done)} / {fmt_size(src.size)}  {fmt_size(speed)}/秒  "
                f"残り約 {fmt_eta(eta) if not final else '0 秒'}  見つかった: 写真 {c['image']}  動画 {c['video']}")
        sys.stdout.write("\r" + line + "   ")
        sys.stdout.flush()

    def on_progress(p):
        nonlocal last
        state["pos"] = p
        if time.time() - last > 0.5:
            last = time.time()
            progress()

    try:
        for r in iter_scan(src, all_offsets, on_progress=on_progress):
            out.save(src, r)
    except KeyboardInterrupt:
        stopped = True
    progress(final=not stopped)
    print()
    return min(state["pos"], src.size), time.time() - t0, stopped


# ---------------------------------------------------------------- drives

def run(cmd):
    try:
        flags = 0x08000000 if IS_WIN else 0  # CREATE_NO_WINDOW: no console flash from the app
        return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
                              creationflags=flags).stdout
    except Exception:
        return ""


def list_drives():
    items = []
    if IS_WIN:
        ps = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
              "$v=Get-CimInstance Win32_LogicalDisk|Select-Object DeviceID,VolumeName,Size,DriveType,FileSystem;"
              "$d=Get-CimInstance Win32_DiskDrive|Select-Object Index,Model,Size,InterfaceType,MediaType;"
              "$p=Get-Partition -ErrorAction SilentlyContinue|Where-Object DriveLetter|Select-Object DiskNumber,DriveLetter;"
              "@{v=@($v);d=@($d);p=@($p)}|ConvertTo-Json -Depth 4 -Compress")
        try:
            data = json.loads(run(["powershell", "-NoProfile", "-Command", ps]) or "{}")
        except ValueError:
            data = {}
        sysdrv = os.environ.get("SystemDrive", "C:").upper()
        letter_disk = {f"{p['DriveLetter']}:".upper(): p["DiskNumber"] for p in data.get("p") or [] if p.get("DriveLetter")}
        for v in data.get("v") or []:
            if not v.get("Size"):
                continue
            dev = v["DeviceID"].upper()
            kind = {2: "取り外し可能", 3: "内蔵・固定"}.get(v.get("DriveType"), "その他")
            note = "このパソコンの起動ドライブ" if dev == sysdrv else kind
            items.append({"label": f"{dev}  {v.get('VolumeName') or '(名前なし)'}  ({v.get('FileSystem') or '?'}, {fmt_size(int(v['Size']))})  [{note}]",
                          "path": "\\\\.\\" + dev, "disk": letter_disk.get(dev), "letters": [dev], "system": dev == sysdrv})
        for d in data.get("d") or []:
            letters = [k for k, n in letter_disk.items() if n == d.get("Index")]
            items.append({"label": f"ディスク {d.get('Index')} 全体: {d.get('Model') or ''} ({fmt_size(int(d.get('Size') or 0))}, {d.get('InterfaceType') or ''})"
                                   + (f"  [{' '.join(letters)}]" if letters else "  [ドライブ文字なし・フォーマットが壊れたカードなど]"),
                          "path": f"\\\\.\\PhysicalDrive{d.get('Index')}", "disk": d.get("Index"), "letters": letters,
                          "system": sysdrv in letters})
    elif IS_MAC:
        import plistlib
        try:
            data = plistlib.loads(subprocess.run(["diskutil", "list", "-plist"], capture_output=True, timeout=60).stdout)
        except Exception:
            data = {}
        for d in data.get("AllDisksAndPartitions", []):
            ident = d.get("DeviceIdentifier")
            try:
                info = plistlib.loads(subprocess.run(["diskutil", "info", "-plist", ident], capture_output=True, timeout=60).stdout)
            except Exception:
                info = {}
            internal = info.get("Internal", False)
            if info.get("VirtualOrPhysical") == "Virtual":
                continue
            items.append({"label": f"{ident} 全体: {info.get('MediaName', '')} ({fmt_size(d.get('Size', 0))})  [{'内蔵' if internal else '外付け・カード'}]",
                          "path": f"/dev/r{ident}", "disk": ident, "system": internal})
            for p in d.get("Partitions", []):
                items.append({"label": f"  {p.get('DeviceIdentifier')}: {p.get('VolumeName') or p.get('Content', '')} ({fmt_size(p.get('Size', 0))})",
                              "path": f"/dev/r{p.get('DeviceIdentifier')}", "disk": ident, "system": internal})
    else:
        try:
            data = json.loads(run(["lsblk", "-J", "-b", "-o", "NAME,SIZE,TYPE,MODEL,MOUNTPOINT,RM,TRAN,FSTYPE,LABEL"]) or "{}")
        except ValueError:
            data = {}

        def walk(nodes, parent=None):
            for n in nodes or []:
                if n.get("type") in ("disk", "part") and int(n.get("size") or 0) > 0:
                    root = parent or n["name"]
                    mp = n.get("mountpoint") or ""
                    items.append({"label": f"{'  ' if parent else ''}/dev/{n['name']}: {n.get('model') or n.get('label') or n.get('fstype') or ''} ({fmt_size(int(n.get('size') or 0))})"
                                           + (f"  [{mp}]" if mp else "") + ("  [取り外し可能]" if n.get("rm") in (True, "1") else ""),
                                  "path": f"/dev/{n['name']}", "disk": root, "system": mp == "/"})
                    walk(n.get("children"), root)
        walk(data.get("blockdevices"))
    return items


def same_drive(src_item, out_dir):
    """True when the output folder is on the drive being recovered."""
    if not src_item:
        return False
    out_dir = os.path.abspath(out_dir)
    if IS_WIN:
        letter = os.path.splitdrive(out_dir)[0].upper()
        return letter in (src_item.get("letters") or [])
    df = run(["df", "-P", out_dir]).strip().splitlines()
    if len(df) < 2:
        return False
    dev = df[-1].split()[0]
    if IS_MAC:
        m = re.match(r"/dev/(disk\d+)", dev)
        return bool(m) and m.group(1) == src_item.get("disk")
    parent = run(["lsblk", "-no", "PKNAME", dev]).strip() or os.path.basename(dev)
    return src_item.get("disk") in (parent, os.path.basename(dev))


def is_admin():
    if IS_WIN:
        try:
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    return os.geteuid() == 0


def relaunch_as_admin():
    script = os.path.abspath(__file__)
    if IS_WIN:
        import ctypes
        params = " ".join(f'"{a}"' for a in [script] + sys.argv[1:])
        r = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)
        if r <= 32:
            print("管理者として起動できませんでした。「はい」を選んでください。")
            pause()
        sys.exit(0)
    print("管理者のパスワードを入力してください（入力中は文字が表示されません）。")
    os.execvp("sudo", ["sudo", sys.executable, script] + sys.argv[1:])


def user_home():
    su = os.environ.get("SUDO_USER")
    if su and not IS_WIN:
        import pwd
        try:
            return pwd.getpwnam(su).pw_dir
        except KeyError:
            pass
    return os.path.expanduser("~")


def give_back_ownership(root):
    uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
    if IS_WIN or not uid:
        return
    for d, dirs, files in os.walk(root):
        for n in [d] + [os.path.join(d, x) for x in files]:
            try:
                os.chown(n, int(uid), int(gid or uid))
            except OSError:
                pass


def pause():
    if IS_WIN:
        try:
            input("\nEnter キーを押すと閉じます。")
        except EOFError:
            pass


def ask(prompt):
    try:
        return input(prompt).strip().strip('"').strip("'")
    except EOFError:
        return ""


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="消えた写真・動画の復元ツール")
    ap.add_argument("source", nargs="?", help="ドライブ（例: \\\\.\\E:  /dev/rdisk4  /dev/sdb）またはイメージファイル")
    ap.add_argument("out", nargs="?", help="保存先フォルダ")
    ap.add_argument("--all-offsets", action="store_true", help="セクタ境界以外も探す（遅いが、ほかのファイルに埋もれた画像も見つかる）")
    ap.add_argument("--keep-small", action="store_true", help="アイコンなど小さい画像も保存する")
    ap.add_argument("-y", "--yes", action="store_true", help="確認せずに始める")
    args = ap.parse_args()

    print("=" * 64)
    print(f" 消えた写真・動画の復元ツール  v{VERSION}（無料）")
    print(" 元のドライブには書き込みません。読み取るだけです。")
    print("=" * 64)

    image_mode = bool(args.source and os.path.isfile(args.source))
    if not image_mode and not is_admin():
        relaunch_as_admin()

    item = None
    source = args.source
    if not source:
        drives = list_drives()
        print("\n復元したいドライブの番号を選んでください。")
        print("（SD カードや USB メモリは、ドライブ文字が無いものも「ディスク ○ 全体」から選べます）\n")
        for i, d in enumerate(drives, 1):
            print(f"  {i:2d}) {d['label']}")
        print(f"  {len(drives) + 1:2d}) イメージファイル（.img など）を指定する")
        while True:
            a = ask("\n番号: ")
            if a.isdigit() and 1 <= int(a) <= len(drives) + 1:
                break
            print("  一覧の番号を入力してください。")
        if int(a) == len(drives) + 1:
            source = ask("イメージファイルの場所（ファイルをこの画面にドラッグしても入力できます）: ")
            image_mode = True
        else:
            item = drives[int(a) - 1]
            source = item["path"]
            if item.get("system"):
                print("\n  注意: これはパソコン本体のドライブです。動いている間も書き込みが続くため、")
                print("  消したファイルが上書きされている可能性があります。SSD は削除後すぐに中身が消える")
                print("  （TRIM）ことが多く、見つからない場合があります。")

    try:
        src = Source(source)
    except PermissionError:
        print("\nドライブを開けませんでした（アクセスが拒否されました）。")
        if IS_MAC:
            print("「システム設定」→「プライバシーとセキュリティ」→「フルディスクアクセス」で「ターミナル」を許可してから、もう一度実行してください。")
        pause()
        return 1
    except OSError as e:
        print(f"\nドライブを開けませんでした: {e}")
        pause()
        return 1
    if not src.size:
        print("\nドライブの大きさを読み取れませんでした。別のドライブを選んでください。")
        pause()
        return 1

    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M")
    desktop = os.path.join(user_home(), "Desktop")
    default_out = os.path.join(desktop if os.path.isdir(desktop) else user_home(), f"復元結果_{stamp}")
    out_dir = args.out
    if not out_dir:
        print(f"\n保存先フォルダ（Enter だけで次の場所）:\n  {default_out}")
        out_dir = ask("保存先: ") or default_out
    if same_drive(item, out_dir):
        print("\n保存先が、復元するドライブと同じです。消えたデータを上書きしてしまうため、")
        print("別のドライブ（パソコン本体や外付けドライブ）のフォルダを指定してください。")
        pause()
        return 1

    print(f"\n  調べるもの : {source}（{fmt_size(src.size)}）")
    print(f"  保存先     : {out_dir}")
    print("  途中でやめるときは Ctrl + C を押してください（それまでに見つかった分は残ります）。")
    if not args.yes and ask("\n始めますか？ [Y/n]: ").lower() in ("n", "no", "いいえ"):
        return 0
    os.makedirs(out_dir, exist_ok=True)

    print()
    out = Output(out_dir, args.keep_small)
    scanned, seconds, stopped = scan(src, out, args.all_offsets, source)
    src.close()
    out.write_index(source, scanned, src.size, seconds, src.bad)
    give_back_ownership(out_dir)

    c = out.counts
    print("\n" + ("途中で止めました。" if stopped else "完了しました。"))
    print(f"  写真・画像 {c['image']} 件、動画 {c['video']} 件を取り出しました。")
    if c["partial"]:
        print(f"  そのうち {c['partial']} 件は途中が上書きされていたため、残っている部分だけです（ファイル名に「一部欠損」）。")
    if c["noindex"]:
        print(f"  索引（moov）が無い動画 {c['noindex']} 件は「索引が無い動画」フォルダに入れました。")
        print("  同じ機種の正常な動画を参照にして、ファイル修復ラボのページで作り直せます。")
    if c["dup"]:
        print(f"  まったく同じデータの重複 {c['dup']} 件は 1 件にまとめました。")
    if c["small"]:
        print(f"  アイコンなど小さい画像 {c['small']} 件は保存していません（--keep-small で保存できます）。")
    if src.bad:
        print(f"  読めない場所が {src.bad} か所ありました。ドライブが傷んでいる可能性があります。")
    if not c["image"] and not c["video"]:
        print("  見つかりませんでした。上書きされたか、SSD の TRIM で消去された可能性があります。")
    print(f"\n  結果: {os.path.join(out_dir, '復元結果を見る.html')}")
    print("  年・月ごとのフォルダに分けて保存しています。")
    try:
        if IS_WIN:
            os.startfile(out_dir)
        elif IS_MAC:
            subprocess.run(["open", out_dir])
    except Exception:
        pass
    pause()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n中止しました。")
        pause()
