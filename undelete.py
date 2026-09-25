# -*- coding: utf-8 -*-
"""Find deleted files through the file system's own records (NTFS and exFAT).

When Windows deletes a file it only marks the file's record as free. Until that record
and the file's clusters are reused, the record still holds the name, folder, dates, size
and the exact clusters the data sits in, even when the file was stored in pieces.
"""
import datetime as dt
import os
import struct

import fukugen as core

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".heic", ".heif", ".bmp", ".tif", ".tiff", ".webp",
             ".cr2", ".cr3", ".nef", ".arw", ".dng", ".orf", ".rw2", ".raf"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".wmv", ".mts", ".m2ts", ".3gp", ".mpg", ".mpeg", ".webm", ".flv"}
MEDIA_EXT = IMAGE_EXT | VIDEO_EXT
DISK_IMAGE_EXT = {".img", ".dd", ".bin", ".raw", ".iso", ".001"}


def le16(b, o):
    return struct.unpack_from("<H", b, o)[0]


def le32(b, o):
    return struct.unpack_from("<I", b, o)[0]


def le64(b, o):
    return struct.unpack_from("<Q", b, o)[0]


def fs_kind(b):
    if b[3:11] == b"NTFS    ":
        return "NTFS"
    if b[3:11] == b"EXFAT   ":
        return "exFAT"
    return None


def volumes(src):
    """(byte offset, file system) for every NTFS/exFAT volume on a disk, volume or disk image."""
    b0 = src.read_at(0, 512)
    if len(b0) < 512:
        return []
    k = fs_kind(b0)
    if k:
        return [(0, k)]
    if b0[510:512] != b"\x55\xaa":
        return []
    starts = []
    types = [b0[0x1BE + 16 * i + 4] for i in range(4)]
    if 0xEE in types:
        for ss in (512, 4096):
            h = src.read_at(ss, 512)
            if h[:8] != b"EFI PART":
                continue
            lba, n, esz = le64(h, 72), le32(h, 80), le32(h, 84)
            if not (0 < n <= 1024 and 128 <= esz <= 1024):
                break
            ents = src.read_at(lba * ss, n * esz)
            for i in range(n):
                e = ents[i * esz:(i + 1) * esz]
                if len(e) >= 48 and e[:16] != b"\0" * 16:
                    starts.append(le64(e, 32) * ss)
            break
    else:
        for i in range(4):
            e = 0x1BE + 16 * i
            if b0[e + 4] and b0[e + 4] not in (0x05, 0x0F):
                lba = le32(b0, e + 8)
                if lba:
                    starts += [lba * 512, lba * 4096]
    out = []
    for s in starts:
        k = fs_kind(src.read_at(s, 512))
        if k and all(o != s for o, _ in out):
            out.append((s, k))
    return out


def kind_of(name):
    ext = os.path.splitext(name)[1].lower()
    return "image" if ext in IMAGE_EXT else "video" if ext in VIDEO_EXT else "other"


def filetime(ft):
    if not ft:
        return None
    try:
        d = dt.datetime.fromtimestamp(ft / 1e7 - 11644473600)
    except (OSError, OverflowError, ValueError):
        return None
    return d if 1990 <= d.year <= dt.date.today().year + 1 else None


def dostime(v):
    try:
        d = dt.datetime((v >> 25) + 1980, (v >> 21) & 15, (v >> 16) & 31, (v >> 11) & 31, (v >> 5) & 63, (v & 31) * 2)
    except ValueError:
        return None
    return d if d.year <= dt.date.today().year + 1 else None


def make_record(fs, uid, name, folder, size, extents, mtime, overwritten, resident=None):
    ext = os.path.splitext(name)[1].lower()
    start = next((o for o, _ in extents if o is not None), 0)
    return {"kind": kind_of(name), "ext": ext[1:] or "bin", "type": (ext[1:] or "file").upper(), "orig": name,
            "folder": folder, "s": start, "e": start + size, "size": size, "runs": extents, "resident": resident,
            "trunc": False, "date": None, "mtime": mtime, "overwritten": overwritten, "fs": fs, "uid": f"{fs}{uid}"}


class Bitmap:
    """Lazy lookups in a volume's cluster allocation bitmap."""

    def __init__(self, src, extents):
        self.src, self.extents, self.cache = src, extents, {}

    def allocated(self, cluster):
        byte = cluster // 8
        blk = byte // core.MB
        if blk not in self.cache:
            self.cache[blk] = read_extents(self.src, self.extents, blk * core.MB, core.MB)
        b = self.cache[blk]
        i = byte - blk * core.MB
        return i < len(b) and bool(b[i] >> (cluster % 8) & 1)


def read_extents(src, extents, pos, n):
    """Read n bytes starting at logical position pos of a file stored as [(offset or None, length)]."""
    out = bytearray()
    p = 0
    for off, ln in extents:
        if pos < p + ln and len(out) < n:
            a = max(pos, p) - p
            k = min(ln - a, n - len(out))
            out += b"\0" * k if off is None else src.read_at(off + a, k)
        p += ln
        if len(out) >= n:
            break
    return bytes(out)


# ---------------------------------------------------------------- NTFS

class NTFS:
    def __init__(self, src, off):
        b = src.read_at(off, 512)
        self.src, self.off = src, off
        self.bps = le16(b, 11)
        spc = b[13]
        self.cs = self.bps * (1 << (256 - spc) if spc > 0x80 else spc)
        c = struct.unpack_from("<b", b, 0x40)[0]
        self.rs = self.cs * c if c > 0 else 1 << -c
        rec0 = self.fixup(src.read_at(off + le64(b, 0x30) * self.cs, self.rs))
        if not rec0:
            raise ValueError("MFT not readable")
        data = next(a for a in self.attrs(rec0) if a["type"] == 0x80 and not a["name"])
        self.mft = self.extents(data["runs"], data["size"])
        self.mft_size = data["size"]
        rec6 = self.fixup(read_extents(src, self.mft, 6 * self.rs, self.rs))
        bm = next((a for a in self.attrs(rec6) if a["type"] == 0x80 and not a["name"]), None) if rec6 else None
        self.bitmap = Bitmap(src, self.extents(bm["runs"], bm["size"])) if bm and bm.get("runs") else None

    @staticmethod
    def fixup(r):
        if len(r) < 48 or r[:4] != b"FILE":
            return None
        r = bytearray(r)
        uo, uc = le16(r, 4), le16(r, 6)
        usn = bytes(r[uo:uo + 2])
        for i in range(1, uc):
            p = i * 512 - 2
            if p + 2 > len(r):
                break
            if bytes(r[p:p + 2]) != usn:
                return None
            r[p:p + 2] = r[uo + 2 * i:uo + 2 * i + 2]
        return bytes(r)

    @staticmethod
    def runlist(b):
        out, lcn, i = [], 0, 0
        while i < len(b) and b[i]:
            h = b[i]
            ls, os_ = h & 15, h >> 4
            i += 1
            if not ls or i + ls + os_ > len(b):
                break
            n = int.from_bytes(b[i:i + ls], "little")
            i += ls
            if os_:
                lcn += int.from_bytes(b[i:i + os_], "little", signed=True)
                i += os_
                out.append((lcn, n))
            else:
                out.append((None, n))
        return out

    def attrs(self, r):
        out, p = [], le16(r, 0x14)
        while p + 16 <= len(r):
            t = le32(r, p)
            if t in (0, 0xFFFFFFFF):
                break
            ln = le32(r, p + 4)
            if ln < 16 or p + ln > len(r):
                break
            nlen, noff = r[p + 9], le16(r, p + 10)
            a = {"type": t, "name": r[p + noff:p + noff + 2 * nlen].decode("utf-16le", "replace") if nlen else "", "nonres": r[p + 8]}
            if a["nonres"]:
                if le64(r, p + 0x10) == 0:
                    a["size"] = le64(r, p + 0x30)
                    a["runs"] = self.runlist(r[p + le16(r, p + 0x20):p + ln])
            else:
                co, cl = le16(r, p + 0x14), le32(r, p + 0x10)
                a["data"] = r[p + co:p + co + cl]
                a["size"] = cl
            out.append(a)
            p += ln
        return out

    def extents(self, runs, size):
        out, left = [], size
        for lcn, n in runs:
            if left <= 0:
                break
            k = min(n * self.cs, left)
            out.append((None if lcn is None else self.off + lcn * self.cs, k))
            left -= k
        return out

    def overwritten(self, runs):
        if not self.bitmap:
            return False
        checks = 0
        for lcn, n in runs:
            if lcn is None:
                continue
            for c in {lcn, lcn + n // 2, lcn + n - 1}:
                if self.bitmap.allocated(c):
                    return True
                checks += 1
            if checks > 48:
                break
        return False

    def scan(self, want_all, progress, stop):
        n = self.mft_size // self.rs
        dirs, found = {5: ("", 5)}, []
        idx = 0
        for off, ln in self.mft:
            if off is None:
                idx += ln // self.rs
                continue
            p = 0
            while p < ln and idx < n:
                if stop and stop.is_set():
                    return []
                chunk = self.src.read_at(off + p, min(4 * core.MB, ln - p))
                if not chunk:
                    break
                for k in range(0, len(chunk) - self.rs + 1, self.rs):
                    if idx >= n:
                        break
                    self.record(idx, chunk[k:k + self.rs], dirs, found, want_all)
                    idx += 1
                p += len(chunk)
                if progress:
                    progress(min(idx / n, 1))
        out = []
        for idx, name, parent, size, runs, resident, mtime in found:
            parts, cur, depth = [], parent, 0
            while cur != 5 and depth < 64:
                d = dirs.get(cur)
                if not d:
                    parts.append("（場所不明）")
                    break
                parts.append(d[0])
                cur, depth = d[1], depth + 1
            folder = "\\" + "\\".join(reversed(parts))
            ext = self.extents(runs, size) if runs is not None else []
            over = self.overwritten(runs) if runs else False
            out.append(make_record("NTFS", idx, name, folder, size, ext, mtime, over, resident))
        return out

    def record(self, idx, raw, dirs, found, want_all):
        r = self.fixup(raw)
        if not r:
            return
        flags = le16(r, 0x16)
        if le64(r, 0x20) & 0xFFFFFFFFFFFF:
            return
        fname = data = mtime = None
        for a in self.attrs(r):
            if a["type"] == 0x10 and not a["nonres"] and len(a["data"]) >= 16:
                mtime = filetime(le64(a["data"], 8))
            elif a["type"] == 0x30 and not a["nonres"] and len(a["data"]) >= 0x42:
                d = a["data"]
                nm = d[0x42:0x42 + 2 * d[0x40]].decode("utf-16le", "replace")
                if fname is None or d[0x41] != 2:
                    fname = (nm, le64(d, 0) & 0xFFFFFFFFFFFF)
            elif a["type"] == 0x80 and not a["name"]:
                data = a
        if not fname:
            return
        if flags & 2:
            dirs[idx] = fname
            return
        if flags & 1 or not data:
            return
        if not want_all and os.path.splitext(fname[0])[1].lower() not in MEDIA_EXT:
            return
        if data["nonres"]:
            if "runs" not in data or not data["size"]:
                return
            found.append((idx, fname[0], fname[1], data["size"], data["runs"], None, mtime))
        elif data["size"]:
            found.append((idx, fname[0], fname[1], data["size"], None, bytes(data["data"]), mtime))


# ---------------------------------------------------------------- exFAT

class ExFAT:
    def __init__(self, src, off):
        b = src.read_at(off, 512)
        self.src, self.off = src, off
        bps = 1 << b[0x6C]
        self.cs = bps << b[0x6D]
        self.fat = off + le32(b, 0x50) * bps
        self.heap = off + le32(b, 0x58) * bps
        self.count = le32(b, 0x5C)
        self.root = le32(b, 0x60)
        self.bitmap = None

    def coff(self, c):
        return self.heap + (c - 2) * self.cs

    def clusters(self, first, size, nofat):
        need = -(-size // self.cs) if size else None
        if nofat and need:
            return [(first, need)]
        chain, c, seen = [], first, set()
        while 2 <= c < self.count + 2 and c not in seen and len(chain) < 4_000_000:
            chain.append(c)
            seen.add(c)
            if need and len(chain) >= need:
                break
            c = le32(self.src.read_at(self.fat + c * 4, 4), 0)
        if need and len(chain) < need and chain:
            chain += list(range(chain[-1] + 1, chain[-1] + 1 + need - len(chain)))
        runs = []
        for c in chain:
            if runs and runs[-1][0] + runs[-1][1] == c:
                runs[-1] = (runs[-1][0], runs[-1][1] + 1)
            else:
                runs.append((c, 1))
        return runs

    def extents(self, runs, size):
        out, left = [], size
        for c, n in runs:
            k = min(n * self.cs, left)
            if k <= 0:
                break
            out.append((self.coff(c), k))
            left -= k
        return out

    def overwritten(self, runs):
        if not self.bitmap:
            return False
        for c, n in runs[:16]:
            for x in {c, c + n // 2, c + n - 1}:
                if self.bitmap.allocated(x - 2):
                    return True
        return False

    def scan(self, want_all, progress, stop):
        out, stack, seen = [], [(self.root, 0, False, "", False)], set()
        done = 0
        while stack:
            if stop and stop.is_set():
                break
            first, size, nofat, path, gone = stack.pop()
            if first < 2 or first in seen:
                continue
            seen.add(first)
            runs = self.clusters(first, size, nofat)
            data = read_extents(self.src, self.extents(runs, sum(n for _, n in runs) * self.cs), 0, 64 * core.MB)
            i = 0
            while i + 32 <= len(data):
                t = data[i]
                if t == 0x00:
                    break
                if t == 0x81 and self.bitmap is None and path == "":
                    blen = le64(data, i + 24)
                    self.bitmap = Bitmap(self.src, self.extents(self.clusters(le32(data, i + 20), blen, False), blen))
                if t in (0x85, 0x05):
                    sc = data[i + 1]
                    ents = data[i:i + 32 * (sc + 1)]
                    if sc < 2 or len(ents) < 32 * (sc + 1) or ents[32] & 0x7F != 0x40:
                        i += 32
                        continue
                    attr, mod = le16(ents, 4), le32(ents, 12)
                    st = ents[32:64]
                    nofat_f, nlen, fc, fsize = bool(st[1] & 2), st[3], le32(st, 20), le64(st, 24)
                    name = "".join(ents[32 * k + 2:32 * k + 32].decode("utf-16le", "replace")
                                   for k in range(2, sc + 1) if ents[32 * k] & 0x7F == 0x41)[:nlen]
                    deleted = gone or t == 0x05
                    if attr & 0x10:
                        stack.append((fc, fsize, nofat_f, path + "\\" + name, deleted))
                    elif deleted and fsize and fc >= 2 and (want_all or os.path.splitext(name)[1].lower() in MEDIA_EXT):
                        fr = self.clusters(fc, fsize, nofat_f)
                        out.append(make_record("exFAT", f"{fc}_{i}", name, path or "\\", fsize, self.extents(fr, fsize),
                                               dostime(mod), self.overwritten(fr)))
                    i += 32 * (sc + 1)
                    continue
                i += 32
            done += 1
            if progress:
                progress(min(done / (done + len(stack) + 1), 0.99))
        return out


def scan(src, want_all=False, progress=None, stop=None, on_volume=None):
    """Deleted files found through every NTFS/exFAT volume on src."""
    found = []
    for off, kind in volumes(src):
        if on_volume:
            on_volume(kind)
        try:
            fs = NTFS(src, off) if kind == "NTFS" else ExFAT(src, off)
            found += fs.scan(want_all, progress, stop)
        except (ValueError, StopIteration, struct.error, IndexError):
            continue
    for r in found:
        head = read_extents(src, r["runs"], 0, 512 * 1024) if r["runs"] else (r["resident"] or b"")[:512 * 1024]
        r["empty"] = not head.strip(b"\0")
        if head[:3] == b"\xff\xd8\xff":
            h = core.jpeg_header(head)
            if h:
                r["date"], r["w"], r["h"], r["model"] = h.get("date"), h["w"], h["h"], h.get("model", "")
        elif r["ext"].lower() in ("heic", "heif"):
            r["date"] = core.heif_meta(head)[2]
        r["date"] = r["date"] or r["mtime"]
    return found
