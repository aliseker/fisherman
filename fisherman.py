"""
=============================================================================
Metin2 Fishing Helper - Gelişmiş Multi-Thread Mimari
=============================================================================
Tamamen Asenkron Producer-Consumer (Çoklu İşlemci) Mimarisi.
=============================================================================
"""

import sys
import os

# venv_gpu otomatik köprü: Sistem python ile çalıştırılsa bile venv_gpu paketlerini kullan
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

import time
import random
import math
import difflib
import threading
import queue
import re
import logging
from logging.handlers import RotatingFileHandler
import json
import ctypes
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from enum import Enum, auto
from typing import Optional, Tuple, List, Callable, Dict, Set

if _base_dir not in sys.path:
    sys.path.insert(0, _base_dir)

import cv2
import numpy as np
import mss
from mss.exception import ScreenShotError
import pydirectinput
pydirectinput.PAUSE = 0  # CRITICAL: Disable 0.1s default pause between actions
import win32api
import win32gui
import win32con
import win32ui
import win32process

from hardware_manager import (
    ensure_dpi_awareness,
    get_thread_sct,
    close_thread_sct,
    thread_sct_scope,
    thread_grab_bgr,
    InputPriority,
    PriorityHardwareMutex,
    WindowFocusGuard,
    HumanMouse,
    HumanKeyboard,
)

ensure_dpi_awareness()

# OCR (Sadece yerel GPU / CPU — VM uzak OCR kaldırıldı)
try:
    import easyocr
except ImportError:
    easyocr = None

_HAS_CUDA = False
if easyocr is not None:
    try:
        import torch
        _HAS_CUDA = torch.cuda.is_available()
    except Exception:
        _HAS_CUDA = False

_OCR_BACKEND = "easyocr" if easyocr is not None else "none"

# EasyOCR modeli modül genelinde tek sefer yüklenir (Singleton).
# 1-4 istemci aynı anda çalışsa bile tek bir GPU modeli (~450 MB VRAM) ortak kullanılır.
_EASYOCR_READER = None


class _PriorityOCRLock:
    """Minigame balık türü okumasını (urgent=True) olta/sonuç OCR'larından öne alır."""

    def __init__(self) -> None:
        self._gate = PriorityHardwareMutex(aging_sec=0.75)

    @contextmanager
    def hold(self, urgent: bool = False):
        prio = InputPriority.URGENT_MINIGAME_HIT if urgent else InputPriority.NORMAL_CAST
        ok = self._gate.acquire(priority=prio, timeout=15.0)
        try:
            yield ok
        finally:
            if ok:
                self._gate.release()

    def __enter__(self):
        self._gate.acquire(priority=InputPriority.NORMAL_CAST, timeout=15.0)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._gate.release()


_OCR_LOCK = _PriorityOCRLock()


def _jitter(mean: float, std: float, lo: float, hi: float) -> float:
    """Anti-ban için sabit bekleme süreleri yerine çan eğrisi (Gaussian) dağılımlı rastgele süre üretir."""
    return max(lo, min(hi, random.gauss(mean, std)))


def _get_ocr_reader():
    """EasyOCR Reader singleton'ı döndürür; ilk çağrıda thread-safe olarak yükler."""
    global _EASYOCR_READER
    if _OCR_BACKEND != "easyocr" or easyocr is None:
        return None
    if _EASYOCR_READER is None:
        with _OCR_LOCK:
            if _EASYOCR_READER is None:
                log_init = logging.getLogger("FisherAppMT")
                log_init.info("[OCR] EasyOCR modeli ilk kez yükleniyor...")
                try:
                    import torch
                    is_cuda = torch.cuda.is_available()
                    log_init.info(f"[OCR] Ekran Kartı (CUDA) Aktif Mi: {'EVET (Hızlı)' if is_cuda else 'HAYIR (CPU)'}")
                except Exception:
                    pass
                _EASYOCR_READER = easyocr.Reader(["tr", "en"], gpu=_HAS_CUDA, verbose=False)
                log_init.info("[OCR] EasyOCR modeli yüklendi.")
    return _EASYOCR_READER


try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        RotatingFileHandler("FisherApp_MT.log", maxBytes=2_000_000, backupCount=1, encoding="utf-8"),
    ],
)
log = logging.getLogger("FisherAppMT")


class AppState(Enum):
    IDLE          = auto()
    CAST          = auto()
    DECISION      = auto()
    MINIGAME      = auto()
    ANIM_CANCEL   = auto()
    COOLDOWN      = auto()
    REST          = auto()
    STOPPED       = auto()


@dataclass
class FisherConfig:
    game_window_title: str = "metin2"
    bait_keys: list = field(default_factory=lambda: ["1", "2", "3", "4", "f1", "f2", "f3", "f4"])
    cast_key: str = "space"
    chat_roi: dict = field(default_factory=lambda: {"top": 620, "left": 130, "width": 500, "height": 70})
    minigame_roi: dict = field(default_factory=lambda: {"top": 200, "left": 400, "width": 300, "height": 300})
    rel_chat_roi: Optional[dict] = None
    rel_minigame_roi: Optional[dict] = None
    whitelist: dict = field(default_factory=dict)
    fishing_close_key: str = "escape"
    mount_key: str = "g"
    farm_duration_minutes: int = 90
    rest_duration_minutes: int = 5
    circle_radius: int = 45
    minigame_fps_target: int = 35


_EXCLUDED_WINDOW_TITLES = (
    "visual studio", "helper", "fisher", "chrome", "edge", "firefox",
    "code", "vmware", "virtualbox", "antigravity", "powershell", "cmd",
    "notepad", "discord", "ayarlar", "fiyat",
)


def discover_game_windows(title_hint: str = "metin2") -> List[dict]:
    """Açık olan tüm Metin2 istemci pencerelerini (1..4+) bulur ve koordinatlarıyla döndürür."""
    hint = (title_hint or "metin2").lower()
    my_pid = os.getpid()
    found: List[dict] = []

    def _enum_cb(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return
            raw_title = win32gui.GetWindowText(hwnd) or ""
            t_low = raw_title.lower()
            if not t_low or hint not in t_low:
                return
            if any(ex in t_low for ex in _EXCLUDED_WINDOW_TITLES):
                return
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if pid == my_pid:
                return
            is_min = bool(win32gui.IsIconic(hwnd))
            if is_min:
                cx, cy, cw, ch = 0, 0, 800, 600
            else:
                crect = win32gui.GetClientRect(hwnd)
                cw, ch = crect[2] - crect[0], crect[3] - crect[1]
                if cw < 200 or ch < 200:
                    return
                cx, cy = win32gui.ClientToScreen(hwnd, (0, 0))
            found.append({
                "hwnd": hwnd,
                "pid": pid,
                "title": raw_title.strip(),
                "client_x": cx,
                "client_y": cy,
                "width": cw,
                "height": ch,
                "minimized": is_min,
            })
        except Exception:
            pass

    try:
        win32gui.EnumWindows(_enum_cb, None)
    except Exception:
        pass

    # Ekrandaki diziliş sırasına göre (önce üst satır sol->sağ, sonra alt satır sol->sağ) sırala
    found.sort(key=lambda w: (9999 if w["minimized"] else w["client_y"] // 180, w["client_x"], w["hwnd"]))
    return found


def tile_game_windows(hwnds: List[int], panel_reserve_width: int = 360) -> List[Tuple[int, int, int]]:
    """Seçili 1-6 oyun penceresini çözünürlüklerini bozmadan (SWP_NOSIZE) ekrana hizalar.
    1:1 800x600 DPI ayarında 2K (2560x1440) ekrana 3-4 pencere 2x2 (panel sağda boşlukta),
    5-6 pencere ise 3x2 sıfır çakışmayla dizilir."""
    valid = [h for h in hwnds if h and win32gui.IsWindow(h)]
    if not valid:
        return []

    try:
        mon = win32api.MonitorFromPoint((0, 0), win32con.MONITOR_DEFAULTTOPRIMARY)
        work = win32api.GetMonitorInfo(mon).get("Work", (0, 0, 1920, 1040))
    except Exception:
        work = (0, 0, 1920, 1040)

    wx0, wy0, wx1, wy1 = work
    screen_w = max(800, wx1 - wx0)
    avail_h = max(600, wy1 - wy0)

    placed: List[Tuple[int, int, int]] = []
    for h in valid:
        try:
            if win32gui.IsIconic(h):
                win32gui.ShowWindow(h, win32con.SW_RESTORE)
                time.sleep(0.08)
        except Exception:
            pass

    try:
        r0 = win32gui.GetWindowRect(valid[0])
        win_w = max(400, r0[2] - r0[0])
        win_h = max(300, r0[3] - r0[1])
    except Exception:
        win_w, win_h = 816, 639

    n = len(valid)
    row1_y = wy0
    row2_y = wy0 + (win_h if (win_h * 2 <= avail_h) else max(140, avail_h - win_h))

    if n == 1:
        coords = [(wx0, row1_y)]
    elif n == 2:
        if screen_w >= win_w * 2:
            coords = [(wx0, row1_y), (wx0 + win_w, row1_y)]
        else:
            diag_x = max(wx0 + 420, wx1 - win_w)
            diag_y = max(wy0 + 140, wy1 - win_h)
            coords = [(wx0, wy0), (diag_x, diag_y)]
    elif n <= 4:
        # 2x2 tam sığıyorsa (ör. 2K ekranda 1:1 800x600) pencereleri ASLA üst üste bindirme!
        if win_w * 2 <= screen_w:
            col1_x, col2_x = wx0, wx0 + win_w
        else:
            col1_x = wx0
            col2_x = max(wx0 + 420, wx1 - win_w)
        coords = [(col1_x, row1_y), (col2_x, row1_y), (col1_x, row2_y), (col2_x, row2_y)][:n]
    else:
        # 5-6 istemci: 3x2 sıfır çakışmalı ızgara (ör. 2560x1368 üzerinde 3x816 x 2x639)
        cols = 3 if (win_w * 3 <= screen_w or n >= 5) else 2
        coords = []
        for idx_w in range(n):
            r = (idx_w // cols) % 2
            c = idx_w % cols
            if win_w * cols <= screen_w:
                tx = wx0 + c * win_w
            else:
                step_x = max(240, (screen_w - win_w) // max(1, cols - 1))
                tx = wx0 + min(screen_w - win_w, c * step_x)
            ty = row1_y if r == 0 else row2_y
            coords.append((tx, ty))

    flags = win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE
    for h, (tx, ty) in zip(valid, coords):
        try:
            win32gui.SetWindowPos(h, win32con.HWND_TOP, int(tx), int(ty), 0, 0, flags)
            placed.append((h, int(tx), int(ty)))
        except Exception as e:
            log.warning(f"[DİZİLİM] Pencere taşınamadı (hwnd={h}): {e}")
    return placed


def screen_roi_to_relative_for_hwnd(abs_roi: dict, hwnd: Optional[int]) -> Optional[dict]:
    """Ekrandan seçilen mutlak ROI koordinatını doğrudan belirtilen hedef pencerenin (hwnd)
    istemci (client) içi göreli koordinatına çevirir."""
    if not abs_roi or not hwnd:
        return None
    try:
        if win32gui.IsWindow(hwnd) and not win32gui.IsIconic(hwnd):
            cx, cy = win32gui.ClientToScreen(hwnd, (0, 0))
            return {
                "rel_left": int(abs_roi["left"] - cx),
                "rel_top": int(abs_roi["top"] - cy),
                "width": int(abs_roi["width"]),
                "height": int(abs_roi["height"]),
            }
    except Exception:
        pass
    return None


def screen_roi_to_relative(abs_roi: dict, title_hint: str = "metin2", require_inside: bool = False) -> Optional[dict]:
    """Ekrandan seçilen mutlak ROI koordinatını, ait olduğu Metin2 penceresinin
    istemci (client) içi göreli koordinatına çevirir. Böylece tek kalibrasyon 4 pencerede de çalışır."""
    if not abs_roi:
        return None
    wins = [w for w in discover_game_windows(title_hint) if not w["minimized"]]
    if not wins:
        return None

    rcx = abs_roi["left"] + abs_roi["width"] / 2.0
    rcy = abs_roi["top"] + abs_roi["height"] / 2.0

    # Önce ROI merkezini gerçekten kapsayan pencereyi ara
    containing = []
    for w in wins:
        if (w["client_x"] - 25 <= rcx <= w["client_x"] + w["width"] + 25 and
                w["client_y"] - 45 <= rcy <= w["client_y"] + w["height"] + 25):
            containing.append(w)

    if containing:
        # Birden fazla pencere üst üste biniyorsa ön planda olana öncelik ver
        fg = win32gui.GetForegroundWindow()
        chosen = next((w for w in containing if w["hwnd"] == fg), containing[0])
    elif require_inside:
        return None
    else:
        # Hiçbiri tam kapsamıyorsa merkeze en yakın pencereyi seç
        chosen = min(wins, key=lambda w: math.hypot(
            (w["client_x"] + w["width"] / 2.0) - rcx,
            (w["client_y"] + w["height"] / 2.0) - rcy,
        ))

    return {
        "rel_left": int(abs_roi["left"] - chosen["client_x"]),
        "rel_top": int(abs_roi["top"] - chosen["client_y"]),
        "width": int(abs_roi["width"]),
        "height": int(abs_roi["height"]),
    }


def resolve_window_roi(hwnd: Optional[int], abs_roi: dict, rel_roi: Optional[dict]) -> dict:
    """Hedef pencerenin güncel ekran konumuna göre ROI koordinatlarını hesaplar."""
    if hwnd and rel_roi:
        try:
            if win32gui.IsWindow(hwnd) and not win32gui.IsIconic(hwnd):
                cx, cy = win32gui.ClientToScreen(hwnd, (0, 0))
                return {
                    "left": int(cx + rel_roi["rel_left"]),
                    "top": int(cy + rel_roi["rel_top"]),
                    "width": int(rel_roi["width"]),
                    "height": int(rel_roi["height"]),
                }
        except Exception:
            pass
    return dict(abs_roi)


def capture_window_roi(
    hwnd: Optional[int],
    roi: dict,
    allow_background: bool = False,
    use_printwindow: bool = False,
) -> np.ndarray:
    """Belirtilen ekran ROI alanını yakalar.
    Varsayılan olarak kilitsiz thread-local mss kullanır (DirectX ekran titremesi/siyahlaşması yaratmaz)."""
    if allow_background and use_printwindow and hwnd:
        try:
            if win32gui.IsWindow(hwnd) and not win32gui.IsIconic(hwnd) and win32gui.GetForegroundWindow() != hwnd:
                wx0, wy0, wx1, wy1 = win32gui.GetWindowRect(hwnd)
                ww, wh = wx1 - wx0, wy1 - wy0
                ox = roi["left"] - wx0
                oy = roi["top"] - wy0
                rw, rh = roi["width"], roi["height"]
                if ww > 100 and wh > 100 and ox >= 0 and oy >= 0 and ox + rw <= ww and oy + rh <= wh:
                    hwnd_dc = win32gui.GetWindowDC(hwnd)
                    mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
                    save_dc = mfc_dc.CreateCompatibleDC()
                    save_bmp = win32ui.CreateBitmap()
                    old_bmp = None
                    try:
                        save_bmp.CreateCompatibleBitmap(mfc_dc, ww, wh)
                        old_bmp = save_dc.SelectObject(save_bmp)
                        ok = ctypes.windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 2)
                        if ok == 1:
                            bmp_info = save_bmp.GetInfo()
                            bmp_bytes = save_bmp.GetBitmapBits(True)
                            full = np.frombuffer(bmp_bytes, dtype=np.uint8).reshape(
                                (bmp_info["bmHeight"], bmp_info["bmWidth"], 4)
                            )
                            crop = cv2.cvtColor(full[oy:oy + rh, ox:ox + rw], cv2.COLOR_BGRA2BGR)
                            if crop.size > 0 and float(np.mean(crop)) > 5.0 and float(np.std(crop)) > 8.0:
                                return crop
                    finally:
                        if old_bmp is not None:
                            try:
                                save_dc.SelectObject(old_bmp)
                            except Exception:
                                pass
                        try:
                            win32gui.DeleteObject(save_bmp.GetHandle())
                        except Exception:
                            pass
                        try:
                            save_dc.DeleteDC()
                        except Exception:
                            pass
                        try:
                            mfc_dc.DeleteDC()
                        except Exception:
                            pass
                        try:
                            win32gui.ReleaseDC(hwnd, hwnd_dc)
                        except Exception:
                            pass
        except Exception:
            pass

    return thread_grab_bgr(roi)


def is_game_window_foreground(target_hwnd: Optional[int] = None, title_hint: str = "metin2") -> bool:
    """Metin2 oyun penceresinin gerçekten ön planda (aktif odak) olup olmadığını kontrol eder.
    Kullanıcı tarayıcıya, Discord'a veya panele Alt-Tab attığında tuş/ESC basılmasını engeller."""
    try:
        fg = win32gui.GetForegroundWindow()
        if not fg:
            return False
        _, pid = win32process.GetWindowThreadProcessId(fg)
        if pid == os.getpid():
            return False
        if target_hwnd and win32gui.IsWindow(target_hwnd):
            return fg == target_hwnd
        title = win32gui.GetWindowText(fg).lower()
        if any(ex in title for ex in _EXCLUDED_WINDOW_TITLES):
            return False
        return title_hint.lower() in title
    except Exception:
        return False


class InputCoordinator:
    """Çoklu pencere (1-4 Client) modunda PriorityHardwareMutex + WindowFocusGuard köprüsü.
    - URGENT_MINIGAME_HIT (0) > HIGH_ANIM_CANCEL (1) > NORMAL_CAST (2) önceliği uygular.
    - Aging mekanizmasıyla bekleyen olta atışlarının askıda kalmasını (starvation) önler."""

    def __init__(self, session_hwnds: Optional[List[int]] = None, title_hint: str = "metin2"):
        self.session_hwnds: Set[int] = {h for h in (session_hwnds or []) if h}
        self.title_hint = title_hint
        self.mutex = PriorityHardwareMutex(aging_sec=2.4)
        self.focus_guard = WindowFocusGuard(
            session_hwnds=self.session_hwnds,
            title_hint=self.title_hint,
            excluded_titles=_EXCLUDED_WINDOW_TITLES,
        )

    def has_urgent_waiter(self) -> bool:
        return self.mutex.has_urgent_waiter()

    def register_hwnd(self, hwnd: Optional[int]) -> None:
        if hwnd:
            self.session_hwnds.add(hwnd)
            self.focus_guard.register_hwnd(hwnd)

    def is_user_in_game(self) -> bool:
        return self.focus_guard.is_user_in_game()

    def acquire(
        self,
        urgent: bool = False,
        stop_check: Optional[Callable[[], bool]] = None,
        priority: Optional[InputPriority] = None,
        client_id: int = 0,
    ) -> bool:
        prio = priority if priority is not None else (
            InputPriority.URGENT_MINIGAME_HIT if urgent else InputPriority.NORMAL_CAST
        )
        return self.mutex.acquire(
            priority=prio,
            client_id=client_id,
            timeout=10.0,
            stop_check=stop_check,
        )

    def release(self) -> None:
        self.mutex.release()

    def focus_client(
        self,
        hwnd: Optional[int],
        force_initial: bool = False,
        urgent: bool = False,
        pre_move_to: Optional[Tuple[int, int]] = None,
    ) -> bool:
        return self.focus_guard.focus(
            hwnd,
            force_initial=force_initial,
            urgent=urgent,
            pre_move_to=pre_move_to,
        )


class ScreenCapture:
    def capture(self, region: dict) -> np.ndarray:
        return capture_window_roi(None, region, allow_background=False)


def fold_tr(text: str) -> str:
    """Türkçe karakterleri ve yaygın OCR homoglyphlerini normalize eder."""
    if not text:
        return ""
    for k, v in (("ö", "o"), ("ç", "c"), ("ş", "s"), ("ı", "i"), ("ü", "u"), ("ğ", "g"), ("İ", "i")):
        text = text.replace(k, v)
    return text


class ChatReader:
    @staticmethod
    def _fold_tr(text: str) -> str:
        return fold_tr(text)

    def __init__(self, config: FisherConfig, target_hwnd: Optional[int] = None):
        self.config = config
        self.target_hwnd = target_hwnd
        self.capture = ScreenCapture()
        self._backend = _OCR_BACKEND
        self._reader = _get_ocr_reader()  # Singleton — model sadece ilk seferde yüklenir
        self._last_chat_text = ""
        self._last_grab_err_ts: float = 0.0

    def read_chat(self, roi_override: Optional[dict] = None, urgent: bool = False) -> str:
        roi = roi_override or resolve_window_roi(self.target_hwnd, self.config.chat_roi, self.config.rel_chat_roi)
        try:
            frame = capture_window_roi(self.target_hwnd, roi, allow_background=False)
        except ScreenShotError as e:
            now_t = time.monotonic()
            if now_t - self._last_grab_err_ts >= 3.0:
                self._last_grab_err_ts = now_t
                log.warning(f"[OCR] Sohbet ekran görüntüsü alınamadı: {e}")
            return ""
        # 1. Görüntüyü büyüt (1:1 DPI 800x600 pencerelerde 2.25x ince harfleri keskinleştirir; büyükte 2.0x)
        fx = 2.0
        if self.target_hwnd:
            try:
                if win32gui.IsWindow(self.target_hwnd) and not win32gui.IsIconic(self.target_hwnd):
                    cr = win32gui.GetClientRect(self.target_hwnd)
                    if 200 <= (cr[2] - cr[0]) <= 960:
                        fx = 2.25
            except Exception:
                pass
        elif frame.shape[0] <= 42 and frame.shape[1] <= 500:
            fx = 2.25
        y_tol = 13.0 * (fx / 2.0)

        resized = cv2.resize(frame, None, fx=fx, fy=fx, interpolation=cv2.INTER_CUBIC)
        # 2. Yarı-saydam chat arka planında Otsu ikilileştirme (binarization) 't', 'ı', 'l' harflerini
        #    eritip "Altın Sudak" -> "Akın Sudak" / "Alın Sudak" hatasına yol açıyordu.
        #    Bunun yerine yumuşak kontrast germe + ters gri tonlama kullanıyoruz.
        gray = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY)
        norm_gray = cv2.normalize(gray, None, 0, 255, cv2.NORM_MINMAX)
        inv_gray = 255 - norm_gray
        if self._backend == "easyocr" and self._reader:
            with _OCR_LOCK.hold(urgent=urgent):
                raw_items = self._reader.readtext(inv_gray, detail=1)
            if not raw_items:
                return ""
            # Kelime kutularını Y merkezine göre gerçek yatay sohbet satırlarına grupla,
            # her satırın içini soldan sağa (X) sırala, satırları yukarıdan aşağıya birleştir.
            parsed = []
            for bbox, txt, _conf in raw_items:
                s = txt.lower().strip()
                if not s:
                    continue
                cy = 0.25 * (bbox[0][1] + bbox[1][1] + bbox[2][1] + bbox[3][1])
                x0 = min(bbox[0][0], bbox[3][0])
                parsed.append((cy, x0, s))
            if not parsed:
                return ""
            parsed.sort(key=lambda it: it[0])
            lines_grouped: List[List[Tuple[float, float, str]]] = []
            for item in parsed:
                if not lines_grouped:
                    lines_grouped.append([item])
                else:
                    avg_cy = sum(x[0] for x in lines_grouped[-1]) / len(lines_grouped[-1])
                    if abs(item[0] - avg_cy) <= y_tol:
                        lines_grouped[-1].append(item)
                    else:
                        lines_grouped.append([item])
            out_lines = []
            for grp in lines_grouped:
                grp.sort(key=lambda it: it[1])
                out_lines.append(" ".join(it[2] for it in grp))
            return "\n".join(out_lines)
        return ""

    def snapshot_baseline(self) -> None:
        self._last_chat_text = self.read_chat()
        log.info("[SNAPSHOT] Chat referansı alındı.")

    def _normalize(self, text: str) -> str:
        """OCR hata duzeltme: rakam/sembol harf donusumu + kucuk harf + çöp kelime temizliği."""
        t = text.lower()
        for k, v in [("1","l"),("0","o"),("5","s"),("$","s"),("4","a"),("@","a"),
                     ("3","e"),("!","i"),("I","i"),("ş","s"),("ğ","g"),("ü","u"),("ö","o"),("ç","c"),("ı","i"),("İ","i")]:
            t = t.replace(k, v)
            
        import re
        t = re.sub(r'[^a-z0-9\s]', '', t)
        
        # Metin2 balıkçılık yazılarını temizle ki gereksiz kelimeler puanı bozmasın
        # "taktin" / "takhin" kelimesi "altin" ile eşleşip Altın Sudak zannedilmesini önle
        garbage_words = [
            "cengele", "cengek", "engele", "yem", "olarak", "solucan", "sokucan", "hamur",
            "taktin", "taklin", "taktn", "takhin", "takhm", "takhn", "takim",
            "baligi", "bal", "olaya", "oltaya", "oltaniza", "takildi", "takild", "takdm", "yakaladin", "yakalandi",
            "tuttun", "tullun", "kaybettin", "kavbettin", "kaybeltin", "uzunlugu", "kazandin", "kazanilan",
            "geldi", "bir", "sey", "oltanin", "ucuna", "takilmis", "taklmis", "takilmi", "taklmi", "takilmiy", "takikmiy", "gibi", 
            "gorunuyor", "gurunuyor", "gorunuyo", "gurunuyo", "grnyo", "grnyor"
        ]
        words = t.split()
        cleaned_words = [w for w in words if w not in garbage_words]
        
        return " ".join(cleaned_words).strip()

    def _best_score(self, keyword: str, text: str) -> float:
        """Kelime bazlı fuzzy matching: Keyword'deki HER kelime text içinde ayrı ayrı aranır."""
        norm_text = self._normalize(text)
        norm_kw   = self._normalize(keyword)

        if norm_kw in norm_text:
            return 1.0

        words    = norm_text.split()
        kw_words = norm_kw.split()
        
        if not words or not kw_words:
            return 0.0

        total_score = 0.0
        used_idx = set()
        for kw_w in sorted(kw_words, key=len, reverse=True):
            best_match_for_word = 0.0
            best_idx = -1
            for i, w in enumerate(words):
                if i in used_idx:
                    continue
                s = difflib.SequenceMatcher(None, kw_w, w).ratio()
                if s > best_match_for_word:
                    best_match_for_word = s
                    best_idx = i
            min_word_score = 0.66 if len(kw_w) <= 3 else 0.50
            if best_match_for_word < min_word_score:
                return 0.0
            used_idx.add(best_idx)
            total_score += best_match_for_word

        return total_score / len(kw_words)

    @staticmethod
    def _extract_latest_bite_text(raw_text: str) -> str:
        """Chat ROI birden fazla satır içerdiğinde önceki olta atışından kalan eski balık isimlerinin
        ('altın yüzük', 'minik balık', 'sudak' vb.) yeni atışla karışmasını engeller.
        En son yem takma ('solucan/hamur taktın') ifadesinden SONRAKİ kısmı döndürür."""
        if not raw_text:
            return ""
        folded = raw_text.lower()
        for k, v in (("ş", "s"), ("ğ", "g"), ("ü", "u"), ("ö", "o"), ("ç", "c"), ("ı", "i"), ("İ", "i")):
            folded = folded.replace(k, v)

        # En son yem takma veya yem değiştirme mesajının bittiği noktayı bul (OCR bozuk halleri dahil)
        bait_end_pat = re.compile(
            r"(?:solu\w*|soku\w*|hamur\w*|minik\s+bal\w*|yem\s+ol\w*|vem\s+ol\w*|cengel\w*|su\s+anki\w*)"
            r"(?:\s+\w+){0,4}?\s+(?:tak\w+|lak\w+|degis\w*)"
        )
        last_end = -1
        for m in bait_end_pat.finditer(folded):
            # 'oltaya ... takılmış' cümlesini yanlışlıkla yem mesajı sanmamak için kontrol et
            seg = folded[m.start():m.end()]
            if "oltaya" in seg or "oltaniza" in seg or "takilmi" in seg or "taklmi" in seg:
                continue
            last_end = m.end()
        if last_end >= 0 and last_end < len(raw_text) - 3:
            tail = raw_text[last_end:].strip(" ,.-_;:\n")
            if tail:
                return tail

        # Yem mesajı görünmüyorsa: en alttaki anlamlı satırı (veya taşmışsa son 2 satırı) al
        lines = [ln.strip() for ln in raw_text.splitlines() if ln.strip()]
        if len(lines) >= 2:
            # Üst satır yem/uyarı satırıysa atla, doğrudan alt satırdaki balık mesajını al
            last_ln_f = folded.splitlines()[-1] if folded.splitlines() else ""
            if any(w in last_ln_f for w in ("gorun", "gurun", "oltay", "takil", "takl", "belli")):
                prev_ln_f = folded.splitlines()[-2] if len(folded.splitlines()) >= 2 else ""
                if any(b in prev_ln_f for b in ("cengel", "solu", "soku", "hamur", "degis", "once")):
                    return lines[-1]
            return " ".join(lines[-2:])
        return raw_text

    def identify_fish_bite(self, text: str) -> Tuple[str, Optional[str]]:
        catch_unknown = self.config.whitelist.get("belli değil", False)
        if not text or not text.strip():
            if catch_unknown:
                log.info("[WHITELIST] Chat metni boş/okunamadı, 'belli değil' AÇIK -> Yakalanıyor.")
                return ("play", "belli değil")
            else:
                log.warning("[WHITELIST] Chat metni boş/okunamadı, 'belli değil' KAPALI -> İptal ediliyor (Skip).")
                return ("skip", "belli değil")

        latest_text = self._extract_latest_bite_text(text)
        norm_text = self._normalize(latest_text)

        # 0. Oyun içi "ne olduğu belli değil" mesajı
        if ("belli" in norm_text or "bell" in norm_text) and ("degil" in norm_text or "degl" in norm_text):
            f = "belli değil"
            return ("play" if catch_unknown else "skip", f)
        if "birsey" in norm_text and ("ama" in norm_text or "ne" in norm_text):
            f = "belli değil"
            return ("play" if catch_unknown else "skip", f)

        # 1. Kesin ve Doğrudan Semantik Çözümleyici (Fuzzy karmaşasını önler, anında eşleştirir)
        # MINIK BALIK
        if "minik" in norm_text or "mink" in norm_text:
            f = "minik balık"
            return ("play" if self.config.whitelist.get(f, False) else "skip", f)

        # DENIZ KIZI ANAHTARI (denizkızı, deniz kızı, deniz anahtar vb.)
        if "deniz" in norm_text:
            f = "deniz kızı anahtarı"
            return ("play" if self.config.whitelist.get(f, False) else "skip", f)

        # PALAMUT
        if "palamut" in norm_text:
            f = "palamut"
            return ("play" if self.config.whitelist.get(f, False) else "skip", f)

        # HAMSI
        if "hamsi" in norm_text or "hemsi" in norm_text:
            f = "hamsi"
            return ("play" if self.config.whitelist.get(f, False) else "skip", f)

        # ZARGANA
        if "zargana" in norm_text or "zargan" in norm_text:
            f = "zargana"
            return ("play" if self.config.whitelist.get(f, False) else "skip", f)

        # YABBIE YENGECİ (OCR'da 'yabble', 'yabb1e'->'yabble', 'yahbie', 'vabbie' gibi varyasyonları dahil et)
        if any(yv in norm_text for yv in ("yabb", "yabi", "yabl", "yahb", "yalb", "vabb")) or (
            ("yengec" in norm_text or "vengec" in norm_text)
            and not any(kv in norm_text for kv in ("kral", "krak", "krai"))
        ):
            f = "yabbie yengeci"
            return ("play" if self.config.whitelist.get(f, False) else "skip", f)

        # KRAL YENGEÇ vs BİLGE KRAL EŞYALARI ('kralın' OCR'da 'krakn' olarak da okunabiliyor)
        if "kral" in norm_text or "bilge" in norm_text or "krak" in norm_text:
            if "eldiven" in norm_text:
                f = "bilge kralın eldiveni"
            elif "sembol" in norm_text:
                f = "bilge kralın sembolü"
            else:
                f = "kral yengeç"
            return ("play" if self.config.whitelist.get(f, False) else "skip", f)

        # SUDAK BALIKLARI (Altın Sudak vs Büyük Sudak vs Normal Sudak)
        # KRİTİK DÜZELTME: EasyOCR Metin2 fontunda "altın sudak" yazısını çok sık
        # "akın sudak" veya "alın sudak" olarak okur. Önceden sadece "altin" arandığı için
        # Altın Sudak'ları normal "sudak" sanıp iptal ediyordu!
        if "sudak" in norm_text or "suoak" in norm_text or "sudk" in norm_text:
            pre_part = re.split(r"sudak|suoak|sudk", norm_text, maxsplit=1)[0].strip()
            pre_words = pre_part.split()
            altin_variants = {"altin", "alin", "akin", "allin", "altn", "altm", "alhn", "atin", "aitin", "alkin", "alt"}
            buyuk_variants = {"buyuk", "byuk", "buyk", "buvuk", "boyuk"}

            has_altin = any(w in altin_variants or any(av in w for av in ("altin", "alin", "akin", "altn")) for w in pre_words)
            has_buyuk = any(w in buyuk_variants or "buyuk" in w or "byuk" in w for w in pre_words)

            if not has_altin and not has_buyuk:
                for pw in pre_words:
                    if len(pw) >= 3:
                        if difflib.SequenceMatcher(None, "altin", pw).ratio() >= 0.60:
                            has_altin = True
                        elif difflib.SequenceMatcher(None, "buyuk", pw).ratio() >= 0.65:
                            has_buyuk = True

            if has_altin:
                f = "altın sudak"
            elif has_buyuk:
                f = "büyük sudak"
            else:
                f = "sudak"
            return ("play" if self.config.whitelist.get(f, False) else "skip", f)

        # SAZAN BALIKLARI (Aynalı Sazan vs Ot Sazanı vs Normal Sazan)
        if "sazan" in norm_text:
            if "aynali" in norm_text:
                f = "aynalı sazan"
            elif "ot" in norm_text:
                f = "ot sazanı"
            else:
                f = "sazan"
            return ("play" if self.config.whitelist.get(f, False) else "skip", f)

        # ALABALIKLAR (Gökkuşağı vs Nehir vs Dere)
        if "alabal" in norm_text:
            if "gokkusagi" in norm_text:
                f = "gökkuşağı alabalığı"
            elif "nehir" in norm_text:
                f = "nehir alabalığı"
            else:
                f = "dere alabalığı"
            return ("play" if self.config.whitelist.get(f, False) else "skip", f)

        # DİĞER BALIKLAR
        if "som" in norm_text or "somon" in norm_text:
            f = "som balığı"
            return ("play" if self.config.whitelist.get(f, False) else "skip", f)
        if "yayin" in norm_text:
            f = "yayın balığı"
            return ("play" if self.config.whitelist.get(f, False) else "skip", f)
        if "yilan" in norm_text:
            f = "yılan başı balığı"
            return ("play" if self.config.whitelist.get(f, False) else "skip", f)
        if "levrek" in norm_text:
            f = "levrek"
            return ("play" if self.config.whitelist.get(f, False) else "skip", f)
        if "lufer" in norm_text:
            f = "lüfer"
            return ("play" if self.config.whitelist.get(f, False) else "skip", f)
        if "tekir" in norm_text:
            f = "tekir"
            return ("play" if self.config.whitelist.get(f, False) else "skip", f)
        if "ringa" in norm_text:
            f = "ringa"
            return ("play" if self.config.whitelist.get(f, False) else "skip", f)
        if "kadife" in norm_text:
            f = "kadife"
            return ("play" if self.config.whitelist.get(f, False) else "skip", f)
        if "karides" in norm_text:
            f = "tatlı su karidesi"
            return ("play" if self.config.whitelist.get(f, False) else "skip", f)
        if "kurbaga" in norm_text:
            f = "kurbağa balığı"
            return ("play" if self.config.whitelist.get(f, False) else "skip", f)
        if "sevimli" in norm_text:
            f = "sevimli balık"
            return ("play" if self.config.whitelist.get(f, False) else "skip", f)

        # DİĞER EŞYALAR
        if "lucy" in norm_text:
            f = "lucy'nin yüzüğü"
            return ("play" if self.config.whitelist.get(f, False) else "skip", f)
        if "yuzuk" in norm_text:
            f = "altın yüzük"
            return ("play" if self.config.whitelist.get(f, False) else "skip", f)
        if "gumus" in norm_text and "anahtar" in norm_text:
            f = "gümüş anahtar"
            return ("play" if self.config.whitelist.get(f, False) else "skip", f)
        if "anahtar" in norm_text:
            f = "altın anahtar"
            return ("play" if self.config.whitelist.get(f, False) else "skip", f)
        if "boya" in norm_text:
            f = "saç boyası"
            return ("play" if self.config.whitelist.get(f, False) else "skip", f)
        if "pelerin" in norm_text:
            f = "kaçağın pelerini"
            return ("play" if self.config.whitelist.get(f, False) else "skip", f)

        # 2. Fuzzy Eşleştirme (Bilinmeyen yazılışlar için)
        MIN_THRESHOLD = 0.72
        best_keyword      = None
        best_should_catch = False
        best_ratio        = 0.0
        best_weight       = 0.0

        for keyword, should_catch in self.config.whitelist.items():
            if keyword == "belli değil":
                continue
            ratio = self._best_score(keyword, latest_text)
            if ratio >= MIN_THRESHOLD:
                weight = ratio * len(keyword)
                if weight > best_weight:
                    best_weight       = weight
                    best_ratio        = ratio
                    best_keyword      = keyword
                    best_should_catch = should_catch

        if best_keyword:
            if best_should_catch:
                log.info(f"[WHITELIST MATCH] '{best_keyword}' (oran: {best_ratio:.2f}) yakalanacak!")
                return ("play", best_keyword)
            else:
                log.info(f"[WHITELIST SKIP] '{best_keyword}' (oran: {best_ratio:.2f}) istenmiyor. İptal.")
                return ("skip", best_keyword)

        # Tanımlanamayan metin geldiğinde kullanıcının 'belli değil' ayarını uygula
        if catch_unknown:
            log.info(f"[WHITELIST UNKNOWN] Tanımlanamayan metin, 'belli değil' AÇIK -> Yakalanıyor. ({latest_text[:50]})")
            return ("play", "belli değil")
        else:
            log.warning(f"[WHITELIST UNKNOWN] Tanımlanamayan metin, 'belli değil' KAPALI -> İptal ediliyor (Skip). ({latest_text[:50]})")
            return ("skip", "belli değil")

    def check_fish_bite(self, text: str) -> str:
        decision, _ = self.identify_fish_bite(text)
        return decision

# ===========================================================================
# MULTI-THREAD MINIGAME CORE (Producer-Consumer)
# ===========================================================================
def is_minigame_open_hsv(hsv: np.ndarray) -> bool:
    """Metin2 Balıkçılık penceresinin gerçekten açık olup olmadığını doğrular.
    Sadece altın sarısı piksel saymak (alt HUD/skill bar veya envanter yüzünden) yanlış alarm üretir
    ve boşa ESC bastırıp Bilgi chatini kapatır!
    Bu yüzden HEM merkezde canlı mavi su havuzu (%14+) HEM DE sağ-üst (0/3 balık ikonu) altın pikseli
    (veya dar kesilmiş kutularda %45+ parlak mavi su havuzu) şartı aranır."""
    if hsv is None or hsv.size == 0:
        return False
    fh, fw = hsv.shape[:2]
    if fh < 40 or fw < 40:
        return False

    # 1. Merkez mavi su havuzu kontrolü:
    # Balıkçılık 2D UI penceresidir (gece/gündüz değişmez: H=98..108, S=145..245, V=210..240 -> %82-%96).
    # 3D nehir/çimen/envanter (<%1) ve kontrol paneli laciverti (V=46..96 -> <%1) V>=115 ile %100 elenir!
    center = hsv[int(fh * 0.20):int(fh * 0.80), int(fw * 0.15):int(fw * 0.85)]
    if center.size == 0:
        return False
    blue_mask = cv2.inRange(center, np.array([92, 100, 115]), np.array([122, 255, 255]))
    blue_ratio = cv2.countNonZero(blue_mask) / float(center.shape[0] * center.shape[1])
    if blue_ratio < 0.20:
        return False

    # 2. Sağ-üst '0/3' altın balık ikonu kontrolü VEYA dar su kesiminde %50+ elektrik mavisi havuz
    roi_top_right = hsv[:int(fh * 0.32), int(fw * 0.56):]
    top_gold = cv2.countNonZero(cv2.inRange(roi_top_right, np.array([10, 80, 80]), np.array([40, 255, 255])))
    if top_gold >= 10:
        return True

    return blue_ratio >= 0.50


def is_minigame_open_bgr(bgr: np.ndarray) -> bool:
    if bgr is None or bgr.size == 0:
        return False
    fh, fw = bgr.shape[:2]
    if fh < 40 or fw < 40:
        return False
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    return is_minigame_open_hsv(hsv)


class MinigameController:
    """Tek döngülü, sıfır kuyruk gecikmeli Keskin Nişancı (Sniper) Minigame Motoru.
    - İç mavi su havuzunu otomatik bulup başlık çubuğu ve siyah çerçeveleri tamamen dışlar.
    - Beyaz/pembe hedef çemberini en-küçük-kareler (least-squares) ile alt-piksel hassasiyetinde kilitler.
    - Yalnızca çember gerçekten pembemsi-kırmızıya döndüğünde (R-G alfa karışımı) VE
      balık gövdesi çemberin gerçekten içindeyken ateş eder."""

    _KERNEL_3 = np.ones((3, 3), np.uint8)
    _KERNEL_9 = np.ones((9, 9), np.uint8)

    def __init__(
        self,
        config: FisherConfig,
        chat_reader: Optional[ChatReader] = None,
        target_hwnd: Optional[int] = None,
        coordinator: Optional[InputCoordinator] = None,
        client_id: int = 1,
    ):
        self.config = config
        self.chat_reader = chat_reader
        self.target_hwnd = target_hwnd
        self.coordinator = coordinator
        self.client_id = client_id
        self._is_multi_client = bool(coordinator is not None and len(coordinator.session_hwnds) > 1)
        self.skip_fish = False
        self.fish_decision: Optional[str] = None
        self.decision_ready = threading.Event()
        self.mouse = HumanMouse()
        self.stop_event = threading.Event()
        self.hits = 0
        self.hits_lock = threading.Lock()
        self.detected_fish: Optional[str] = None

        # Doğrulama & telemetri alanları
        self.caught_verified = False
        self.last_timer_ratio = 0.0
        self.peak_timer_ratio = 0.0
        self.last_click_time = 0.0
        self.close_time = 0.0
        self.pre_chat_text: str = ""
        self.glitched_escaped: bool = False

        # Geometrik önbellek (ilk 3 karede kilitlenir -> kare başı ~0.5ms işleme)
        self._geom_samples: List[Tuple[int, int, int, int, float, float, float]] = []
        self._locked_pool: Optional[Tuple[int, int, int, int]] = None
        self._locked_ring: Optional[Tuple[float, float, float]] = None
        self._ann_mask: Optional[np.ndarray] = None
        self._ann_count: int = 1
        self._fish_zone: Optional[np.ndarray] = None
        self._wb_med: float = 216.0
        self._wg_med: float = 148.0
        self._frame_idx: int = 0

    def _is_game_focused(self) -> bool:
        return is_game_window_foreground(self.target_hwnd, self.config.game_window_title)

    def _is_minigame_closed(self, hsv: np.ndarray) -> bool:
        """Mavi su havuzu + üst ikon kontrolüyle minigame'in kapanıp kapanmadığını hatasız doğrular."""
        return not is_minigame_open_hsv(hsv)

    def _close_minigame_window(self):
        """Minigame penceresini sadece oyun odaktaysa ve minigame gerçekten açıksa ESC tuşuyla kapatır."""
        if not self._is_game_focused():
            log.warning("[MINIGAME] Oyun odakta değil (Alt-Tab), ESC gönderilmedi!")
            return
        HumanKeyboard.press(self.config.fishing_close_key)

    @classmethod
    def _locate_water_pool(cls, hsv: np.ndarray) -> Optional[Tuple[int, int, int, int]]:
        """Kullanıcı ROI'yi nasıl seçmiş olursa olsun ('Balıkçılık' başlığı veya dış dünya dahil)
        sadece içteki parlak mavi su havuzunun (px, py, pw, ph) dikdörtgenini bulur."""
        wm = cv2.inRange(hsv, np.array([92, 95, 105]), np.array([120, 255, 255]))
        wm = cv2.morphologyEx(wm, cv2.MORPH_CLOSE, cls._KERNEL_9)
        cnts, _ = cv2.findContours(wm, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not cnts:
            return None
        c = max(cnts, key=cv2.contourArea)
        if cv2.contourArea(c) < 2500:
            return None
        px, py, pw, ph = cv2.boundingRect(c)
        if pw < 100 or ph < 90:
            return None
        return (px, py, pw, ph)

    @staticmethod
    def _measure_timer_ratio(phsv: np.ndarray) -> float:
        """Su havuzunun altındaki sarı/kırmızı süre barının doluluk oranını (0.0 - 1.0) ölçer."""
        ph, pw = phsv.shape[:2]
        bar = phsv[int(ph * 0.84):int(ph * 0.98), int(pw * 0.14):int(pw * 0.82)]
        if bar.size == 0:
            return 0.0
        bh, bw = bar.shape[:2]
        if bh < 3 or bw < 20:
            return 0.0
        active = (
            ((bar[:, :, 0] <= 38) | (bar[:, :, 0] >= 168))
            & (bar[:, :, 1] >= 115)
            & (bar[:, :, 2] >= 135)
        )
        col_ok = np.count_nonzero(active, axis=0) >= max(2, bh // 5)
        return float(np.count_nonzero(col_ok)) / float(max(1, bw))

    @staticmethod
    def _build_clean_zone(phsv: np.ndarray, px: int, py: int, sw: int, sh: int) -> np.ndarray:
        """İç dekoratif çerçeve çizgisini, sağ üst 0/3 sayacını, sol alt saati ve süre barını maskeler."""
        ph, pw = phsv.shape[:2]
        touch_l = px <= 2
        touch_r = (px + pw) >= (sw - 2)
        touch_t = py <= 2

        ml = 10 if (touch_l and pw < 330) else 20
        mr = 10 if (touch_r and pw < 330) else 20
        mt = 10 if (touch_t and ph < 260) else 20
        mb = 38 if ph >= 265 else 12

        z = np.zeros((ph, pw), dtype=np.uint8)
        if ph - mb > mt + 20 and pw - mr > ml + 20:
            z[mt:ph - mb, ml:pw - mr] = 255
        else:
            z[6:max(7, ph - 6), 6:max(7, pw - 6)] = 255

        if not (touch_t and ph < 230):
            z[:min(54, int(ph * 0.24)), int(pw * 0.54):] = 0
        if ph >= 250:
            z[max(0, ph - 58):, :int(pw * 0.30)] = 0
        else:
            z[max(0, ph - 22):, :int(pw * 0.24)] = 0

        gold_or_bar = (phsv[:, :, 0] <= 42) & (phsv[:, :, 1] >= 100)
        z[gold_or_bar] = 0
        return z

    @staticmethod
    def _fit_target_ring(pool: np.ndarray, phsv: np.ndarray, z: np.ndarray) -> Optional[Tuple[float, float, float]]:
        """Beyaz veya pembe-kırmızı çember piksellerine cebirsel en-küçük-kareler çemberi oturtur."""
        ph, pw = pool.shape[:2]
        rmg = pool[:, :, 2].astype(np.int16) - pool[:, :, 1].astype(np.int16)
        stroke = (
            (((phsv[:, :, 2] >= 208) & (phsv[:, :, 1] <= 85))
             | ((rmg >= 20) & (pool[:, :, 2] >= 165)))
            & (z > 0)
        )
        ry, rx = np.where(stroke)
        if len(rx) < 70:
            return None

        rx_f = rx.astype(np.float64)
        ry_f = ry.astype(np.float64)
        A = np.column_stack([2.0 * rx_f, 2.0 * ry_f, np.ones_like(rx_f)])
        b = rx_f * rx_f + ry_f * ry_f
        try:
            sol, _, _, _ = np.linalg.lstsq(A, b, rcond=None)
        except Exception:
            return None
        cx, cy = float(sol[0]), float(sol[1])
        r_sq = sol[2] + cx * cx + cy * cy
        if r_sq <= 100.0:
            return None
        r = float(math.sqrt(r_sq))

        # 2. Aşama: ±5px çember çevresi dışındaki tüm su parlamalarını atıp yeniden oturt
        d = np.hypot(rx_f - cx, ry_f - cy)
        inl = np.abs(d - r) <= 5.0
        if np.count_nonzero(inl) >= 60:
            rx2, ry2 = rx_f[inl], ry_f[inl]
            A2 = np.column_stack([2.0 * rx2, 2.0 * ry2, np.ones_like(rx2)])
            b2 = rx2 * rx2 + ry2 * ry2
            try:
                sol2, _, _, _ = np.linalg.lstsq(A2, b2, rcond=None)
                cx2, cy2 = float(sol2[0]), float(sol2[1])
                r2_sq = sol2[2] + cx2 * cx2 + cy2 * cy2
                if r2_sq > 100.0:
                    cx, cy, r = cx2, cy2, float(math.sqrt(r2_sq))
            except Exception:
                pass

        if not (26.0 <= r <= min(pw, ph) * 0.49):
            return None
        if not (pw * 0.15 <= cx <= pw * 0.85 and ph * 0.12 <= cy <= ph * 0.88):
            return None
        return (cx, cy, r)

    def analyze_frame(self, bgr: np.ndarray, hsv: Optional[np.ndarray] = None) -> dict:
        """Tek bir minigame karesini analiz eder:
        open, is_pink, pink_px, pink_ratio, fish_roi_pos, fish_pool_pos, ring, fish_dist, in_circle, should_click."""
        if bgr is None or bgr.size == 0:
            return {"open": False, "should_click": False}
        if hsv is None:
            hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)

        if self._is_minigame_closed(hsv):
            return {"open": False, "should_click": False}

        sh, sw = bgr.shape[:2]
        self._frame_idx += 1

        if self._locked_pool is not None and self._locked_ring is not None:
            px, py, pw, ph = self._locked_pool
            rcx, rcy, rr = self._locked_ring
            pool = bgr[py:py + ph, px:px + pw]
            phsv = hsv[py:py + ph, px:px + pw]
            ann_mask = self._ann_mask
            ann_count = self._ann_count
            fish_zone = self._fish_zone
        else:
            pool_rect = self._locate_water_pool(hsv)
            if pool_rect is None:
                return {"open": False, "should_click": False}
            px, py, pw, ph = pool_rect
            pool = bgr[py:py + ph, px:px + pw]
            phsv = hsv[py:py + ph, px:px + pw]
            z = self._build_clean_zone(phsv, px, py, sw, sh)
            ring = self._fit_target_ring(pool, phsv, z)
            if ring is not None:
                rcx, rcy, rr = ring
                self._geom_samples.append((px, py, pw, ph, rcx, rcy, rr))
                if len(self._geom_samples) >= 3:
                    arr = np.array(self._geom_samples, dtype=np.float64)
                    med = np.median(arr, axis=0)
                    lpx, lpy, lpw, lph = int(round(med[0])), int(round(med[1])), int(round(med[2])), int(round(med[3]))
                    lpx = max(0, min(sw - 40, lpx))
                    lpy = max(0, min(sh - 40, lpy))
                    lpw = max(40, min(sw - lpx, lpw))
                    lph = max(40, min(sh - lpy, lph))
                    lrcx, lrcy, lrr = float(med[4]), float(med[5]), float(med[6])
                    ph_l = hsv[lpy:lpy + lph, lpx:lpx + lpw]
                    lz = self._build_clean_zone(ph_l, lpx, lpy, sw, sh)
                    yy_l, xx_l = np.ogrid[:lph, :lpw]
                    dist_l = np.hypot(xx_l - lrcx, yy_l - lrcy)
                    self._locked_pool = (lpx, lpy, lpw, lph)
                    self._locked_ring = (lrcx, lrcy, lrr)
                    ann_half = max(4.0, min(5.8, lrr * 0.085))
                    self._ann_mask = (dist_l >= lrr - ann_half) & (dist_l <= lrr + ann_half) & (lz > 0)
                    self._ann_count = max(1, int(np.count_nonzero(self._ann_mask)))
                    self._fish_zone = (dist_l <= lrr * 1.34) & (lz > 0)
                    pool_l = bgr[lpy:lpy + lph, lpx:lpx + lpw]
                    if np.any(self._fish_zone):
                        self._wb_med = float(np.median(pool_l[:, :, 0][self._fish_zone]))
                        self._wg_med = float(np.median(pool_l[:, :, 1][self._fish_zone]))
            else:
                rcx, rcy, rr = pw * 0.50, ph * 0.50, pw * 0.231

            yy, xx = np.ogrid[:ph, :pw]
            dist = np.hypot(xx - rcx, yy - rcy)
            ann_half = max(4.0, min(5.8, rr * 0.085))
            ann_mask = (dist >= rr - ann_half) & (dist <= rr + ann_half) & (z > 0)
            ann_count = max(1, int(np.count_nonzero(ann_mask)))
            fish_zone = (dist <= rr * 1.34) & (z > 0)

        # Süre barı takibi
        tr = self._measure_timer_ratio(phsv)
        self.last_timer_ratio = tr
        if tr > self.peak_timer_ratio:
            self.peak_timer_ratio = tr

        # 1. PEMBE / KIRMIZI ÇEMBER KONTROLÜ (Mavi su üstü alfa karışımı: R - G >= 18 & R >= 160)
        pool_b = pool[:, :, 0]
        pool_g = pool[:, :, 1]
        pool_r = pool[:, :, 2]

        ann_r = pool_r[ann_mask]
        ann_rmg = ann_r.astype(np.int16) - pool_g[ann_mask].astype(np.int16)
        pink_px = int(np.count_nonzero((ann_rmg >= 18) & (ann_r >= 160)))
        pink_ratio = pink_px / float(ann_count)
        min_pink_px = max(45, int(ann_count * 0.025))
        is_pink = (pink_px >= min_pink_px) and (pink_ratio >= 0.040)

        # 2. ADAPTİF BGR BALIK GÖVDESİ TESPİTİ (Çemberin içi + kenarına teğet geçen %134 bölge!)
        if (self._frame_idx <= 3 or self._frame_idx % 15 == 0) and np.any(fish_zone):
            self._wb_med = float(np.median(pool_b[fish_zone]))
            self._wg_med = float(np.median(pool_g[fish_zone]))

        b_lim = min(176.0, self._wb_med - 36.0)
        g_lim = min(126.0, self._wg_med - 26.0)
        r_lim = min(110.0, self._wb_med * 0.52)

        fmask = ((pool_b < b_lim) & (pool_g < g_lim) & (pool_r < r_lim) & fish_zone).astype(np.uint8) * 255
        fmask = cv2.morphologyEx(fmask, cv2.MORPH_OPEN, self._KERNEL_3)
        fmask = cv2.dilate(fmask, self._KERNEL_3, iterations=1)
        fcnts, _ = cv2.findContours(fmask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        fish_pool_pos: Optional[Tuple[float, float]] = None
        fish_roi_pos: Optional[Tuple[float, float]] = None
        fish_box: Optional[Tuple[int, int, int, int]] = None
        fish_dist = 999.0
        in_circle = False

        if fcnts:
            min_fish_area = max(9, int(round(pw * ph * 0.00024)))
            valid_f = [fc for fc in fcnts if min_fish_area <= cv2.contourArea(fc) <= 2600]
            if valid_f:
                best = min(
                    valid_f,
                    key=lambda fc: math.hypot(
                        (cv2.boundingRect(fc)[0] + cv2.boundingRect(fc)[2] * 0.5) - rcx,
                        (cv2.boundingRect(fc)[1] + cv2.boundingRect(fc)[3] * 0.5) - rcy,
                    ),
                )
                fx, fy, fw2, fh2 = cv2.boundingRect(best)
                fcx = fx + fw2 * 0.5
                fcy = fy + fh2 * 0.5
                fish_pool_pos = (fcx, fcy)
                fish_roi_pos = (px + fcx, py + fcy)
                fish_box = (fx, fy, fw2, fh2)
                fish_dist = math.hypot(fcx - rcx, fcy - rcy)
                # Metin2 kuralı: Çember pembe-kırmızı yandığı sürece balık çembere ucundan/teğet değse bile
                # (merkezi ~1.26*r'ye kadar dışarıda olsa dahi) gövde-çember kesişimine tıklamak HIT sayılır!
                in_circle = fish_dist <= (rr * 1.26)

        return {
            "open": True,
            "pool": (px, py, pw, ph),
            "ring": (rcx, rcy, rr),
            "is_pink": is_pink,
            "pink_px": pink_px,
            "pink_ratio": pink_ratio,
            "fish_pool_pos": fish_pool_pos,
            "fish_roi_pos": fish_roi_pos,
            "fish_box": fish_box,
            "fish_dist": fish_dist,
            "in_circle": in_circle,
            "should_click": bool(is_pink and in_circle and fish_pool_pos is not None),
        }

    def sniper_loop(self):
        """Sıfır kuyruk gecikmeli, kilit-dostu gerçek-zamanlı keskin nişancı (Sniper) döngüsü.
        - Tarama ve hız hesabı %100 DONANIM KİLİTSİZ (paralel) çalışır.
        - Kilit yalnızca atış anında (URGENT_MINIGAME_HIT) alınır, pencere odaklanır, balık doğrudan
          tıklanır ve vuruş sonrası bekleme boyunca kilit DERHAL serbest bırakılır.
        - Çıkışta thread_sct_scope() ile bu geçici thread'e ait tüm GDI handle'ları kapatılır."""
        with thread_sct_scope():
            self._sniper_loop_impl()

    def _sniper_loop_impl(self):
        log.info(f"[C{self.client_id}][SNIPER] Keskin nişancı döngüsü başlatıldı.")
        monitor = self.config.minigame_roi
        wait_decision_start = time.time()
        last_click = 0.0
        click_cooldown = _jitter(0.39, 0.015, 0.36, 0.42)

        prev_fpos: Optional[Tuple[float, float]] = None
        prev_ftime = 0.0
        vx, vy = 0.0, 0.0
        closed_streak = 0
        grab_err_streak = 0
        last_grab_warn_t = 0.0

        while not self.stop_event.is_set():
            loop_t0 = time.perf_counter()
            try:
                bgr = thread_grab_bgr(monitor)
                grab_err_streak = 0
            except ScreenShotError as e:
                grab_err_streak += 1
                now_err = time.monotonic()
                if now_err - last_grab_warn_t >= 2.0:
                    last_grab_warn_t = now_err
                    log.warning(f"[C{self.client_id}][SNIPER] Ekran okuma hatası ({grab_err_streak}): {e}")
                if grab_err_streak >= 25:
                    log.error(f"[C{self.client_id}][SNIPER] Ardışık ekran okuma hatası limiti aşıldı, döngü durduruluyor.")
                    self.stop_event.set()
                    break
                time.sleep(0.05)
                continue
            hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)

            info = self.analyze_frame(bgr, hsv)
            if not info["open"]:
                closed_streak += 1
                if closed_streak >= 2:
                    self.close_time = time.time()
                    log.info(f"[C{self.client_id}][SNIPER] Minigame penceresi kapandı.")
                    self.stop_event.set()
                    break
                time.sleep(0.004)
                continue
            closed_streak = 0

            # Anlık balık hız vektörünü güncelle (vuruş sonrası ışınlanmalarda hızı sıfırla)
            now = time.time()
            fpos = info["fish_pool_pos"]
            if fpos is not None:
                if prev_fpos is not None:
                    dt = now - prev_ftime
                    dx = fpos[0] - prev_fpos[0]
                    dy = fpos[1] - prev_fpos[1]
                    if 0.002 < dt < 0.09 and math.hypot(dx, dy) <= 32.0:
                        vx = 0.55 * vx + 0.45 * (dx / dt)
                        vy = 0.55 * vy + 0.45 * (dy / dt)
                    else:
                        vx, vy = 0.0, 0.0
                prev_fpos = fpos
                prev_ftime = now
            else:
                if now - prev_ftime > 0.08:
                    prev_fpos = None
                    vx, vy = 0.0, 0.0

            # OCR karar kontrolü (ilk ~150ms'de çember/balık zaten kilitlenmiş olur)
            if not self.decision_ready.is_set():
                if now - wait_decision_start > 0.90:
                    catch_unknown = self.config.whitelist.get("belli değil", False)
                    if catch_unknown:
                        log.warning(f"[C{self.client_id}][SNIPER] OCR zaman aşımı, 'belli değil' AÇIK -> Atış serbest.")
                        self.fish_decision = "play"
                    else:
                        log.warning(f"[C{self.client_id}][SNIPER] OCR zaman aşımı, 'belli değil' KAPALI -> Minigame geçiliyor.")
                        self.fish_decision = "skip"
                        self.skip_fish = True
                        self.stop_event.set()
                        break
                    self.decision_ready.set()
                else:
                    time.sleep(0.003)
                    continue

            if self.skip_fish or self.fish_decision == "skip":
                break

            # ÇİFT KİLİT: Çember pembe-kırmızı yanmalı VE balık çemberin içinde / kenarında olmalı!
            if info["should_click"] and (now - last_click >= click_cooldown):
                if self.coordinator is not None:
                    if not self.coordinator.is_user_in_game():
                        log.warning(f"[C{self.client_id}][SNIPER] Oyun odakta değil (Alt-Tab), tıklama iptal edildi.")
                        self.stop_event.set()
                        break

                    acquired = self.coordinator.acquire(
                        priority=InputPriority.URGENT_MINIGAME_HIT,
                        client_id=self.client_id,
                        stop_check=lambda: not self.stop_event.is_set(),
                    )
                    if not acquired:
                        continue

                    try:
                        if self.stop_event.is_set():
                            break
                        if not self.coordinator.is_user_in_game():
                            log.warning(f"[C{self.client_id}][SNIPER] Sıra beklerken Alt-Tab algılandı, iptal.")
                            self.stop_event.set()
                            break

                        was_fg = is_game_window_foreground(self.target_hwnd, self.config.game_window_title)

                        if was_fg:
                            # HIZLI YOL: Pencere zaten odaktaysa (tek istemci veya bu penceredeki seri vuruşlar)
                            # KESİNLİKLE ikinci kez kare çekme veya analiz yapma! info zaten ~0.5ms önce alındı.
                            px, py, pw, ph = info["pool"]
                            rcx, rcy, rr = info["ring"]
                            fcx, fcy = fpos
                            fbox = info["fish_box"] or (int(fcx) - 10, int(fcy) - 6, 20, 12)
                            _, _, fw2, fh2 = fbox
                            hit_info = info
                            use_vx, use_vy = vx, vy
                        else:
                            # Pencere geçişi: Önceki pencereden geçiş koordinatını ön-sürüklemeye ver
                            qroi = info.get("fish_roi_pos")
                            pre_move = (
                                int(round(monitor["left"] + qroi[0])),
                                int(round(monitor["top"] + qroi[1])),
                            ) if qroi else None

                            if not self.coordinator.focus_client(
                                self.target_hwnd,
                                urgent=True,
                                pre_move_to=pre_move,
                            ):
                                continue

                            # Pencere değiştikten sonra balığın yerini taze kareyle doğrula
                            try:
                                fbgr = thread_grab_bgr(monitor)
                            except ScreenShotError as e:
                                log.warning(f"[C{self.client_id}][SNIPER] Geçiş sonrası kare okunamadı: {e}")
                                prev_fpos = None
                                vx, vy = 0.0, 0.0
                                continue
                            f_time = time.time()
                            fresh = self.analyze_frame(fbgr)
                            if not fresh["open"]:
                                self.close_time = f_time
                                log.info(f"[C{self.client_id}][SNIPER] Minigame geçiş sonrası kapandı.")
                                self.stop_event.set()
                                break

                            _, _, frr = fresh["ring"]
                            if not (
                                fresh["is_pink"]
                                and fresh["fish_pool_pos"] is not None
                                and fresh["fish_dist"] <= frr * 1.26
                            ):
                                prev_fpos = fresh["fish_pool_pos"]
                                prev_ftime = f_time
                                vx, vy = 0.0, 0.0
                                continue

                            px, py, pw, ph = fresh["pool"]
                            rcx, rcy, rr = fresh["ring"]
                            fcx, fcy = fresh["fish_pool_pos"]
                            fbox = fresh["fish_box"] or (int(fcx) - 10, int(fcy) - 6, 20, 12)
                            _, _, fw2, fh2 = fbox
                            hit_info = fresh

                            # Geçiş sonrası hız tahmini
                            use_vx, use_vy = 0.0, 0.0
                            if prev_fpos is not None:
                                dt_pf = f_time - prev_ftime
                                dp_x = fcx - prev_fpos[0]
                                dp_y = fcy - prev_fpos[1]
                                if 0.012 < dt_pf < 0.16 and math.hypot(dp_x, dp_y) <= 35.0:
                                    use_vx = dp_x / dt_pf
                                    use_vy = dp_y / dt_pf

                        # KANITLANMIŞ ÖNLEME FORMÜLÜ (ASLA çember içine çekilmez, doğrudan balık gövdesine kilitlenir!):
                        lead_t = 0.012
                        lx = use_vx * lead_t
                        ly = use_vy * lead_t
                        max_lead = max(4.5, min(7.5, rr * 0.105))
                        lmag = math.hypot(lx, ly)
                        if lmag > max_lead:
                            scale = max_lead / lmag
                            lx *= scale
                            ly *= scale
                        max_dx = max(3.0, fw2 * 0.30)
                        max_dy = max(2.5, fh2 * 0.30)
                        lx = max(-max_dx, min(max_dx, lx))
                        ly = max(-max_dy, min(max_dy, ly))

                        tx_p = max(10.0, min(float(pw - 10), fcx + lx))
                        ty_p = max(10.0, min(float(ph - 10), fcy + ly))

                        tgt_x = int(round(px + tx_p))
                        tgt_y = int(round(py + ty_p))
                        screen_x = monitor["left"] + tgt_x
                        screen_y = monitor["top"] + tgt_y

                        self.mouse.sniper_click(screen_x, screen_y)
                        click_done = time.time()
                    finally:
                        # Balığın tekrar dönüşünü beklerken MUTEX DERHAL serbest kalır!
                        self.coordinator.release()
                else:
                    if not self._is_game_focused():
                        log.warning(f"[C{self.client_id}][SNIPER] Oyun odakta değil (Alt-Tab), tıklama iptal edildi.")
                        self.stop_event.set()
                        break

                    px, py, pw, ph = info["pool"]
                    rcx, rcy, rr = info["ring"]
                    fcx, fcy = fpos
                    fbox = info["fish_box"] or (int(fcx) - 10, int(fcy) - 6, 20, 12)
                    _, _, fw2, fh2 = fbox
                    hit_info = info

                    lx = vx * 0.012
                    ly = vy * 0.012
                    max_lead = max(4.5, min(7.5, rr * 0.105))
                    lmag = math.hypot(lx, ly)
                    if lmag > max_lead:
                        scale = max_lead / lmag
                        lx *= scale
                        ly *= scale
                    max_dx = max(3.0, fw2 * 0.30)
                    max_dy = max(2.5, fh2 * 0.30)
                    lx = max(-max_dx, min(max_dx, lx))
                    ly = max(-max_dy, min(max_dy, ly))

                    tx_p = max(10.0, min(float(pw - 10), fcx + lx))
                    ty_p = max(10.0, min(float(ph - 10), fcy + ly))
                    tgt_x = int(round(px + tx_p))
                    tgt_y = int(round(py + ty_p))
                    screen_x = monitor["left"] + tgt_x
                    screen_y = monitor["top"] + tgt_y

                    self.mouse.sniper_click(screen_x, screen_y)
                    click_done = time.time()

                last_click = click_done
                self.last_click_time = click_done
                click_cooldown = _jitter(0.39, 0.015, 0.36, 0.42)
                prev_fpos = None
                vx, vy = 0.0, 0.0

                with self.hits_lock:
                    self.hits += 1
                    hit_no = self.hits

                log.info(
                    f"[C{self.client_id}][SNIPER] 🎯 Vuruş #{hit_no} -> ROI({tgt_x},{tgt_y}) | "
                    f"Mesafe:{hit_info['fish_dist']:.0f}/{rr:.0f}px | Pembe:%{hit_info['pink_ratio']*100:.1f}"
                )
                continue

            elapsed = time.perf_counter() - loop_t0
            if elapsed < 0.004:
                time.sleep(0.004 - elapsed)

    def thread4_chat_reader(self):
        """Paralel OCR Thread: Minigame açılır açılmaz sohbeti okur ve balık kararını verir."""
        if not self.chat_reader:
            self.fish_decision = "play"
            self.decision_ready.set()
            return

        try:
            log.info("[OCR] Minigame açıldı, balık türü okunuyor...")

            current_text = ""
            time.sleep(0.04)
            for _ in range(2):
                if self.stop_event.is_set():
                    return
                current_text = self.chat_reader.read_chat(roi_override=self.config.chat_roi, urgent=True)
                if current_text.strip():
                    break
                time.sleep(0.04)

            self.pre_chat_text = current_text
            flat_text = current_text.replace(chr(10), " ")
            log.info(f"[OCR] Okunan Chat: '{flat_text}'")
            if re.search(r"\b[012]\s*/\s*3\b", flat_text):
                log.warning(
                    "⚠️ [UYARI] Sohbet yerine '0/3' okundu! Balık oyun penceresi bilgi chatinin üstünü kapatıyor. "
                    "Lütfen bu istemcide balık oyun penceresini biraz aşağı kaydırıp panelden '🎯 Oyun Seç' ile güncelleyin!"
                )
            decision, fish_name = self.chat_reader.identify_fish_bite(current_text)
            self.detected_fish = fish_name
            self.fish_decision = decision
            self.decision_ready.set()

            if decision == "skip":
                log.info(f"[OCR] 🛑 İSTENMEYEN BALIK/EŞYA ({fish_name})! Minigame kapatılıyor.")
                self.skip_fish = True
                self.stop_event.set()
            else:
                log.info(f"[OCR] ✅ WHITELIST BALIK ({fish_name}) -> Yakalanıyor!")
        except Exception as e:
            log.error(f"[OCR] Chat okuma hatası: {e}", exc_info=True)
            self.fish_decision = "play"
            self.decision_ready.set()
        finally:
            close_thread_sct()

    def _safe_is_minigame_closed(self) -> Optional[bool]:
        try:
            bgr = thread_grab_bgr(self.config.minigame_roi)
        except ScreenShotError as e:
            log.warning(f"[C{self.client_id}][MINIGAME] Kapanış kontrolünde ekran okunamadı: {e}")
            return None
        hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
        return self._is_minigame_closed(hsv)

    def play(self):
        """Paralel OCR + Tek Döngülü Sniper motorunu çalıştırır ve gerçek yakalama durumunu doğrular."""
        self.stop_event.clear()
        self.decision_ready.clear()
        self.fish_decision = None
        self.hits = 0
        self.skip_fish = False
        self.caught_verified = False
        self.glitched_escaped = False
        self.pre_chat_text = ""
        self.last_timer_ratio = 0.0
        self.peak_timer_ratio = 0.0
        self.last_click_time = 0.0
        self.close_time = 0.0
        self._geom_samples.clear()
        self._locked_pool = None
        self._locked_ring = None
        self._ann_mask = None
        self._fish_zone = None
        self._frame_idx = 0

        play_start = time.time()
        t_sniper = threading.Thread(target=self.sniper_loop, daemon=True)
        t_ocr = threading.Thread(target=self.thread4_chat_reader, daemon=True)

        t_sniper.start()
        t_ocr.start()

        self.stop_event.wait(timeout=16.0)
        timed_out = False
        if not self.stop_event.is_set():
            timed_out = True
            log.warning("[MINIGAME] Zaman aşımı (16sn)! Döngü durduruluyor.")
            self.stop_event.set()

        # İstenmeyen balıksa pencereyi donanım kilidi (HIGH_ANIM_CANCEL) altında ESC ile kapat;
        # kapanış doğrulama beklemesini (polling) kilidin DIŞINDA yaparak diğer istemcileri bloklama.
        if self.skip_fish:
            if self.coordinator is not None:
                sent_esc = False
                got_lock = (
                    self.coordinator.is_user_in_game()
                    and self.coordinator.acquire(
                        priority=InputPriority.HIGH_ANIM_CANCEL,
                        client_id=self.client_id,
                    )
                )
                if got_lock:
                    try:
                        if self.coordinator.focus_client(self.target_hwnd):
                            if self._safe_is_minigame_closed() is False:
                                log.info(f"[C{self.client_id}][MINIGAME] İstenmeyen balık: ESC tuşuyla kapatılıyor...")
                                self._close_minigame_window()
                                time.sleep(_jitter(0.022, 0.004, 0.015, 0.032))
                                sent_esc = True
                    finally:
                        self.coordinator.release()

                if sent_esc:
                    close_start = time.time()
                    closed_ok = False
                    while time.time() - close_start < 0.45:
                        time.sleep(0.035)
                        if self._safe_is_minigame_closed() is True:
                            log.info(f"[C{self.client_id}][MINIGAME] Minigame penceresi ESC ile kapandı.")
                            closed_ok = True
                            break
                    if not closed_ok and self.coordinator.is_user_in_game():
                        got_retry = self.coordinator.acquire(
                            priority=InputPriority.HIGH_ANIM_CANCEL,
                            client_id=self.client_id,
                            timeout=2.0,
                        )
                        if got_retry:
                            try:
                                if self.coordinator.focus_client(self.target_hwnd) and self._safe_is_minigame_closed() is False:
                                    log.warning(f"[C{self.client_id}][MINIGAME] İlk ESC yetmedi, tekrar ESC deneniyor...")
                                    self._close_minigame_window()
                                    time.sleep(0.04)
                            finally:
                                self.coordinator.release()
            elif self._is_game_focused():
                if self._safe_is_minigame_closed() is False:
                    log.info("[MINIGAME] İstenmeyen balık: ESC tuşuyla kapatılıyor...")
                    self._close_minigame_window()
                    close_start = time.time()
                    while time.time() - close_start < 0.45:
                        time.sleep(0.035)
                        if self._safe_is_minigame_closed() is True:
                            log.info("[MINIGAME] Minigame penceresi ESC ile başarıyla kapandı.")
                            break
                    else:
                        if self._is_game_focused() and self._safe_is_minigame_closed() is False:
                            log.warning("[MINIGAME] Pencere ilk ESC'de kapanmadı, tekrar ESC deneniyor...")
                            self._close_minigame_window()
                            time.sleep(0.04)
            else:
                time.sleep(0.04)

        self.stop_event.set()
        t_sniper.join(timeout=0.50)
        t_ocr.join(timeout=0.50)

        # MİNİGAME ÖN DOĞRULAMASI (Kesin sonuç state_anim_cancel sonunda 'Yemi kaybettin / tuttun!' OCR ile netleşir):
        # 3. başarılı vuruşta minigame penceresi vuruştan hemen sonra (<0.42sn) ve sarı süre barı bitmeden kapanır.
        close_t = self.close_time if self.close_time > 0 else time.time()
        since_last_click = (close_t - self.last_click_time) if self.last_click_time > 0 else 999.0
        mg_elapsed = close_t - play_start
        closed_on_hit = (self.hits >= 3) and (since_last_click <= 0.42)
        if self.peak_timer_ratio >= 0.25:
            # Sarı bar görünüyorsa bar gerçekten bitmemiş olmalı (>%2.5); bar %0 ise süre dolup kapanmıştır!
            timer_alive = self.last_timer_ratio > 0.025
        else:
            timer_alive = (since_last_click <= 0.30 and mg_elapsed < 11.8) or (mg_elapsed < 8.0)

        self.caught_verified = (not self.skip_fish) and (not self.glitched_escaped) and (not timed_out) and closed_on_hit and timer_alive
        log.info(
            f"[MINIGAME] Sonlandırıldı. Vuruş: {self.hits} | "
            f"Sonuç: {'3/3 TAMAM (Sohbet kontrol ediliyor...)' if self.caught_verified else ('GEÇİLDİ' if self.skip_fish else 'SÜRE DOLDU / KAÇTI')}"
        )


# ===========================================================================
# BÖLÜM 6: DURUM MAKİNESİ & ÇOKLU İSTEMCİ (1-4 CLIENT) YÖNETİCİSİ
# ===========================================================================
SESSION_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "oturum.json")
_SESSION_KEYS = ("total_casts", "hits", "misses", "valuable_fish", "worthless_fish",
                 "caught_count", "escaped_count", "caught_fish")


def load_session() -> dict:
    """Önceki oturumdan kalan sayaçları (süre dahil) döndürür. Dosya yoksa boş sözlük."""
    try:
        with open(SESSION_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def reset_session() -> None:
    try:
        os.remove(SESSION_FILE)
    except OSError:
        pass


def compute_earnings_from_counts(caught_fish: dict, fiyatlar: dict) -> Tuple[int, float]:
    """fiyatlar.json verisi üzerinden yakalanan balıkların Yang ve Won karşılığını hesaplar."""
    total_yang = 0
    for fish_name, count in (caught_fish or {}).items():
        fn = fish_name.lower().strip()
        item_info = fiyatlar.get(fn, {})
        if not item_info:
            for k, v in fiyatlar.items():
                if k in fn or fn in k:
                    item_info = v
                    break
        birim_yang = item_info.get("birim_yang", 0) if isinstance(item_info, dict) else int(item_info or 0)
        total_yang += int(count) * int(birim_yang)
    return total_yang, round(total_yang / 100_000_000, 3)


def _count_minigame_gold_pixels(bgr: np.ndarray) -> int:
    """Görüntüdeki sağ-üst (saat/skor) ve sol-alt (balık) altın sarısı minigame UI ikon piksellerini sayar."""
    if bgr is None or bgr.size == 0:
        return 0
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    fh, fw = hsv.shape[:2]
    if fh < 20 or fw < 20:
        return 0
    gold_lower = np.array([10, 80, 80])
    gold_upper = np.array([40, 255, 255])
    roi_top_right = hsv[:int(fh * 0.25), int(fw * 0.65):]
    roi_bottom_left = hsv[int(fh * 0.75):, :int(fw * 0.35)]
    gold_pixels = cv2.countNonZero(cv2.inRange(roi_top_right, gold_lower, gold_upper))
    gold_pixels += cv2.countNonZero(cv2.inRange(roi_bottom_left, gold_lower, gold_upper))
    return int(gold_pixels)


class FishingHelper:
    def __init__(
        self,
        config: Optional[FisherConfig] = None,
        client_id: int = 1,
        target_hwnd: Optional[int] = None,
        coordinator: Optional[InputCoordinator] = None,
        on_stats_update: Optional[Callable[[], None]] = None,
        load_saved_session: bool = True,
    ):
        self.config = config or FisherConfig()
        self.client_id = client_id
        self._tag = f"[C{client_id}]"
        self.state = AppState.CAST
        self.running = False
        self._game_hwnd: Optional[int] = target_hwnd
        self.coordinator = coordinator
        self._on_stats_update = on_stats_update
        self.chat_reader = ChatReader(self.config, target_hwnd=target_hwnd)
        self.keyboard = HumanKeyboard()
        self._stats_lock = threading.Lock()

        saved = load_session() if load_saved_session else {}
        self.stats = {
            "total_casts": 0,
            "hits": 0,
            "misses": 0,
            "valuable_fish": 0,
            "worthless_fish": 0,
            "caught_count": 0,
            "escaped_count": 0,
            "caught_fish": {},
            "total_won": 0.0,
            "total_yang": 0,
            "session_start": time.time() - float(saved.get("active_seconds", 0)),
        }
        for k in _SESSION_KEYS:
            if k in saved:
                self.stats[k] = dict(saved[k]) if isinstance(saved[k], dict) else saved[k]

        self._farm_start_time = time.time()
        # Anti-ban: Her istemcinin çalışma ve mola süresi birbirinden farklı rastgele sapmaya sahip olur
        self._target_farm_min = self._roll_farm_minutes()
        self.current_bait_index = 0
        self.last_controller: Optional[MinigameController] = None
        self._cooldown_until: float = 0.0
        self._fiyat_cache: dict = {}
        self._fiyat_mtime: float = 0.0
        self._just_switched_bait = False
        self._last_save = 0.0
        self._load_saved_session = load_saved_session
        self._update_earnings()

    def _roll_farm_minutes(self) -> float:
        base = max(5.0, float(self.config.farm_duration_minutes))
        return base * random.uniform(0.88, 1.12)

    def _roll_rest_seconds(self) -> float:
        base = max(0.5, float(self.config.rest_duration_minutes)) * 60.0
        return base * random.uniform(0.80, 1.20)

    def get_chat_roi(self) -> dict:
        return resolve_window_roi(self._game_hwnd, self.config.chat_roi, self.config.rel_chat_roi)

    def get_minigame_roi(self) -> dict:
        return resolve_window_roi(self._game_hwnd, self.config.minigame_roi, self.config.rel_minigame_roi)

    def _is_user_in_game(self) -> bool:
        if self.coordinator is not None:
            return self.coordinator.is_user_in_game()
        return is_game_window_foreground(self._game_hwnd, self.config.game_window_title)

    def _wait_until_game_focused(self) -> bool:
        """Kullanıcı Alt-Tab attıysa (tarayıcı, Discord, panel vb.) hiçbir tuşa/ESC'ye basmadan
        herhangi bir aktif Metin2 penceresi tekrar ön plana gelene kadar güvenli şekilde bekler."""
        paused_logged = False
        while self.running:
            if self._is_user_in_game():
                if paused_logged:
                    log.info(f"▶️ {self._tag} Oyun tekrar ön planda! Devam ediliyor...")
                    time.sleep(0.25)
                return True
            if not paused_logged:
                log.warning(f"⏸️ {self._tag} Oyun penceresi ön planda değil (Alt-Tab). Tuş gönderimi duraklatıldı...")
                paused_logged = True
            time.sleep(0.35)
        return False

    def _acquire_input(self, urgent: bool = False, priority: Optional[InputPriority] = None) -> bool:
        """Fare/klavye kilidini öncelik sırasıyla alır ve bu istemcinin oyun penceresini öne getirir."""
        if not self._wait_until_game_focused():
            return False
        if self.coordinator is None:
            return is_game_window_foreground(self._game_hwnd, self.config.game_window_title)

        prio = priority if priority is not None else (
            InputPriority.URGENT_MINIGAME_HIT if urgent else InputPriority.NORMAL_CAST
        )
        if not self.coordinator.acquire(
            urgent=urgent,
            priority=prio,
            client_id=self.client_id,
            stop_check=lambda: self.running,
        ):
            return False

        # Kilit alındıktan sonra kullanıcı bu sırada Alt-Tab attıysa kilidi tutmadan bekle
        if not self.coordinator.is_user_in_game():
            self.coordinator.release()
            if not self._wait_until_game_focused():
                return False
            if not self.coordinator.acquire(
                urgent=urgent,
                priority=prio,
                client_id=self.client_id,
                stop_check=lambda: self.running,
            ):
                return False

        if self._game_hwnd and not self.coordinator.focus_client(self._game_hwnd):
            self.coordinator.release()
            return False
        return True

    def _release_input(self) -> None:
        if self.coordinator is not None:
            self.coordinator.release()

    def _notify_stats_changed(self, save_disk: bool = False) -> None:
        if self._on_stats_update is not None:
            self._on_stats_update()
        elif save_disk:
            self.save_session()

    def save_session(self) -> None:
        """Tekli modda sayaçları oturum.json'a yazar (Çoklu modda MultiClientManager kaydeder)."""
        if not self._load_saved_session:
            if self._on_stats_update is not None:
                self._on_stats_update()
            return
        try:
            with self._stats_lock:
                data = {k: (dict(self.stats[k]) if isinstance(self.stats[k], dict) else self.stats[k])
                        for k in _SESSION_KEYS}
                data["active_seconds"] = int(time.time() - self.stats["session_start"])
            tmp = SESSION_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, SESSION_FILE)
            self._last_save = time.time()
        except Exception as e:
            log.warning(f"[OTURUM] Kaydedilemedi: {e}")

    def _update_earnings(self) -> None:
        """fiyatlar.json üzerinden tutulan balıkların toplam Yang ve Won değerini hesaplar (disk önbellekli)."""
        if os.path.exists("fiyatlar.json"):
            try:
                mt = os.path.getmtime("fiyatlar.json")
                if mt != self._fiyat_mtime:
                    with open("fiyatlar.json", "r", encoding="utf-8") as f:
                        self._fiyat_cache = json.load(f)
                    self._fiyat_mtime = mt
            except Exception:
                pass
        with self._stats_lock:
            total_yang, total_won = compute_earnings_from_counts(self.stats["caught_fish"], self._fiyat_cache)
            self.stats["total_yang"] = total_yang
            self.stats["total_won"] = total_won

    @staticmethod
    def _fold_tr(text: str) -> str:
        return fold_tr(text)

    def _chat_last_event(self, chat_text: str) -> str:
        """Sohbet kalıcı olduğundan eski mesajlar silinmez. Bu yüzden metindeki EN SON olaya bakılır.
        ChatReader.read_chat satırları yukarıdan aşağıya, kelimeleri soldan sağa sıraladığı için
        dar ROI'lerde 2 satıra bölünen mesajlar bile tek boşlukla birleştirildiğinde tam okuma sırasını korur.
        Dönüş: 'bait_empty' | 'mount' | 'ok' | 'none'"""
        if not chat_text or not chat_text.strip():
            return "none"
        folded = self._fold_tr(chat_text.lower()).replace("0", "o")
        t = re.sub(r"\s+", " ", folded).strip()
        if not t:
            return "none"

        events = {}
        for name, pats in (
            (
                "bait_empty",
                (
                    r"\bonc[ce]\s+[yv]em",
                    r"\bunc[ce]\s+[yv]em",
                    r"[yv]emi\s+c?engel",
                    r"c?engele\s+[yv]em\s+[tl]ak(?![tiu1]n)",
                    r"c?engele\s+[tl]ak(?![tiu1]n)",
                    r"\byemsiz\b",
                    r"\btakmadan\b",
                ),
            ),
            ("mount", (r"binek", r"uzerindeyken", r"tutamazs")),
            (
                "ok",
                (
                    r"ol\w{0,2}rak",
                    r"yem\s+ol",
                    r"so[lk]u",
                    r"hamur",
                    r"karides",
                    r"su\s+anki",
                    r"degis",
                    r"takt[iu1]n",
                    r"takl[iu1]n",
                    r"takh[iu1]n",
                ),
            ),
        ):
            pos = -1
            for p in pats:
                for m in re.finditer(p, t):
                    pos = max(pos, m.end())
            if pos >= 0:
                events[name] = pos
        if not events:
            return "none"
        return max(events, key=events.get)

    def _classify_catch_outcome(self, chat_text: str, pre_chat: str = "") -> Tuple[str, str]:
        """Minigame kapandıktan sonra sohbet kutusuna düşen gerçek sunucu sonucunu okur.
        Metin2'de minigame 3/3 yapılsa bile olta şansı (RNG) tutmadığında 'Yemi kaybettin.' yazar!
        Dönüş: ('caught' | 'escaped' | 'unknown', incelenen_son_kısım)"""
        if not chat_text or not chat_text.strip():
            return "unknown", ""

        # OCR homoglyph normalizasyonu (1->i, 0->o, 5->s, vb.)
        folded = self._fold_tr(chat_text.lower())
        for old, new in (("0", "o"), ("1", "i"), ("!", "i"), ("5", "s"), ("$", "s"), ("3", "e"), ("4", "a"), ("@", "a")):
            folded = folded.replace(old, new)
        t = re.sub(r"\s+", " ", folded).strip()
        if not t:
            return "unknown", ""

        # Eğer minigame başlangıcında kaydedilen pre_chat varsa, eski mesajları temizle
        if pre_chat and pre_chat.strip():
            pre_folded = self._fold_tr(pre_chat.lower())
            for old, new in (("0", "o"), ("1", "i"), ("!", "i"), ("5", "s"), ("$", "s"), ("3", "e"), ("4", "a"), ("@", "a")):
                pre_folded = pre_folded.replace(old, new)
            pre_t = re.sub(r"\s+", " ", pre_folded).strip()
            if len(pre_t) > 15:
                tail_candidate = pre_t[-30:]
                idx = t.find(tail_candidate)
                if idx >= 0:
                    t_after_pre = t[idx + len(tail_candidate):].strip()
                    if t_after_pre:
                        t = t_after_pre

        # Önceki atışlardan kalan eski 'tuttun!' veya 'yemi kaybettin' mesajlarını elemek için
        # varsa en son yem takma ve en son 'oltaya takılmış gibi görünüyor / belli değil' mesajından SONRASINI al.
        anchor_pos = 0
        anchor_pats = (
            r"(?:solu\w*|soku\w*|hamur\w*|karides\w*|olarak|olrak)\s*takt\w*\.?",
            r"su\s+anki\w*\s+yem\w*.*?degis\w*\.?",
            r"g[io1]b[io1]\s*gorun\w*\.?",
            r"g[io1]b[io1]\s*grn\w*\.?",
            r"tak[iu1l]{1,3}m[iu1s]+\s*gorun\w*\.?",
            r"bell[io1]\s*deg[io1]l\w*\.?",
        )
        for ap in anchor_pats:
            for m in re.finditer(ap, t):
                if m.end() > anchor_pos:
                    anchor_pos = m.end()

        tail = t[anchor_pos:].strip(" .,;:-_!?'\"`") if anchor_pos > 0 else t
        if not tail:
            return "unknown", ""

        escaped_pats = (
            r"[yv]em[iu1l]?\s*ka(?!rides)\w{2,9}",
            r"\bka[vyh][bp][ceo0][tl1i]+\w*",
            r"\bkayb\w*",
            r"\bkac[iu1]rd\w*",
            r"\bkurtuld\w*",
            r"\bbalik\s+kact\w*",
            r"\byakalayama\w*",
        )
        caught_pats = (
            r"\btuttun\b|\btuttun\w*",
            r"\btullun\w*",
            r"\btutlun\w*",
            r"\byakalad[iu1]n\w*",
            r"\byakaland[iu1]\w*",
            r"\buzunlu\w*",
            r"\b\d+\s*[.,]\s*\d+\s*cm\b",
            r"\bkazan[iu1]lan\b",
            r"\bkazand[iu1]n\w*",
            r"sudan\s+.{2,35}\s+cekt[iu1]n",
            r"\belde\s+ett[iu1]n\w*",
        )

        esc_pos = -1
        for p in escaped_pats:
            for m in re.finditer(p, tail):
                esc_pos = max(esc_pos, m.end())

        cat_pos = -1
        for p in caught_pats:
            for m in re.finditer(p, tail):
                cat_pos = max(cat_pos, m.end())

        # Fuzzy yedek: EasyOCR 'kaybettin' kelimesini farklı okursa yine yakala
        if esc_pos < 0 and cat_pos < 0:
            for w in re.findall(r"[a-z0-9]{5,14}", tail):
                if (w.startswith("k") or w.startswith("y")) and not w.startswith("kazan"):
                    if difflib.SequenceMatcher(None, w, "kaybettin").ratio() >= 0.60 or \
                       difflib.SequenceMatcher(None, w, "yemikaybettin").ratio() >= 0.65:
                        esc_pos = len(tail)
                        break

        if esc_pos < 0 and cat_pos < 0:
            return "unknown", tail
        if esc_pos >= cat_pos:
            return "escaped", tail
        return "caught", tail

    def _is_farm_time_up(self) -> bool:
        elapsed_min = (time.time() - self._farm_start_time) / 60.0
        return elapsed_min >= self._target_farm_min

    def _focus_game_window(self) -> None:
        """Tekli doğrudan çalıştırmada Metin2 oyun penceresini bulup öne getirir."""
        wins = discover_game_windows(self.config.game_window_title)
        if wins:
            h = wins[0]["hwnd"]
            self._game_hwnd = h
            self.chat_reader.target_hwnd = h
            coord = self.coordinator or InputCoordinator([h], self.config.game_window_title)
            coord.focus_client(h, force_initial=True)
            log.info(f"{self._tag} Metin2 oyun penceresi öne getirildi (hwnd: {h})")

    def run(self, skip_countdown: bool = False):
        self.running = True
        if not skip_countdown:
            if not self._game_hwnd:
                self._focus_game_window()
            log.info(f"{self._tag} Oyun penceresine geçmek için 5 saniyeniz var...")
            for i in range(5, 0, -1):
                if not self.running:
                    return
                log.info(f"{i} saniye...")
                time.sleep(1)
            if not self._game_hwnd:
                try:
                    fg = win32gui.GetForegroundWindow()
                    if fg and is_game_window_foreground(None, self.config.game_window_title):
                        self._game_hwnd = fg
                        self.chat_reader.target_hwnd = fg
                        if self.coordinator:
                            self.coordinator.register_hwnd(fg)
                except Exception:
                    pass

        log.info(f"{self._tag} Balıkçı işçisi başlatıldı (hwnd={self._game_hwnd}, mola hedefi={self._target_farm_min:.0f} dk).")
        try:
            while self.running:
                try:
                    if self._game_hwnd and not win32gui.IsWindow(self._game_hwnd):
                        log.error(f"{self._tag} Oyun penceresi kapandı (hwnd={self._game_hwnd}). İşçi durduruluyor.")
                        break

                    if self.state == AppState.IDLE:
                        self.state = AppState.CAST
                    elif self.state == AppState.CAST:
                        self.state = self.state_cast()
                    elif self.state == AppState.DECISION:
                        self.state = self.state_decision()
                    elif self.state == AppState.MINIGAME:
                        self.state = self.state_minigame()
                    elif self.state == AppState.ANIM_CANCEL:
                        self.state = self.state_anim_cancel()
                    elif self.state == AppState.COOLDOWN:
                        self.state = self.state_cooldown()
                    elif self.state == AppState.REST:
                        rest_sec = self._roll_rest_seconds()
                        log.info(f"{self._tag} [MOLA] Anti-ban dinlenme molası: {rest_sec / 60.0:.1f} dakika...")
                        end_rest = time.time() + rest_sec
                        while self.running and time.time() < end_rest:
                            time.sleep(0.5)
                        self._farm_start_time = time.time()
                        self._target_farm_min = self._roll_farm_minutes()
                        self.state = AppState.CAST
                    elif self.state == AppState.STOPPED:
                        break

                    if self._load_saved_session and time.time() - self._last_save > 15:
                        self.save_session()
                except KeyboardInterrupt:
                    break
                except Exception as e:
                    log.error(f"{self._tag} [HATA] Beklenmeyen hata: {e}", exc_info=True)
                    break
        finally:
            self.running = False
            close_thread_sct()
            self._notify_stats_changed(save_disk=True)
            log.info(f"{self._tag} İşçi durduruldu.")

    def state_minigame(self) -> AppState:
        """Minigame döngüsünü dış donanım kilidi OLMADAN (time-sharing) yürütür.
        Görüntü tarama + OCR paralel/kilitsiz çalışır; Mutex sadece vuruş ve ESC anında alınır."""
        mg_roi = self.get_minigame_roi()
        chat_roi = self.get_chat_roi()

        bgr_check = None
        for attempt in range(2):
            try:
                bgr_check = capture_window_roi(self._game_hwnd, mg_roi, allow_background=False)
                break
            except ScreenShotError as e:
                if attempt == 0:
                    log.warning(f"{self._tag} Minigame başlangıç yakalama hatası, tekrar deneniyor: {e}")
                    time.sleep(0.12)
                else:
                    log.warning(f"{self._tag} Minigame ekranı okunamadı, atış tazeleniyor: {e}")
                    return AppState.CAST

        if not is_minigame_open_bgr(bgr_check):
            log.info(f"{self._tag} Minigame penceresi kontrol sırasında kapanmış, olta tazeleniyor.")
            return AppState.ANIM_CANCEL

        client_cfg = replace(self.config, chat_roi=chat_roi, minigame_roi=mg_roi)
        controller = MinigameController(
            client_cfg,
            self.chat_reader,
            target_hwnd=self._game_hwnd,
            coordinator=self.coordinator,
            client_id=self.client_id,
        )
        controller.play()
        self.last_controller = controller

        if controller.skip_fish:
            with self._stats_lock:
                self.stats["worthless_fish"] += 1
            self._notify_stats_changed()

        return AppState.ANIM_CANCEL

    def _sleep_stoppable(self, seconds: float) -> bool:
        end_t = time.monotonic() + max(0.0, float(seconds))
        while self.running:
            rem = end_t - time.monotonic()
            if rem <= 0.0:
                return True
            time.sleep(min(0.04, rem))
        return False

    def state_cast(self) -> AppState:
        self.last_controller = None
        mount_retries = 0
        held = False

        try:
            while self.running:
                if self.current_bait_index >= len(self.config.bait_keys):
                    log.error(f"{self._tag} [YEM BİTTİ] Tüm yem slotları tükendi! Bu istemci durduruluyor.")
                    return AppState.STOPPED

                if not held:
                    if not self._acquire_input(priority=InputPriority.NORMAL_CAST):
                        return AppState.STOPPED if not self.running else AppState.CAST
                    held = True

                current_bait = self.config.bait_keys[self.current_bait_index]
                log.info(f"{self._tag} [STATE 1] Olta atılıyor... (Yem Slotu: {current_bait})")

                if not is_game_window_foreground(self._game_hwnd, self.config.game_window_title):
                    return AppState.CAST

                self.keyboard.press(current_bait)
                bait_sent_t = time.monotonic()

                # Metin2'de yem takma paketi (CG_ITEM_USE -> GC_CHAT) ve binekten inme sonrası WAIT
                # animasyon geçişi (~0.56s) bitene kadar donanım kilidini BIRAK ki diğer istemciler
                # minigame vuruşlarını beklemek zorunda kalmasın!
                self._release_input()
                held = False

                bait_wait = _jitter(0.560, 0.035, 0.500, 0.640)
                if not self._sleep_stoppable(bait_wait):
                    return AppState.STOPPED

                if not self._acquire_input(priority=InputPriority.NORMAL_CAST):
                    return AppState.STOPPED if not self.running else AppState.CAST
                held = True

                rem_bait = bait_wait - (time.monotonic() - bait_sent_t)
                if rem_bait > 0.0:
                    time.sleep(rem_bait)

                if not is_game_window_foreground(self._game_hwnd, self.config.game_window_title):
                    if self.coordinator and not self.coordinator.focus_client(self._game_hwnd):
                        return AppState.CAST
                self.keyboard.press(self.config.cast_key)

                # Space tuşu DirectInput motorunda işlenene kadar kısa oturma payı
                time.sleep(_jitter(0.045, 0.008, 0.030, 0.065))

                # Space gönderilir gönderilmez donanım kilidini DERHAL serbest bırak!
                # Olta sonrası sohbet beklemesi ve EasyOCR okuması diğer istemcileri bekletmeden paralel çalışır.
                self._release_input()
                held = False

                # 'Önce yemi çengele tak.' / 'Binek üzerindeyken...' mesajının ekrana çizilmesi için bekle
                time.sleep(_jitter(0.260, 0.025, 0.220, 0.320))
                if not self.running:
                    return AppState.STOPPED

                chat_text = self.chat_reader.read_chat(roi_override=self.get_chat_roi()).lower()
                last_event = self._chat_last_event(chat_text)

                if last_event == "none":
                    # İlk karede sohbet yazısı henüz düşmediyse Space'e dokunmadan 1 kez pasif tekrar oku
                    time.sleep(0.15)
                    if not self.running:
                        return AppState.STOPPED
                    chat_text = self.chat_reader.read_chat(roi_override=self.get_chat_roi()).lower()
                    last_event = self._chat_last_event(chat_text)

                if last_event == "bait_empty":
                    # 'Önce yemi çengele tak.' göründüyse olta kesinlikle suda DEĞİLDİR.
                    # Pencere geçişinde ilk yem tuşu kaçmış olabilir mi diye slota 1 kez daha basıp doğrula:
                    time.sleep(_jitter(0.130, 0.020, 0.095, 0.175))
                    if not self._acquire_input(priority=InputPriority.NORMAL_CAST):
                        return AppState.STOPPED if not self.running else AppState.CAST
                    held = True
                    if not is_game_window_foreground(self._game_hwnd, self.config.game_window_title):
                        if self.coordinator and not self.coordinator.focus_client(self._game_hwnd):
                            return AppState.CAST
                    self.keyboard.press(current_bait)
                    bait2_sent_t = time.monotonic()
                    self._release_input()
                    held = False

                    time.sleep(_jitter(0.400, 0.030, 0.340, 0.480))
                    if not self.running:
                        return AppState.STOPPED
                    probe_chat = self.chat_reader.read_chat(roi_override=self.get_chat_roi()).lower()
                    probe_event = self._chat_last_event(probe_chat)

                    if probe_event == "ok":
                        rem2 = _jitter(0.560, 0.035, 0.500, 0.640) - (time.monotonic() - bait2_sent_t)
                        if rem2 > 0.0 and not self._sleep_stoppable(rem2):
                            return AppState.STOPPED
                        if not self._acquire_input(priority=InputPriority.NORMAL_CAST):
                            return AppState.STOPPED if not self.running else AppState.CAST
                        held = True
                        log.info(f"{self._tag} [YEM DOĞRULAMA] '{current_bait}' slotu 2. denemede yem taktı, olta atılıyor...")
                        if not is_game_window_foreground(self._game_hwnd, self.config.game_window_title):
                            if self.coordinator and not self.coordinator.focus_client(self._game_hwnd):
                                return AppState.CAST
                        self.keyboard.press(self.config.cast_key)
                        time.sleep(_jitter(0.045, 0.008, 0.030, 0.065))
                        self._release_input()
                        held = False
                        time.sleep(_jitter(0.240, 0.025, 0.190, 0.290))
                        self._just_switched_bait = False
                        with self._stats_lock:
                            self.stats["total_casts"] += 1
                        self._notify_stats_changed()
                        return AppState.DECISION

                    self.current_bait_index += 1
                    if self.current_bait_index >= len(self.config.bait_keys):
                        log.error(f"{self._tag} [YEM BİTTİ] Tüm yem slotları (1-4, F1-F4) boş/tükendi! Bu istemci durduruluyor.")
                        return AppState.STOPPED

                    next_bait = self.config.bait_keys[self.current_bait_index]
                    log.warning(f"{self._tag} [YEM BOŞ / BİTTİ] '{current_bait}' slotunda yem yok -> '{next_bait}' slotuna geçiliyor...")
                    self._just_switched_bait = True
                    time.sleep(0.08)
                    continue

                if last_event == "mount":
                    # Karakter hâlâ binek üstündeyse TEK Ctrl+G ile iner (çift g tekrar bindirir!)
                    log.warning(f"{self._tag} [BİNEK HATASI] Binek üzerindeyken balık tutulamaz! Binekten iniliyor...")
                    if not self._acquire_input(priority=InputPriority.NORMAL_CAST):
                        return AppState.STOPPED if not self.running else AppState.CAST
                    held = True
                    if self.coordinator:
                        self.coordinator.focus_client(self._game_hwnd)
                    if is_game_window_foreground(self._game_hwnd, self.config.game_window_title):
                        self.keyboard.hotkey("ctrl", self.config.mount_key)
                    self._release_input()
                    held = False
                    time.sleep(_jitter(0.560, 0.040, 0.480, 0.660))
                    if mount_retries < 2:
                        mount_retries += 1
                        continue
                    return AppState.CAST

                self._just_switched_bait = False
                with self._stats_lock:
                    self.stats["total_casts"] += 1
                self._notify_stats_changed()
                return AppState.DECISION

            return AppState.STOPPED
        finally:
            if held:
                self._release_input()

    def state_decision(self) -> AppState:
        """Kilit tutmadan (paralel) bu istemcinin minigame penceresini izler.
        Böylece 1-4 istemcinin tamamı aynı anda balık bekleyebilir."""
        log.info(f"{self._tag} [STATE 2] Balık bekleniyor...")
        wait_start = time.time()
        # Empirik analiz (193 balık): %100 vuruş <= 5.25 sn -> 5.4-6.4 sn tavanı Space yutulursa boş beklemeyi önler
        bite_timeout = _jitter(5.8, 0.25, 5.4, 6.4)

        # Olta atma animasyonu tamamlanmadan (ilk ~0.6 sn) fiziksel olarak balık vuramaz
        for _ in range(3):
            if not self.running:
                return AppState.STOPPED
            time.sleep(0.20)

        last_grab_warn_t = 0.0
        while time.time() - wait_start < bite_timeout:
            if not self.running:
                return AppState.STOPPED
            if self._is_farm_time_up():
                return AppState.REST
            if not self._is_user_in_game():
                if not self._wait_until_game_focused():
                    return AppState.STOPPED
                # Kullanıcı kısa süreli Alt-Tab yaptıysa ve süre henüz dolmadıysa izlemeye devam et;
                # süre dolduysa ESC basmadan yeniden olta atmaya geç.
                if time.time() - wait_start >= bite_timeout:
                    return AppState.CAST

            mg_roi = self.get_minigame_roi()
            try:
                bgr = capture_window_roi(self._game_hwnd, mg_roi, allow_background=False)
            except ScreenShotError as e:
                now_err = time.monotonic()
                if now_err - last_grab_warn_t >= 3.0:
                    last_grab_warn_t = now_err
                    log.warning(f"{self._tag} [STATE 2] Ekran yakalama hatası (devam ediliyor): {e}")
                time.sleep(0.25)
                continue

            if is_minigame_open_bgr(bgr):
                log.info(f"{self._tag} [STATE 2] Balık vurdu (Mavi su + Minigame doğrulandı)! Sıra alınıyor...")
                return AppState.MINIGAME

            time.sleep(0.10)

        # KRİTİK DÜZELTME: Balık vurmadığında minigame penceresi AÇIK DEĞİLDİR!
        # Körlemesine ESC basmadan animasyon sıfırlanır.
        log.warning(f"{self._tag} [STATE 2] {bite_timeout:.1f}sn geçti, balık vurmadı. Olta tazeleniyor.")
        return AppState.ANIM_CANCEL

    def _process_last_controller_outcome(self) -> None:
        """Minigame sonrası yakalanan/kaçan balık doğrulaması (donanım kilidi dışında çalışır)."""
        ctrl = self.last_controller
        self.last_controller = None
        if not ctrl or ctrl.skip_fish:
            return

        mg_passed = bool(getattr(ctrl, "caught_verified", False))
        pre_chat = getattr(ctrl, "pre_chat_text", "")
        glitched = bool(getattr(ctrl, "glitched_escaped", False))

        # Metin2 kuralı: 3 başarılı vuruş atılmadan veya oyun kilitlenip bozulduğunda balık tutulması İMKANSIZDIR!
        if ctrl.hits < 3 or glitched:
            mg_passed = False

        outcome, tail_txt = "unknown", ""
        post_chat = ""
        if self.chat_reader:
            post_chat = self.chat_reader.read_chat(roi_override=self.get_chat_roi())
            outcome, tail_txt = self._classify_catch_outcome(post_chat, pre_chat=pre_chat)
            # Metin2 sunucu pingi sebebiyle 'Yemi kaybettin.' veya '... tuttun!' mesajı 100-300ms geç düşebilir.
            # Cooldown beklemesi sırasında en fazla 3 kez tekrar kontrol et:
            if outcome == "unknown" and ctrl.hits >= 3 and not glitched:
                for _ in range(3):
                    time.sleep(0.12)
                    post_chat = self.chat_reader.read_chat(roi_override=self.get_chat_roi())
                    outcome, tail_txt = self._classify_catch_outcome(post_chat, pre_chat=pre_chat)
                    if outcome != "unknown":
                        break

        if outcome == "escaped" or glitched or ctrl.hits < 3:
            actually_caught = False
        elif outcome == "caught":
            actually_caught = True
        else:
            # Sohbet okunabiliyorsa ama 'unknown' kaldıysa kaçan/kaybolan balığı ASLA tutuldu sayma!
            actually_caught = mg_passed if (not post_chat or not post_chat.strip()) else False

        ctrl.caught_verified = actually_caught

        if actually_caught:
            fish_name = ctrl.detected_fish or "Bilinmeyen Balık"
            with self._stats_lock:
                self.stats["valuable_fish"] += 1
                self.stats["hits"] += min(3, ctrl.hits) if ctrl.hits > 0 else 3
                self.stats["misses"] += max(0, ctrl.hits - 3)
                self.stats["caught_count"] += 1
                cf = self.stats["caught_fish"]
                cf[fish_name] = cf.get(fish_name, 0) + 1
            self._update_earnings()
            self._notify_stats_changed(save_disk=True)
            log.info(
                f"🎣 {self._tag} [TUTULDU] {fish_name.title()} yakalandı! "
                f"(Bu İstemci Tutulan: {self.stats['caught_count']}, Sohbet: '{tail_txt[:40]}')"
            )
        else:
            with self._stats_lock:
                self.stats["valuable_fish"] += 1
                if mg_passed:
                    self.stats["hits"] += min(3, ctrl.hits)
                    self.stats["misses"] += max(0, ctrl.hits - 3)
                else:
                    self.stats["hits"] += min(2, ctrl.hits)
                    self.stats["misses"] += max(1, ctrl.hits - 2)
                self.stats["escaped_count"] += 1
            self._notify_stats_changed(save_disk=True)
            if outcome == "escaped" or glitched:
                log.warning(
                    f"💨 {self._tag} [KAÇTI - YEMİ KAYBETTİN] Balık kaçtı / olta şansı tutmadı "
                    f"(Sohbet: '{tail_txt[:45]}', Tıklama: {ctrl.hits})!"
                )
            else:
                log.warning(
                    f"💨 {self._tag} [KAÇTI] Balık kaçtı / süre doldu "
                    f"(Tıklama: {ctrl.hits}, Son Bar: %{ctrl.last_timer_ratio * 100:.0f})!"
                )

    def state_anim_cancel(self) -> AppState:
        """Bölünmüş güvenli CTRL+G+G animasyon iptali:
        1. darbe (Bin, ~0.13s kilit) -> Kilitsiz ~0.275s bekleme -> 2. darbe (İn, ~0.13s kilit), sonra COOLDOWN.
        Böylece uzun blokaj yaratmadan diğer istemcilerin minigame vuruşlarını özgür bırakır."""
        if not self._acquire_input(priority=InputPriority.HIGH_ANIM_CANCEL):
            return AppState.STOPPED if not self.running else AppState.CAST

        log.info(f"{self._tag} [STATE 4] Güvenli animasyon iptali (CTRL+G+G)...")
        mount_gap = _jitter(0.275, 0.025, 0.235, 0.325)
        post_delay = _jitter(0.62, 0.05, 0.52, 0.74)
        mounted_step1 = False
        try:
            time.sleep(_jitter(0.016, 0.004, 0.010, 0.025))
            mounted_step1 = HumanKeyboard.mount_single_pulse(
                self.config.mount_key,
                focus_check=lambda: is_game_window_foreground(self._game_hwnd, self.config.game_window_title),
            )
        finally:
            self._release_input()

        if not mounted_step1:
            self._process_last_controller_outcome()
            return AppState.CAST if self.running else AppState.STOPPED

        # Aradaki ~0.275 sn biniş beklemesini kilitsiz geçir (minigame vuruşları araya girebilsin)
        self._sleep_stoppable(mount_gap)

        # Karakteri binek üstünde bırakmamak için bot durdurulsa bile oyun odaktaysa 2. CTRL+G ile indir
        acquired_step2 = False
        if self.running:
            acquired_step2 = self._acquire_input(priority=InputPriority.HIGH_ANIM_CANCEL)
        elif self.coordinator is not None and self.coordinator.is_user_in_game():
            if self.coordinator.acquire(priority=InputPriority.HIGH_ANIM_CANCEL, client_id=self.client_id, timeout=1.5):
                if self._game_hwnd and not self.coordinator.focus_client(self._game_hwnd):
                    self.coordinator.release()
                else:
                    acquired_step2 = True

        cancel_done_t = time.monotonic()
        if acquired_step2:
            try:
                HumanKeyboard.mount_single_pulse(
                    self.config.mount_key,
                    focus_check=lambda: is_game_window_foreground(self._game_hwnd, self.config.game_window_title),
                )
            finally:
                cancel_done_t = time.monotonic()
                self._release_input()

        self._cooldown_until = cancel_done_t + post_delay
        self._process_last_controller_outcome()
        return AppState.COOLDOWN if self.running else AppState.STOPPED

    def state_cooldown(self) -> AppState:
        """Binek in-bin sonrası 0.46-0.72sn sunucu tick gecikmesini donanım kilidi OLMADAN bekler."""
        while self.running:
            rem = self._cooldown_until - time.monotonic()
            if rem <= 0:
                break
            time.sleep(min(0.05, rem))
        return AppState.CAST if self.running else AppState.STOPPED


class MultiClientManager:
    """Tek ekranda 1-6 Metin2 istemcisini ortak GPU OCR modeli ve
    öncelikli fare/klavye kilidiyle yöneten orkestratör."""

    def __init__(
        self,
        config: FisherConfig,
        target_hwnds: Optional[List[int]] = None,
        client_rois: Optional[Dict[int, dict]] = None,
    ):
        self.config = config
        self.target_hwnds: List[int] = [h for h in (target_hwnds or []) if h and win32gui.IsWindow(h)]
        self.client_rois: Dict[int, dict] = dict(client_rois or {})
        self.running = False
        self.coordinator = InputCoordinator(self.target_hwnds, config.game_window_title)
        self.workers: List[FishingHelper] = []
        self._threads: List[threading.Thread] = []
        self._agg_lock = threading.Lock()
        self._fiyat_cache: dict = {}
        self._fiyat_mtime: float = 0.0
        self._last_save: float = 0.0

        # EasyOCR singleton modelini işçiler başlamadan önce ısıt
        _get_ocr_reader()

        saved = load_session()
        self._base_stats = {
            "total_casts": int(saved.get("total_casts", 0)),
            "hits": int(saved.get("hits", 0)),
            "misses": int(saved.get("misses", 0)),
            "valuable_fish": int(saved.get("valuable_fish", 0)),
            "worthless_fish": int(saved.get("worthless_fish", 0)),
            "caught_count": int(saved.get("caught_count", 0)),
            "escaped_count": int(saved.get("escaped_count", 0)),
            "caught_fish": dict(saved.get("caught_fish") or {}),
        }
        self._session_start = time.time() - float(saved.get("active_seconds", 0))
        self.stats: dict = {}
        self._refresh_aggregated_stats(save_disk=False)

    def _load_fiyatlar(self) -> dict:
        if os.path.exists("fiyatlar.json"):
            try:
                mt = os.path.getmtime("fiyatlar.json")
                if mt != self._fiyat_mtime:
                    with open("fiyatlar.json", "r", encoding="utf-8") as f:
                        self._fiyat_cache = json.load(f)
                    self._fiyat_mtime = mt
            except Exception:
                pass
        return self._fiyat_cache

    def _refresh_aggregated_stats(self, save_disk: bool = False) -> None:
        with self._agg_lock:
            merged_fish = dict(self._base_stats["caught_fish"])
            sums = {k: self._base_stats[k] for k in _SESSION_KEYS if k != "caught_fish"}

            for w in self.workers:
                with w._stats_lock:
                    for k in sums:
                        sums[k] += int(w.stats.get(k, 0))
                    for fn, cnt in w.stats.get("caught_fish", {}).items():
                        merged_fish[fn] = merged_fish.get(fn, 0) + int(cnt)

            fiyatlar = self._load_fiyatlar()
            total_yang, total_won = compute_earnings_from_counts(merged_fish, fiyatlar)
            self.stats = {
                **sums,
                "caught_fish": merged_fish,
                "total_yang": total_yang,
                "total_won": total_won,
                "session_start": self._session_start,
            }
            if save_disk or (time.time() - self._last_save > 15.0):
                self._write_session_locked()

    def _write_session_locked(self) -> None:
        try:
            data = {k: (dict(self.stats[k]) if isinstance(self.stats[k], dict) else self.stats[k])
                    for k in _SESSION_KEYS}
            data["active_seconds"] = int(time.time() - self._session_start)
            tmp = SESSION_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, SESSION_FILE)
            self._last_save = time.time()
        except Exception as e:
            log.warning(f"[OTURUM] Kaydedilemedi: {e}")

    def save_session(self) -> None:
        self._refresh_aggregated_stats(save_disk=True)

    def _update_earnings(self) -> None:
        self._fiyat_mtime = 0.0
        self._refresh_aggregated_stats(save_disk=False)

    def state_summary(self) -> str:
        if not self.workers:
            return ""
        if len(self.workers) == 1:
            return f"[{self.workers[0].state.name}]"
        return " | ".join(f"C{w.client_id}:{w.state.name[:4]}" for w in self.workers)

    def stop(self) -> None:
        self.running = False
        for w in self.workers:
            w.running = False
            w.state = AppState.STOPPED
        self.save_session()

    def run(self) -> None:
        self.running = True
        hwnd_list: List[Optional[int]] = list(self.target_hwnds)

        if not hwnd_list:
            # Açık Metin2 penceresi otomatik taranır; yine bulunamazsa klasik 5sn odak moduna düşer
            discovered = discover_game_windows(self.config.game_window_title)
            if discovered:
                hwnd_list = [w["hwnd"] for w in discovered[:6]]
                for h in hwnd_list:
                    self.coordinator.register_hwnd(h)

        # Çoklu pencere varsa ve özel client_rois verilmediyse göreli ROI türetmeyi dene
        if len(hwnd_list) >= 1 and not self.client_rois:
            if self.config.rel_chat_roi is None:
                self.config.rel_chat_roi = screen_roi_to_relative(
                    self.config.chat_roi, self.config.game_window_title, require_inside=(len(hwnd_list) > 1)
                )
            if self.config.rel_minigame_roi is None:
                self.config.rel_minigame_roi = screen_roi_to_relative(
                    self.config.minigame_roi, self.config.game_window_title, require_inside=(len(hwnd_list) > 1)
                )
            if len(hwnd_list) > 1 and (self.config.rel_chat_roi is None or self.config.rel_minigame_roi is None):
                log.warning(
                    "⚠️ [KALİBRASYON] Çoklu pencere için her istemcinin kendi Chat ve Oyun alanını seçin!"
                )

        if hwnd_list:
            n_clients = len(hwnd_list)
            log.info(f"🪟 {n_clients} istemci hazırlandı. 3 saniye içinde ilk pencereye geçilip başlanacak...")
            self.coordinator.focus_client(hwnd_list[0], force_initial=True)
            for _ in range(12):
                if not self.running:
                    return
                time.sleep(0.25)
        else:
            hwnd_list = [None]
            log.info("Oyun penceresine geçmek için 5 saniyeniz var...")
            for i in range(5, 0, -1):
                if not self.running:
                    return
                log.info(f"{i} saniye...")
                time.sleep(1.0)
            try:
                fg = win32gui.GetForegroundWindow()
                if fg and is_game_window_foreground(None, self.config.game_window_title):
                    hwnd_list = [fg]
                    self.coordinator.register_hwnd(fg)
            except Exception:
                pass

        self.workers = []
        self._threads = []
        for idx, hwnd in enumerate(hwnd_list, start=1):
            slot_cfg = self.client_rois.get(hwnd) if hwnd else None
            if slot_cfg:
                worker_cfg = replace(
                    self.config,
                    chat_roi=slot_cfg.get("chat_roi") or self.config.chat_roi,
                    minigame_roi=slot_cfg.get("minigame_roi") or self.config.minigame_roi,
                    rel_chat_roi=slot_cfg.get("rel_chat_roi"),
                    rel_minigame_roi=slot_cfg.get("rel_minigame_roi"),
                )
            else:
                worker_cfg = self.config

            helper = FishingHelper(
                config=worker_cfg,
                client_id=idx,
                target_hwnd=hwnd,
                coordinator=self.coordinator,
                on_stats_update=lambda: self._refresh_aggregated_stats(save_disk=False),
                load_saved_session=False,
            )
            log.info(
                f"[C{idx}] Aktif Alanlar -> Chat: {helper.get_chat_roi()} | Oyun: {helper.get_minigame_roi()}"
            )
            self.workers.append(helper)
            t = threading.Thread(
                target=helper.run,
                kwargs={"skip_countdown": True},
                daemon=True,
                name=f"ClientWorker-{idx}",
            )
            self._threads.append(t)

        for t in self._threads:
            if not self.running:
                break
            t.start()
            time.sleep(0.55)

        while self.running:
            if not any(t.is_alive() for t in self._threads):
                break
            time.sleep(0.4)

        self.stop()
        for t in self._threads:
            t.join(timeout=1.0)
        log.info(f"Tüm istemciler durduruldu. Toplam Tutulan: {self.stats.get('caught_count', 0)}")


if __name__ == "__main__":
    pass
