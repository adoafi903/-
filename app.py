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
import traceback
import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, ttk

import fukugen as core
import iphone
import undelete

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
T0 = time.time()
LOG_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "Fukugen")


def log_error(text):
    """Keep startup and runtime errors in a file the user can send, since the app has no console."""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        path = os.path.join(LOG_DIR, "error.log")
        with open(path, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + text + "\n")
        return path
    except OSError:
        return ""


def show_fatal(text):
    path = log_error(text)
    msg = "アプリを起動できませんでした。\n\n" + text[-1500:] + (f"\n\n記録: {path}" if path else "")
    try:
        r = tk.Tk()
        r.withdraw()
        messagebox.showerror(APP_NAME, msg, parent=r)
        r.destroy()
    except Exception:
        if core.IS_WIN:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, msg, APP_NAME, 0x10)


def close_splash():
    try:
        import pyi_splash
        pyi_splash.close()
    except Exception:
        pass


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
    return subprocess.run(["osascript", "-e", script], capture_output=True, stdin=subprocess.DEVNULL).returncode == 0


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
    if r["e"] - r["s"] > 80 * core.MB:
        return None
    data = core.read_record(src, r)
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
        self.targets = []
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
        self.all_types = tk.BooleanVar(value=False)
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
        ttk.Label(f, text="調べたいドライブや iPhone を選んでください", font=self.f_title).pack(anchor="w")
        ttk.Label(f, text="SD カード、USB メモリ、外付けドライブ、パソコンのドライブ、iPhone とそのバックアップから写真と動画を探します。元のデータには書き込みません。",
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
        ttk.Checkbutton(opts, text="写真・動画以外の削除ファイル（書類など）も探す", variable=self.all_types).pack(side="left", padx=(16, 0))
        ttk.Checkbutton(opts, text="ファイルの区切り以外も探す（時間がかかる）", variable=self.all_offsets).pack(side="left", padx=(16, 0))
        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=(14, 0))
        ttk.Button(bar, text="一覧を更新", command=self.refresh_drives).pack(side="left")
        ttk.Button(bar, text="ファイルを選んで調べる…", command=self.pick_files).pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="フォルダを選んで調べる…", command=self.pick_folder).pack(side="left", padx=8)
        self.scan_btn = ttk.Button(bar, text="スキャンを始める", style="Accent.TButton", command=self.start_scan, state="disabled")
        self.scan_btn.pack(side="right")
        tips = ("・消したドライブには、写真を撮ったりファイルを保存したりしないでください。上書きされると戻せなくなります。\n"
                "・SD カードがドライブ文字で出てこない、「フォーマットしますか」と聞かれる場合は「ディスク ○ 全体」を選んでください。\n"
                "・SSD から消したデータは、すぐに消去されている（TRIM）ことが多く、見つからない場合があります。\n"
                "・iPhone は中身が暗号化されているため、USB でつないでも「今ある写真」しか読めません。消した写真は、消す前に作った iPhone のバックアップから探せます。")
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
        threading.Thread(target=self.gather_sources, daemon=True).start()

    def gather_sources(self):
        items = []
        for name in iphone.detect_usb():
            items.append({"type": "usb", "name": name, "label": f"【iPhone】{name}（USB で接続中）— 今 iPhone にある写真・動画を読み込む"})
        backups, denied = iphone.find_backups()
        for b in backups:
            when = b["date"].strftime("%Y/%m/%d %H:%M") if b["date"] else "日時不明"
            items.append({"type": "backup", "path": b["path"], "encrypted": b["encrypted"],
                          "label": f"【iPhone のバックアップ】{b['device']}（{when} に作成{'・暗号化あり' if b['encrypted'] else ''}）— 消す前の写真が残っている可能性"})
        for d in denied:
            items.append({"type": "denied", "path": d, "label": "【iPhone のバックアップ】あるかもしれませんが、読む許可がありません（選ぶと設定方法を表示）"})
        for d in core.list_drives():
            d["type"] = "drive"
            items.append(d)
        self.q.put(("drives", items))

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
        notes = {"usb": "iPhone の中の写真と動画をこのパソコンに読み込んでから一覧にします。iPhone のロックを解除しておいてください。削除した写真は iPhone の仕組み上ここでは読めないため、バックアップも調べてください。",
                 "backup": "バックアップを作った時点で iPhone にあった写真・動画を取り出します。消す前のバックアップなら、消した写真も入っています。新しいバックアップを作ると上書きされるので、復元が終わるまで作らないでください。",
                 "denied": ""}
        text = notes.get(item["type"])
        if text is None:
            text = "これはパソコン本体のドライブです。使っている間も書き込みが続くため、消したファイルが上書きされている場合があります。" if item.get("system") else ""
        self.drive_note.configure(text=text)

    def pick_files(self):
        paths = filedialog.askopenfilenames(title="調べるファイルを選んでください（複数選べます）",
                                            filetypes=[("すべてのファイル", "*"), ("写真・動画", "*.jpg *.jpeg *.heic *.png *.gif *.mov *.mp4"),
                                                       ("ドライブのイメージ", "*.img *.dd *.bin *.raw *.iso *.dmg *.001")])
        if paths:
            label = os.path.basename(paths[0]) if len(paths) == 1 else f"{len(paths)} 個のファイル"
            targets = [iphone.file_target(p) for p in paths]
            for t in targets:
                t["raw"] = os.path.splitext(t["path"])[1].lower() in undelete.DISK_IMAGE_EXT
            self.begin_targets(targets, label)

    def pick_folder(self):
        d = filedialog.askdirectory(title="調べるフォルダを選んでください")
        if not d:
            return
        try:
            targets = iphone.folder_targets(d)
        except iphone.EncryptedBackup:
            self.encrypted_help()
            return
        if not targets:
            messagebox.showinfo(APP_NAME, "このフォルダには調べられるファイルがありませんでした。")
            return
        self.begin_targets(targets, os.path.basename(d.rstrip("/\\")) or d)

    def encrypted_help(self):
        messagebox.showinfo(APP_NAME, "このバックアップはパスワードで暗号化されているため、このアプリでは中身を読めません。\n\n"
                            "新しいバックアップを作ると、消した写真が入った古いバックアップが上書きされることがあります。復元が終わるまで新しいバックアップは作らないでください。")

    def start_scan(self):
        sel = self.drive_tree.selection()
        if not (sel and sel[0].isdigit()):
            return
        item = self.drives[int(sel[0])]
        t = item["type"]
        if t == "drive":
            self.begin(item["path"], item)
        elif t == "backup":
            if item["encrypted"]:
                self.encrypted_help()
                return
            try:
                targets = iphone.backup_targets(item["path"])
            except iphone.EncryptedBackup:
                self.encrypted_help()
                return
            if not targets:
                messagebox.showinfo(APP_NAME, "このバックアップには写真・動画が入っていませんでした。")
                return
            self.begin_targets(targets, item["label"].split("—")[0].strip())
        elif t == "denied":
            messagebox.showinfo(APP_NAME, "Mac の設定で、このアプリが iPhone のバックアップを読むことを許可してください。\n\n"
                                "「システム設定」→「プライバシーとセキュリティ」→「フルディスクアクセス」で「Fukugen」（Python から起動した場合は「ターミナル」）をオンにして、アプリを開き直してください。")
        elif t == "usb":
            if core.IS_MAC:
                messagebox.showinfo(APP_NAME, "Mac では、iPhone の写真を直接読み込めません。\n\n"
                                    "1. これから開く「イメージキャプチャ」で iPhone を選び、読み込み先を新しいフォルダにして「すべてを読み込む」を押します。\n"
                                    "2. 終わったら、このアプリの「フォルダを選んで調べる…」でそのフォルダを選びます。")
                subprocess.Popen(["open", "-a", "Image Capture"])
                return
            dest = tempfile.mkdtemp(prefix="iphone_")

            def prep(report, stop):
                name = iphone.import_usb(dest, lambda a, b: report(f"iPhone から写真・動画を読み込んでいます… {a} / {b} 件", a / b if b else 0), stop,
                                         on_wait=lambda text, left: report(f"{text}（あと {left} 秒待ちます）", 0))
                report(f"{name} から読み込みました。一覧を作っています…", 1)
                return iphone.folder_targets(dest)
            self.begin_targets(None, item["name"], prep=prep)

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
        self.status = ttk.Label(f, text="", wraplength=1120, justify="left")
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
        src.close()
        label = (item or {}).get("label", os.path.basename(path)).strip()
        self.begin_targets([{"path": path, "orig": None, "label": label, "raw": True}], label, item)

    def begin_targets(self, targets, label, item=None, prep=None):
        self.reset()
        self.source_item = item
        self.targets = targets or []
        self.src_label.configure(text=label)
        self.show(self.results)
        self.scanning = True
        self.stop.clear()
        self.stop_btn.state(["!disabled"])
        self.t0 = time.time()
        gen = self.gen
        self.status.configure(text="準備しています…")
        threading.Thread(target=self.scan_worker, args=(targets, prep, gen), daemon=True).start()
        threading.Thread(target=self.media_worker, args=(gen,), daemon=True).start()

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

    def scan_worker(self, targets, prep, gen):
        keep_small, all_off, all_types = self.keep_small.get(), self.all_offsets.get(), self.all_types.get()
        bad = 0
        try:
            if prep:
                targets = prep(lambda text, frac: self.q.put(("stage", gen, text, frac)), self.stop)
            self.q.put(("targets", gen, targets))
            sizes = []
            for t in targets:
                try:
                    sizes.append(os.path.getsize(t["path"]) if os.path.isfile(t["path"]) else 0)
                except OSError:
                    sizes.append(0)
            done, last = 0, [0.0]
            for ti, t in enumerate(targets):
                if self.stop.is_set() or gen != self.gen:
                    break
                try:
                    src = core.Source(t["path"])
                except OSError:
                    self.q.put(("count", gen, "unreadable"))
                    continue
                total = sum(sizes) if len(targets) > 1 else src.size
                known = set()
                if t.get("raw"):
                    fsname = [""]

                    def on_vol(kind):
                        fsname[0] = kind
                        self.q.put(("stage", gen, f"削除したファイルの記録（{kind}）を調べています…", 0))

                    def fs_prog(f, last_t=[0.0]):
                        if time.time() - last_t[0] > 0.3:
                            last_t[0] = time.time()
                            self.q.put(("stage", gen, f"削除したファイルの記録（{fsname[0]}）を調べています… {int(f * 100)}%", f))
                    for r in undelete.scan(src, all_types, fs_prog, self.stop, on_vol):
                        if gen != self.gen or self.stop.is_set():
                            break
                        key = core.record_key(src, r)
                        if key in self.seen:
                            self.q.put(("count", gen, "dup"))
                            continue
                        self.seen.add(key)
                        known.add(r["s"])
                        r["src"] = ti
                        self.q.put(("item", gen, r))
                    self.q.put(("stage", gen, "空き領域から写真・動画を探しています…", 0))

                def prog(p, base=done):
                    if time.time() - last[0] > 0.2:
                        last[0] = time.time()
                        self.q.put(("prog", gen, base + p, total))
                for r in core.iter_scan(src, all_off, self.stop, prog):
                    if gen != self.gen:
                        break
                    if r["s"] in known:
                        continue
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
                    r["src"] = ti
                    if r["s"] == 0 and r["e"] >= src.size - 1:
                        r["orig"] = t.get("orig")
                        if not r.get("date") and t.get("date"):
                            r["date"] = t["date"]
                    self.q.put(("item", gen, r))
                done += src.size
                bad += src.bad
                src.close()
                self.q.put(("prog", gen, done, sum(sizes) if len(targets) > 1 else done))
        except Exception as e:
            self.q.put(("error", gen, str(e)))
        self.q.put(("done", gen, bad))

    def media_worker(self, gen):
        srcs = {}

        def source(r):
            i = r["src"]
            if i not in srcs:
                srcs[i] = core.Source(self.targets[i]["path"])
            return srcs[i]
        while gen == self.gen:
            try:
                _, _, job = self.work.get(timeout=0.5)
            except queue.Empty:
                continue
            kind, iid = job
            r = self.records.get(iid)
            if not r:
                continue
            try:
                src = source(r)
                if kind == "thumb":
                    self.q.put(("thumb", gen, iid, render_image(src, r, THUMB) if r["kind"] == "image" else None))
                elif kind == "preview":
                    self.q.put(("preview", gen, iid, render_image(src, r, PREVIEW) if r["kind"] == "image" else None))
                elif kind == "open":
                    self.q.put(("open", gen, core.write_record(src, r, os.path.join(tempfile.gettempdir(), "fukugen_preview"))))
            except OSError as e:
                if kind != "thumb":
                    self.q.put(("error", gen, str(e)))
        for s_ in srcs.values():
            s_.close()

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
            self.stats[msg[2]] = self.stats.get(msg[2], 0) + 1
        elif kind == "prog":
            self.source_size = msg[3]
            self.show_progress(msg[2])
        elif kind == "stage":
            self.status.configure(text=msg[2])
            self.pbar["value"] = msg[3] * 1000
        elif kind == "targets":
            self.targets = msg[2]
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
            self.status.configure(text=msg[2])
            messagebox.showerror(APP_NAME, msg[2])

    def show_progress(self, pos, done=False, bad=0):
        s = self.stats
        found = f"写真・画像 {s['image']} 件　動画 {s['video']} 件" + (f"　その他 {s['other']} 件" if s.get("other") else "") + \
            (f"（うち元の名前で見つかった削除ファイル {s['named']} 件）" if s.get("named") else "")
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
            if s.get("unreadable"):
                extra.append(f"開けなかったファイル {s['unreadable']} 個")
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
        iid = f"f{r.get('src', 0)}_{r.get('uid', r['s'])}"
        r["checked"] = not r.get("empty")
        self.records[iid] = r
        self.stats[r["kind"]] = self.stats.get(r["kind"], 0) + 1
        if r.get("fs"):
            self.stats["named"] = self.stats.get("named", 0) + 1
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
        name = r.get("orig") or ((d.strftime("%Y-%m-%d_%H%M%S") if d else f"{r['s']:012X}") + "." + r["ext"])
        state = ("中身が消えています" if r.get("empty") else "上書きの可能性" if r.get("overwritten") else
                 "一部欠損" if r["trunc"] and r["kind"] != "noindex" else "索引なし" if r["kind"] == "noindex" else "完全")
        typ = {"image": "写真", "video": "動画", "noindex": "動画", "other": "ファイル"}[r["kind"]] + " " + (r["ext"].upper() if r["kind"] != "image" else r["type"])
        self.tree.insert(parent, idx, iid=iid, text=f"  {name}" + (f"  {dims}" if dims else ""),
                         values=(CHECK_ON if r["checked"] else CHECK_OFF, d.strftime("%Y/%m/%d %H:%M") if d else "不明", typ,
                                 core.fmt_size(r["e"] - r["s"]), state))
        if iid in self.photos:
            self.tree.item(iid, image=self.photos[iid])
        elif HAS_PIL:
            if r["kind"] == "image":
                self.work.put((1, r["s"], ("thumb", iid)))
            else:
                ph = ImageTk.PhotoImage(placeholder("video" if r["kind"] != "other" else "file", THUMB))
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
        t = self.targets[r.get("src", 0)] if self.targets else {}
        if r.get("fs"):
            lines.append(f"元の場所：{r['folder']}" if r["folder"].startswith("（") else f"元の場所：{r['folder'].rstrip(chr(92))}\\{r['orig']}")
            lines.append(f"ドライブの記録（{r['fs']}）から見つかった削除ファイルです。")
            if r.get("empty"):
                lines.append("記録は残っていましたが、中身は 0 で消されています（取り戻せません）。")
            elif r.get("overwritten"):
                lines.append("この場所の一部に、あとから別のデータが書かれています。中身が壊れているかもしれません。")
        elif r.get("orig"):
            lines.append(f"元のファイル：{t.get('label') or r['orig']}")
        else:
            lines.append(f"位置：{os.path.basename(t.get('label') or t.get('path', ''))} の {r['s']:,} バイト目")
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
        if self.source_item and core.same_drive(self.source_item, out):
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
                srcs = {}
                o = core.Output(root, True)
                for r in chosen:
                    i = r.get("src", 0)
                    if i not in srcs:
                        srcs[i] = core.Source(self.targets[i]["path"])
                    src = srcs[i]
                    p = core.write_record(src, r, root, on_bytes=lambda n: state.__setitem__("bytes", state["bytes"] + n))
                    state["n"] += 1
                    d = r.get("date")
                    o.counts[r["kind"]] += 1
                    o.records.append({"path": os.path.relpath(p, root), "kind": r["kind"], "type": r["type"],
                                      "date": d.isoformat(" ") if d else "", "w": r.get("w") or 0, "h": r.get("h") or 0,
                                      "size": r["e"] - r["s"], "offset": r["s"], "partial": bool(r["trunc"]), "model": r.get("model", "")})
                o.write_index(self.src_label.cget("text"), self.source_size, self.source_size, time.time() - self.t0,
                              sum(x.bad for x in srcs.values()))
                core.give_back_ownership(root)
                for x in srcs.values():
                    x.close()
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


def gui_selftest(root, app, out_path):
    """Start-up check used by the build: list drives, scan a small card image, write the result."""
    import json
    import selftest
    res = {"ok": False, "startup_seconds": round(time.time() - T0, 1)}
    tmp = tempfile.mkdtemp()
    img = os.path.join(tmp, "card.img")
    selftest.make_card_image(img)
    t = time.time()

    def finish():
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False)
        root.destroy()

    def wait_scan():
        if app.scanning and time.time() - t < 90:
            root.after(300, wait_scan)
            return
        res["found"] = len(app.records)
        res["thumbnails"] = len(app.photos)
        if res["thumbnails"] < res["found"] and time.time() - t < 90:
            root.after(300, wait_scan)
            return
        res["ok"] = res["found"] >= 3 and res["thumbnails"] >= 3
        root.after(500, finish)

    def wait_drives():
        if not app.drives and time.time() - t < 45:
            root.after(300, wait_drives)
            return
        res["drives"] = [d["label"] for d in app.drives]
        app.begin(img, None)
        root.after(500, wait_scan)

    wait_drives()


def main():
    selftest_out = sys.argv[sys.argv.index("--selftest-gui") + 1] if "--selftest-gui" in sys.argv else None
    if core.IS_WIN and not selftest_out and not core.is_admin() and "--no-elevate" not in sys.argv:
        try:
            if relaunch_elevated():
                close_splash()
                return
        except Exception:
            log_error(traceback.format_exc())
    try:
        root = tk.Tk()

        def report(*exc):
            text = "".join(traceback.format_exception(*exc))
            path = log_error(text)
            messagebox.showerror(APP_NAME, "予期しないエラーが起きました。\n\n" + text[-800:] + (f"\n\n記録: {path}" if path else ""))
        root.report_callback_exception = report
        app = App(root)
        close_splash()
        root.lift()
        root.attributes("-topmost", True)
        root.after(800, lambda: root.attributes("-topmost", False))
        root.focus_force()
        if selftest_out:
            gui_selftest(root, app, selftest_out)
        else:
            images = [a for a in sys.argv[1:] if os.path.isfile(a)]
            if images:
                root.after(300, lambda: app.begin(images[0], None))
        root.mainloop()
    except Exception:
        close_splash()
        text = traceback.format_exc()
        if selftest_out:
            with open(selftest_out, "w", encoding="utf-8") as f:
                f.write('{"ok": false, "error": %r}' % text)
        show_fatal(text)


if __name__ == "__main__":
    main()
