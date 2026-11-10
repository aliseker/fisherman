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

import threading
import queue
import logging
import time
import sys
import os
import json

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

from hardware_manager import ensure_dpi_awareness, get_thread_sct, close_thread_sct
ensure_dpi_awareness()

import tkinter as tk
from tkinter import scrolledtext

from fisherman import (
    FisherConfig, AppState, MultiClientManager, InputCoordinator,
    discover_game_windows, tile_game_windows,
    screen_roi_to_relative, screen_roi_to_relative_for_hwnd, resolve_window_roi,
    _OCR_BACKEND, _HAS_CUDA, load_session, reset_session, compute_earnings_from_counts,
)
from jigsaw_bot import JigsawRunner, JigsawStats

import ctypes
import win32api
import win32gui
import win32con
import mss
import numpy as np
import cv2
from PIL import Image, ImageTk


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
# Ekran üzerinde sürükle-bırak ROI seçici (Hedef pencere odaklı & çakışma korumalı)
# ===========================================================================
class ROISelector:
    """
    Seçim yapılmadan önce kontrol panelini geçici gizler ve hedef Metin2 penceresini öne getirir.
    Böylece panel hiçbir zaman Client 2'nin veya oyunun üstünü kapatmaz.
    """

    def __init__(
        self,
        root: tk.Tk,
        title: str = "Alan seç",
        target_hwnd: int | None = None,
        other_roi: dict | None = None,
        other_label: str = "",
    ):
        self._root = root
        self._title = title
        self._target_hwnd = target_hwnd
        self._other_roi = other_roi
        self._other_label = other_label
        self._result: dict | None = None

    def select(self) -> dict | None:
        """
        Blocking — seçim tamamlanana kadar bekler.
        Returns: {"top": y, "left": x, "width": w, "height": h} veya None
        """
        win_rect = None
        try:
            self._root.withdraw()
            self._root.update_idletasks()
            if self._target_hwnd and win32gui.IsWindow(self._target_hwnd):
                InputCoordinator([self._target_hwnd]).focus_client(self._target_hwnd, force_initial=True)
                time.sleep(0.22)
                win_rect = win32gui.GetWindowRect(self._target_hwnd)
            else:
                time.sleep(0.15)
        except Exception:
            pass

        try:
            sct = get_thread_sct()
            mon = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
            shot = sct.grab(mon)
            img = np.array(shot)  # BGRA
            img_rgb = cv2.cvtColor(img, cv2.COLOR_BGRA2RGB)

            mon_x, mon_y = int(mon.get("left", 0)), int(mon.get("top", 0))
            sw, sh = mon["width"], mon["height"]

            overlay = tk.Toplevel(self._root)
            overlay.title(self._title)
            overlay.geometry(f"{sw}x{sh}+{mon_x}+{mon_y}")
            overlay.overrideredirect(True)
            overlay.attributes("-topmost", True)
            overlay.attributes("-alpha", 0.88)
            overlay.configure(bg="black")

            pil_img = Image.fromarray(img_rgb)
            tk_img = ImageTk.PhotoImage(pil_img)

            canvas = tk.Canvas(overlay, width=sw, height=sh,
                               cursor="crosshair", bg="black",
                               highlightthickness=0)
            canvas.pack(fill="both", expand=True)
            canvas.create_image(0, 0, anchor="nw", image=tk_img)

            # Hedef Metin2 penceresinin etrafına parlak turkuaz çerçeve çiz
            if win_rect:
                wx0, wy0, wx1, wy1 = win_rect
                wx0 -= mon_x
                wy0 -= mon_y
                wx1 -= mon_x
                wy1 -= mon_y
                canvas.create_rectangle(wx0, wy0, wx1, wy1, outline="#00e5ff", width=3)
                canvas.create_text(
                    max(140, wx0 + 160), max(18, wy0 + 16),
                    text=f"📍 {self._title}",
                    fill="#00e5ff", font=("Segoe UI", 11, "bold"),
                )

            # Diğer alan (örn. Chat seçiliyken Oyun veya Oyun seçiliyken Chat) varsa sarı kesikli göster ki üst üste binmesin!
            if self._other_roi:
                ox0 = self._other_roi["left"] - mon_x
                oy0 = self._other_roi["top"] - mon_y
                ox1 = ox0 + self._other_roi["width"]
                oy1 = oy0 + self._other_roi["height"]
                canvas.create_rectangle(ox0, oy0, ox1, oy1, outline="#ffd700", width=2, dash=(5, 3))
                if self._other_label:
                    canvas.create_text(
                        (ox0 + ox1) // 2, max(14, oy0 - 10),
                        text=self._other_label,
                        fill="#ffd700", font=("Segoe UI", 9, "bold"),
                    )

            # Üst bilgi bandı
            canvas.create_rectangle(0, 0, sw, 42, fill="#0d0d1a", outline="")
            canvas.create_text(
                sw // 2, 21,
                text=f"[ {self._title} ] — Fareyle kutu içine alıp bırakın  |  ESC = İptal",
                fill="#ffd700", font=("Segoe UI", 12, "bold"),
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
                left   = mon_x + min(x0, x1)
                top    = mon_y + min(y0, y1)
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
        finally:
            close_thread_sct()
            try:
                self._root.deiconify()
                self._root.lift()
            except Exception:
                pass
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

    _JIGSAW_STRAT_MAP = {
        "⚖️ Dengeli Yang (Büyük+Orta Odaklı - Önerilen)": "balanced",
        "🎯 Maksimum Büyük Sandık (<=10 Adım)": "large",
        "⚡ Hızlı Bitir (Az Parça At)": "fast",
    }
    _JIGSAW_AUTO_LABEL = "auto: Otomatik (Ön Plandaki Metin2)"

    def __init__(self):
        super().__init__()
        self.title("⚓ Metin2 Fishing Helper (1-6 Client)")
        try:
            sh_avail = max(860, self.winfo_screenheight() - 90)
            init_h = min(960, sh_avail)
        except Exception:
            init_h = 880
        self.geometry(f"490x{init_h}")
        self.resizable(True, True)
        self.configure(bg=self.BG)
        self.attributes("-topmost", True)

        # Geriye dönük tekli varsayılanlar
        self._chat_roi = {"top": 218, "left": 35, "width": 342, "height": 43}
        self._minigame_roi = {"top": 350, "left": 179, "width": 342, "height": 278}
        self._rel_chat_roi: dict | None = None
        self._rel_minigame_roi: dict | None = None

        # Her istemci slotu ("1", "2", "3", "4") için bağımsız Chat ve Oyun ROI kayıtları
        self._client_slots: dict[str, dict] = {}

        # Çoklu pencere seçim durumu: [(slot_idx, hwnd, BooleanVar, info_dict), ...]
        self._win_items: list[tuple[int, int, tk.BooleanVar, dict]] = []
        self._f8_was_down = False
        self._f9_was_down = False

        # Balık Yapboz Botu
        self._jigsaw_runner: JigsawRunner | None = None
        self._jigsaw_win: tk.Toplevel | None = None
        self._jigsaw_stat_vars: dict[str, tk.StringVar] = {}
        self._jigsaw_status_lbl: tk.Label | None = None
        self._jigsaw_status_dot: tk.Label | None = None
        self._jigsaw_start_btn: tk.Button | None = None
        self._jigsaw_log_box: scrolledtext.ScrolledText | None = None
        self._jigsaw_strategy_var: tk.StringVar = tk.StringVar(value=next(iter(self._JIGSAW_STRAT_MAP)))
        self._jigsaw_client_var: tk.StringVar = tk.StringVar(value=self._JIGSAW_AUTO_LABEL)

        # Ayarları yükle
        self._load_settings()

        # MultiClientManager instance (1, 2, 3 veya 4 pencereyi yönetir)
        self._manager: MultiClientManager | None = None
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
        self._refresh_windows(silent=True)
        try:
            if ctypes.windll.shell32.IsUserAnAdmin() == 0:
                self._append_log(
                    "⚠️ UYARI: Yönetici (Admin) izni yok! Metin2 yönetici modunda çalıştığı için "
                    "çoklu pencere odak geçişi ve tuş basımı engellenebilir. Lütfen 'gpu_baslat.bat' ile açın."
                )
        except Exception:
            pass
        self._poll_logs()
        self._poll_stats()
        self._poll_hotkey()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # -----------------------------------------------------------------------
    # Ayarlar Kaydet / Yükle (Her Pencere Slotu Ayrı Kaydedilir)
    # -----------------------------------------------------------------------
    def _load_settings(self) -> None:
        try:
            if os.path.exists("settings.json"):
                with open("settings.json", "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if "chat_roi" in data:
                        self._chat_roi = data["chat_roi"]
                    if "minigame_roi" in data:
                        self._minigame_roi = data["minigame_roi"]
                    if "rel_chat_roi" in data:
                        self._rel_chat_roi = data["rel_chat_roi"]
                    if "rel_minigame_roi" in data:
                        self._rel_minigame_roi = data["rel_minigame_roi"]
                    if isinstance(data.get("client_slots"), dict):
                        self._client_slots = data["client_slots"]
                    elif "chat_roi" in data and "minigame_roi" in data:
                        # Mevcut tekli kalibrasyonu yalnızca Client 1 başlangıç kaydı olarak koru
                        self._client_slots = {
                            "1": {
                                "chat_roi": data["chat_roi"],
                                "minigame_roi": data["minigame_roi"],
                                "rel_chat_roi": data.get("rel_chat_roi"),
                                "rel_minigame_roi": data.get("rel_minigame_roi"),
                            }
                        }
        except Exception:
            pass

    def _save_settings(self) -> None:
        try:
            data = {}
            if os.path.exists("settings.json"):
                try:
                    with open("settings.json", "r", encoding="utf-8") as f:
                        data = json.load(f)
                except Exception:
                    data = {}
            data.pop("ocr_url", None)
            data["chat_roi"] = self._chat_roi
            data["minigame_roi"] = self._minigame_roi
            if self._rel_chat_roi is not None:
                data["rel_chat_roi"] = self._rel_chat_roi
            if self._rel_minigame_roi is not None:
                data["rel_minigame_roi"] = self._rel_minigame_roi
            data["client_slots"] = self._client_slots
            with open("settings.json", "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    # -----------------------------------------------------------------------
    # UI
    # -----------------------------------------------------------------------
    def _build_ui(self) -> None:
        pad = {"padx": 10, "pady": 3}

        # ── Başlık ──────────────────────────────────────────────────────────
        header = tk.Frame(self, bg=self.ACCENT, height=44)
        header.pack(fill="x")
        tk.Label(header, text="⚓  Metin2 Fisher (Multi-Client)",
                 font=("Segoe UI Semibold", 12),
                 fg=self.TEXT, bg=self.ACCENT,
                 ).pack(side="left", padx=12, pady=7)

        if _OCR_BACKEND == "easyocr":
            ocr_text = "OCR: RTX GPU" if _HAS_CUDA else "OCR: CPU"
            ocr_color = self.GREEN if _HAS_CUDA else self.GOLD
        else:
            ocr_text = "OCR: YOK!"
            ocr_color = self.RED

        self._ocr_label = tk.Label(
            header, text=ocr_text,
            font=self.FONT_UI, fg=ocr_color, bg=self.ACCENT,
        )
        self._ocr_label.pack(side="right", padx=12)

        # ── Üst Kontrol & Durum Barı (Sağ-üst boşlukta hep görünür kalır!) ───
        ctrl_frame = tk.Frame(self, bg=self.BG)
        ctrl_frame.pack(fill="x", padx=10, pady=(5, 2))
        self._start_btn = tk.Button(
            ctrl_frame, text="▶  BAŞLAT  (F8)",
            font=("Segoe UI Semibold", 11),
            bg=self.GREEN, fg="#000000",
            activebackground="#00b055", activeforeground="#000000",
            relief="flat", cursor="hand2", bd=0,
            padx=16, pady=6,
            command=self._toggle_app,
        )
        self._start_btn.pack(side="left", fill="x", expand=True, padx=(0, 5))
        tk.Button(ctrl_frame, text="🗑  Temizle / Sıfırla",
                  font=self.FONT_UI,
                  bg=self.ACCENT, fg=self.TEXT,
                  activebackground="#1a4a8a", activeforeground=self.TEXT,
                  relief="flat", cursor="hand2", bd=0,
                  padx=10, pady=6,
                  command=self._clear_all,
                  ).pack(side="right")

        status_frame = tk.Frame(self, bg=self.SURFACE, pady=4)
        status_frame.pack(fill="x", padx=10, pady=(0, 3))
        self._status_dot = tk.Label(status_frame, text="●",
                                    font=("Segoe UI", 12),
                                    fg=self.RED, bg=self.SURFACE)
        self._status_dot.pack(side="left", padx=(10, 4))
        self._status_label = tk.Label(status_frame, text="Durduruldu  (Kısayol: F8)",
                                      font=("Segoe UI Semibold", 9),
                                      fg=self.RED, bg=self.SURFACE)
        self._status_label.pack(side="left")
        self._state_label = tk.Label(status_frame, text="",
                                     font=("Consolas", 8),
                                     fg=self.DIM, bg=self.SURFACE)
        self._state_label.pack(side="right", padx=8)

        # ── Mod & Whitelist & Yapboz Botu ────────────────────────────────────
        mode_frame = tk.LabelFrame(self, text=" Mod & Mini Oyun Botları ", bg=self.SURFACE,
                                   fg=self.DIM, font=self.FONT_H,
                                   bd=1, relief="flat")
        mode_frame.pack(fill="x", **pad)

        btn_row_m = tk.Frame(mode_frame, bg=self.SURFACE)
        btn_row_m.pack(fill="x", padx=8, pady=(4, 5))

        tk.Button(btn_row_m, text="🖼️ Balık Whitelist",
                  bg=self.ACCENT, fg=self.TEXT,
                  activebackground="#1a4a8a", activeforeground=self.TEXT,
                  font=("Segoe UI", 9, "bold"), relief="flat", cursor="hand2",
                  padx=6, pady=4,
                  command=self._open_visual_settings,
                  ).pack(side="left", fill="x", expand=True, padx=(0, 4))

        tk.Button(btn_row_m, text="🧩 Yapboz Botu (F9)",
                  bg="#4a148c", fg="#ffffff",
                  activebackground="#6a1b9a", activeforeground="#ffffff",
                  font=("Segoe UI", 9, "bold"), relief="flat", cursor="hand2",
                  padx=6, pady=4,
                  command=self._open_jigsaw_modal,
                  ).pack(side="right", fill="x", expand=True, padx=(4, 0))

        # ── 🪟 Çoklu Pencere & Her Pencere İçin Ayrı Alan Seçimi ─────────────
        win_frame = tk.LabelFrame(
            self,
            text=" 🪟 Oyun Pencereleri & Ayrı Alan Seçimi (Her Client Ayrı) ",
            bg=self.SURFACE, fg=self.GOLD,
            font=self.FONT_H, bd=1, relief="flat",
        )
        win_frame.pack(fill="x", **pad)

        win_btn_row = tk.Frame(win_frame, bg=self.SURFACE)
        win_btn_row.pack(fill="x", padx=8, pady=(4, 3))
        tk.Button(win_btn_row, text="🔄 Pencereleri Tara",
                  bg=self.ACCENT, fg=self.TEXT,
                  font=("Segoe UI", 9, "bold"), relief="flat", cursor="hand2",
                  padx=8, pady=3,
                  command=self._refresh_windows,
                  ).pack(side="left", fill="x", expand=True, padx=(0, 4))
        tk.Button(win_btn_row, text="📐 Çapraz / Oto Diz",
                  bg="#1b5e20", fg="#ffffff",
                  activebackground="#2e7d32", activeforeground="#ffffff",
                  font=("Segoe UI", 9, "bold"), relief="flat", cursor="hand2",
                  padx=8, pady=3,
                  command=self._tile_selected_windows,
                  ).pack(side="right", fill="x", expand=True, padx=(4, 0))

        tk.Label(
            win_frame,
            text="💡 Önce her oyunda balık penceresini chat'in altına çekin, sonra o satırdaki Chat & Oyun'u seçin:",
            fg=self.DIM, bg=self.SURFACE, font=("Segoe UI", 8), anchor="w",
        ).pack(fill="x", padx=8, pady=(0, 2))

        self._win_list_box = tk.Frame(win_frame, bg=self.SURFACE)
        self._win_list_box.pack(fill="x", padx=8, pady=(0, 5))

        # ── İstatistikler ────────────────────────────────────────────────────
        stats_frame = tk.LabelFrame(self, text=" İstatistikler (Tüm Aktif Hesaplar Toplamı) ",
                                    bg=self.SURFACE, fg=self.DIM,
                                    font=self.FONT_H, bd=1, relief="flat")
        stats_frame.pack(fill="x", padx=10, pady=(2, 3))
        labels = [("Cast", "total_casts"), ("Hit", "hits"), ("Miss", "misses"),
                  ("Değerli", "valuable_fish"), ("Skip", "worthless_fish"), ("Süre", "duration")]
        self._stat_vars = {}
        for i, (lbl, key) in enumerate(labels):
            col, row = i % 3, i // 3
            cell = tk.Frame(stats_frame, bg=self.SURFACE)
            cell.grid(row=row, column=col, padx=12, pady=3, sticky="w")
            tk.Label(cell, text=lbl, font=("Segoe UI", 8),
                     fg=self.DIM, bg=self.SURFACE).pack(anchor="w")
            var = tk.StringVar(value="0")
            self._stat_vars[key] = var
            tk.Label(cell, textvariable=var,
                     font=("Segoe UI Semibold", 14),
                     fg=self.TEXT, bg=self.SURFACE).pack(anchor="w")

        # ── 💰 Kazanç & Piyasa Değeri (Yerel fiyatlar.json) ──────────────────
        earn_frame = tk.LabelFrame(self, text=" 💰 Tahmini Kazanç (fiyatlar.json) ",
                                   bg=self.SURFACE, fg=self.GOLD,
                                   font=self.FONT_H, bd=1, relief="flat")
        earn_frame.pack(fill="x", padx=10, pady=(2, 3))

        won_row = tk.Frame(earn_frame, bg=self.SURFACE)
        won_row.pack(fill="x", padx=10, pady=(4, 1))
        self._won_label = tk.Label(won_row, text="0.000 Won",
                                   font=("Segoe UI Semibold", 15),
                                   fg=self.GOLD, bg=self.SURFACE)
        self._won_label.pack(side="left")
        self._yang_label = tk.Label(won_row, text="(0 Yang)",
                                    font=self.FONT_UI,
                                    fg=self.DIM, bg=self.SURFACE)
        self._yang_label.pack(side="left", padx=8)

        self._catch_rate_label = tk.Label(earn_frame,
                                          text="Tutulan: 0 | Kaçan: 0  (Başarı: %0)",
                                          font=self.FONT_UI, fg=self.GREEN, bg=self.SURFACE)
        self._catch_rate_label.pack(anchor="w", padx=10, pady=(0, 4))

        earn_btn_row = tk.Frame(earn_frame, bg=self.SURFACE)
        earn_btn_row.pack(fill="x", padx=10, pady=(0, 4))

        tk.Button(earn_btn_row, text="📊  Balık Detayları",
                  font=self.FONT_UI, bg=self.ACCENT, fg=self.TEXT,
                  relief="flat", cursor="hand2", padx=8, pady=4,
                  command=self._open_earnings_modal).pack(side="left", fill="x", expand=True, padx=(0, 4))

        tk.Button(earn_btn_row, text="✏️  Birim Fiyatları Düzenle",
                  font=self.FONT_UI, bg="#2a5298", fg="#ffffff",
                  activebackground="#1e3c72", activeforeground="#ffffff",
                  relief="flat", cursor="hand2", padx=8, pady=4,
                  command=self._open_price_editor).pack(side="right", fill="x", expand=True, padx=(4, 0))

        tk.Button(earn_frame, text="♻️  Sayaçları ve Süreyi Sıfırla",
                  font=self.FONT_UI, bg="#5a2a2a", fg="#ffffff",
                  activebackground="#7a3a3a", activeforeground="#ffffff",
                  relief="flat", cursor="hand2", padx=10, pady=3,
                  command=self._reset_counters).pack(fill="x", padx=10, pady=(0, 6))

        # ── Log ─────────────────────────────────────────────────────────────
        log_header = tk.Frame(self, bg=self.BG)
        log_header.pack(fill="x", padx=10, pady=(2, 0))
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
        self._log_box.pack(fill="both", expand=True, padx=10, pady=(2, 8))
        self._log_box.tag_config("ERROR",   foreground="#ff6b6b")
        self._log_box.tag_config("WARNING", foreground="#ffd700")
        self._log_box.tag_config("INFO",    foreground="#b0ffb0")
        self._log_box.tag_config("HIT",     foreground="#00ff88")
        self._log_box.tag_config("MISS",    foreground="#ff9944")

    # -----------------------------------------------------------------------
    # Çoklu Pencere Tarama, Çapraz Dizilim & Her Pencere İçin Ayrı Alan Seçimi
    # -----------------------------------------------------------------------
    @staticmethod
    def _short_roi_str(roi: dict | None, rel_roi: dict | None = None) -> str:
        if rel_roi:
            return f"Δ({rel_roi['rel_left']},{rel_roi['rel_top']}) {rel_roi['width']}×{rel_roi['height']}"
        if roi:
            return f"({roi['left']},{roi['top']}) {roi['width']}×{roi['height']}"
        return "Seçilmedi"

    @staticmethod
    def _rois_overlap(r1: dict | None, r2: dict | None) -> bool:
        if not r1 or not r2:
            return False
        x_ov = max(0, min(r1["left"] + r1["width"], r2["left"] + r2["width"]) - max(r1["left"], r2["left"]))
        y_ov = max(0, min(r1["top"] + r1["height"], r2["top"] + r2["height"]) - max(r1["top"], r2["top"]))
        return x_ov > 4 and y_ov > 4

    def _refresh_windows(self, silent: bool = False) -> None:
        prev_selected = {hwnd for _, hwnd, var, _ in self._win_items if var.get()}
        had_prev = len(self._win_items) > 0

        for child in self._win_list_box.winfo_children():
            child.destroy()
        self._win_items.clear()

        wins = discover_game_windows("metin2")
        if not wins:
            empty_row = tk.Frame(self._win_list_box, bg="#111930", pady=5, padx=6)
            empty_row.pack(fill="x", pady=2)
            tk.Label(
                empty_row,
                text="Açık Metin2 penceresi bulunamadı (Oyunları açıp '🔄 Pencereleri Tara'ya basın)",
                fg=self.DIM, bg="#111930", font=("Segoe UI", 8),
            ).pack(anchor="w")
            btn_f = tk.Frame(empty_row, bg="#111930")
            btn_f.pack(fill="x", pady=(3, 0))
            slot1 = self._client_slots.get("1", {})
            has_c1 = bool(slot1.get("chat_roi"))
            has_m1 = bool(slot1.get("minigame_roi"))
            tk.Label(btn_f, text="Tekli Varsayılan:", fg=self.TEXT, bg="#111930", font=("Consolas", 8)).pack(side="left")
            tk.Button(
                btn_f, text="💬 Chat ✓" if has_c1 else "💬 Chat Seç",
                bg="#1b5e20" if has_c1 else self.ACCENT, fg="#ffffff",
                font=("Segoe UI", 8, "bold"), relief="flat", cursor="hand2", padx=6, pady=1,
                command=lambda: self._select_slot_roi(1, None, "chat"),
            ).pack(side="right", padx=(3, 0))
            tk.Button(
                btn_f, text="🎯 Oyun ✓" if has_m1 else "🎯 Oyun Seç",
                bg="#1b5e20" if has_m1 else self.ACCENT, fg="#ffffff",
                font=("Segoe UI", 8, "bold"), relief="flat", cursor="hand2", padx=6, pady=1,
                command=lambda: self._select_slot_roi(1, None, "minigame"),
            ).pack(side="right")
            if not silent:
                self._append_log("⚠️ Açık Metin2 penceresi bulunamadı.")
            return

        for idx, w in enumerate(wins, start=1):
            hwnd = w["hwnd"]
            default_on = (hwnd in prev_selected) if had_prev else (idx <= 6)
            var = tk.BooleanVar(value=default_on)
            slot_key = str(idx)
            slot_data = self._client_slots.get(slot_key, {})
            has_chat = bool(slot_data.get("chat_roi"))
            has_mg = bool(slot_data.get("minigame_roi"))

            card = tk.Frame(self._win_list_box, bg="#111930", padx=6, pady=4)
            card.pack(fill="x", pady=2)

            top_r = tk.Frame(card, bg="#111930")
            top_r.pack(fill="x")

            pos_txt = "Simge" if w["minimized"] else f"@({w['client_x']},{w['client_y']})"
            lbl_txt = f"Client {idx}  [{pos_txt} | PID:{w['pid']}]"
            cb = tk.Checkbutton(
                top_r,
                text=lbl_txt,
                variable=var,
                bg="#111930",
                fg=self.GREEN if (has_chat and has_mg) else self.GOLD,
                selectcolor=self.ACCENT,
                activebackground="#111930",
                activeforeground=self.TEXT,
                font=("Consolas", 8, "bold"),
                anchor="w",
            )
            cb.pack(side="left")

            tk.Button(
                top_r,
                text="🎯 Oyun ✓" if has_mg else "🎯 Oyun Seç",
                bg="#1b5e20" if has_mg else "#2a5298",
                fg="#ffffff",
                activebackground="#2e7d32", activeforeground="#ffffff",
                font=("Segoe UI", 8, "bold"), relief="flat", cursor="hand2",
                padx=6, pady=1,
                command=lambda s=idx, h=hwnd: self._select_slot_roi(s, h, "minigame"),
            ).pack(side="right", padx=(4, 0))

            tk.Button(
                top_r,
                text="💬 Chat ✓" if has_chat else "💬 Chat Seç",
                bg="#1b5e20" if has_chat else "#2a5298",
                fg="#ffffff",
                activebackground="#2e7d32", activeforeground="#ffffff",
                font=("Segoe UI", 8, "bold"), relief="flat", cursor="hand2",
                padx=6, pady=1,
                command=lambda s=idx, h=hwnd: self._select_slot_roi(s, h, "chat"),
            ).pack(side="right")

            if has_chat and has_mg:
                c_txt = self._short_roi_str(slot_data.get("chat_roi"), slot_data.get("rel_chat_roi"))
                m_txt = self._short_roi_str(slot_data.get("minigame_roi"), slot_data.get("rel_minigame_roi"))
                sub_txt = f"  ✓ Chat: {c_txt}   |   Oyun: {m_txt}"
                sub_fg = "#88ddaa"
            else:
                missing = []
                if not has_chat:
                    missing.append("'💬 Chat Seç'")
                if not has_mg:
                    missing.append("'🎯 Oyun Seç'")
                sub_txt = f"  ⚠️ Bu pencere için henüz {' ve '.join(missing)} yapılmadı"
                sub_fg = self.GOLD

            tk.Label(
                card, text=sub_txt, fg=sub_fg, bg="#111930",
                font=("Consolas", 8), anchor="w",
            ).pack(fill="x", pady=(1, 0))

            self._win_items.append((idx, hwnd, var, w))

        if not silent:
            self._append_log(
                f"🪟 {len(wins)} Metin2 penceresi bulundu. Her aktif pencerenin kendi '💬 Chat Seç' ve '🎯 Oyun Seç' alanını seçebilirsiniz."
            )

    def _get_selected_items(self) -> list[tuple[int, int]]:
        return [(slot_idx, hwnd) for slot_idx, hwnd, var, _ in self._win_items if var.get()]

    def _get_selected_hwnds(self) -> list[int]:
        return [hwnd for _, hwnd in self._get_selected_items()]

    def _tile_selected_windows(self) -> None:
        if not self._win_items:
            self._refresh_windows(silent=True)
        hwnds = self._get_selected_hwnds()
        if not hwnds and self._win_items:
            hwnds = [h for _, h, _, _ in self._win_items[:6]]
        if not hwnds:
            self._append_log("⚠️ Ekrana dizilecek açık Metin2 penceresi bulunamadı!")
            return

        placed = tile_game_windows(hwnds, panel_reserve_width=380)
        # Kontrol panelini sağ-üst boşluğa hizala
        try:
            self.update_idletasks()
            sw = self.winfo_screenwidth()
            pw = max(460, self.winfo_width())
            ph = min(self.winfo_height(), self.winfo_screenheight() - 50)
            self.geometry(f"{pw}x{ph}+{max(0, sw - pw - 4)}+0")
        except Exception:
            pass

        self._refresh_windows(silent=True)
        self._append_log(
            f"📐 {len(placed)} oyun penceresi çapraz/otomatik dizildi! "
            f"Şimdi her pencerenin kendi satırından '💬 Chat Seç' ve '🎯 Oyun Seç' butonlarıyla alanlarını seçin."
        )

    def _select_slot_roi(self, slot_idx: int, hwnd: int | None, kind: str) -> None:
        """Belirtilen istemci (Client 1, Client 2, ...) için bağımsız Chat veya Oyun alanı seçtirir."""
        if self._running:
            self._append_log("⚠️ Alan seçmeden önce lütfen botu durdurun (⏹ DURDUR / F8).")
            return
        slot_key = str(slot_idx)
        slot_data = dict(self._client_slots.get(slot_key) or {})

        if kind == "chat":
            title = f"Client {slot_idx} — Bilgi Chat Alanını Seçin"
            other_abs = slot_data.get("minigame_roi")
            other_rel = slot_data.get("rel_minigame_roi")
            other_roi = resolve_window_roi(hwnd, other_abs, other_rel) if other_abs else None
            other_lbl = f"Client {slot_idx} Balık Oyun Alanı (Üst üste binmesin)"
        else:
            title = f"Client {slot_idx} — Balık Oyun Penceresini (0/3 ve Mavi Su Dahil) Seçin"
            other_abs = slot_data.get("chat_roi")
            other_rel = slot_data.get("rel_chat_roi")
            other_roi = resolve_window_roi(hwnd, other_abs, other_rel) if other_abs else None
            other_lbl = f"Client {slot_idx} Bilgi Chat Alanı (Üstünü kapatmayın!)"

        sel = ROISelector(self, title=title, target_hwnd=hwnd, other_roi=other_roi, other_label=other_lbl)
        result = sel.select()
        if not result:
            self._append_log(f"⚠️ Client {slot_idx} alan seçimi iptal edildi.")
            return

        rel = screen_roi_to_relative_for_hwnd(result, hwnd) if hwnd else screen_roi_to_relative(result, "metin2")
        if kind == "chat":
            slot_data["chat_roi"] = result
            slot_data["rel_chat_roi"] = rel
            if slot_idx == 1:
                self._chat_roi = result
                self._rel_chat_roi = rel
            self._append_log(f"✅ Client {slot_idx} Chat alanı kaydedildi: {self._short_roi_str(result, rel)}")
        else:
            slot_data["minigame_roi"] = result
            slot_data["rel_minigame_roi"] = rel
            if slot_idx == 1:
                self._minigame_roi = result
                self._rel_minigame_roi = rel
            self._append_log(f"✅ Client {slot_idx} Balık Oyun alanı kaydedildi: {self._short_roi_str(result, rel)}")

        self._client_slots[slot_key] = slot_data
        self._save_settings()
        self._refresh_windows(silent=True)

        # Chat ve Oyun kutuları çakışıyorsa anında uyar!
        c_check = resolve_window_roi(hwnd, slot_data["chat_roi"], slot_data.get("rel_chat_roi")) if slot_data.get("chat_roi") else None
        m_check = resolve_window_roi(hwnd, slot_data["minigame_roi"], slot_data.get("rel_minigame_roi")) if slot_data.get("minigame_roi") else None
        if self._rois_overlap(c_check, m_check):
            self._append_log(
                f"⚠️ UYARI: Client {slot_idx}'de Chat ve Balık Oyun kutuları üst üste biniyor! "
                f"Oyunda balık penceresini biraz daha aşağı kaydırıp '🎯 Oyun Seç' ile tekrar seçin."
            )

    # -----------------------------------------------------------------------
    # 🧩 Balık Yapboz Botu (Oto Çözücü, Bellman Stratejisi & Arayüz)
    # -----------------------------------------------------------------------
    def _get_selected_jigsaw_hwnd(self) -> int | None:
        sel = (self._jigsaw_client_var.get() or "").strip()
        if not sel or sel.startswith("auto"):
            return None
        try:
            for idx, hwnd, var, info in self._win_items:
                if f"Client {idx}" in sel:
                    return hwnd
        except Exception:
            pass
        return None

    def _get_jigsaw_strategy_key(self) -> str:
        raw = self._jigsaw_strategy_var.get()
        return self._JIGSAW_STRAT_MAP.get(raw, raw if raw in ("balanced", "large", "fast") else "balanced")

    def _ensure_jigsaw_runner(self) -> JigsawRunner:
        if self._jigsaw_runner is None:
            hwnd = self._get_selected_jigsaw_hwnd()
            strat = self._get_jigsaw_strategy_key()
            self._jigsaw_runner = JigsawRunner(
                target_hwnd=hwnd,
                on_status_change=self._on_jigsaw_status_change,
                on_log=self._on_jigsaw_log,
            )
            self._jigsaw_runner.set_strategy(strat)
        return self._jigsaw_runner

    def _toggle_jigsaw(self) -> None:
        runner = self._ensure_jigsaw_runner()
        if runner.is_running():
            runner.stop()
            self._update_jigsaw_ui_state(False)
        else:
            hwnd = self._get_selected_jigsaw_hwnd()
            runner.target_hwnd = hwnd
            runner.set_strategy(self._get_jigsaw_strategy_key())
            runner.start()
            self._update_jigsaw_ui_state(True)
            self._poll_jigsaw_stats()

    def _update_jigsaw_ui_state(self, running: bool) -> None:
        if self._jigsaw_start_btn and self._jigsaw_start_btn.winfo_exists():
            if running:
                self._jigsaw_start_btn.configure(
                    text="⏹  YAPBOZU DURDUR  (F9)",
                    bg="#ff5252",
                    activebackground="#ff1744",
                    fg="#ffffff",
                )
            else:
                self._jigsaw_start_btn.configure(
                    text="▶  YAPBOZU BAŞLAT  (F9)",
                    bg=self.GREEN,
                    activebackground="#00b055",
                    fg="#000000",
                )
        if self._jigsaw_status_dot and self._jigsaw_status_dot.winfo_exists():
            self._jigsaw_status_dot.configure(
                text="●",
                fg=self.GREEN if running else self.RED,
            )
        if self._jigsaw_status_lbl and self._jigsaw_status_lbl.winfo_exists():
            self._jigsaw_status_lbl.configure(
                text="Çözülüyor... (Kısayol: F9)" if running else "Durduruldu (Kısayol: F9)",
                fg=self.GREEN if running else self.RED,
            )

    def _on_jigsaw_status_change(self, text: str, color: str) -> None:
        def _update():
            if self._jigsaw_status_lbl and self._jigsaw_status_lbl.winfo_exists():
                self._jigsaw_status_lbl.configure(text=f"{text} (Kısayol: F9)", fg=color)
            if self._jigsaw_status_dot and self._jigsaw_status_dot.winfo_exists():
                self._jigsaw_status_dot.configure(fg=color)
        self.after(0, _update)

    def _on_jigsaw_log(self, msg: str) -> None:
        def _append():
            if self._jigsaw_log_box and self._jigsaw_log_box.winfo_exists():
                try:
                    self._jigsaw_log_box.configure(state="normal")
                    self._jigsaw_log_box.insert("end", msg + "\n")
                    self._jigsaw_log_box.see("end")
                    self._jigsaw_log_box.configure(state="disabled")
                except Exception:
                    pass
            self._append_log(msg)
        self.after(0, _append)

    def _poll_jigsaw_stats(self) -> None:
        if self._jigsaw_runner:
            st = self._jigsaw_runner.stats
            tot = max(1, st.total_games)
            lp = (st.large_chests / tot) * 100.0 if st.total_games else 0.0
            mp = (st.med_chests / tot) * 100.0 if st.total_games else 0.0
            sp = (st.small_chests / tot) * 100.0 if st.total_games else 0.0

            if "games" in self._jigsaw_stat_vars:
                self._jigsaw_stat_vars["games"].set(str(st.total_games))
            if "large" in self._jigsaw_stat_vars:
                self._jigsaw_stat_vars["large"].set(f"{st.large_chests}  (%{lp:.1f})")
            if "med" in self._jigsaw_stat_vars:
                self._jigsaw_stat_vars["med"].set(f"{st.med_chests}  (%{mp:.1f})")
            if "small" in self._jigsaw_stat_vars:
                self._jigsaw_stat_vars["small"].set(f"{st.small_chests}  (%{sp:.1f})")
            if "won" in self._jigsaw_stat_vars:
                self._jigsaw_stat_vars["won"].set(f"{st.total_won:.3f} Won ({st.total_won * 100:.1f}M Yang)")
            if "step" in self._jigsaw_stat_vars:
                self._jigsaw_stat_vars["step"].set(f"{st.current_moves} / 24")

        if self._jigsaw_runner and self._jigsaw_runner.is_running():
            self.after(250, self._poll_jigsaw_stats)

    def _reset_jigsaw_stats(self) -> None:
        if self._jigsaw_runner:
            self._jigsaw_runner.stats.reset()
        for v in self._jigsaw_stat_vars.values():
            v.set("0")
        if "won" in self._jigsaw_stat_vars:
            self._jigsaw_stat_vars["won"].set("0.000 Won")
        if "step" in self._jigsaw_stat_vars:
            self._jigsaw_stat_vars["step"].set("0 / 24")
        self._append_log("♻️ Yapboz sayaçları ve istatistikleri sıfırlandı.")

    def _open_jigsaw_modal(self) -> None:
        if self._jigsaw_win and self._jigsaw_win.winfo_exists():
            self._jigsaw_win.lift()
            self._jigsaw_win.focus_force()
            return

        jwin = tk.Toplevel(self)
        self._jigsaw_win = jwin
        jwin.title("🧩 Metin2 Balık Yapboz Botu (Yapay Zeka & Bellman)")
        jwin.geometry("500x670")
        jwin.configure(bg=self.BG)
        jwin.attributes("-topmost", True)

        # ── Başlık ──
        hdr = tk.Frame(jwin, bg="#311b92", height=50)
        hdr.pack(fill="x")
        tk.Label(hdr, text="🧩  Metin2 Balık Yapboz Botu",
                 font=("Segoe UI Semibold", 13), fg=self.TEXT, bg="#311b92").pack(anchor="w", padx=14, pady=(6, 1))
        tk.Label(hdr, text="Alt-Uzay Bellman + BFS Lookahead Çözücü (Büyük & Orta Sandık Odaklı)",
                 font=("Segoe UI", 8), fg="#b388ff", bg="#311b92").pack(anchor="w", padx=14, pady=(0, 6))

        # ── Başlat / Durdur & Durum ──
        ctrl_f = tk.Frame(jwin, bg=self.BG)
        ctrl_f.pack(fill="x", padx=12, pady=(10, 4))

        is_run = bool(self._jigsaw_runner and self._jigsaw_runner.is_running())
        self._jigsaw_start_btn = tk.Button(
            ctrl_f,
            text="⏹  YAPBOZU DURDUR  (F9)" if is_run else "▶  YAPBOZU BAŞLAT  (F9)",
            font=("Segoe UI Semibold", 12),
            bg="#ff5252" if is_run else self.GREEN,
            fg="#ffffff" if is_run else "#000000",
            activebackground="#ff1744" if is_run else "#00b055",
            relief="flat", cursor="hand2", bd=0, padx=16, pady=8,
            command=self._toggle_jigsaw,
        )
        self._jigsaw_start_btn.pack(fill="x")

        status_f = tk.Frame(jwin, bg=self.SURFACE, pady=4)
        status_f.pack(fill="x", padx=12, pady=(4, 8))
        self._jigsaw_status_dot = tk.Label(status_f, text="●", font=("Segoe UI", 12),
                                           fg=self.GREEN if is_run else self.RED, bg=self.SURFACE)
        self._jigsaw_status_dot.pack(side="left", padx=(10, 4))
        self._jigsaw_status_lbl = tk.Label(status_f,
                                           text="Çözülüyor... (Kısayol: F9)" if is_run else "Durduruldu (Kısayol: F9)",
                                           font=("Segoe UI Semibold", 9),
                                           fg=self.GREEN if is_run else self.RED, bg=self.SURFACE)
        self._jigsaw_status_lbl.pack(side="left")

        # ── Ayarlar & Strateji ──
        opt_box = tk.LabelFrame(jwin, text=" Strateji & Hedef İstemci ", bg=self.SURFACE,
                                fg=self.GOLD, font=self.FONT_H, bd=1, relief="flat")
        opt_box.pack(fill="x", padx=12, pady=4)

        s_row = tk.Frame(opt_box, bg=self.SURFACE)
        s_row.pack(fill="x", padx=10, pady=(6, 4))
        tk.Label(s_row, text="Strateji Kipi:", font=("Segoe UI Semibold", 9),
                 fg=self.TEXT, bg=self.SURFACE).pack(side="left")

        def _on_strat_change(val):
            if self._jigsaw_runner:
                self._jigsaw_runner.set_strategy(self._JIGSAW_STRAT_MAP.get(val, "balanced"))

        strat_options = list(self._JIGSAW_STRAT_MAP.keys())
        strat_menu = tk.OptionMenu(s_row, self._jigsaw_strategy_var, *strat_options, command=_on_strat_change)
        strat_menu.configure(bg="#223565", fg="#ffffff", activebackground="#1a4a8a", activeforeground="#ffffff",
                             font=("Segoe UI", 9), relief="flat", bd=0, highlightthickness=0)
        strat_menu["menu"].configure(bg="#223565", fg="#ffffff", activebackground="#311b92", font=("Segoe UI", 9))
        strat_menu.pack(side="right", fill="x", expand=True, padx=(8, 0))

        c_row = tk.Frame(opt_box, bg=self.SURFACE)
        c_row.pack(fill="x", padx=10, pady=(2, 8))
        tk.Label(c_row, text="Oyun Penceresi:", font=("Segoe UI Semibold", 9),
                 fg=self.TEXT, bg=self.SURFACE).pack(side="left")

        client_opts = [self._JIGSAW_AUTO_LABEL]
        for idx, hwnd, var, info in self._win_items:
            client_opts.append(f"Client {idx} (PID:{info['pid']})")
        if not self._win_items:
            client_opts.append("Client 1 (Varsayılan)")

        c_menu = tk.OptionMenu(c_row, self._jigsaw_client_var, *client_opts)
        c_menu.configure(bg="#223565", fg="#ffffff", activebackground="#1a4a8a", activeforeground="#ffffff",
                         font=("Segoe UI", 9), relief="flat", bd=0, highlightthickness=0)
        c_menu["menu"].configure(bg="#223565", fg="#ffffff", activebackground="#311b92", font=("Segoe UI", 9))
        c_menu.pack(side="right", fill="x", expand=True, padx=(8, 0))

        # ── İstatistikler (Canlı Kartlar) ──
        stats_box = tk.LabelFrame(jwin, text=" Canlı Yapboz İstatistikleri ", bg=self.SURFACE,
                                  fg=self.GOLD, font=self.FONT_H, bd=1, relief="flat")
        stats_box.pack(fill="x", padx=12, pady=4)

        stat_items = [
            ("Tamamlanan", "games", self.TEXT),
            ("Büyük (<=10 Adım)", "large", self.GOLD),
            ("Orta (11-24 Adım)", "med", self.GREEN),
            ("Küçük (25+ Adım)", "small", "#ff8a80"),
            ("Toplam Gelir", "won", self.GOLD),
            ("Anlık Adım / Miktar", "step", "#80d8ff"),
        ]

        grid_f = tk.Frame(stats_box, bg=self.SURFACE)
        grid_f.pack(fill="x", padx=8, pady=6)

        for i, (lbl, key, color) in enumerate(stat_items):
            r, c = divmod(i, 2)
            cell = tk.Frame(grid_f, bg="#111930", padx=8, pady=4)
            cell.grid(row=r, column=c, padx=4, pady=3, sticky="nsew")
            grid_f.grid_columnconfigure(c, weight=1)

            tk.Label(cell, text=lbl, font=("Segoe UI", 8), fg=self.DIM, bg="#111930").pack(anchor="w")
            if key not in self._jigsaw_stat_vars:
                self._jigsaw_stat_vars[key] = tk.StringVar(value="0")
            tk.Label(cell, textvariable=self._jigsaw_stat_vars[key],
                     font=("Segoe UI Semibold", 11), fg=color, bg="#111930").pack(anchor="w")

        # ── Canlı Log Akışı ──
        log_f = tk.LabelFrame(jwin, text=" Yapboz Hamle Akışı ", bg=self.SURFACE,
                              fg=self.DIM, font=self.FONT_H, bd=1, relief="flat")
        log_f.pack(fill="both", expand=True, padx=12, pady=4)

        self._jigsaw_log_box = scrolledtext.ScrolledText(
            log_f, wrap="word", font=("Consolas", 8),
            bg="#0d0d1a", fg="#b388ff",
            relief="flat", bd=0, height=8,
        )
        self._jigsaw_log_box.pack(fill="both", expand=True, padx=6, pady=4)
        self._jigsaw_log_box.configure(state="disabled")

        # ── Sıfırla Butonu ──
        b_f = tk.Frame(jwin, bg=self.BG)
        b_f.pack(fill="x", padx=12, pady=(4, 10))
        tk.Button(b_f, text="♻️  Yapboz Sayaçlarını Sıfırla",
                  font=("Segoe UI", 9), bg="#5a2a2a", fg="#ffffff",
                  activebackground="#7a3a3a", activeforeground="#ffffff",
                  relief="flat", cursor="hand2", padx=10, pady=4,
                  command=self._reset_jigsaw_stats).pack(fill="x")

        # Açıldığında sayaçları senkronize et
        self._poll_jigsaw_stats()


    # -----------------------------------------------------------------------
    # Görsel Whitelist
    # -----------------------------------------------------------------------
    def _open_visual_settings(self) -> None:
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
            if canvas.winfo_exists():
                canvas.yview_scroll(int(-1*(event.delta/120)), "units")
        canvas.bind_all("<MouseWheel>", _on_mousewheel)

        canvas.pack(side="top", fill="both", expand=True, padx=5, pady=5)
        scrollbar.pack(side="right", fill="y", before=canvas)

        settings_win._img_refs = []
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
                    settings_win._img_refs.append(tk_img)
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

        def _close_settings_win():
            try:
                canvas.unbind_all("<MouseWheel>")
            except Exception:
                pass
            settings_win._img_refs.clear()
            settings_win.destroy()

        settings_win.protocol("WM_DELETE_WINDOW", _close_settings_win)

        def save_and_close():
            new_wl = {k: v.get() for k, v in vars_dict.items()}
            with open("ayarlar.json", "w", encoding="utf-8") as f:
                json.dump(new_wl, f, ensure_ascii=False, indent=4)
            self._append_log("✅ Görsel ayarlar başarıyla kaydedildi!")
            _close_settings_win()
            
        btn_frame = tk.Frame(settings_win, bg=self.BG, pady=10)
        btn_frame.pack(fill="x", side="bottom")
        tk.Button(btn_frame, text="💾 Ayarları Kaydet", bg=self.GREEN, fg="#000", 
                  font=("Segoe UI Semibold", 12), cursor="hand2", relief="flat",
                  command=save_and_close, pady=8).pack(fill="x", padx=20)

    # -----------------------------------------------------------------------
    # Başlat / Durdur & F8 / F9 Global Kısayolları
    # -----------------------------------------------------------------------
    def _poll_hotkey(self) -> None:
        try:
            # F8: Balık Botu (Başlat / Durdur)
            down_f8 = bool(win32api.GetAsyncKeyState(win32con.VK_F8) & 0x8000)
            if down_f8 and not self._f8_was_down:
                self._f8_was_down = True
                if str(self._start_btn.cget("state")) != "disabled":
                    self._toggle_app()
            elif not down_f8:
                self._f8_was_down = False

            # F9: Yapboz Botu (Başlat / Durdur)
            down_f9 = bool(win32api.GetAsyncKeyState(win32con.VK_F9) & 0x8000)
            if down_f9 and not self._f9_was_down:
                self._f9_was_down = True
                self._toggle_jigsaw()
            elif not down_f9:
                self._f9_was_down = False
        except Exception:
            pass
        self.after(120, self._poll_hotkey)

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

        # Liste boşsa başlamadan önce pencereleri otomatik bir kez daha tara
        if not self._win_items:
            self._refresh_windows(silent=True)

        selected_items = self._get_selected_items()
        selected_hwnds: list[int] = []
        client_rois: dict[int, dict] = {}

        if selected_items:
            for slot_idx, hwnd in selected_items:
                slot_data = self._client_slots.get(str(slot_idx)) or {}
                if not slot_data.get("chat_roi") or not slot_data.get("minigame_roi"):
                    self._append_log(
                        f"❌ HATA: Client {slot_idx} için henüz '💬 Chat Seç' ve '🎯 Oyun Seç' alanları seçilmedi! "
                        f"Lütfen önce Client {slot_idx} satırındaki butonlara basıp kendi alanlarını seçin."
                    )
                    return
                selected_hwnds.append(hwnd)
                client_rois[hwnd] = dict(slot_data)
        else:
            slot1 = self._client_slots.get("1") or {}
            if slot1.get("chat_roi"):
                self._chat_roi = slot1["chat_roi"]
                self._rel_chat_roi = slot1.get("rel_chat_roi")
            if slot1.get("minigame_roi"):
                self._minigame_roi = slot1["minigame_roi"]
                self._rel_minigame_roi = slot1.get("rel_minigame_roi")

        config = FisherConfig(
            chat_roi=self._chat_roi,
            minigame_roi=self._minigame_roi,
            rel_chat_roi=self._rel_chat_roi,
            rel_minigame_roi=self._rel_minigame_roi,
            whitelist=whitelist,
            farm_duration_minutes=90,
            rest_duration_minutes=5,
        )
        # Bot çalışırken panel oyun pencerelerinin (özellikle sağ-alttaki Client 2'nin) üstünü kapatmasın!
        try:
            self.attributes("-topmost", False)
        except Exception:
            pass

        self._running = True
        self._worker_thread = threading.Thread(
            target=self._run_worker,
            args=(config, selected_hwnds, client_rois),
            daemon=True,
            name="MultiClientOrchestrator",
        )
        self._worker_thread.start()
        self._start_btn.config(
            text="⏳  Hazırlanıyor...",
            bg="#888800", activebackground="#666600",
            state="disabled",
        )
        self._status_dot.config(fg="#ffd700")
        self._status_label.config(text="OCR & İstemciler hazırlanıyor...", fg="#ffd700")

    def _run_worker(self, config: FisherConfig, selected_hwnds: list[int], client_rois: dict[int, dict]) -> None:
        try:
            self._manager = MultiClientManager(
                config=config,
                target_hwnds=selected_hwnds,
                client_rois=client_rois,
            )
            n_active = max(1, len(selected_hwnds))
            self.after(0, lambda: self._set_status_running(n_active))
            self._manager.run()
        except Exception as e:
            logging.getLogger("Fisher").exception(f"Hata: {e}")
        finally:
            self._running = False
            self.after(0, self._set_status_stopped)

    def _stop_app(self) -> None:
        if self._manager:
            self._manager.stop()
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
        self._manager = None
        self._saved_shown = True
        self._show_stats({"session_start": time.time()}, running=False)
        self._append_log("♻️ Sayaçlar, süre ve kazanç sıfırlandı.")

    # -----------------------------------------------------------------------
    # Durum
    # -----------------------------------------------------------------------
    def _set_status_running(self, n_clients: int = 1) -> None:
        color = self.GOLD
        self._status_dot.config(fg=color)
        self._status_label.config(text=f"Çalışıyor ({n_clients} Client) — Durdur: F8", fg=color)
        self._start_btn.config(
            text="⏹  DURDUR  (F8)", bg=self.RED,
            activebackground="#cc4444",
            state="normal",
        )

    def _set_status_stopped(self) -> None:
        try:
            self.attributes("-topmost", True)
            self.lift()
        except Exception:
            pass
        self._status_dot.config(fg=self.RED)
        self._status_label.config(text="Durduruldu  (Kısayol: F8)", fg=self.RED)
        self._state_label.config(text="")
        self._start_btn.config(
            text="▶  BAŞLAT  (F8)", bg=self.GREEN,
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
        elif "HIT" in msg or "TUTULDU" in msg:
            tag = "HIT"
        elif "MISS" in msg or "KAÇTI" in msg:
            tag = "MISS"
        self._log_box.insert("end", msg + "\n", tag)
        lines = int(self._log_box.index("end-1c").split(".")[0])
        if lines > 500:
            self._log_box.delete("1.0", f"{lines-500}.0")
        if self._autoscroll_var.get():
            self._log_box.see("end")
        self._log_box.config(state="disabled")

    def _clear_all(self) -> None:
        """Log + sayaçlar + süre + kazanç temizlenir (bot çalışıyorsa sadece log temizlenir)."""
        self._clear_logs()
        if self._running:
            self._append_log("⚠️ Bot çalışırken sadece log temizlendi; sayaç/süre için önce durdur.")
            return
        self._reset_counters()

    def _clear_logs(self) -> None:
        self._log_box.config(state="normal")
        self._log_box.delete("1.0", "end")
        self._log_box.config(state="disabled")

    # -----------------------------------------------------------------------
    # İstatistik
    # -----------------------------------------------------------------------
    def _poll_stats(self) -> None:
        if self._manager and self._running:
            self._show_stats(self._manager.stats, running=True)
            try:
                self._state_label.config(text=self._manager.state_summary(), fg=self.DIM)
            except Exception:
                pass
        elif not self._running and not self._manager and not getattr(self, "_saved_shown", False):
            self._saved_shown = True
            self._show_stats(self._saved_to_stats(load_session()), running=False)
        self.after(1000, self._poll_stats)

    @staticmethod
    def _saved_to_stats(saved: dict) -> dict:
        """oturum.json içeriğini panelin gösterebileceği stats sözlüğüne çevirir."""
        st = dict(saved)
        st["session_start"] = time.time() - float(saved.get("active_seconds", 0))
        try:
            with open("fiyatlar.json", "r", encoding="utf-8") as f:
                fiyat = json.load(f)
        except Exception:
            fiyat = {}
        total_yang, total_won = compute_earnings_from_counts(saved.get("caught_fish") or {}, fiyat)
        st["total_yang"] = total_yang
        st["total_won"] = total_won
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
    # Kazanç & Piyasa Modalı (Yerel fiyatlar.json tabanlı — Chrome/Web Yok)
    # -----------------------------------------------------------------------
    def _open_earnings_modal(self) -> None:
        modal = tk.Toplevel(self)
        modal.title("💰 Balık Detayları & Kazanç")
        modal.geometry("500x560")
        modal.configure(bg=self.BG)
        modal.attributes("-topmost", True)

        top = tk.Frame(modal, bg=self.ACCENT, pady=10)
        top.pack(fill="x")
        tk.Label(top, text="📊 Yakalanan Balıklar & Piyasa Karşılığı",
                 bg=self.ACCENT, fg=self.TEXT, font=self.FONT_H).pack()

        fiyatlar = {}
        if os.path.exists("fiyatlar.json"):
            try:
                with open("fiyatlar.json", "r", encoding="utf-8") as f:
                    fiyatlar = json.load(f)
            except Exception:
                pass

        canvas = tk.Canvas(modal, bg=self.BG, highlightthickness=0)
        scrollbar = tk.Scrollbar(modal, orient="vertical", command=canvas.yview)
        scrollable = tk.Frame(canvas, bg=self.BG)

        scrollable.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=scrollable, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side="top", fill="both", expand=True, padx=8, pady=8)
        scrollbar.pack(side="right", fill="y", before=canvas)

        cur_stats = self._manager.stats if self._manager else self._saved_to_stats(load_session())
        caught_fish = cur_stats.get("caught_fish", {})
        total_won = cur_stats.get("total_won", 0.0)
        total_yang = cur_stats.get("total_yang", 0)

        summary_f = tk.Frame(scrollable, bg=self.SURFACE, pady=10, padx=12)
        summary_f.pack(fill="x", pady=4, padx=5)
        tk.Label(summary_f, text=f"Toplam Kazanç: {total_won:.3f} Won ({total_yang:,} Yang)",
                 font=("Segoe UI Semibold", 13), fg=self.GOLD, bg=self.SURFACE).pack(anchor="w")
        tk.Label(summary_f, text="* 1 Won = 100M Yang (fiyatlar.json yerel birim fiyatlarına göre)",
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

        bot_bar = tk.Frame(modal, bg=self.BG, pady=10)
        bot_bar.pack(fill="x", side="bottom", padx=10)

        tk.Button(bot_bar, text="✏️  Fiyatları Elle Düzenle (fiyatlar.json)",
                  font=self.FONT_UI, bg=self.ACCENT, fg=self.TEXT,
                  relief="flat", cursor="hand2", pady=6,
                  command=lambda: [modal.destroy(), self._open_price_editor()]).pack(fill="x")

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

            if self._manager:
                self._manager._update_earnings()
            else:
                self._show_stats(self._saved_to_stats(load_session()), running=False)

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
        if self._jigsaw_runner and self._jigsaw_runner.is_running():
            self._jigsaw_runner.stop()
        close_thread_sct()
        self.destroy()


if __name__ == "__main__":
    app = FisherPanel()
    app.mainloop()
