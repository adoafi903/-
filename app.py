#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""消えた写真・動画の復元 — デスクトップアプリ版

1. 復元したいドライブを選ぶ
2. ドライブ全体（空き領域も含む）を読み、残っている写真・動画を一覧にする
3. 取り戻したいものにチェックを付けて、別のドライブに保存する
"""
import bisect
import io
import os
import queue
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, ttk

import fukugen as core

try:
    from PIL import Image, ImageDraw, ImageFile, ImageOps, ImageTk
    ImageFile.LOAD_TRUNCATED_IMAGES = True
    HAS_PIL = True
except ImportError:
    HAS_PIL = False
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except Exception:
    pass

APP_NAME = "消えた写真・動画の復元"
THUMB = 48
PREVIEW = 360
CHECK_ON, CHECK_OFF, CHECK_MIXED = "☑", "☐", "▣"
FROZEN = getattr(sys, "frozen", False)


# ---------------------------------------------------------------- admin

def relaunch_elevated():
    """Start this app again with administrator rights (Windows)."""
    import ctypes
    if FROZEN:
        exe, params = sys.executable, " ".join(f'"{a}"' for a in sys.argv[1:])
    else:
        exe, params = sys.executable, " ".join(f'"{a}"' for a in [os.path.abspath(__file__)] + sys.argv[1:])
    return ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1) > 32


def mac_grant_read(path):
    """Ask for the admin password once and allow this user to read the raw device until it is unplugged."""
    cmd = "chmod o+r " + shlex.quote(path)
    script = f'do shell script "{cmd}" with administrator privileges'
    return subprocess.run(["osascript", "-e", script], capture_output=True).returncode == 0


def open_source(path, parent):
    try:
        return core.Source(path)
    except PermissionError:
        if core.IS_MAC and path.startswith("/dev/") and mac_grant_read(path):
            return core.Source(path)
        if core.IS_MAC:
            messagebox.showerror(APP_NAME, "ドライブを読む許可がありません。\n\n管理者のパスワードを入力するか、「システム設定」→「プライバシーとセキュリティ」→「フルディスクアクセス」でこのアプリを許可してください。", parent=parent)
        elif core.IS_WIN:
            messagebox.showerror(APP_NAME, "ドライブを読む許可がありません。アプリを管理者として起動してください。", parent=parent)
        else:
            messagebox.showerror(APP_NAME, "ドライブを読む許可がありません。sudo を付けて起動してください。", parent=parent)
    except OSError as e:
        messagebox.showerror(APP_NAME, f"ドライブを開けませんでした。\n{e}", parent=parent)
    return None


def open_with_system(path):
    try:
        if core.IS_WIN:
            os.startfile(path)
        elif core.IS_MAC:
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        pass


# ---------------------------------------------------------------- images

def placeholder(kind, size):
    im = Image.new("RGB", (size, size), (214, 224, 230) if kind != "video" else (38, 52, 62))
    d = ImageDraw.Draw(im)
    c = size // 2
    if kind == "video":
        r = size // 5
        d.polygon([(c - r // 2, c - r), (c - r // 2, c + r), (c + r, c)], fill=(240, 244, 246))
    elif kind == "broken":
        r = size // 5
        d.line([(c - r, c - r), (c + r, c + r)], fill=(150, 160, 168), width=3)
        d.line([(c - r, c + r), (c + r, c - r)], fill=(150, 160, 168), width=3)
    else:
        d.rectangle([size // 4, size // 3, size * 3 // 4, size * 2 // 3], outline=(150, 160, 168), width=2)
    return im


def render_image(src, r, size):
    """Decode a found photo into a PIL image no bigger than size x size. None when it cannot be shown."""
    if not HAS_PIL or r["kind"] != "image":
        return None
    n = r["e"] - r["s"]
    if n > 80 * core.MB:
        return None
    data = src.read_at(r["s"], n) + core.record_tail(r)
    try:
        im = Image.open(io.BytesIO(data))
        if im.format == "JPEG":
            im.draft("RGB", (size * 2, size * 2))
        im = ImageOps.exif_transpose(im)
        im.thumbnail((size, size))
        return im.convert("RGB")
    except Exception:
        return None


# ---------------------------------------------------------------- app

class App:
    def __init__(self, root):
        self.root = root
        root.title(APP_NAME)
        root.geometry("1180x760")
        root.minsize(900, 600)
        self.setup_style()
        self.q = queue.Queue()
        self.work = queue.PriorityQueue()
        self.drives = []
        self.source_path = None
        self.source_item = None
        self.source_size = 0
        self.stop = threading.Event()
        self.scanning = False
        self.records = {}
        self.photos = {}
        self.group_keys = {}
        self.group_count = {}
        self.seen = set()
        self.stats = {"image": 0, "video": 0, "small": 0, "dup": 0, "noindex": 0}
        self.preview_img = None
        self.filter = tk.StringVar(value="all")
        self.keep_small = tk.BooleanVar(value=False)
        self.all_offsets = tk.BooleanVar(value=False)
        self.gen = 0
        self.build_start()
        self.build_results()
        self.show(self.start)
        self.refresh_drives()
        self.root.after(100, self.poll)

    # -------------------------------------------------- look
    def setup_style(self):
        fam = "Yu Gothic UI" if core.IS_WIN else "Hiragino Sans" if core.IS_MAC else None
        size = 10 if core.IS_WIN else 13 if core.IS_MAC else 10
        for name in ("TkDefaultFont", "TkTextFont", "TkHeadingFont", "TkMenuFont"):
            try:
                f = tkfont.nametofont(name)
                if fam and fam in tkfont.families():
                    f.configure(family=fam)
                f.configure(size=size)
            except tk.TclError:
                pass
        base = tkfont.nametofont("TkDefaultFont").actual()
        self.f_title = (base["family"], size + 8, "bold")
        self.f_sub = (base["family"], size)
        self.f_bold = (base["family"], size, "bold")
        st = ttk.Style()
        if not core.IS_WIN and not core.IS_MAC:
            st.theme_use("clam")
        st.configure("Treeview", rowheight=THUMB + 8)
        st.configure("Drives.Treeview", rowheight=30)
        st.configure("Accent.TButton", font=self.f_bold, padding=(16, 8))
        st.configure("Muted.TLabel", foreground="#556672")
        st.configure("Warn.TLabel", foreground="#A8670E")
        self.root.configure(bg=st.lookup("TFrame", "background") or "#F0F0F0")

    def show(self, frame):
        for f in (self.start, self.results):
            f.pack_forget()
        frame.pack(fill="both", expand=True)

    # -------------------------------------------------- page 1
    def build_start(self):
        f = self.start = ttk.Frame(self.root, padding=24)
        ttk.Label(f, text="復元したいドライブを選んでください", font=self.f_title).pack(anchor="w")
        ttk.Label(f, text="SD カード、USB メモリ、外付けドライブ、パソコンのドライブから、削除した写真と動画を探します。元のドライブには書き込みません。",
                  style="Muted.TLabel", wraplength=1000).pack(anchor="w", pady=(4, 14))
        self.admin_bar = ttk.Frame(f)
        self.admin_bar.pack(fill="x")
        box = ttk.Frame(f)
        box.pack(fill="both", expand=True)
        self.drive_tree = ttk.Treeview(box, columns=("d",), show="tree", style="Drives.Treeview", selectmode="browse")
        self.drive_tree.column("#0", width=0, stretch=False)
        self.drive_tree.column("d", stretch=True)
        sb = ttk.Scrollbar(box, orient="vertical", command=self.drive_tree.yview)
        self.drive_tree.configure(yscrollcommand=sb.set)
        self.drive_tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        self.drive_tree.bind("<<TreeviewSelect>>", self.on_drive_select)
        self.drive_tree.bind("<Double-1>", lambda e: self.start_scan())
        self.drive_note = ttk.Label(f, text="", style="Warn.TLabel", wraplength=1000)
        self.drive_note.pack(anchor="w", pady=(8, 0))
        opts = ttk.Frame(f)
        opts.pack(fill="x", pady=(10, 0))
        ttk.Checkbutton(opts, text="アイコンなど小さい画像も探す", variable=self.keep_small).pack(side="left")
        ttk.Checkbutton(opts, text="ファイルの区切り以外も探す（時間がかかるが、見つかる数が増えることがある）", variable=self.all_offsets).pack(side="left", padx=(16, 0))
        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=(14, 0))
        ttk.Button(bar, text="一覧を更新", command=self.refresh_drives).pack(side="left")
        ttk.Button(bar, text="イメージファイルを開く…", command=self.pick_image).pack(side="left", padx=8)
        self.scan_btn = ttk.Button(bar, text="スキャンを始める", style="Accent.TButton", command=self.start_scan, state="disabled")
        self.scan_btn.pack(side="right")
        tips = ("・消したドライブには、写真を撮ったりファイルを保存したりしないでください。上書きされると戻せなくなります。\n"
                "・SD カードがドライブ文字で出てこない、「フォーマットしますか」と聞かれる場合は「ディスク ○ 全体」を選んでください。\n"
                "・SSD から消したデータは、すぐに消去されている（TRIM）ことが多く、見つからない場合があります。スマホ本体の中身は暗号化されているため読めません。")
        ttk.Label(f, text=tips, style="Muted.TLabel", justify="left", wraplength=1100).pack(anchor="w", pady=(18, 0))

    def refresh_drives(self):
        for w in self.admin_bar.winfo_children():
            w.destroy()
        if core.IS_WIN and not core.is_admin():
            ttk.Label(self.admin_bar, text="管理者として起動していないため、ドライブを直接読めません。", style="Warn.TLabel").pack(side="left")
            ttk.Button(self.admin_bar, text="管理者として起動し直す", command=self.elevate).pack(side="left", padx=8, pady=(0, 10))
        elif not core.IS_WIN and not core.IS_MAC and os.geteuid() != 0:
            ttk.Label(self.admin_bar, text="ドライブを読むには sudo を付けて起動してください（イメージファイルはそのまま読めます）。", style="Warn.TLabel").pack(side="left", pady=(0, 10))
        self.drive_tree.delete(*self.drive_tree.get_children())
        self.drive_tree.insert("", "end", iid="loading", values=("ドライブを調べています…",))
        self.root.update_idletasks()
        threading.Thread(target=lambda: self.q.put(("drives", core.list_drives())), daemon=True).start()

    def elevate(self):
        if relaunch_elevated():
            self.root.destroy()

    def on_drive_select(self, _=None):
        sel = self.drive_tree.selection()
        if not sel or not sel[0].isdigit():
            self.scan_btn.state(["disabled"])
            return
        item = self.drives[int(sel[0])]
        self.scan_btn.state(["!disabled"])
        self.drive_note.configure(text=("これはパソコン本体のドライブです。使っている間も書き込みが続くため、消したファイルが上書きされている場合があります。"
                                        if item.get("system") else ""))

    def pick_image(self):
        p = filedialog.askopenfilename(title="ドライブのイメージファイルを選んでください",
                                       filetypes=[("イメージファイル", "*.img *.dd *.bin *.raw *.iso *.dmg *.001"), ("すべてのファイル", "*")])
        if p:
            self.begin(p, None)

    def start_scan(self):
        sel = self.drive_tree.selection()
        if sel and sel[0].isdigit():
            item = self.drives[int(sel[0])]
            self.begin(item["path"], item)

    # -------------------------------------------------- page 2
    def build_results(self):
        f = self.results = ttk.Frame(self.root, padding=(16, 12))
        top = ttk.Frame(f)
        top.pack(fill="x")
        ttk.Button(top, text="← ドライブ選択に戻る", command=self.back).pack(side="left")
        self.src_label = ttk.Label(top, text="", font=self.f_bold)
        self.src_label.pack(side="left", padx=12)
        self.stop_btn = ttk.Button(top, text="スキャンを止める", command=self.stop_scan)
        self.stop_btn.pack(side="right")
        self.pbar = ttk.Progressbar(f, maximum=1000)
        self.pbar.pack(fill="x", pady=(10, 4))
        self.status = ttk.Label(f, text="")
        self.status.pack(anchor="w")

        bot = ttk.Frame(f)
        bot.pack(side="bottom", fill="x", pady=(8, 0))
        mid = ttk.PanedWindow(f, orient="horizontal")
        mid.pack(fill="both", expand=True, pady=(10, 0))
        left = ttk.Frame(mid)
        right = ttk.Frame(mid, padding=(12, 0, 0, 0))
        mid.add(left, weight=3)
        mid.add(right, weight=2)

        filt = ttk.Frame(left)
        filt.pack(fill="x", pady=(0, 6))
        ttk.Label(filt, text="表示：").pack(side="left")
        for v, t in (("all", "すべて"), ("image", "写真・画像"), ("video", "動画")):
            ttk.Radiobutton(filt, text=t, value=v, variable=self.filter, command=self.rebuild).pack(side="left", padx=4)
        tv = ttk.Frame(left)
        tv.pack(fill="both", expand=True)
        cols = ("check", "date", "type", "size", "state")
        self.tree = ttk.Treeview(tv, columns=cols, selectmode="extended")
        self.tree.heading("#0", text="見つかったファイル")
        for c, t, w in (("check", "復元", 52), ("date", "撮影日時", 150), ("type", "種類", 80), ("size", "大きさ", 90), ("state", "状態", 90)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, stretch=c == "date", anchor="center" if c in ("check", "type") else "w")
        self.tree.column("#0", width=230)
        sb = ttk.Scrollbar(tv, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        self.tree.bind("<Button-1>", self.on_click, add=True)
        self.tree.bind("<<TreeviewSelect>>", self.on_select)
        self.tree.bind("<space>", self.on_space)
        self.tree.bind("<Double-1>", lambda e: self.open_selected())

        self.pv = ttk.Label(right, anchor="center")
        self.pv.pack(fill="x")
        self.pv_info = ttk.Label(right, text="ファイルを選ぶと、ここに大きく表示します。", justify="left", wraplength=380, style="Muted.TLabel")
        self.pv_info.pack(anchor="w", pady=(10, 6))
        self.open_btn = ttk.Button(right, text="アプリで開いて見る", command=self.open_selected, state="disabled")
        self.open_btn.pack(anchor="w")

        ttk.Button(bot, text="すべてにチェック", command=lambda: self.check_all(True)).pack(side="left")
        ttk.Button(bot, text="チェックをすべて外す", command=lambda: self.check_all(False)).pack(side="left", padx=8)
        self.sel_label = ttk.Label(bot, text="")
        self.sel_label.pack(side="left", padx=12)
        self.save_btn = ttk.Button(bot, text="チェックしたファイルを復元する…", style="Accent.TButton", command=self.save_checked, state="disabled")
        self.save_btn.pack(side="right")

    # -------------------------------------------------- scanning
    def begin(self, path, item):
        src = open_source(path, self.root)
        if not src:
            return
        if not src.size:
            messagebox.showerror(APP_NAME, "ドライブの大きさを読み取れませんでした。別のドライブを選んでください。")
            src.close()
            return
        self.reset()
        self.source_path, self.source_item, self.source_size = path, item, src.size
        self.src_label.configure(text=f"{(item or {}).get('label', os.path.basename(path)).strip()}")
        self.show(self.results)
        self.scanning = True
        self.stop.clear()
        self.stop_btn.state(["!disabled"])
        self.t0 = time.time()
        gen = self.gen
        threading.Thread(target=self.scan_worker, args=(src, gen), daemon=True).start()
        threading.Thread(target=self.media_worker, args=(path, gen), daemon=True).start()

    def reset(self):
        self.gen += 1
        self.stop.set()
        self.records.clear()
        self.photos.clear()
        self.group_keys.clear()
        self.group_count.clear()
        self.seen.clear()
        self.stats = {"image": 0, "video": 0, "small": 0, "dup": 0, "noindex": 0}
        self.tree.delete(*self.tree.get_children())
        self.pv.configure(image="")
        self.pv_info.configure(text="ファイルを選ぶと、ここに大きく表示します。")
        self.open_btn.state(["disabled"])
        self.pbar["value"] = 0
        self.work = queue.PriorityQueue()
        self.update_selection()

    def scan_worker(self, src, gen):
        last = [0.0]
        keep_small, all_off = self.keep_small.get(), self.all_offsets.get()

        def prog(p):
            if time.time() - last[0] > 0.2:
                last[0] = time.time()
                self.q.put(("prog", gen, p))

        try:
            for r in core.iter_scan(src, all_off, self.stop, prog):
                if gen != self.gen:
                    break
                if r["kind"] == "image" and r.get("w") and max(r["w"], r["h"]) < 256 and not keep_small:
                    self.q.put(("count", gen, "small"))
                    continue
                if r["kind"] == "noindex" and r["e"] - r["s"] < core.MB:
                    continue
                key = core.record_key(src, r)
                if key in self.seen:
                    self.q.put(("count", gen, "dup"))
                    continue
                self.seen.add(key)
                self.q.put(("item", gen, r))
        except Exception as e:
            self.q.put(("error", gen, str(e)))
        self.q.put(("done", gen, src.bad))
        src.close()

    def media_worker(self, path, gen):
        try:
            src = core.Source(path)
        except OSError:
            return
        while gen == self.gen:
            try:
                _, _, job = self.work.get(timeout=0.5)
            except queue.Empty:
                continue
            kind, iid = job
            r = self.records.get(iid)
            if not r:
                continue
            if kind == "thumb":
                im = render_image(src, r, THUMB) if r["kind"] == "image" else None
                self.q.put(("thumb", gen, iid, im))
            elif kind == "preview":
                im = render_image(src, r, PREVIEW) if r["kind"] == "image" else None
                self.q.put(("preview", gen, iid, im))
            elif kind == "open":
                d = os.path.join(tempfile.gettempdir(), "fukugen_preview")
                try:
                    p = core.write_record(src, r, d)
                    self.q.put(("open", gen, p))
                except OSError as e:
                    self.q.put(("error", gen, str(e)))
        src.close()

    def stop_scan(self):
        self.stop.set()
        self.stop_btn.state(["disabled"])

    def back(self):
        if self.scanning and not messagebox.askyesno(APP_NAME, "スキャンを止めて、ドライブ選択に戻りますか？"):
            return
        self.reset()
        self.scanning = False
        self.show(self.start)

    # -------------------------------------------------- queue
    def poll(self):
        try:
            for _ in range(400):
                msg = self.q.get_nowait()
                self.handle(msg)
        except queue.Empty:
            pass
        self.root.after(80, self.poll)

    def handle(self, msg):
        if msg[0] == "drives":
            self.drives = msg[1]
            self.drive_tree.delete(*self.drive_tree.get_children())
            for i, d in enumerate(self.drives):
                self.drive_tree.insert("", "end", iid=str(i), values=(d["label"].strip(),))
            if not self.drives:
                self.drive_tree.insert("", "end", iid="none", values=("ドライブが見つかりませんでした。管理者として起動しているか確かめてください。",))
            self.on_drive_select()
            return
        if msg[0] == "open":
            open_with_system(msg[2])
            return
        if len(msg) > 1 and msg[1] != self.gen:
            return
        kind = msg[0]
        if kind == "item":
            self.add_record(msg[2])
        elif kind == "count":
            self.stats[msg[2]] += 1
        elif kind == "prog":
            self.show_progress(msg[2])
        elif kind == "thumb":
            iid, im = msg[2], msg[3]
            r = self.records.get(iid)
            if r is not None and HAS_PIL and self.tree.exists(iid):
                im = im or placeholder("video" if r["kind"] != "image" else "broken", THUMB)
                ph = ImageTk.PhotoImage(im)
                self.photos[iid] = ph
                self.tree.item(iid, image=ph)
        elif kind == "preview":
            iid, im = msg[2], msg[3]
            sel = self.tree.selection()
            if sel and sel[0] == iid:
                r = self.records[iid]
                if HAS_PIL:
                    im = im or placeholder("video" if r["kind"] != "image" else "broken", 200)
                    self.preview_img = ImageTk.PhotoImage(im)
                    self.pv.configure(image=self.preview_img)
        elif kind == "done":
            self.scanning = False
            self.stop_btn.state(["disabled"])
            self.show_progress(self.source_size if not self.stop.is_set() else None, done=True, bad=msg[2])
        elif kind == "error":
            messagebox.showerror(APP_NAME, msg[2])

    def show_progress(self, pos, done=False, bad=0):
        s = self.stats
        found = f"写真・画像 {s['image']} 件　動画 {s['video']} 件"
        if done:
            if pos is not None:
                self.pbar["value"] = 1000
            head = "スキャンを止めました。" if pos is None else "スキャンが終わりました。"
            extra = []
            if s["noindex"]:
                extra.append(f"索引の無い動画 {s['noindex']} 件")
            if s["small"]:
                extra.append(f"小さい画像 {s['small']} 件は非表示")
            if s["dup"]:
                extra.append(f"重複 {s['dup']} 件はまとめました")
            if bad:
                extra.append(f"読めない場所 {bad} か所")
            tail = "　チェックを付けたファイルを「チェックしたファイルを復元する」で保存してください。" if (s["image"] or s["video"] or s["noindex"]) else \
                "　見つかりませんでした。上書きされたか、SSD の TRIM で消去された可能性があります。"
            self.status.configure(text=f"{head}{found}" + (f"（{'、'.join(extra)}）" if extra else "") + tail)
            return
        el = time.time() - self.t0
        speed = pos / el if el > 0 else 0
        eta = (self.source_size - pos) / speed if speed else None
        self.pbar["value"] = pos / self.source_size * 1000 if self.source_size else 0
        pct = pos / self.source_size * 100 if self.source_size else 0
        self.status.configure(text=f"スキャン中… {pct:.1f}%（{core.fmt_size(pos)} / {core.fmt_size(self.source_size)}、残り約 {core.fmt_eta(eta)}）　見つかった数：{found}")

    # -------------------------------------------------- tree
    def matches(self, r):
        f = self.filter.get()
        return f == "all" or (f == "video" and r["kind"] in ("video", "noindex")) or (f == "image" and r["kind"] == "image")

    def add_record(self, r):
        iid = f"f{r['s']}"
        r["checked"] = True
        self.records[iid] = r
        self.stats[r["kind"]] += 1
        if self.matches(r):
            self.insert_row(iid, r)
        self.update_selection()
        self.save_btn.state(["!disabled"])

    def group_ids(self, r):
        d = r.get("date")
        if r["kind"] == "noindex":
            return ("g-noindex", "索引が無い動画", -2), None
        if not d:
            return ("g-unknown", "日付不明", -1), None
        return (f"y{d.year}", f"{d.year}年", d.year), (f"y{d.year}m{d.month}", f"{d.month:02d}月", d.month)

    def ensure_node(self, parent, iid, label, key):
        if self.tree.exists(iid):
            return
        keys = self.group_keys.setdefault(parent, [])
        idx = bisect.bisect_right(keys, -key)
        keys.insert(idx, -key)
        self.tree.insert(parent, idx, iid=iid, text=label, open=True, values=(CHECK_ON, "", "", "", ""))
        self.group_count[iid] = [0, label]

    def bump(self, iid, n):
        c = self.group_count[iid]
        c[0] += n
        self.tree.item(iid, text=f"{c[1]}（{c[0]} 件）")

    def insert_row(self, iid, r):
        (yid, ylab, ykey), month = self.group_ids(r)
        self.ensure_node("", yid, ylab, ykey)
        parent = yid
        if month:
            mid, mlab, mkey = month
            self.ensure_node(yid, mid, mlab, mkey)
            parent = mid
        d = r.get("date")
        t = d.timestamp() if d else -r["s"]
        keys = self.group_keys.setdefault(parent, [])
        idx = bisect.bisect_right(keys, -t)
        keys.insert(idx, -t)
        dims = f"{r['w']}×{r['h']}" if r.get("w") else ""
        name = (d.strftime("%Y-%m-%d_%H%M%S") if d else f"{r['s']:012X}") + "." + r["ext"]
        state = "一部欠損" if r["trunc"] and r["kind"] != "noindex" else "索引なし" if r["kind"] == "noindex" else "完全"
        typ = {"image": "写真", "video": "動画", "noindex": "動画"}[r["kind"]] + f" {r['type'].split('/')[0]}"
        self.tree.insert(parent, idx, iid=iid, text=f"  {name}" + (f"  {dims}" if dims else ""),
                         values=(CHECK_ON if r["checked"] else CHECK_OFF, d.strftime("%Y/%m/%d %H:%M") if d else "不明", typ,
                                 core.fmt_size(r["e"] - r["s"]), state))
        if iid in self.photos:
            self.tree.item(iid, image=self.photos[iid])
        elif HAS_PIL:
            if r["kind"] == "image":
                self.work.put((1, r["s"], ("thumb", iid)))
            else:
                ph = ImageTk.PhotoImage(placeholder("video", THUMB))
                self.photos[iid] = ph
                self.tree.item(iid, image=ph)
        self.bump(yid, 1)
        if month:
            self.bump(month[0], 1)

    def rebuild(self):
        self.tree.delete(*self.tree.get_children())
        self.group_keys.clear()
        self.group_count.clear()
        for iid, r in self.records.items():
            if self.matches(r):
                self.insert_row(iid, r)
        self.update_selection()

    def leaves(self, node):
        kids = self.tree.get_children(node)
        if not kids:
            return [node] if node in self.records else []
        out = []
        for k in kids:
            out += self.leaves(k)
        return out

    def set_checked(self, iids, value):
        for iid in iids:
            if iid in self.records:
                self.records[iid]["checked"] = value
                self.tree.set(iid, "check", CHECK_ON if value else CHECK_OFF)
        self.refresh_groups()
        self.update_selection()

    def refresh_groups(self):
        for g in self.group_count:
            if self.tree.exists(g):
                states = {self.records[i]["checked"] for i in self.leaves(g)}
                self.tree.set(g, "check", CHECK_ON if states == {True} else CHECK_OFF if states == {False} else CHECK_MIXED)

    def on_click(self, e):
        if self.tree.identify_region(e.x, e.y) != "cell" or self.tree.identify_column(e.x) != "#1":
            return
        iid = self.tree.identify_row(e.y)
        if not iid:
            return
        leaves = self.leaves(iid)
        if leaves:
            self.set_checked(leaves, not all(self.records[i]["checked"] for i in leaves))
        return "break"

    def on_space(self, _):
        leaves = [l for i in self.tree.selection() for l in self.leaves(i)]
        if leaves:
            self.set_checked(leaves, not all(self.records[i]["checked"] for i in leaves))
        return "break"

    def check_all(self, value):
        self.set_checked([i for i, r in self.records.items() if self.matches(r)], value)

    def update_selection(self):
        chosen = [r for r in self.records.values() if r["checked"] and self.matches(r)]
        size = sum(r["e"] - r["s"] for r in chosen)
        self.sel_label.configure(text=f"復元するもの：{len(chosen)} 件（{core.fmt_size(size)}）")

    def on_select(self, _=None):
        sel = self.tree.selection()
        if not sel or sel[0] not in self.records:
            self.open_btn.state(["disabled"])
            return
        iid = sel[0]
        r = self.records[iid]
        d = r.get("date")
        lines = [f"撮影日時：{d.strftime('%Y年%m月%d日 %H:%M:%S') if d else '不明'}",
                 f"種類：{r['type']}　大きさ：{core.fmt_size(r['e'] - r['s'])}" + (f"　{r['w']}×{r['h']} px" if r.get("w") else "")]
        if r.get("model"):
            lines.append(f"カメラ：{r['model']}")
        if r["kind"] == "noindex":
            lines.append("索引（moov）が無い動画です。保存したあと、ファイル修復ラボのページで同じ機種の正常な動画を参照にして作り直せます。")
        elif r["trunc"]:
            lines.append("途中が上書きされていたため、残っている部分だけを復元します。")
        if r["kind"] != "image":
            lines.append("動画は「アプリで開いて見る」で中身を確かめられます。")
        lines.append(f"ドライブ上の位置：{r['s']:,} バイト目")
        self.pv_info.configure(text="\n".join(lines))
        self.open_btn.state(["!disabled"])
        self.pv.configure(image="")
        self.work.put((0, time.time(), ("preview", iid)))

    def open_selected(self):
        sel = self.tree.selection()
        if sel and sel[0] in self.records:
            self.work.put((0, time.time(), ("open", sel[0])))

    # -------------------------------------------------- saving
    def save_checked(self):
        chosen = [r for r in self.records.values() if r["checked"] and self.matches(r)]
        if not chosen:
            messagebox.showinfo(APP_NAME, "復元するファイルにチェックを付けてください。")
            return
        out = filedialog.askdirectory(title="保存先のフォルダを選んでください（復元するドライブとは別のドライブ）")
        if not out:
            return
        if core.same_drive(self.source_item, out):
            messagebox.showerror(APP_NAME, "保存先が、復元するドライブと同じです。\n消えたデータを上書きしてしまうため、別のドライブのフォルダを選んでください。")
            return
        total = sum(r["e"] - r["s"] for r in chosen)
        try:
            free = shutil.disk_usage(out).free
        except OSError:
            free = total
        if free < total and not messagebox.askyesno(APP_NAME, f"保存先の空きが足りない可能性があります（必要 {core.fmt_size(total)}、空き {core.fmt_size(free)}）。続けますか？"):
            return
        root = os.path.join(out, "復元結果_" + time.strftime("%Y%m%d_%H%M"))
        win = tk.Toplevel(self.root)
        win.title("復元しています")
        win.transient(self.root)
        win.grab_set()
        fr = ttk.Frame(win, padding=20)
        fr.pack(fill="both", expand=True)
        lab = ttk.Label(fr, text="準備しています…", width=60)
        lab.pack(anchor="w")
        pb = ttk.Progressbar(fr, maximum=max(total, 1), length=460)
        pb.pack(fill="x", pady=10)
        state = {"bytes": 0, "n": 0, "done": False, "err": None, "paths": []}

        def worker():
            try:
                src = core.Source(self.source_path)
                o = core.Output(root, True)
                for r in chosen:
                    p = core.write_record(src, r, root, on_bytes=lambda n: state.__setitem__("bytes", state["bytes"] + n))
                    state["n"] += 1
                    d = r.get("date")
                    o.counts[r["kind"]] += 1
                    o.records.append({"path": os.path.relpath(p, root), "kind": r["kind"], "type": r["type"],
                                      "date": d.isoformat(" ") if d else "", "w": r.get("w") or 0, "h": r.get("h") or 0,
                                      "size": r["e"] - r["s"], "offset": r["s"], "partial": bool(r["trunc"]), "model": r.get("model", "")})
                o.write_index(self.src_label.cget("text"), self.source_size, self.source_size, time.time() - self.t0, src.bad)
                core.give_back_ownership(root)
                src.close()
            except Exception as e:
                state["err"] = str(e)
            state["done"] = True

        def tick():
            pb["value"] = state["bytes"]
            lab.configure(text=f"{state['n']} / {len(chosen)} 件（{core.fmt_size(state['bytes'])} / {core.fmt_size(total)}）")
            if not state["done"]:
                win.after(150, tick)
                return
            win.grab_release()
            win.destroy()
            if state["err"]:
                messagebox.showerror(APP_NAME, f"保存中にエラーが起きました。\n{state['err']}\n\n{state['n']} 件は保存できています。")
            else:
                messagebox.showinfo(APP_NAME, f"{state['n']} 件を復元しました。\n\n{root}\n\n撮影した年・月ごとのフォルダに分けています。「復元結果を見る.html」で一覧できます。")
            open_with_system(root)

        threading.Thread(target=worker, daemon=True).start()
        tick()


def main():
    if core.IS_WIN and not core.is_admin() and "--no-elevate" not in sys.argv:
        try:
            if relaunch_elevated():
                return
        except Exception:
            pass
    root = tk.Tk()
    app = App(root)
    images = [a for a in sys.argv[1:] if os.path.isfile(a)]
    if images:
        root.after(300, lambda: app.begin(images[0], None))
    root.mainloop()


if __name__ == "__main__":
    main()
