# -*- coding: utf-8 -*-
"""iPhone sources: backups on this computer and an iPhone connected by USB.

An iPhone's own storage is encrypted and a computer only sees the photos that are
currently in its camera roll, so deleted photos cannot be read from the phone itself.
They can still be inside a backup made before they were deleted, which is why backups
are listed alongside the phone.
"""
import datetime as dt
import os
import plistlib
import shutil
import sqlite3
import subprocess
import tempfile

import fukugen as core

MEDIA_EXT = {".jpg", ".jpeg", ".heic", ".heif", ".png", ".gif", ".mov", ".mp4", ".m4v"}


class EncryptedBackup(Exception):
    pass


def backup_roots():
    if core.IS_WIN:
        return [os.path.join(os.environ.get("APPDATA", ""), "Apple Computer", "MobileSync", "Backup"),
                os.path.join(os.environ.get("USERPROFILE", ""), "Apple", "MobileSync", "Backup")]
    if core.IS_MAC:
        return [os.path.expanduser("~/Library/Application Support/MobileSync/Backup")]
    return [os.path.expanduser("~/.local/share/MobileSync/Backup")]


def _plist(path):
    try:
        with open(path, "rb") as f:
            return plistlib.load(f)
    except Exception:
        return {}


def read_backup(path):
    """Describe one backup folder, or None when it is not an iPhone backup."""
    if not (os.path.isfile(os.path.join(path, "Manifest.db")) or os.path.isfile(os.path.join(path, "Manifest.plist"))):
        return None
    info = _plist(os.path.join(path, "Info.plist"))
    manifest = _plist(os.path.join(path, "Manifest.plist"))
    when = info.get("Last Backup Date")
    if not isinstance(when, dt.datetime):
        try:
            when = dt.datetime.fromtimestamp(os.path.getmtime(path))
        except OSError:
            when = None
    return {"path": path, "device": info.get("Device Name") or info.get("Display Name") or "iPhone",
            "model": info.get("Product Type", ""), "date": when, "encrypted": bool(manifest.get("IsEncrypted"))}


def find_backups(roots=None):
    """Return (backups, denied_roots). denied_roots are folders the app may not read (Mac privacy setting)."""
    found, denied = [], []
    for root in roots or backup_roots():
        try:
            names = os.listdir(root)
        except PermissionError:
            denied.append(root)
            continue
        except OSError:
            continue
        for n in names:
            b = read_backup(os.path.join(root, n))
            if b:
                found.append(b)
    found.sort(key=lambda b: b["date"] or dt.datetime.min, reverse=True)
    return found, denied


def backup_targets(path):
    """Files to scan inside a backup, with their original names when the backup index can be read."""
    b = read_backup(path) or {}
    db = os.path.join(path, "Manifest.db")
    rows = None
    if os.path.isfile(db):
        tmp = tempfile.mkdtemp()
        try:
            copy = os.path.join(tmp, "Manifest.db")
            shutil.copyfile(db, copy)
            con = sqlite3.connect(copy)
            rows = con.execute("SELECT fileID, relativePath FROM Files WHERE domain = 'CameraRollDomain' AND flags = 1 "
                               "AND relativePath LIKE 'Media/DCIM/%'").fetchall()
            con.close()
        except sqlite3.DatabaseError:
            rows = None
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    if rows is None and b.get("encrypted"):
        raise EncryptedBackup(path)
    targets = []
    if rows is not None:
        for file_id, rel in rows:
            if os.path.splitext(rel)[1].lower() not in MEDIA_EXT:
                continue
            p = os.path.join(path, file_id[:2], file_id)
            if os.path.isfile(p):
                targets.append({"path": p, "orig": os.path.basename(rel), "label": rel})
        return targets
    for d, _, files in os.walk(path):
        for n in files:
            p = os.path.join(d, n)
            if n.startswith(("Manifest", "Info.plist", "Status.plist")) or os.path.getsize(p) < 1024:
                continue
            targets.append({"path": p, "orig": None, "label": n})
    return targets


# ---------------------------------------------------------------- iPhone over USB

_DETECT_PS = r"""
[Console]::OutputEncoding=[Text.Encoding]::UTF8
$sh = New-Object -ComObject Shell.Application
foreach ($i in $sh.NameSpace(17).Items()) { if ($i.Name -match 'iPhone|iPad|iPod') { $i.Name } }
"""

# Copies every photo and video in the phone's DCIM folders. Only ASCII in this script so
# Windows PowerShell reads it correctly without a byte-order mark.
_IMPORT_PS = r"""
param([string]$dest, [int]$wait = 90)
[Console]::OutputEncoding=[Text.Encoding]::UTF8
$deadline = (Get-Date).AddSeconds($wait)
$dcim = $null
while ($true) {
  # A fresh Shell object each round, so a phone that was just trusted is seen.
  $sh = New-Object -ComObject Shell.Application
  $dev = $null
  foreach ($i in $sh.NameSpace(17).Items()) { if ($i.Name -match 'iPhone|iPad|iPod') { $dev = $i; break } }
  $state = 'nodevice'
  if ($dev) {
    $state = 'locked'
    foreach ($storage in $dev.GetFolder.Items()) {
      foreach ($i in $storage.GetFolder.Items()) { if ($i.Name -eq 'DCIM') { $dcim = $i } }
    }
  }
  if ($dcim) { break }
  $left = [int]($deadline - (Get-Date)).TotalSeconds
  if ($left -le 0) {
    $svc = Get-Service -Name 'Apple Mobile Device Service' -ErrorAction SilentlyContinue
    if ($state -eq 'locked' -and -not $svc) { $state = 'locked-nodriver' }
    Write-Output ('ERR ' + $state); exit
  }
  Write-Output ('WAIT ' + $state + ' ' + $left)
  Start-Sleep -Seconds 2
}
Write-Output ('DEV ' + $dev.Name)
$subs = @($dcim.GetFolder.Items() | Where-Object { $_.IsFolder })
$total = 0
foreach ($s in $subs) { $total += $s.GetFolder.Items().Count }
Write-Output ('TOTAL ' + $total)
if ($total -eq 0) { Write-Output 'ERR empty'; exit }
$done = 0
foreach ($s in $subs) {
  $d = Join-Path $dest $s.Name
  New-Item -ItemType Directory -Force -Path $d | Out-Null
  $items = $s.GetFolder.Items()
  $want = $items.Count
  $sh.NameSpace($d).CopyHere($items, 4 + 16 + 1024)
  $lastSize = -1; $still = 0; $idle = 0; $lastN = -1
  while ($true) {
    Start-Sleep -Milliseconds 700
    $files = @(Get-ChildItem -LiteralPath $d -File -ErrorAction SilentlyContinue)
    $n = $files.Count
    $size = ($files | Measure-Object -Property Length -Sum).Sum
    Write-Output ('PROG ' + ($done + $n) + ' ' + $total)
    if ($n -ge $want) { if ($size -eq $lastSize) { $still++ } else { $still = 0 }; if ($still -ge 2) { break } }
    if ($n -eq $lastN -and $size -eq $lastSize) { $idle++ } else { $idle = 0 }
    if ($idle -gt 120) { break }
    $lastSize = $size; $lastN = $n
  }
  $done += $want
}
Write-Output 'DONE'
"""


def _ps(script, args=(), stream=False):
    tmp = tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False, encoding="ascii")
    tmp.write(script)
    tmp.close()
    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", tmp.name, *args]
    kw = dict(stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
              encoding="utf-8", errors="replace", creationflags=0x08000000 if core.IS_WIN else 0)
    if stream:
        return subprocess.Popen(cmd, **kw), tmp.name
    try:
        return subprocess.run(cmd, timeout=60, **kw).stdout, tmp.name
    finally:
        os.unlink(tmp.name)


def detect_usb():
    """Names of iPhones/iPads connected by USB (Windows and Mac)."""
    try:
        if core.IS_WIN:
            out, _ = _ps(_DETECT_PS)
            return [l.strip() for l in out.splitlines() if l.strip()]
        if core.IS_MAC:
            out = subprocess.run(["ioreg", "-p", "IOUSB", "-w0"], capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=20).stdout
            names = []
            for l in out.splitlines():
                for key in ("iPhone", "iPad", "iPod"):
                    if key in l and key not in names:
                        names.append(key)
            return names
    except Exception:
        pass
    return []


MANUAL_COPY = ("別の方法：エクスプローラーで「PC」→「Apple iPhone」→「Internal Storage」→「DCIM」を開き、中のフォルダを"
               "パソコンにコピーしてから、このアプリの「フォルダを選んで調べる…」でそのフォルダを選んでください。")

WAIT_TEXT = {
    "nodevice": "iPhone を探しています。USB ケーブルでつないでください",
    "locked": "iPhone のロックを解除し、「このコンピュータを信頼しますか？」と出たら「信頼」を押してパスコードを入れてください。押すと自動で続きます",
}


def import_usb(dest, on_progress, stop, on_wait=None, wait=90):
    """Copy the phone's current photos and videos into dest (Windows). Returns the device name.

    Waits up to `wait` seconds for the phone to be connected, unlocked and trusted.
    Raises RuntimeError with a message for the user when the phone cannot be read.
    """
    proc, script = _ps(_IMPORT_PS, ["-dest", dest, "-wait", str(wait)], stream=True)
    name, err = "iPhone", None
    try:
        for line in proc.stdout:
            line = line.strip()
            if stop.is_set():
                proc.kill()
                break
            if line.startswith("WAIT ") and on_wait:
                _, state, left = line.split()
                on_wait(WAIT_TEXT.get(state, WAIT_TEXT["locked"]), int(left))
            elif line.startswith("DEV "):
                name = line[4:]
            elif line.startswith("PROG "):
                a, b = line.split()[1:3]
                on_progress(int(a), int(b))
            elif line.startswith("ERR "):
                err = line[4:]
        proc.wait()
    finally:
        try:
            os.unlink(script)
        except OSError:
            pass
    if err == "nodevice":
        raise RuntimeError("iPhone が見つかりませんでした。USB ケーブルでつなぎ直してから、もう一度選んでください。\n"
                           "充電専用のケーブルでは読めません。iPhone に付属のケーブルなど、データ通信できるものを使ってください。")
    if err and err.startswith("locked"):
        tips = ["iPhone の中が見えませんでした。次を試してから、もう一度選んでください。",
                "・iPhone のロックを解除したままにする（画面を消さない）",
                "・ケーブルを抜いて差し直し、「このコンピュータを信頼しますか？」で「信頼」→ パスコードを入力",
                "・「信頼」が出ないときは、iPhone の「設定」→「一般」→「転送またはiPhoneをリセット」→「リセット」→「位置情報とプライバシーをリセット」のあと、つなぎ直す"]
        if err == "locked-nodriver":
            tips.append("・Microsoft Store から無料の「Apple Devices」アプリを入れる（iPhone をつなぐための部品が入ります）")
        tips.append(MANUAL_COPY)
        raise RuntimeError("\n".join(tips))
    if err == "empty":
        raise RuntimeError("iPhone の中に写真・動画が見つかりませんでした。\n"
                           "iCloud 写真の「iPhone のストレージを最適化」を使っていると、元の写真は iCloud にだけあります。"
                           "その場合は icloud.com の「写真」からダウンロードしてください。")
    return name


def folder_targets(folder):
    """Every file under a folder, or the photos of an iPhone backup when the folder is one."""
    if read_backup(folder):
        return backup_targets(folder)
    out = []
    for d, dirs, files in os.walk(folder):
        dirs[:] = [x for x in dirs if not x.startswith(".")]
        for n in files:
            p = os.path.join(d, n)
            try:
                if os.path.getsize(p) >= 1024:
                    out.append(file_target(p))
            except OSError:
                pass
    return out


def file_target(path):
    try:
        mtime = dt.datetime.fromtimestamp(os.path.getmtime(path))
    except (OSError, ValueError):
        mtime = None
    return {"path": path, "orig": os.path.basename(path), "label": path, "date": mtime}
