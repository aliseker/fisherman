"""
=============================================================================
Metin2 Balık Yapboz (Fishing Jigsaw) — Otomasyon ve Görsel Motoru
=============================================================================
Özellikler:
  * "Balık yapboz oyunu" penceresini ekranda otomatik tespit eder (kahverengi başlık barı).
  * 6x4 tahtadaki 24 hücrenin doluluk durumunu mikro-saniyelerde okur.
  * Çekilen parçanın rengini HSV uzayında analiz ederek 6 standart şekil + deluxe şekli tanır.
  * JigsawPolicy (Bellman kesin geriye doğru indirme + açılış kitabı) ile:
      - Tahtaya en uygun hücreye sol tıkla yerleştirir.
      - Veya uymayan / büyük sandık şansını düşüren parçayı sağ tık + "Evet" onayı ile atar.
  * Tahta dolduğunda (24/24) ödülü (Büyük / Orta / Küçük Sandık) sayar ve yeni yapboza geçer.
  * Kullanıcı oyundan çıkarsa (Alt-Tab) güvenle bekler, dışarıya tıklamaz.
=============================================================================
"""
from __future__ import annotations

import ctypes
import logging
import os
import random
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

import cv2
import mss
import numpy as np

# Proje yolları
_base_dir = os.path.dirname(os.path.abspath(__file__))
if _base_dir not in sys.path:
    sys.path.insert(0, _base_dir)

from jigsaw_solver import (
    BUILTIN_SHAPES,
    COLS,
    FULL_MASK,
    NCELL,
    ROWS,
    Action,
    Cells,
    JigsawPolicy,
    ShapeBook,
    popcount,
)
from hardware_manager import (
    HumanMouse,
    WindowFocusGuard,
    ensure_dpi_awareness,
    get_thread_sct,
    thread_grab_bgr,
)

ensure_dpi_awareness()

try:
    import pydirectinput
    pydirectinput.PAUSE = 0.015
except ImportError:
    pydirectinput = None

import win32api
import win32con
import win32gui
import win32process

logger = logging.getLogger("jigsaw")


# ===========================================================================
# İstatistik Veri Sınıfı
# ===========================================================================
@dataclass
class JigsawStats:
    total_games: int = 0
    large_chests: int = 0   # <=10 adım (16.5M Yang)
    med_chests: int = 0     # 11-24 adım (8.0M Yang)
    small_chests: int = 0   # >=25 adım (4.0M Yang)
    total_moves: int = 0
    current_moves: int = 0
    total_won: float = 0.0  # Milyon Yang / 100
    start_time: float = field(default_factory=time.time)

    def record_game(self, moves: int, r_large: float = 16.5, r_med: float = 8.0, r_small: float = 4.0) -> str:
        self.total_games += 1
        self.total_moves += moves
        if moves <= 10:
            self.large_chests += 1
            yang = r_large
            chest_name = "Büyük Sandık (Altın)"
        elif moves <= 24:
            self.med_chests += 1
            yang = r_med
            chest_name = "Orta Sandık"
        else:
            self.small_chests += 1
            yang = r_small
            chest_name = "Küçük Sandık"
        self.total_won += (yang / 100.0)
        return chest_name

    def reset(self) -> None:
        self.total_games = 0
        self.large_chests = 0
        self.med_chests = 0
        self.small_chests = 0
        self.total_moves = 0
        self.current_moves = 0
        self.total_won = 0.0
        self.start_time = time.time()


# ===========================================================================
# Görsel Tespit Motoru (Vision)
# ===========================================================================
class JigsawVision:
    """
    Ekrandaki Metin2 Balık Yapboz Oyunu penceresinin koordinatlarını,
    tahtanın 24 hücresini, çekilen aktif parçayı ve onay penceresini algılar.
    """

    # 7 Şeklin HSV Renk Aralıkları (H, S, V)
    # Metin2'de her parça türü sabit renktedir.
    COLOR_MAP = [
        ("Mavi I (3 dikey)", 0, (100, 150, 120), (130, 255, 255)),
        ("Turuncu 1x1",      1, (10,  150, 140), (23,  255, 255)),
        ("Yeşil L",         2, (40,  150, 140), (70,  255, 255)),
        ("Sarı 7",          3, (24,  150, 140), (38,  255, 255)),
        ("Turkuaz 2x2",     4, (80,  150, 140), (100, 255, 255)),
        ("Kırmızı Z",       5, (0,   150, 140), (8,   255, 255)),
        ("Mor 2x3 (Deluxe)",6, (135, 150, 140), (160, 255, 255)),
    ]

    def __init__(self):
        self.last_bx: int = 0
        self.last_by: int = 0
        self.last_seen: float = 0.0

    @staticmethod
    def grab_screen(bbox: Optional[Tuple[int, int, int, int]] = None) -> np.ndarray:
        sct = get_thread_sct()
        if bbox is not None:
            mon = {
                "left": int(bbox[0]),
                "top": int(bbox[1]),
                "width": max(10, int(bbox[2])),
                "height": max(10, int(bbox[3])),
            }
        else:
            mon = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
        return thread_grab_bgr(mon)

    def find_window(self, target_hwnd: Optional[int] = None) -> Optional[Tuple[int, int]]:
        """
        'Balık yapboz oyunu' penceresinin sol-üst başlık çubuğu konumunu (bx, by) bulur.
        Dönüş: (bx, by) ekran koordinatları veya None
        """
        # Önce son bilinen koordinatın etrafındaki küçük alanı doğrula (düşük CPU)
        if self.last_bx > 0 and self.last_by > 0:
            probe_bbox = (max(0, self.last_bx - 16), max(0, self.last_by - 12), 380, 56)
            probe_im = self.grab_screen(probe_bbox)
            phsv = cv2.cvtColor(probe_im, cv2.COLOR_BGR2HSV)
            pmask = (
                (phsv[:, :, 0] >= 5) & (phsv[:, :, 0] <= 18)
                & (phsv[:, :, 1] >= 110)
                & (phsv[:, :, 2] >= 45) & (phsv[:, :, 2] <= 190)
            )
            pcnts, _ = cv2.findContours(pmask.astype(np.uint8) * 255, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            for c in pcnts:
                x, y, w, h = cv2.boundingRect(c)
                if 250 <= w <= 360 and 12 <= h <= 32:
                    self.last_bx = probe_bbox[0] + x
                    self.last_by = probe_bbox[1] + y
                    self.last_seen = time.time()
                    return (self.last_bx, self.last_by)

        bbox = None
        off_x, off_y = 0, 0
        if target_hwnd and win32gui.IsWindow(target_hwnd) and not win32gui.IsIconic(target_hwnd):
            try:
                rect = win32gui.GetWindowRect(target_hwnd)
                w, h = rect[2] - rect[0], rect[3] - rect[1]
                if w > 200 and h > 200:
                    bbox = (rect[0], rect[1], w, h)
                    off_x, off_y = rect[0], rect[1]
            except Exception:
                pass

        if bbox is None:
            sct = get_thread_sct()
            m = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
            off_x, off_y = m["left"], m["top"]
            im = self.grab_screen((m["left"], m["top"], m["width"], m["height"]))
        else:
            im = self.grab_screen(bbox)

        hsv = cv2.cvtColor(im, cv2.COLOR_BGR2HSV)

        # Kahverengi başlık barı: H: 5..18, S >= 110, V: 45..190
        title_mask = (
            (hsv[:, :, 0] >= 5) & (hsv[:, :, 0] <= 18)
            & (hsv[:, :, 1] >= 110)
            & (hsv[:, :, 2] >= 45) & (hsv[:, :, 2] <= 190)
        )
        cnts, _ = cv2.findContours(title_mask.astype(np.uint8) * 255, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for c in cnts:
            x, y, w, h = cv2.boundingRect(c)
            # Metin2 minigame başlık barı: genişlik ~280..340 px, yükseklik ~14..28 px
            if 250 <= w <= 360 and 12 <= h <= 32:
                bx = off_x + x
                by = off_y + y
                self.last_bx = bx
                self.last_by = by
                self.last_seen = time.time()
                return (bx, by)

        self.last_bx = 0
        self.last_by = 0
        return None

    @staticmethod
    def read_board(im: np.ndarray, bx: int, by: int) -> Tuple[np.ndarray, int, int]:
        """
        6x4 tahtadaki 24 hücrenin doluluğunu okur.
        Dönüş: (board_2d, mask, filled_count)
        """
        hsv = cv2.cvtColor(im, cv2.COLOR_BGR2HSV)
        gx0 = bx + 9
        gy0 = by + 41
        cw = 39.6
        ch = 39.5
        board = np.zeros((ROWS, COLS), dtype=int)
        mask = 0

        H, W = im.shape[:2]
        for r in range(ROWS):
            for c in range(COLS):
                cx = int(gx0 + (c + 0.5) * cw)
                cy = int(gy0 + (r + 0.5) * ch)
                if cy - 6 < 0 or cy + 7 >= H or cx - 6 < 0 or cx + 7 >= W:
                    continue
                patch = hsv[cy - 6:cy + 7, cx - 6:cx + 7]
                s_mean = float(np.mean(patch[:, :, 1]))
                v_mean = float(np.mean(patch[:, :, 2]))
                # Boş hücreler mat gri/siyah (S<50 veya V<50)
                # Dolu parçalar canlı renkli (S>80 ve V>80)
                if s_mean > 80.0 and v_mean > 80.0:
                    board[r, c] = 1
                    mask |= (1 << (r * COLS + c))

        filled_cnt = popcount(mask)
        return board, mask, filled_cnt

    @staticmethod
    def is_discard_dialog_open(im: np.ndarray, bx: int, by: int) -> bool:
        """
        'Bu nesneyi gerçekten düşürmek istiyor musun?' onay diyaloğunun açık olup olmadığını kontrol eder.
        """
        H, W = im.shape[:2]
        y1, y2 = by + 100, by + 125
        x1, x2 = bx + 40, bx + 280
        if y1 < 0 or y2 >= H or x1 < 0 or x2 >= W:
            return False

        patch = im[y1:y2, x1:x2]
        hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
        gray = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY)
        # Diyalog metninin beyaz pikselleri (S < 45, Gray > 180)
        white_pixels = np.sum((hsv[:, :, 1] < 45) & (gray > 180))
        return bool(white_pixels > 80)

    @classmethod
    def detect_active_piece(cls, im: np.ndarray, bx: int, by: int) -> Optional[Tuple[str, int, Cells]]:
        """
        Ekranda tahta DIŞINDA tutulan / çekilen aktif yapboz parçasını algılar.
        Dönüş: (name, shape_id, cells) veya None
        """
        H, W = im.shape[:2]
        hsv = cv2.cvtColor(im, cv2.COLOR_BGR2HSV)

        # 6x4 tahta alanını ve alt sandık ikonunu maskele (hariç tut)
        mask_exclude = np.zeros((H, W), dtype=bool)
        b_y1, b_y2 = max(0, by + 41), min(H, by + 41 + 158)
        b_x1, b_x2 = max(0, bx + 9), min(W, bx + 9 + 238)
        mask_exclude[b_y1:b_y2, b_x1:b_x2] = True

        # Alt sandık slot alanı (bx+265..bx+315, by+141..by+185)
        s_y1, s_y2 = max(0, by + 141), min(H, by + 185)
        s_x1, s_x2 = max(0, bx + 265), min(W, bx + 315)
        mask_exclude[s_y1:s_y2, s_x1:s_x2] = True

        out_mask = ~mask_exclude

        # Şekil renklerini tara
        best_candidate = None
        max_area = 0

        for name, sid, lower, upper in cls.COLOR_MAP:
            # Kırmızı renk HSV'de 0-8 ve 170-180 olarak çift taraflıdır
            if sid == 5:  # Kırmızı Z
                cm1 = cv2.inRange(hsv, np.array((0, 150, 140)), np.array((8, 255, 255)))
                cm2 = cv2.inRange(hsv, np.array((172, 150, 140)), np.array((180, 255, 255)))
                cm = (cm1 | cm2) & out_mask
            else:
                cm = cv2.inRange(hsv, np.array(lower), np.array(upper)) & out_mask

            cnts, _ = cv2.findContours(cm.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            for c in cnts:
                area = cv2.contourArea(c)
                # En az 1 hücre alanı (~400+ px)
                if area > 450:
                    if area > max_area:
                        max_area = area
                        cells = BUILTIN_SHAPES[sid][1]
                        best_candidate = (name, sid, cells)

        return best_candidate

    @staticmethod
    def has_boxes_in_chest_slot(im: np.ndarray, bx: int, by: int) -> bool:
        """
        Alt yuvada (bx+288, by+161) açılmayı bekleyen yapboz sandığı olup olmadığını kontrol eder.
        """
        H, W = im.shape[:2]
        cy, cx = by + 161, bx + 288
        if cy - 10 < 0 or cy + 10 >= H or cx - 10 < 0 or cx + 10 >= W:
            return False
        patch = im[cy - 10:cy + 10, cx - 10:cx + 10]
        # Kutu ikonu yeşilimsi kapak ve turuncu balık içerir (S > 70, V > 70)
        hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
        sat_mean = float(np.mean(hsv[:, :, 1]))
        val_mean = float(np.mean(hsv[:, :, 2]))
        return sat_mean > 60.0 and val_mean > 60.0


# ===========================================================================
# Fare ve Klavye İcra Motoru
# ===========================================================================
class JigsawInput:
    """Metin2 ile %100 uyumlu Kübik Bézier + Gauss Jitter DirectInput tıklama icracısı."""

    @staticmethod
    def move(x: int, y: int) -> None:
        HumanMouse.human_move(int(x), int(y), duration_range=(0.030, 0.055))

    @staticmethod
    def click(x: int, y: int, button: str = "left", fast: bool = True) -> None:
        if pydirectinput:
            HumanMouse.click(int(x), int(y), button=button, move_first=True, fast=fast)
        else:
            jx = max(0, int(x) + random.randint(-1, 1))
            jy = max(0, int(y) + random.randint(-1, 1))
            win32api.SetCursorPos((jx, jy))
            time.sleep(0.025)
            down = win32con.MOUSEEVENTF_LEFTDOWN if button == "left" else win32con.MOUSEEVENTF_RIGHTDOWN
            up = win32con.MOUSEEVENTF_LEFTUP if button == "left" else win32con.MOUSEEVENTF_RIGHTUP
            win32api.mouse_event(down, 0, 0, 0, 0)
            time.sleep(0.04)
            win32api.mouse_event(up, 0, 0, 0, 0)


# ===========================================================================
# Balık Yapboz Ana Otomasyon Yöneticisi (Runner)
# ===========================================================================
class JigsawRunner:
    """
    Arka planda bağımsız bir thread olarak çalışan, yapbozları ardı ardına
    en karlı şekilde çözen otomasyon motoru.
    """

    def __init__(
        self,
        target_hwnd: Optional[int] = None,
        box_cost: float = 0.60,
        exact_k: int = 12,
        discard_bad_early: bool = True,
        on_status_change: Optional[Callable[[str, str], None]] = None,
        on_log: Optional[Callable[[str], None]] = None,
    ):
        self.target_hwnd = target_hwnd
        self.box_cost = box_cost
        self.exact_k = min(14, max(6, int(exact_k)))
        self.discard_bad_early = discard_bad_early
        self.on_status_change = on_status_change
        self.on_log = on_log

        self.book = ShapeBook(os.path.join(_base_dir, "jigsaw_shapes.json"))
        self.policy = JigsawPolicy(
            self.book,
            r_large=16.5,
            r_med=8.0,
            r_small=4.0,
            box_cost=self.box_cost,
            exact_k=self.exact_k,
            discard_bad_early=self.discard_bad_early,
        )
        self.vision = JigsawVision()
        self.stats = JigsawStats()
        self._focus_guard = WindowFocusGuard({target_hwnd} if target_hwnd else None)

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def set_strategy(self, mode: str) -> None:
        """
        Strateji kipi:
          - 'large': Maksimum Büyük Sandık (<=10 Adım) — λ = 0.40, agresif eleme
          - 'balanced': Dengeli Yang Getirisi (Önerilen) — λ = 0.60, Büyük+Orta ~%90
          - 'fast': Hızlı Tamamlama (Az Atma) — λ = 1.00
        """
        if mode == "large":
            self.box_cost = 0.40
            self.discard_bad_early = True
        elif mode == "fast":
            self.box_cost = 1.00
            self.discard_bad_early = False
        else:  # balanced
            self.box_cost = 0.60
            self.discard_bad_early = True

        self.policy = JigsawPolicy(
            self.book,
            r_large=16.5,
            r_med=8.0,
            r_small=4.0,
            box_cost=self.box_cost,
            exact_k=self.exact_k,
            discard_bad_early=self.discard_bad_early,
        )
        self._log(f"🧩 Strateji güncellendi: {mode.upper()} (λ={self.box_cost:.2f})")

    def _log(self, msg: str) -> None:
        logger.info(msg)
        if self.on_log:
            try:
                self.on_log(msg)
            except Exception:
                pass

    def _set_status(self, text: str, color: str = "#ffffff") -> None:
        if self.on_status_change:
            try:
                self.on_status_change(text, color)
            except Exception:
                pass

    def is_running(self) -> bool:
        return self._running

    def start(self) -> bool:
        if self._running:
            return True
        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name="JigsawRunnerThread", daemon=True)
        self._thread.start()
        self._log("▶ Yapboz Çözücü başlatıldı!")
        self._set_status("Çalışıyor", "#00ff88")
        return True

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        self._stop_event.set()
        self._log("⏹ Yapboz Çözücü durduruldu.")
        self._set_status("Durduruldu", "#ff6b6b")

    def _is_foreground(self) -> bool:
        if not self.target_hwnd:
            return self._focus_guard.is_user_in_game()
        try:
            fg = win32gui.GetForegroundWindow()
            return fg == self.target_hwnd
        except Exception:
            return True

    def _focus_window(self) -> bool:
        if not self.target_hwnd or not win32gui.IsWindow(self.target_hwnd):
            return self._focus_guard.is_user_in_game()
        self._focus_guard.register_hwnd(self.target_hwnd)
        return self._focus_guard.focus(self.target_hwnd, force_initial=True)

    def _safe_discard_point(self, bx: int, by: int) -> Tuple[int, int]:
        if self.target_hwnd and win32gui.IsWindow(self.target_hwnd):
            try:
                wx0, _, wx1, _ = win32gui.GetWindowRect(self.target_hwnd)
                if bx - 28 >= wx0 + 10:
                    return (bx - 25, by + 90)
                if bx + 345 <= wx1 - 10:
                    return (bx + 345, by + 90)
            except Exception:
                pass
        if bx >= 35:
            return (bx - 25, by + 90)
        return (bx + 345, by + 90)

    def _loop(self) -> None:
        moves_used = 0
        last_board_mask = -1
        draw_attempts = 0
        consecutive_full_checks = 0

        self._log("🔍 'Balık yapboz oyunu' penceresi aranıyor...")

        while self._running and not self._stop_event.is_set():
            time.sleep(0.05)

            # 1) Pencereyi bul
            pos = self.vision.find_window(self.target_hwnd)
            if not pos:
                self._set_status("Pencere Bekleniyor...", "#ffd700")
                time.sleep(0.4)
                continue

            bx, by = pos
            im = self.vision.grab_screen()

            # 2) Discard onay diyaloğu açık mı? ("Evet" / "Hayır")
            if self.vision.is_discard_dialog_open(im, bx, by):
                self._log("⚠️ Atma onay kutusu açık -> 'Evet' butonuna tıklanıyor.")
                evet_x = bx + 114
                evet_y = by + 145
                if self._focus_window():
                    JigsawInput.click(evet_x, evet_y, button="left", fast=True)
                time.sleep(random.uniform(0.12, 0.20))
                continue

            # 3) 6x4 Tahtayı oku
            board, mask, filled_cnt = self.vision.read_board(im, bx, by)

            # Tahta tamamen doldu mu? (24/24)
            if mask == FULL_MASK:
                consecutive_full_checks += 1
                if consecutive_full_checks >= 2:
                    chest_name = self.stats.record_game(moves_used)
                    self._log(f"🎉 YAPBOZ BİTTİ! Toplam {moves_used} Adım -> Ödül: {chest_name}")
                    self._log(
                        f"📊 Genel Durum: {self.stats.total_games} Oyun | "
                        f"Büyük: {self.stats.large_chests} | Orta: {self.stats.med_chests} | Küçük: {self.stats.small_chests} | "
                        f"Kazanılan: {self.stats.total_won:.3f} Won"
                    )
                    self.policy.reset()
                    moves_used = 0
                    last_board_mask = -1
                    consecutive_full_checks = 0
                    # Metin2'nin ödülü vermesi ve tahtanın sıfırlanması için bekle
                    time.sleep(0.70)
                continue
            else:
                consecutive_full_checks = 0

            # Yeni tahtaya geçilmişse (önceki tahta doluydu, şimdi boş)
            if filled_cnt == 0 and last_board_mask > 0:
                self._log("🆕 Yeni yapboz tahtası başladı (0/24 hücre).")
                moves_used = 0
                self.policy.reset()

            last_board_mask = mask
            self.stats.current_moves = moves_used

            # 4) Aktif (tutulan / çekilen) parça var mı?
            active = self.vision.detect_active_piece(im, bx, by)

            if active is None:
                # Elimizde parça yok -> Sandıktan yeni parça çek
                # Sandık çekme butonu: (bx + 288, by + 161)
                chest_btn_x = bx + 288
                chest_btn_y = by + 161

                draw_attempts += 1
                if draw_attempts > 12:
                    self._log("⚠️ Sandıktan parça çekilemedi. Sandık sayısı bitmiş olabilir mi?")
                    time.sleep(0.8)
                    draw_attempts = 0
                    continue

                if self._focus_window():
                    JigsawInput.click(chest_btn_x, chest_btn_y, button="left", fast=True)
                time.sleep(random.uniform(0.12, 0.20))
                continue

            # Parça başarıyla çekildi / görüldü!
            draw_attempts = 0
            p_name, p_sid, p_cells = active

            # 5) Matematiksel En Optimal Kararı Sor (JigsawPolicy)
            action: Action = self.policy.choose(mask, moves_used, p_cells)

            if action.kind == "place":
                r0, c0 = action.r0, action.c0
                # Hücre (r0, c0) merkez koordinatları
                cw = 39.6
                ch = 39.5
                cell_x = bx + 9 + int((c0 + 0.5) * cw)
                cell_y = by + 41 + int((r0 + 0.5) * ch)

                self._log(f"📥 [{moves_used + 1}. Adım] {p_name} -> Tahtaya Yerleştir: Satır {r0}, Sütun {c0} ({action.note})")
                if self._focus_window():
                    JigsawInput.click(cell_x, cell_y, button="left", fast=True)
                    moves_used += 1
                time.sleep(random.uniform(0.14, 0.22))

            elif action.kind == "discard":
                self._log(f"🗑️ [{moves_used + 1}. Adım] {p_name} -> ATILIYOR (Sağ Tık + Onay)! ({action.note})")
                if not self._focus_window():
                    time.sleep(0.2)
                    continue
                # Parçayı sağ tıklayarak yok etme diyaloğunu tetikle
                discard_x, discard_y = self._safe_discard_point(bx, by)
                JigsawInput.click(discard_x, discard_y, button="right", fast=True)
                time.sleep(random.uniform(0.12, 0.18))

                # Diyalog açılmasını bekle ve 'Evet' butonuna tıkla
                im_diag = self.vision.grab_screen()
                if self.vision.is_discard_dialog_open(im_diag, bx, by):
                    evet_x = bx + 114
                    evet_y = by + 145
                    JigsawInput.click(evet_x, evet_y, button="left", fast=True)
                    moves_used += 1
                    time.sleep(random.uniform(0.12, 0.20))
                else:
                    time.sleep(0.10)
                    im_diag2 = self.vision.grab_screen()
                    if self.vision.is_discard_dialog_open(im_diag2, bx, by):
                        evet_x = bx + 114
                        evet_y = by + 145
                        JigsawInput.click(evet_x, evet_y, button="left", fast=True)
                        moves_used += 1
                        time.sleep(random.uniform(0.12, 0.20))

