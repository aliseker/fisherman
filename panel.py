"""
=============================================================================
Metin2 Fishing Helper — Kontrol Paneli
=============================================================================
Her zaman üstte kalan (always-on-top) tkinter penceresi.
Başlatmak için:  python panel.py

Özellikler:
  - Start / Stop
  - NORMAL / GOLD mod
  - 📐 Chat ve Minigame alanını ekrandan sürükle-bırak seçme
  - Canlı log akışı (renkli)
  - Anlık istatistik
=============================================================================
"""

import tkinter as tk
from tkinter import scrolledtext
import threading
import queue
import logging
import time
import sys
import os
import json

import subprocess
import urllib.request
import webbrowser

_base_dir = os.path.dirname(os.path.abspath(__file__))
_venv_site = os.path.join(_base_dir, "venv_gpu", "Lib", "site-packages")
if os.path.isdir(_venv_site) and _venv_site not in sys.path:
    sys.path.insert(0, _venv_site)
    for _sub in ("win32", "win32/lib", "Pythonwin"):
        _sp = os.path.join(_venv_site, _sub)
        if os.path.isdir(_sp) and _sp not in sys.path:
            sys.path.insert(0, _sp)
    if hasattr(os, "add_dll_directory"):
        for _dll in (os.path.join(_venv_site, "pywin32_system32"), os.path.join(_venv_site, "torch", "lib")):
            if os.path.isdir(_dll):
                try:
                    os.add_dll_directory(_dll)
                except Exception:
                    pass
sys.path.insert(0, _base_dir)

from fisherman import (FisherConfig, AppState, FishingHelper, _OCR_BACKEND, _REMOTE_OCR_URL,
                       load_session, reset_session)

PRICE_REFRESH_MIN = 60   # bot çalışırken fiyatlar kaç dakikada bir siteden yenilensin


# ===========================================================================
# Log yönlendirici
# ===========================================================================
class QueueLogHandler(logging.Handler):
    def __init__(self, log_queue: queue.Queue):
        super().__init__()
        self._q = log_queue

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._q.put_nowait(self.format(record))
        except Exception:
            pass


# ===========================================================================
# Ekran üzerinde sürükle-bırak ROI seçici
# ===========================================================================
class ROISelector:
    """
    Tüm ekranı kaplayan yarı-saydam bir tkinter penceresi açar.
    Kullanıcı fareyle bir dikdörtgen çizer; koordinatlar döndürülür.
    """

    def __init__(self, root: tk.Tk, title: str = "Alan seç"):
        self._root = root
        self._title = title
        self._result: dict | None = None

    def select(self) -> dict | None:
        """
        Blocking — seçim tamamlanana kadar bekler.
        Returns: {"top": y, "left": x, "width": w, "height": h} veya None
        """
        import mss, numpy as np, cv2
        from PIL import Image, ImageTk

        # Ekran görüntüsü al
        with mss.mss() as sct:
            mon = sct.monitors[1]
            shot = sct.grab(mon)
            img = np.array(shot)  # BGRA
            img_rgb = cv2.cvtColor(img, cv2.COLOR_BGRA2RGB)

        sw, sh = mon["width"], mon["height"]

        overlay = tk.Toplevel(self._root)
        overlay.title(self._title)
        overlay.geometry(f"{sw}x{sh}+0+0")
        overlay.overrideredirect(True)          # başlık çubuğu yok
        overlay.attributes("-topmost", True)
        overlay.attributes("-alpha", 0.85)
        overlay.configure(bg="black")

        # PIL → tk
        pil_img = Image.fromarray(img_rgb)
        tk_img = ImageTk.PhotoImage(pil_img)

        canvas = tk.Canvas(overlay, width=sw, height=sh,
                           cursor="crosshair", bg="black",
                           highlightthickness=0)
        canvas.pack(fill="both", expand=True)
        canvas.create_image(0, 0, anchor="nw", image=tk_img)

        # Yardım etiketi
        help_lbl = canvas.create_text(
            sw // 2, 30,
            text=f"[ {self._title} ] — Sürükle, bırak. ESC = iptal.",
            fill="yellow", font=("Segoe UI", 14, "bold"),
        )

        state = {"x0": 0, "y0": 0, "rect": None}

        def on_press(e):
            state["x0"], state["y0"] = e.x, e.y
            if state["rect"]:
                canvas.delete(state["rect"])

        def on_drag(e):
            if state["rect"]:
                canvas.delete(state["rect"])
            state["rect"] = canvas.create_rectangle(
                state["x0"], state["y0"], e.x, e.y,
                outline="#00ff88", width=2, fill="", dash=(6, 3),
            )

        def on_release(e):
            x0, y0 = state["x0"], state["y0"]
            x1, y1 = e.x, e.y
            left   = min(x0, x1)
            top    = min(y0, y1)
            width  = abs(x1 - x0)
            height = abs(y1 - y0)
            if width > 10 and height > 10:
                self._result = {"top": top, "left": left,
                                "width": width, "height": height}
            overlay.destroy()

        def on_esc(e):
            overlay.destroy()

        canvas.bind("<ButtonPress-1>",   on_press)
        canvas.bind("<B1-Motion>",       on_drag)
        canvas.bind("<ButtonRelease-1>", on_release)
        overlay.bind("<Escape>",         on_esc)

        self._root.wait_window(overlay)
        return self._result


# ===========================================================================
# Ana Panel
# ===========================================================================
class FisherPanel(tk.Tk):
    BG      = "#1a1a2e"
    SURFACE = "#16213e"
    ACCENT  = "#0f3460"
    GREEN   = "#00d26a"
    RED     = "#ff6b6b"
    GOLD    = "#ffd700"
    SILVER  = "#c0c0c0"
    TEXT    = "#e0e0e0"
    DIM     = "#888888"
    FONT_MONO = ("Consolas", 9)
    FONT_UI   = ("Segoe UI", 10)
    FONT_H    = ("Segoe UI Semibold", 11)

    def __init__(self):
        super().__init__()
        self.title("⚓ Metin2 Fishing Helper")
        self.geometry("500x830")
        self.resizable(True, True)
        self.configure(bg=self.BG)
        self.attributes("-topmost", True)

        # (Yerel modda OCR zaten bu işlemin içinde GPU ile çalışır; ayrı servis başlatılmaz.
        #  ocr_server.py yalnızca sanal makineler için ocr_servisi_baslat.bat ile elle açılır.)
        self._ocr_proc: subprocess.Popen | None = None
        self._price_stop = threading.Event()

        # Çalışma zamanı ROI'lar (panel üzerinden ayarlanabilir)
        self._chat_roi    = {"top": 620, "left": 130, "width": 500, "height": 70}
        self._minigame_roi = {"top": 222, "left": 365, "width": 250, "height": 230}

        # Ayarları yükle
        self._load_settings()

        # FishingHelper instance
        self._helper: FishingHelper | None = None
        self._worker_thread: threading.Thread | None = None
        self._running = False

        # Log queue
        self._log_queue: queue.Queue = queue.Queue(maxsize=500)
        handler = QueueLogHandler(self._log_queue)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S"
        ))
        logging.getLogger().addHandler(handler)
        logging.getLogger().setLevel(logging.INFO)

        self._build_ui()
        self._poll_logs()
        self._poll_stats()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # -----------------------------------------------------------------------
    # Ayarlar Kaydet / Yükle
    # -----------------------------------------------------------------------
    def _load_settings(self) -> None:
        try:
            if os.path.exists("settings.json"):
                with open("settings.json", "r") as f:
                    data = json.load(f)
                    if "chat_roi" in data:
                        self._chat_roi = data["chat_roi"]
                    if "minigame_roi" in data:
                        self._minigame_roi = data["minigame_roi"]
        except Exception:
            pass

    def _save_settings(self) -> None:
        try:
            with open("settings.json", "w") as f:
                json.dump({
                    "chat_roi": self._chat_roi,
                    "minigame_roi": self._minigame_roi
                }, f)
        except Exception:
            pass

    # -----------------------------------------------------------------------
    # UI
    # -----------------------------------------------------------------------
    def _build_ui(self) -> None:
        pad = {"padx": 10, "pady": 4}

        # ── Başlık ──────────────────────────────────────────────────────────
        header = tk.Frame(self, bg=self.ACCENT, height=50)
        header.pack(fill="x")
        tk.Label(header, text="⚓  Metin2 Fishing Helper",
                 font=("Segoe UI Semibold", 13),
                 fg=self.TEXT, bg=self.ACCENT,
                 ).pack(side="left", padx=14, pady=10)
        
        ocr_text = f"OCR: {_OCR_BACKEND.upper()}"
        ocr_color = self.GREEN
        if _REMOTE_OCR_URL:
            ocr_text = f"OCR: Uzak Servis"
        elif _OCR_BACKEND == "easyocr":
            ocr_text = "OCR: GPU (Yerel)"
        else:
            ocr_text = "OCR: YOK (Metin Okunamaz!)"
            ocr_color = self.RED
            
        self._ocr_label = tk.Label(
            header, text=ocr_text,
            font=self.FONT_UI, fg=ocr_color, bg=self.ACCENT,
        )
        self._ocr_label.pack(side="right", padx=14)

        # ── Mod ──────────────────────────────────────────────────────────────
        mode_frame = tk.LabelFrame(self, text=" Mod ", bg=self.SURFACE,
                                   fg=self.DIM, font=self.FONT_H,
                                   bd=1, relief="flat")
        mode_frame.pack(fill="x", **pad)
        tk.Label(mode_frame, text="📄 Sistem 'ayarlar.json' tabanlı çalışıyor.\nSadece 'true' işaretli balıklar yakalanır.",
                 bg=self.SURFACE, fg=self.GREEN, font=self.FONT_UI, justify="left").pack(anchor="w", padx=8, pady=(8, 4))
                 
        tk.Button(mode_frame, text="🖼️  Görsel Ayarlar (Whitelist Düzenle)",
                  bg=self.ACCENT, fg=self.TEXT,
                  font=("Segoe UI", 10, "bold"), relief="flat", cursor="hand2",
                  padx=8, pady=6,
                  command=self._open_visual_settings,
                  ).pack(fill="x", padx=8, pady=(0, 8))

        # ── ROI Seçici Butonları ─────────────────────────────────────────────
        roi_frame = tk.LabelFrame(self, text=" 📐 Alan Seçimi (Ekrandan sürükle) ",
                                  bg=self.SURFACE, fg=self.DIM,
                                  font=self.FONT_H, bd=1, relief="flat")
        roi_frame.pack(fill="x", **pad)

        # Chat ROI
        chat_row = tk.Frame(roi_frame, bg=self.SURFACE)
        chat_row.pack(fill="x", padx=8, pady=4)
        tk.Label(chat_row, text="Chat alanı:", fg=self.TEXT,
                 bg=self.SURFACE, font=self.FONT_UI, width=14, anchor="w",
                 ).pack(side="left")
        self._chat_roi_label = tk.Label(
            chat_row, text=self._roi_str(self._chat_roi),
            fg=self.GREEN, bg=self.SURFACE, font=self.FONT_MONO,
        )
        self._chat_roi_label.pack(side="left", padx=6)
        tk.Button(chat_row, text="Seç",
                  bg=self.ACCENT, fg=self.TEXT,
                  font=self.FONT_UI, relief="flat", cursor="hand2",
                  padx=8, pady=2,
                  command=self._select_chat_roi,
                  ).pack(side="right")

        # Minigame ROI
        mg_row = tk.Frame(roi_frame, bg=self.SURFACE)
        mg_row.pack(fill="x", padx=8, pady=4)
        tk.Label(mg_row, text="Minigame alanı:", fg=self.TEXT,
                 bg=self.SURFACE, font=self.FONT_UI, width=14, anchor="w",
                 ).pack(side="left")
        self._mg_roi_label = tk.Label(
            mg_row, text=self._roi_str(self._minigame_roi),
            fg=self.GREEN, bg=self.SURFACE, font=self.FONT_MONO,
        )
        self._mg_roi_label.pack(side="left", padx=6)
        tk.Button(mg_row, text="Seç",
                  bg=self.ACCENT, fg=self.TEXT,
                  font=self.FONT_UI, relief="flat", cursor="hand2",
                  padx=8, pady=2,
                  command=self._select_mg_roi,
                  ).pack(side="right")

        # ── Kontrol Butonları ────────────────────────────────────────────────
        ctrl_frame = tk.Frame(self, bg=self.BG)
        ctrl_frame.pack(fill="x", **pad)
        self._start_btn = tk.Button(
            ctrl_frame, text="▶  BAŞLAT",
            font=("Segoe UI Semibold", 12),
            bg=self.GREEN, fg="#000000",
            activebackground="#00b055", activeforeground="#000000",
            relief="flat", cursor="hand2", bd=0,
            padx=20, pady=8,
            command=self._toggle_app,
        )
        self._start_btn.pack(side="left", fill="x", expand=True, padx=(0, 5))
        tk.Button(ctrl_frame, text="🗑  Temizle",
                  font=self.FONT_UI,
                  bg=self.ACCENT, fg=self.TEXT,
                  activebackground="#1a4a8a", activeforeground=self.TEXT,
                  relief="flat", cursor="hand2", bd=0,
                  padx=12, pady=8,
                  command=self._clear_logs,
                  ).pack(side="right")

        # ── Durum ────────────────────────────────────────────────────────────
        status_frame = tk.Frame(self, bg=self.SURFACE, pady=8)
        status_frame.pack(fill="x", padx=10)
        self._status_dot = tk.Label(status_frame, text="●",
                                    font=("Segoe UI", 14),
                                    fg=self.RED, bg=self.SURFACE)
        self._status_dot.pack(side="left", padx=(10, 4))
        self._status_label = tk.Label(status_frame, text="Durduruldu",
                                      font=("Segoe UI Semibold", 10),
                                      fg=self.RED, bg=self.SURFACE)
        self._status_label.pack(side="left")
        self._state_label = tk.Label(status_frame, text="",
                                     font=self.FONT_UI,
                                     fg=self.DIM, bg=self.SURFACE)
        self._state_label.pack(side="right", padx=10)

        # ── İstatistikler ────────────────────────────────────────────────────
        stats_frame = tk.LabelFrame(self, text=" İstatistikler ",
                                    bg=self.SURFACE, fg=self.DIM,
                                    font=self.FONT_H, bd=1, relief="flat")
        stats_frame.pack(fill="x", padx=10, pady=(0, 4))
        labels = [("Cast","total_casts"), ("Hit","hits"), ("Miss","misses"),
                  ("Değerli","valuable_fish"), ("Skip","worthless_fish"), ("Süre","duration")]
        self._stat_vars = {}
        for i, (lbl, key) in enumerate(labels):
            col, row = i % 3, i // 3
            cell = tk.Frame(stats_frame, bg=self.SURFACE)
            cell.grid(row=row, column=col, padx=12, pady=5, sticky="w")
            tk.Label(cell, text=lbl, font=("Segoe UI", 8),
                     fg=self.DIM, bg=self.SURFACE).pack(anchor="w")
            var = tk.StringVar(value="0")
            self._stat_vars[key] = var
            tk.Label(cell, textvariable=var,
                     font=("Segoe UI Semibold", 15),
                     fg=self.TEXT, bg=self.SURFACE).pack(anchor="w")

        # ── 💰 Kazanç & Piyasa Değeri ─────────────────────────────────────────
        earn_frame = tk.LabelFrame(self, text=" 💰 Tahmini Kazanç & Piyasa Değeri ",
                                   bg=self.SURFACE, fg=self.GOLD,
                                   font=self.FONT_H, bd=1, relief="flat")
        earn_frame.pack(fill="x", padx=10, pady=(4, 4))

        won_row = tk.Frame(earn_frame, bg=self.SURFACE)
        won_row.pack(fill="x", padx=10, pady=(6, 2))
        self._won_label = tk.Label(won_row, text="0.00 Won",
                                   font=("Segoe UI Semibold", 16),
                                   fg=self.GOLD, bg=self.SURFACE)
        self._won_label.pack(side="left")
        self._yang_label = tk.Label(won_row, text="(0 Yang)",
                                    font=self.FONT_UI,
                                    fg=self.DIM, bg=self.SURFACE)
        self._yang_label.pack(side="left", padx=8)

        self._catch_rate_label = tk.Label(earn_frame,
                                          text="Tutulan: 0 | Kaçan: 0  (Başarı: %0)",
                                          font=self.FONT_UI, fg=self.GREEN, bg=self.SURFACE)
        self._catch_rate_label.pack(anchor="w", padx=10, pady=(0, 6))

        earn_btn_row = tk.Frame(earn_frame, bg=self.SURFACE)
        earn_btn_row.pack(fill="x", padx=10, pady=(0, 4))
        
        tk.Button(earn_btn_row, text="📊  Balık Detayları & Kazanç",
                  font=self.FONT_UI, bg=self.ACCENT, fg=self.TEXT,
                  relief="flat", cursor="hand2", padx=10, pady=4,
                  command=self._open_earnings_modal).pack(side="left", fill="x", expand=True, padx=(0, 4))
                  
        tk.Button(earn_btn_row, text="✏️  Birim Fiyatları Düzenle",
                  font=self.FONT_UI, bg="#2a5298", fg="#ffffff",
                  activebackground="#1e3c72", activeforeground="#ffffff",
                  relief="flat", cursor="hand2", padx=10, pady=4,
                  command=self._open_price_editor).pack(side="right", fill="x", expand=True, padx=(4, 0))

        tk.Button(earn_frame, text="🌐  Siteden Canlı Fiyatları Güncelle (DostGame Lucifer)",
                  font=("Segoe UI Semibold", 10), bg="#1e5f74", fg="#ffffff",
                  activebackground="#133b47", activeforeground="#ffffff",
                  relief="flat", cursor="hand2", padx=10, pady=5,
                  command=self._update_prices_from_web).pack(fill="x", padx=10, pady=(2, 4))

        tk.Button(earn_frame, text="♻️  Sayaçları ve Süreyi Sıfırla",
                  font=self.FONT_UI, bg="#5a2a2a", fg="#ffffff",
                  activebackground="#7a3a3a", activeforeground="#ffffff",
                  relief="flat", cursor="hand2", padx=10, pady=3,
                  command=self._reset_counters).pack(fill="x", padx=10, pady=(0, 8))

        # ── Log ─────────────────────────────────────────────────────────────
        log_header = tk.Frame(self, bg=self.BG)
        log_header.pack(fill="x", padx=10, pady=(4, 0))
        tk.Label(log_header, text="📋  Log Çıktısı",
                 font=self.FONT_H, fg=self.TEXT, bg=self.BG).pack(side="left")
        self._autoscroll_var = tk.BooleanVar(value=True)
        tk.Checkbutton(log_header, text="Oto kaydır",
                       variable=self._autoscroll_var,
                       bg=self.BG, fg=self.DIM, selectcolor=self.ACCENT,
                       activebackground=self.BG,
                       font=self.FONT_UI).pack(side="right")

        self._log_box = scrolledtext.ScrolledText(
            self, wrap="word", font=self.FONT_MONO,
            bg="#0d0d1a", fg="#b0ffb0",
            insertbackground=self.TEXT, relief="flat", bd=0,
            state="disabled",
        )
        self._log_box.pack(fill="both", expand=True, padx=10, pady=(2, 10))
        self._log_box.tag_config("ERROR",   foreground="#ff6b6b")
        self._log_box.tag_config("WARNING", foreground="#ffd700")
        self._log_box.tag_config("INFO",    foreground="#b0ffb0")
        self._log_box.tag_config("HIT",     foreground="#00ff88")
        self._log_box.tag_config("MISS",    foreground="#ff9944")

    # -----------------------------------------------------------------------
    # ROI Seçici
    # -----------------------------------------------------------------------
    @staticmethod
    def _roi_str(roi: dict) -> str:
        return f"x={roi['left']} y={roi['top']}  {roi['width']}×{roi['height']}"

    def _select_chat_roi(self) -> None:
        """Chat alanını ekrandan seçtir."""
        self._append_log("💡 Chat alanını seçin: sürükle → bırak. ESC = iptal")
        # Panel'i geçici olarak geri çek ama topmost'u koru
        self.after(200, self._do_select_chat)

    def _do_select_chat(self) -> None:
        sel = ROISelector(self, "Chat alanını seçin")
        result = sel.select()
        if result:
            self._chat_roi = result
            self._chat_roi_label.config(text=self._roi_str(result))
            self._append_log(f"✅ Chat ROI güncellendi: {result}")
            self._save_settings()
        else:
            self._append_log("⚠️ Chat ROI seçimi iptal edildi.")

    def _select_mg_roi(self) -> None:
        self._append_log("💡 Minigame (mavi balık) alanını seçin: sürükle → bırak. ESC = iptal")
        self.after(200, self._do_select_mg)

    def _do_select_mg(self) -> None:
        sel = ROISelector(self, "Minigame alanını seçin")
        result = sel.select()
        if result:
            self._minigame_roi = result
            self._mg_roi_label.config(text=self._roi_str(result))
            self._append_log(f"✅ Minigame ROI güncellendi: {result}")
            self._save_settings()
        else:
            self._append_log("⚠️ Minigame ROI seçimi iptal edildi.")

    # -----------------------------------------------------------------------
    # Görsel Whitelist
    # -----------------------------------------------------------------------
    def _open_visual_settings(self) -> None:
        try:
            from PIL import Image, ImageTk
        except ImportError:
            self._append_log("⚠️ Pillow (PIL) yüklü değil, ikonlar gösterilemeyebilir.")
            Image, ImageTk = None, None
            
        settings_win = tk.Toplevel(self)
        settings_win.title("Görsel Whitelist Düzenleyici")
        settings_win.geometry("450x650")
        settings_win.configure(bg=self.BG)
        settings_win.attributes("-topmost", True)
        
        # Üst Panel
        top_frame = tk.Frame(settings_win, bg=self.ACCENT, pady=10)
        top_frame.pack(fill="x")
        tk.Label(top_frame, text="Yakalanacakları (TUT) seçin", bg=self.ACCENT, fg=self.TEXT, font=self.FONT_H).pack()
        
        # Load current config
        try:
            with open("ayarlar.json", "r", encoding="utf-8") as f:
                current_wl = json.load(f)
        except Exception:
            current_wl = {}

        canvas = tk.Canvas(settings_win, bg=self.BG, highlightthickness=0)
        scrollbar = tk.Scrollbar(settings_win, orient="vertical", command=canvas.yview)
        scrollable_frame = tk.Frame(canvas, bg=self.BG)

        scrollable_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )

        canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        # Mouse tekerleği ile kaydırma desteği (Windows için)
        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1*(event.delta/120)), "units")
        canvas.bind_all("<MouseWheel>", _on_mousewheel)

        canvas.pack(side="top", fill="both", expand=True, padx=5, pady=5)
        scrollbar.pack(side="right", fill="y", before=canvas)
        
        if not hasattr(self, "img_refs"):
            self.img_refs = []
        vars_dict = {}
        
        for item, value in current_wl.items():
            f = tk.Frame(scrollable_frame, bg=self.SURFACE, pady=6, padx=6)
            f.pack(fill="x", pady=2, padx=5, expand=True)
            
            img_name = "soru_işareti" if item == "belli değil" else item.lower().replace(" ", "_")
            img_path = os.path.join("assets", f"{img_name}.png")
            
            if Image and ImageTk and os.path.exists(img_path):
                try:
                    pil_img = Image.open(img_path).resize((40, 40))
                    tk_img = ImageTk.PhotoImage(pil_img)
                    self.img_refs.append(tk_img)
                    tk.Label(f, image=tk_img, bg=self.SURFACE).pack(side="left", padx=5)
                except Exception:
                    tk.Label(f, text="🖼️", bg=self.SURFACE, fg=self.DIM, width=4).pack(side="left", padx=5)
            else:
                tk.Label(f, text="🖼️", bg=self.SURFACE, fg=self.DIM, width=4).pack(side="left", padx=5)
                
            tk.Label(f, text=item.title(), font=self.FONT_H, bg=self.SURFACE, fg=self.TEXT, width=18, anchor="w").pack(side="left")
            
            var = tk.BooleanVar(value=value)
            vars_dict[item] = var
            cb = tk.Checkbutton(f, text="TUT", variable=var, bg=self.SURFACE, fg=self.GREEN, 
                                selectcolor=self.ACCENT, activebackground=self.SURFACE, font=("Segoe UI", 12, "bold"))
            cb.pack(side="right", padx=10)

        def save_and_close():
            new_wl = {k: v.get() for k, v in vars_dict.items()}
            with open("ayarlar.json", "w", encoding="utf-8") as f:
                json.dump(new_wl, f, ensure_ascii=False, indent=4)
            self._append_log("✅ Görsel ayarlar başarıyla kaydedildi!")
            canvas.unbind_all("<MouseWheel>")
            settings_win.destroy()
            
        btn_frame = tk.Frame(settings_win, bg=self.BG, pady=10)
        btn_frame.pack(fill="x", side="bottom")
        tk.Button(btn_frame, text="💾 Ayarları Kaydet", bg=self.GREEN, fg="#000", 
                  font=("Segoe UI Semibold", 12), cursor="hand2", relief="flat",
                  command=save_and_close, pady=8).pack(fill="x", padx=20)

    # -----------------------------------------------------------------------
    # Başlat / Durdur
    # -----------------------------------------------------------------------
    def _toggle_app(self) -> None:
        if self._running:
            self._stop_app()
        else:
            self._start_app()

    def _start_app(self) -> None:
        whitelist = {}
        try:
            if os.path.exists("ayarlar.json"):
                with open("ayarlar.json", "r", encoding="utf-8") as f:
                    whitelist = json.load(f)
        except Exception as e:
            self._append_log(f"⚠️ ayarlar.json okunamadı, boş whitelist: {e}")

        config = FisherConfig(
            chat_roi=self._chat_roi,
            minigame_roi=self._minigame_roi,
            whitelist=whitelist,
            farm_duration_minutes=90,
            rest_duration_minutes=5,
        )
        self._running = True
        # EasyOCR modeli yüklemek uzun sürebilir (30-60sn ilk seferde)
        # Worker thread içinde başlatıyoruz ki UI donmayı önleyelim
        self._worker_thread = threading.Thread(
            target=self._run_worker,
            args=(config,),
            daemon=True,
            name="FisherWorker",
        )
        self._worker_thread.start()
        # OCR yüklenirken buton kilitlensin
        self._start_btn.config(
            text="⏳  Yükleniyor...",
            bg="#888800", activebackground="#666600",
            state="disabled",
        )
        self._status_dot.config(fg="#ffd700")
        self._status_label.config(text="EasyOCR yükleniyor, lütfen bekle...",
                                   fg="#ffd700")
        self._append_log("⏳ EasyOCR başlatılıyor (ilk seferde 30-60sn sürebilir)...")

    def _run_worker(self, config: FisherConfig) -> None:
        try:
            # FishingHelper.__init__ içinde ChatReader çağrılır →
            # EasyOCR modeli burada (arka planda) yüklenir
            self._helper = FishingHelper(config=config)
            
            # Yüklenme bitti, hemen ÇALIŞIYOR durumuna al
            self.after(0, self._set_status_running)
            
            # Fiyat yenileme botun olta atmasını ASLA engellemesin (tamamen arka planda)
            self._price_stop.clear()
            threading.Thread(target=self._price_refresh_loop, daemon=True, name="PriceLoop").start()
            
            # Botu hemen başlat (5 saniyelik geri sayım ve olta atma)
            self._helper.run()
        except Exception as e:
            logging.getLogger("Fisher").exception(f"Hata: {e}")
        finally:
            self._price_stop.set()
            self._running = False
            self.after(0, self._set_status_stopped)

    def _stop_app(self) -> None:
        self._price_stop.set()
        if self._helper:
            self._helper.running = False
            self._helper.state = AppState.STOPPED
            self._helper.save_session()
        self._running = False
        self._set_status_stopped()

    # -----------------------------------------------------------------------
    # Sayaç sıfırlama
    # -----------------------------------------------------------------------
    def _reset_counters(self) -> None:
        if self._running:
            self._append_log("⚠️ Sayaçları sıfırlamak için önce botu durdur.")
            return
        reset_session()
        self._helper = None
        self._show_stats(load_session(), running=False)
        self._append_log("♻️ Sayaçlar, süre ve kazanç sıfırlandı.")

    # -----------------------------------------------------------------------
    # Durum
    # -----------------------------------------------------------------------
    def _set_status_running(self) -> None:
        color = self.GOLD
        self._status_dot.config(fg=color)
        self._status_label.config(text="Çalışıyor — Whitelist Modu", fg=color)
        self._start_btn.config(
            text="⏹  DURDUR", bg=self.RED,
            activebackground="#cc4444",
            state="normal",   # yükleniyor disabled'dan geri aon
        )

    def _set_status_stopped(self) -> None:
        self._status_dot.config(fg=self.RED)
        self._status_label.config(text="Durduruldu", fg=self.RED)
        self._state_label.config(text="")
        self._start_btn.config(
            text="▶  BAŞLAT", bg=self.GREEN,
            activebackground="#00b055",
            state="normal",
        )

    # -----------------------------------------------------------------------
    # Log
    # -----------------------------------------------------------------------
    def _poll_logs(self) -> None:
        try:
            while True:
                self._append_log(self._log_queue.get_nowait())
        except queue.Empty:
            pass
        self.after(100, self._poll_logs)

    def _append_log(self, msg: str) -> None:
        self._log_box.config(state="normal")
        tag = "INFO"
        if "ERROR" in msg or "HATA" in msg:
            tag = "ERROR"
        elif "WARNING" in msg or "UYARI" in msg:
            tag = "WARNING"
        elif "HIT" in msg:
            tag = "HIT"
        elif "MISS" in msg:
            tag = "MISS"
        self._log_box.insert("end", msg + "\n", tag)
        lines = int(self._log_box.index("end-1c").split(".")[0])
        if lines > 500:
            self._log_box.delete("1.0", f"{lines-500}.0")
        if self._autoscroll_var.get():
            self._log_box.see("end")
        self._log_box.config(state="disabled")

    def _clear_logs(self) -> None:
        self._log_box.config(state="normal")
        self._log_box.delete("1.0", "end")
        self._log_box.config(state="disabled")

    # -----------------------------------------------------------------------
    # İstatistik
    # -----------------------------------------------------------------------
    def _poll_stats(self) -> None:
        if self._helper and self._running:
            self._show_stats(self._helper.stats, running=True)
            try:
                self._state_label.config(
                    text=f"[{self._helper.state.name}]", fg=self.DIM)
            except Exception:
                pass
        elif not self._running and not self._helper and not getattr(self, "_saved_shown", False):
            # Panel yeni açıldı: önceki oturumdan kalan sayaç ve kazancı göster
            self._saved_shown = True
            self._show_stats(self._saved_to_stats(load_session()), running=False)
        self.after(1000, self._poll_stats)

    @staticmethod
    def _saved_to_stats(saved: dict) -> dict:
        """oturum.json içeriğini panelin gösterebileceği stats sözlüğüne çevirir (kazancı fiyatlardan hesaplar)."""
        st = dict(saved)
        st["session_start"] = time.time() - float(saved.get("active_seconds", 0))
        total = 0
        try:
            with open("fiyatlar.json", "r", encoding="utf-8") as f:
                fiyat = json.load(f)
        except Exception:
            fiyat = {}
        for name, cnt in (saved.get("caught_fish") or {}).items():
            fn = name.lower().strip()
            info = fiyat.get(fn) or next((v for k, v in fiyat.items() if k in fn or fn in k), {})
            total += cnt * (info.get("birim_yang", 0) if isinstance(info, dict) else int(info or 0))
        st["total_yang"] = total
        st["total_won"] = round(total / 100_000_000, 3)
        return st

    def _show_stats(self, stats: dict, running: bool) -> None:
        elapsed = int(time.time() - stats.get("session_start", time.time())) if stats else 0
        h, m, s = elapsed // 3600, (elapsed % 3600) // 60, elapsed % 60
        self._stat_vars["total_casts"].set(str(stats.get("total_casts", 0)))
        self._stat_vars["hits"].set(str(stats.get("hits", 0)))
        self._stat_vars["misses"].set(str(stats.get("misses", 0)))
        self._stat_vars["valuable_fish"].set(str(stats.get("valuable_fish", 0)))
        self._stat_vars["worthless_fish"].set(str(stats.get("worthless_fish", 0)))
        self._stat_vars["duration"].set(f"{h:02d}:{m:02d}:{s:02d}")

        total_won = stats.get("total_won", 0.0)
        total_yang = stats.get("total_yang", 0)
        caught = stats.get("caught_count", 0)
        escaped = stats.get("escaped_count", 0)
        total_bites = caught + escaped
        rate = (caught / total_bites * 100) if total_bites > 0 else 0

        self._won_label.config(text=f"{total_won:.3f} Won")
        self._yang_label.config(text=f"({total_yang:,} Yang)")
        self._catch_rate_label.config(text=f"Tutulan: {caught} | Kaçan: {escaped}  (Başarı: %{rate:.1f})")

    # -----------------------------------------------------------------------
    # DostGame canlı fiyat çekme
    # -----------------------------------------------------------------------
    def _fetch_prices_blocking(self, restore_focus: bool = True) -> None:
        """Fiyatları DostGame'den çeker (bloklar). Hata olursa eski fiyatlar korunur.
        restore_focus=True ise (bot çalışırken) işlem bitince oyun penceresine odak geri verilir."""
        fg = None
        if restore_focus:
            try:
                import win32gui
                fg = win32gui.GetForegroundWindow()
            except Exception:
                fg = None
        try:
            from fiyat_guncelleyici import fetch_live_prices
            res = fetch_live_prices(
                progress_callback=lambda msg: self.after(0, lambda m=msg: self._append_log(m)))
            items = []
            for k, v in res.items():
                val = v.get("birim_yang", 0) if isinstance(v, dict) else v
                items.append(f"{k.title()}: {val/1_000_000:.2f}M")
            self.after(0, lambda: self._append_log("🎉 [FİYAT] Güncel fiyatlar: " + ", ".join(items)))
            if self._helper:
                self._helper._update_earnings()
        except Exception as e:
            self.after(0, lambda: self._append_log(f"⚠️ [FİYAT] Fiyat çekilemedi, eski fiyatlar kullanılacak: {e}"))
        finally:
            if fg:
                try:
                    import win32gui, win32api, win32con
                    time.sleep(0.5)
                    # Windows'un odak çalma korumasını aşmak için kısa bir ALT tuş darbesi
                    win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
                    win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
                    win32gui.SetForegroundWindow(fg)
                except Exception:
                    pass

    def _price_refresh_loop(self) -> None:
        """Bot çalıştığı sürece fiyatları PRICE_REFRESH_MIN dakikada bir arka planda yeniler."""
        while not self._price_stop.wait(PRICE_REFRESH_MIN * 60):
            if not self._running:
                break
            self._fetch_prices_blocking(restore_focus=True)

    # -----------------------------------------------------------------------
    # Kazanç & Piyasa Modalı
    # -----------------------------------------------------------------------
    def _open_earnings_modal(self) -> None:
        modal = tk.Toplevel(self)
        modal.title("💰 Balık Detayları & Piyasa Değeri")
        modal.geometry("520x600")
        modal.configure(bg=self.BG)
        modal.attributes("-topmost", True)

        top = tk.Frame(modal, bg=self.ACCENT, pady=10)
        top.pack(fill="x")
        tk.Label(top, text="📊 Yakalanan Balıklar & Piyasa Karşılığı",
                 bg=self.ACCENT, fg=self.TEXT, font=self.FONT_H).pack()

        # Fiyatları yükle
        fiyatlar = {}
        if os.path.exists("fiyatlar.json"):
            try:
                with open("fiyatlar.json", "r", encoding="utf-8") as f:
                    fiyatlar = json.load(f)
            except Exception:
                pass

        # Liste Container
        canvas = tk.Canvas(modal, bg=self.BG, highlightthickness=0)
        scrollbar = tk.Scrollbar(modal, orient="vertical", command=canvas.yview)
        scrollable = tk.Frame(canvas, bg=self.BG)

        scrollable.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=scrollable, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side="top", fill="both", expand=True, padx=8, pady=8)
        scrollbar.pack(side="right", fill="y", before=canvas)

        caught_fish = self._helper.stats.get("caught_fish", {}) if self._helper else {}
        total_won = self._helper.stats.get("total_won", 0.0) if self._helper else 0.0
        total_yang = self._helper.stats.get("total_yang", 0) if self._helper else 0

        # Başlık Özeti
        summary_f = tk.Frame(scrollable, bg=self.SURFACE, pady=10, padx=12)
        summary_f.pack(fill="x", pady=4, padx=5)
        tk.Label(summary_f, text=f"Toplam Kazanç: {total_won:.3f} Won ({total_yang:,} Yang)",
                 font=("Segoe UI Semibold", 13), fg=self.GOLD, bg=self.SURFACE).pack(anchor="w")
        tk.Label(summary_f, text="* 1 Won = 100M Yang (DostGame Lucifer piyasa fiyatlarına göre)",
                 font=("Segoe UI", 9), fg=self.DIM, bg=self.SURFACE).pack(anchor="w", pady=(2, 0))

        if not caught_fish:
            tk.Label(scrollable, text="\nHenüz balık tutulmadı.\nBot balık tuttukça burada adet ve Won değerleri listelenir.",
                     font=self.FONT_UI, fg=self.DIM, bg=self.BG, justify="center").pack(pady=30)
        else:
            for fish_name, count in caught_fish.items():
                row = tk.Frame(scrollable, bg=self.SURFACE, pady=8, padx=10)
                row.pack(fill="x", pady=3, padx=5)

                fn = fish_name.lower().strip()
                item_info = fiyatlar.get(fn, {})
                if not item_info:
                    for k, v in fiyatlar.items():
                        if k in fn or fn in k:
                            item_info = v
                            break
                birim_yang = item_info.get("birim_yang", 0) if isinstance(item_info, dict) else int(item_info or 0)
                fish_total_yang = count * birim_yang
                fish_total_won = fish_total_yang / 100_000_000

                tk.Label(row, text=f"🐟 {fish_name.title()}",
                         font=("Segoe UI Semibold", 11), fg=self.TEXT, bg=self.SURFACE).pack(anchor="w")
                detail_txt = f"{count} Adet  |  Birim: {birim_yang:,} Yang ({birim_yang/100_000_000:.3f} Won)  |  Toplam: {fish_total_won:.3f} Won"
                tk.Label(row, text=detail_txt, font=self.FONT_UI, fg=self.GREEN, bg=self.SURFACE).pack(anchor="w", pady=(2, 0))

        # Alt Butonlar
        bot_bar = tk.Frame(modal, bg=self.BG, pady=10)
        bot_bar.pack(fill="x", side="bottom", padx=10)

        tk.Button(bot_bar, text="🌐  Siteden Canlı Fiyatları Çek (Lucifer)",
                  font=self.FONT_UI, bg="#1e5f74", fg=self.TEXT,
                  relief="flat", cursor="hand2", pady=6,
                  command=lambda: [modal.destroy(), self._update_prices_from_web()]).pack(fill="x", pady=(0, 4))

        tk.Button(bot_bar, text="✏️  Fiyatları Düzenle (fiyatlar.json)",
                  font=self.FONT_UI, bg=self.ACCENT, fg=self.TEXT,
                  relief="flat", cursor="hand2", pady=6,
                  command=lambda: [modal.destroy(), self._open_price_editor()]).pack(fill="x", pady=(0, 4))

        tk.Button(bot_bar, text="🔗  DostGame Ticaret Camını Tarayıcıda Aç",
                  font=self.FONT_UI, bg="#33334d", fg=self.TEXT,
                  relief="flat", cursor="hand2", pady=6,
                  command=lambda: webbrowser.open("https://www.dostgame.com/tr/metin2-ticaret-cami/ruby-lucifer")).pack(fill="x")

    def _update_prices_from_web(self) -> None:
        """DostGame pazar camından güncel balık fiyatlarını arka planda çeker."""
        self._append_log("🌐 [FİYAT] DostGame Ruby Lucifer'dan canlı fiyatlar çekiliyor, lütfen bekle...")
        threading.Thread(target=lambda: self._fetch_prices_blocking(restore_focus=True),
                         daemon=True, name="PriceFetcher").start()

    # -----------------------------------------------------------------------
    # Fiyat Düzenleyici
    # -----------------------------------------------------------------------
    def _open_price_editor(self) -> None:
        editor = tk.Toplevel(self)
        editor.title("✏️ Balık Birim Fiyatları Düzenle")
        editor.geometry("480x620")
        editor.configure(bg=self.BG)
        editor.attributes("-topmost", True)

        top = tk.Frame(editor, bg=self.ACCENT, pady=10)
        top.pack(fill="x")
        tk.Label(top, text="Piyasa Birim Fiyatları (Yang Cinsinden)",
                 bg=self.ACCENT, fg=self.TEXT, font=self.FONT_H).pack()
        tk.Label(top, text="Örnek: 1.5m için 1500000 yazın (1 Won = 100.000.000 Yang)",
                 bg=self.ACCENT, fg=self.DIM, font=("Segoe UI", 9)).pack()

        fiyatlar = {}
        if os.path.exists("fiyatlar.json"):
            try:
                with open("fiyatlar.json", "r", encoding="utf-8") as f:
                    fiyatlar = json.load(f)
            except Exception:
                pass

        canvas = tk.Canvas(editor, bg=self.BG, highlightthickness=0)
        scrollbar = tk.Scrollbar(editor, orient="vertical", command=canvas.yview)
        scrollable = tk.Frame(canvas, bg=self.BG)
        scrollable.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=scrollable, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="top", fill="both", expand=True, padx=6, pady=6)
        scrollbar.pack(side="right", fill="y", before=canvas)

        entries = {}
        for item, data in fiyatlar.items():
            f = tk.Frame(scrollable, bg=self.SURFACE, pady=5, padx=8)
            f.pack(fill="x", pady=2, padx=4)

            tk.Label(f, text=item.title(), font=self.FONT_H, fg=self.TEXT, bg=self.SURFACE, width=18, anchor="w").pack(side="left")

            val = data.get("birim_yang", 0) if isinstance(data, dict) else int(data or 0)
            ent = tk.Entry(f, font=self.FONT_MONO, width=14, justify="right", bg="#0d0d1a", fg="#b0ffb0", insertbackground=self.TEXT)
            ent.insert(0, str(val))
            ent.pack(side="right", padx=5)
            tk.Label(f, text="Yang:", font=self.FONT_UI, fg=self.DIM, bg=self.SURFACE).pack(side="right")
            entries[item] = ent

        def save_prices():
            for item, ent in entries.items():
                try:
                    val = int(ent.get().replace(".", "").replace(",", "").strip())
                    if item in fiyatlar and isinstance(fiyatlar[item], dict):
                        fiyatlar[item]["birim_yang"] = val
                    else:
                        fiyatlar[item] = {"birim_yang": val, "not": f"{item} birim fiyatı"}
                except Exception:
                    pass

            with open("fiyatlar.json", "w", encoding="utf-8") as f:
                json.dump(fiyatlar, f, ensure_ascii=False, indent=4)

            if self._helper:
                self._helper._update_earnings()

            self._append_log("✅ Balık piyasa fiyatları başarıyla kaydedildi!")
            editor.destroy()

        bot = tk.Frame(editor, bg=self.BG, pady=10)
        bot.pack(fill="x", side="bottom", padx=10)
        tk.Button(bot, text="💾 Fiyatları Kaydet", font=("Segoe UI Semibold", 12),
                  bg=self.GREEN, fg="#000", relief="flat", cursor="hand2", pady=8,
                  command=save_prices).pack(fill="x")

    # -----------------------------------------------------------------------
    # Kapat
    # -----------------------------------------------------------------------
    def _on_close(self) -> None:
        if self._running:
            self._stop_app()
        if self._ocr_proc:
            try:
                self._ocr_proc.terminate()
            except Exception:
                pass
        self.destroy()


if __name__ == "__main__":
    app = FisherPanel()
    app.mainloop()
