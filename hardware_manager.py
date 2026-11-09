"""
=============================================================================
Donanım, Odak, İnsansı Fare ve Güvenli Girdi Yönetim Katmanı
=============================================================================
- PriorityHardwareMutex : Öncelik kuyruklu (heapq + aging) pydirectinput kilidi
- WindowFocusGuard      : Windows ForegroundLock aşan & kullanıcı Alt-Tab dostu odak
- HumanizedMouse        : Kübik Bézier + min-jerk hız eğrisi + Gauss jitter
- HumanKeyboard         : try...finally güvenli tuş + safe_mount_cancel (CTRL+G+G)
- thread_grab_bgr       : Thread-local mss.mss() ile kilitsiz / flicker'sız ROI okuma
=============================================================================
"""
from __future__ import annotations

import atexit
import ctypes
import heapq
import logging
import math
import os
import random
import threading
import time
from contextlib import contextmanager
from enum import IntEnum
from typing import Callable, Iterator, List, Optional, Set, Tuple

import cv2
import mss
import numpy as np
import pydirectinput
import win32api
import win32con
import win32gui
import win32process

pydirectinput.PAUSE = 0.0
pydirectinput.FAILSAFE = False

log = logging.getLogger("FisherAppMT")

_DPI_INITIALIZED = False
_DPI_LOCK = threading.Lock()
_TIMER_INITIALIZED = False
_MSS_TLS = threading.local()


def _ensure_timer_resolution() -> None:
    """Windows zamanlayıcı çözünürlüğünü 1 ms'ye ayarlar (kısa fare adımlarının kaymasını önler)."""
    global _TIMER_INITIALIZED
    if _TIMER_INITIALIZED:
        return
    try:
        winmm = ctypes.windll.winmm
        if winmm.timeBeginPeriod(1) == 0:
            _TIMER_INITIALIZED = True
            atexit.register(winmm.timeEndPeriod, 1)
    except Exception:
        _TIMER_INITIALIZED = True


def _precise_sleep(duration: float) -> None:
    """Kısa mikro-adımlarda (~1-25 ms) OS sleep kaymasını önleyen hibrit bekleme."""
    if duration <= 0.0:
        return
    end = time.perf_counter() + duration
    if duration >= 0.0055:
        bulk = duration - 0.0022
        if bulk > 0.001:
            time.sleep(bulk)
    while time.perf_counter() < end:
        pass


def ensure_dpi_awareness() -> None:
    """2K / yüksek ölçekli Windows ekranlarda Win32, mss, tkinter ve DirectInput
    koordinatlarının birebir aynı fiziksel piksel uzayında kalmasını sağlar."""
    global _DPI_INITIALIZED
    _ensure_timer_resolution()
    if _DPI_INITIALIZED:
        return
    with _DPI_LOCK:
        if _DPI_INITIALIZED:
            return
        try:
            # PROCESS_PER_MONITOR_DPI_AWARE = 2
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass
        _DPI_INITIALIZED = True


def _jitter(mean: float, std: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, random.gauss(mean, std)))


def get_thread_sct() -> mss.mss:
    """Her worker thread için tek bir kalıcı mss.mss() tutar (sıfır GDI handle sızıntısı)."""
    sct = getattr(_MSS_TLS, "sct", None)
    if sct is None:
        ensure_dpi_awareness()
        sct = mss.mss()
        _MSS_TLS.sct = sct
    return sct


def thread_grab_bgr(roi: dict) -> np.ndarray:
    """Belirtilen ekran ROI alanını donanım kilidi olmadan ~0.5 ms içinde BGR olarak okur."""
    safe_roi = {
        "left": int(roi["left"]),
        "top": int(roi["top"]),
        "width": max(10, int(roi["width"])),
        "height": max(10, int(roi["height"])),
    }
    sct = get_thread_sct()
    raw = np.asarray(sct.grab(safe_roi))
    # mss BGRA döner; copy() bellek yaşam döngüsünü güvenli kılar
    return cv2.cvtColor(raw, cv2.COLOR_BGRA2BGR)


class InputPriority(IntEnum):
    URGENT_MINIGAME_HIT = 0  # Balık hedef halkasında -> milisaniyelik öncelik
    HIGH_ANIM_CANCEL    = 1  # Güvenli CTRL+G animasyon iptali / ESC
    NORMAL_CAST         = 2  # Yem + Space olta atma
    LOW_MAINTENANCE     = 3  # Yapboz / ikincil bakım işleri


class PriorityHardwareMutex:
    """
    pydirectinput fare/klavye kaynaklarını öncelik sırası ve yaşlandırma
    (anti-starvation aging) ile koruyan donanım Mutex'i.
    """

    def __init__(self, aging_sec: float = 1.6) -> None:
        self._cond = threading.Condition()
        self._busy = False
        self._owner_tid: Optional[int] = None
        self._depth = 0
        self._seq = 0
        self._aging_sec = max(0.3, float(aging_sec))
        # Heap öğesi: (effective_priority, enqueue_ts, seq, client_id, event)
        self._waiters: List[Tuple[int, float, int, int, threading.Event]] = []

    @contextmanager
    def acquire_lock(
        self,
        client_id: int = 0,
        priority: InputPriority = InputPriority.NORMAL_CAST,
        timeout: float = 10.0,
        stop_check: Optional[Callable[[], bool]] = None,
    ) -> Iterator[bool]:
        ok = self.acquire(priority=priority, client_id=client_id, timeout=timeout, stop_check=stop_check)
        try:
            yield ok
        finally:
            if ok:
                self.release()

    def acquire(
        self,
        priority: InputPriority = InputPriority.NORMAL_CAST,
        client_id: int = 0,
        timeout: float = 10.0,
        stop_check: Optional[Callable[[], bool]] = None,
    ) -> bool:
        tid = threading.get_ident()
        ev = threading.Event()

        with self._cond:
            if self._busy and self._owner_tid == tid:
                self._depth += 1
                return True

            self._seq += 1
            entry_seq = self._seq
            now = time.monotonic()

            if not self._busy and not self._waiters:
                self._busy = True
                self._owner_tid = tid
                self._depth = 1
                return True

            heapq.heappush(self._waiters, (int(priority), now, entry_seq, int(client_id), ev))

        deadline = time.monotonic() + max(0.05, float(timeout))
        while True:
            if stop_check is not None and not stop_check():
                with self._cond:
                    self._remove_waiter_locked(entry_seq)
                return False

            rem = deadline - time.monotonic()
            if rem <= 0:
                with self._cond:
                    self._remove_waiter_locked(entry_seq)
                return False

            with self._cond:
                self._promote_aged_locked()

                if not self._busy and self._waiters and self._waiters[0][2] == entry_seq:
                    heapq.heappop(self._waiters)
                    self._busy = True
                    self._owner_tid = tid
                    self._depth = 1
                    return True

            ev.wait(timeout=min(0.025, rem))

    def _promote_aged_locked(self) -> None:
        if not self._waiters:
            return
        cur_t = time.monotonic()
        aged = False
        for idx, (prio, ts, sq, cid, wev) in enumerate(self._waiters):
            if prio > 0 and (cur_t - ts) >= self._aging_sec:
                self._waiters[idx] = (0, ts, sq, cid, wev)
                aged = True
        if aged:
            heapq.heapify(self._waiters)

    def _remove_waiter_locked(self, entry_seq: int) -> None:
        self._waiters = [w for w in self._waiters if w[2] != entry_seq]
        heapq.heapify(self._waiters)
        self._promote_aged_locked()
        if not self._busy and self._waiters:
            self._waiters[0][4].set()

    def release(self) -> None:
        tid = threading.get_ident()
        with self._cond:
            if not self._busy or self._owner_tid != tid:
                return
            self._depth -= 1
            if self._depth <= 0:
                self._busy = False
                self._owner_tid = None
                self._depth = 0
                self._promote_aged_locked()
                if self._waiters:
                    self._waiters[0][4].set()
                self._cond.notify_all()


class WindowFocusGuard:
    """
    Windows ForegroundLockTimeout korumasını güvenli şekilde aşan ve kullanıcı
    oyun dışına (Chrome, Discord vb.) Alt-Tab yaptığında asla odak çalmayan yönetici.
    """

    def __init__(
        self,
        session_hwnds: Optional[Set[int]] = None,
        title_hint: str = "metin2",
        excluded_titles: Tuple[str, ...] = (),
    ) -> None:
        self.session_hwnds: Set[int] = {int(h) for h in (session_hwnds or set()) if h}
        self.title_hint = (title_hint or "metin2").lower()
        self.excluded_titles = excluded_titles
        self._switching_until: float = 0.0
        self._my_pid = os.getpid()
        self._user32 = ctypes.windll.user32

    def register_hwnd(self, hwnd: Optional[int]) -> None:
        if hwnd:
            self.session_hwnds.add(int(hwnd))

    def is_user_in_game(self) -> bool:
        if time.time() < self._switching_until:
            return True
        try:
            fg = win32gui.GetForegroundWindow()
            if not fg:
                return False
            _, pid = win32process.GetWindowThreadProcessId(fg)
            if pid == self._my_pid:
                return False
            if self.session_hwnds:
                return fg in self.session_hwnds
            title = (win32gui.GetWindowText(fg) or "").lower()
            if any(ex in title for ex in self.excluded_titles):
                return False
            return self.title_hint in title
        except Exception:
            return False

    def focus(
        self,
        hwnd: Optional[int],
        force_initial: bool = False,
        urgent: bool = False,
        pre_move_to: Optional[Tuple[int, int]] = None,
    ) -> bool:
        if not hwnd or not win32gui.IsWindow(hwnd):
            return self.is_user_in_game()
        if not force_initial and not self.is_user_in_game():
            return False

        try:
            fg = win32gui.GetForegroundWindow()
            if fg == hwnd:
                return True

            self._switching_until = time.time() + 0.65
            if win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                time.sleep(0.06)

            fg_tid = win32process.GetWindowThreadProcessId(fg)[0] if fg else 0
            cur_tid = win32api.GetCurrentThreadId()
            attached = False
            try:
                if fg_tid and fg_tid != cur_tid:
                    attached = bool(self._user32.AttachThreadInput(cur_tid, fg_tid, True))
                win32gui.BringWindowToTop(hwnd)
                win32gui.SetForegroundWindow(hwnd)
            except Exception:
                pass
            finally:
                if attached:
                    try:
                        self._user32.AttachThreadInput(cur_tid, fg_tid, False)
                    except Exception:
                        pass

            if win32gui.GetForegroundWindow() != hwnd:
                try:
                    win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
                    try:
                        win32gui.SetForegroundWindow(hwnd)
                    finally:
                        win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
                except Exception:
                    pass

            if win32gui.GetForegroundWindow() != hwnd:
                try:
                    self._user32.SwitchToThisWindow(ctypes.c_void_p(hwnd), True)
                except Exception:
                    pass

            switched_t = time.perf_counter()
            if urgent:
                # Minigame vuruşu: Pencere odaklanırken eşzamanlı olarak fareyi diğer pencereye yaklaştır
                if pre_move_to is not None and win32gui.GetForegroundWindow() == hwnd:
                    HumanMouse.fast_glide(pre_move_to[0], pre_move_to[1], duration=_jitter(0.011, 0.002, 0.008, 0.015))
                target_settle = _jitter(0.042, 0.007, 0.030, 0.058)
            else:
                # Klavye girdisi (CAST / CTRL+G+G / ESC): DirectInput odağı için tam oturma payı
                target_settle = _jitter(0.105, 0.014, 0.082, 0.135)

            rem = target_settle - (time.perf_counter() - switched_t)
            if rem > 0.0:
                _precise_sleep(rem)
            return win32gui.GetForegroundWindow() == hwnd
        except Exception as e:
            log.warning(f"[ODAK] Pencere öne getirilemedi (hwnd={hwnd}): {e}")
            return False


class HumanMouse:
    """Kübik Bézier eğrisi + sönümlenen Gauss gürültüsü (jitter) ile insansı fare motoru."""

    @staticmethod
    def _cubic_bezier_path(
        p0: Tuple[int, int],
        p3: Tuple[int, int],
        steps: Optional[int] = None,
        jitter_sigma: float = 0.90,
    ) -> List[Tuple[int, int]]:
        x0, y0 = float(p0[0]), float(p0[1])
        x3, y3 = float(p3[0]), float(p3[1])
        dist = math.hypot(x3 - x0, y3 - y0)
        if dist < 2.0:
            return [(int(round(x3)), int(round(y3)))]

        if steps is None:
            steps = max(7, min(24, int(dist / 18.0)))

        spread = min(80.0, max(10.0, dist * 0.22))
        dx, dy = (x3 - x0), (y3 - y0)
        px, py = -dy / dist, dx / dist  # Doğrultuya dik normal vektör

        side = 1.0 if random.random() < 0.5 else -1.0
        c1x = x0 + dx * random.uniform(0.20, 0.40) + px * side * random.uniform(0.25, 0.95) * spread
        c1y = y0 + dy * random.uniform(0.20, 0.40) + py * side * random.uniform(0.25, 0.95) * spread
        c2x = x0 + dx * random.uniform(0.60, 0.85) + px * (-side * 0.40) * random.uniform(0.20, 0.80) * spread
        c2y = y0 + dy * random.uniform(0.60, 0.85) + py * (-side * 0.40) * random.uniform(0.20, 0.80) * spread

        points: List[Tuple[int, int]] = []
        for i in range(1, steps + 1):
            u = i / steps
            # Minimum-jerk hız eğrisi: t = 3u^2 - 2u^3 (başta ve sonda yavaş, ortada seri)
            t = 3.0 * u * u - 2.0 * u * u * u
            omt = 1.0 - t
            bx = (omt ** 3) * x0 + 3 * (omt ** 2) * t * c1x + 3 * omt * (t ** 2) * c2x + (t ** 3) * x3
            by = (omt ** 3) * y0 + 3 * (omt ** 2) * t * c1y + 3 * omt * (t ** 2) * c2y + (t ** 3) * y3

            envelope = math.sin(math.pi * u)
            nx = max(0, int(round(bx + random.gauss(0.0, jitter_sigma) * envelope)))
            ny = max(0, int(round(by + random.gauss(0.0, jitter_sigma) * envelope)))
            points.append((nx, ny))

        points[-1] = (max(0, int(round(x3))), max(0, int(round(y3))))
        return points

    @classmethod
    def _bezier_curve(cls, p0: Tuple[int, int], p3: Tuple[int, int], steps: int) -> List[Tuple[int, int]]:
        return cls._cubic_bezier_path(p0, p3, steps=steps, jitter_sigma=0.90)

    @classmethod
    def fast_glide(cls, target_x: int, target_y: int, duration: float = 0.011) -> None:
        """Pencere geçişi veya uzun atlamalarda fareyi hızlı Kübik Bézier ile yaklaştırır."""
        tx = max(0, int(round(target_x)))
        ty = max(0, int(round(target_y)))
        cur_x, cur_y = win32api.GetCursorPos()
        dist = math.hypot(tx - cur_x, ty - cur_y)
        if dist < 4.0:
            return
        steps = 3 if dist <= 180.0 else (4 if dist <= 550.0 else 5)
        path = cls._cubic_bezier_path((cur_x, cur_y), (tx, ty), steps=steps, jitter_sigma=0.80)
        step_dt = max(0.0014, float(duration) / max(1, len(path)))
        for px, py in path:
            win32api.SetCursorPos((px, py))
            _precise_sleep(step_dt)
        win32api.SetCursorPos((tx, ty))
        try:
            pydirectinput.moveTo(tx, ty)
        except Exception:
            pass

    @classmethod
    def human_move(
        cls,
        target_x: int,
        target_y: int,
        steps: Optional[int] = None,
        duration_range: Tuple[float, float] = (0.075, 0.145),
    ) -> None:
        current_x, current_y = win32api.GetCursorPos()
        path = cls._cubic_bezier_path((current_x, current_y), (target_x, target_y), steps=steps, jitter_sigma=0.95)
        total_duration = random.uniform(*duration_range)
        step_duration = total_duration / max(1, len(path))
        for px, py in path:
            win32api.SetCursorPos((px, py))
            time.sleep(step_duration)
        win32api.SetCursorPos((max(0, int(target_x)), max(0, int(target_y))))

    @classmethod
    def click(cls, x: int, y: int, button: str = "left", move_first: bool = True, fast: bool = False) -> None:
        jx = max(0, int(x) + random.randint(-2, 2))
        jy = max(0, int(y) + random.randint(-2, 2))
        if move_first:
            if fast:
                cls.human_move(jx, jy, steps=7, duration_range=(0.025, 0.048))
            else:
                cls.human_move(jx, jy, duration_range=(0.065, 0.130))
        pydirectinput.mouseDown(button=button)
        time.sleep(_jitter(0.034, 0.007, 0.022, 0.052) if fast else _jitter(0.075, 0.018, 0.045, 0.125))
        pydirectinput.mouseUp(button=button)

    @classmethod
    def sniper_click(cls, x: int, y: int, button: str = "left") -> None:
        """Minigame vuruşlarında doğrudan, anlık ve keskin tıklama (DirectX/DirectInput uyumlu).
        DirectInput olay kuyruğunu ve Metin2'nin hover tespitini garanti etmek için:
        1. win32api.SetCursorPos (çoklu monitör / 2K DPI koordinat yerleşimi)
        2. pydirectinput.moveTo (DirectX MOUSEEVENTF_MOVE tetiklemesi)
        3. ~9ms hover yerleşme payı (Metin2 frame tick'inin fareyi balık üstünde görmesi için)
        4. pydirectinput.mouseDown
        5. ~26ms basılı tutma payı (oyun motorunun tıklamayı yutmaması için)
        6. pydirectinput.mouseUp
        """
        jx = max(0, int(x) + random.randint(-1, 1))
        jy = max(0, int(y) + random.randint(-1, 1))
        try:
            win32api.SetCursorPos((jx, jy))
        except Exception:
            pass
        pydirectinput.moveTo(jx, jy)
        _precise_sleep(_jitter(0.009, 0.002, 0.006, 0.013))
        pydirectinput.mouseDown(button=button)
        _precise_sleep(_jitter(0.026, 0.004, 0.020, 0.034))
        pydirectinput.mouseUp(button=button)


class HumanKeyboard:
    @staticmethod
    def press(key: str) -> None:
        k = key.lower()
        pydirectinput.keyDown(k)
        try:
            if k == "escape":
                hold = _jitter(0.088, 0.012, 0.068, 0.115)
            elif k == "space":
                hold = _jitter(0.165, 0.020, 0.135, 0.200)
            else:
                hold = _jitter(0.082, 0.014, 0.058, 0.118)
            time.sleep(hold)
        finally:
            pydirectinput.keyUp(k)
        time.sleep(_jitter(0.040, 0.009, 0.024, 0.060))

    @staticmethod
    def hotkey(modifier: str, key: str) -> None:
        mod = modifier.lower()
        k = key.lower()
        pydirectinput.keyDown(mod)
        try:
            time.sleep(_jitter(0.048, 0.012, 0.028, 0.080))
            pydirectinput.keyDown(k)
            try:
                time.sleep(_jitter(0.075, 0.018, 0.048, 0.120))
            finally:
                pydirectinput.keyUp(k)
            time.sleep(_jitter(0.045, 0.011, 0.025, 0.075))
        finally:
            pydirectinput.keyUp(mod)

    @staticmethod
    def safe_mount_cancel(mount_key: str = "g", focus_check: Optional[Callable[[], bool]] = None) -> float:
        """
        Güvenli Binek Animasyon İptali (CTRL+G+G):
        - keyDown('ctrl') -> press('g') -> ~0.28s -> press('g') (Bin ve İn).
        - try...finally bloğu CTRL tuşunun istisna halinde bile bırakılmasını garantiler.
        - Kilidi serbest bıraktıktan sonra istemci tarafında beklenecek (0.52 - 0.74s)
          ağ gecikmesi / server-tick oturma süresini döndürür.
        """
        mk = mount_key.lower()
        if focus_check is not None and not focus_check():
            return _jitter(0.62, 0.05, 0.52, 0.74)

        pydirectinput.keyDown("ctrl")
        try:
            time.sleep(_jitter(0.045, 0.008, 0.030, 0.065))
            if focus_check is None or focus_check():
                pydirectinput.keyDown(mk)
                try:
                    time.sleep(_jitter(0.075, 0.010, 0.055, 0.095))
                finally:
                    pydirectinput.keyUp(mk)
            time.sleep(_jitter(0.275, 0.025, 0.235, 0.325))
            if focus_check is None or focus_check():
                pydirectinput.keyDown(mk)
                try:
                    time.sleep(_jitter(0.075, 0.010, 0.055, 0.095))
                finally:
                    pydirectinput.keyUp(mk)
            time.sleep(_jitter(0.045, 0.008, 0.030, 0.065))
        finally:
            pydirectinput.keyUp("ctrl")

        # Donanım kilidi dışı beklenecek sunucu tick oturma gecikmesi (0.52 - 0.74 sn)
        return _jitter(0.62, 0.05, 0.52, 0.74)
