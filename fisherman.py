"""
=============================================================================
Metin2 Fishing Helper - Gelişmiş Multi-Thread Mimari
=============================================================================
Tamamen Asenkron Producer-Consumer (Çoklu İşlemci) Mimarisi.
=============================================================================
"""

import time
import random
import math
import difflib
import threading
import queue
import logging
import sys
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional, Tuple, List

import cv2
import numpy as np
import mss
import pydirectinput
pydirectinput.PAUSE = 0  # CRITICAL: Disable 0.1s default pause between actions
import win32gui
import win32con

# OCR
try:
    import easyocr
    _OCR_BACKEND = "easyocr"
except ImportError:
    easyocr = None
    _OCR_BACKEND = "none"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("FisherApp_MT.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("FisherAppMT")

class AppState(Enum):
    CAST          = auto()
    DECISION      = auto()
    MINIGAME      = auto()
    ANIM_CANCEL   = auto()
    REST          = auto()
    STOPPED       = auto()

@dataclass
class FisherConfig:
    game_window_title: str = "metin2"
    bait_key: str = "1"
    cast_key: str = "space"
    chat_roi: dict = field(default_factory=lambda: {"top": 620, "left": 130, "width": 500, "height": 70})
    minigame_roi: dict = field(default_factory=lambda: {"top": 200, "left": 400, "width": 300, "height": 300})
    whitelist: dict = field(default_factory=dict)
    fishing_close_key: str = "escape"
    mount_key: str = "g"
    farm_duration_minutes: int = 90
    rest_duration_minutes: int = 5
    circle_radius: int = 45
    minigame_fps_target: int = 60

class HumanMouse:
    @staticmethod
    def _bezier_curve(p0: Tuple[int, int], p3: Tuple[int, int], steps: int) -> List[Tuple[int, int]]:
        x0, y0 = p0
        x3, y3 = p3
        dx = x3 - x0
        dy = y3 - y0
        ctrl_x1 = x0 + dx * 0.33 + random.randint(-20, 20)
        ctrl_y1 = y0 + dy * 0.33 + random.randint(-20, 20)
        ctrl_x2 = x0 + dx * 0.66 + random.randint(-20, 20)
        ctrl_y2 = y0 + dy * 0.66 + random.randint(-20, 20)
        
        points = []
        for i in range(steps + 1):
            t = i / steps
            x = int((1 - t)**3 * x0 + 3 * (1 - t)**2 * t * ctrl_x1 + 3 * (1 - t) * t**2 * ctrl_x2 + t**3 * x3)
            y = int((1 - t)**3 * y0 + 3 * (1 - t)**2 * t * ctrl_y1 + 3 * (1 - t) * t**2 * ctrl_y2 + t**3 * y3)
            points.append((x, y))
        return points

    @staticmethod
    def human_move(target_x: int, target_y: int, steps: int = 10, duration_range: Tuple[float, float] = (0.10, 0.20)) -> None:
        """Anti-Cheat Human Mouse Movement without teleporting."""
        current_x, current_y = pydirectinput.position()
        path = HumanMouse._bezier_curve((current_x, current_y), (target_x, target_y), steps)
        total_duration = random.uniform(*duration_range)
        step_duration = total_duration / len(path)
        for point in path:
            px = max(0, point[0] + random.randint(-2, 2))
            py = max(0, point[1] + random.randint(-2, 2))
            pydirectinput.moveTo(px, py)
            time.sleep(step_duration)
        pydirectinput.moveTo(target_x, target_y)

    @staticmethod
    def click(x: int, y: int, button: str = "left", move_first: bool = True, fast: bool = False) -> None:
        if move_first:
            if fast:
                pydirectinput.moveTo(x, y)
                time.sleep(0.01)  # Oyunun fare imlecinin yeni konumunu algılaması için mikro saniye bekle
            else:
                HumanMouse.human_move(x, y)
        pydirectinput.mouseDown(button=button)
        time.sleep(random.uniform(0.02, 0.05) if fast else random.uniform(0.05, 0.15))
        pydirectinput.mouseUp(button=button)

class HumanKeyboard:
    @staticmethod
    def press(key: str) -> None:
        pydirectinput.keyDown(key)
        time.sleep(random.uniform(0.05, 0.15))
        pydirectinput.keyUp(key)
        time.sleep(random.uniform(0.02, 0.08))

    @staticmethod
    def hotkey(modifier: str, key: str) -> None:
        pydirectinput.keyDown(modifier)
        time.sleep(random.uniform(0.03, 0.08))
        pydirectinput.keyDown(key)
        time.sleep(random.uniform(0.05, 0.12))
        pydirectinput.keyUp(key)
        time.sleep(random.uniform(0.03, 0.07))
        pydirectinput.keyUp(modifier)

class ScreenCapture:
    def __init__(self):
        self._sct = mss.mss()

    def capture(self, region: dict) -> np.ndarray:
        screenshot = self._sct.grab(region)
        return cv2.cvtColor(np.array(screenshot), cv2.COLOR_BGRA2BGR)

class ChatReader:
    def __init__(self, config: FisherConfig):
        self.config = config
        self.capture = ScreenCapture()
        self._backend = _OCR_BACKEND
        self._reader = None
        self._last_chat_text = ""
        if self._backend == "easyocr":
            self._reader = easyocr.Reader(["tr", "en"], gpu=False, verbose=False)

    def read_chat(self) -> str:
        frame = self.capture.capture(self.config.chat_roi)
        # 1. Görüntüyü 3 kat büyüt (OCR doğruluğunu artırır)
        resized = cv2.resize(frame, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
        # 2. Gri tonlamaya çevir
        gray = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY)
        # 3. Metni siyah, arkaplanı beyaz yapmak için Otsu & Inverted Threshold
        _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
        if self._backend == "easyocr" and self._reader:
            results = self._reader.readtext(thresh, detail=1)
            results.sort(key=lambda r: r[0][0][1])
            return "\n".join(r[1].lower().strip() for r in results if r[1].strip())
        return ""

    def snapshot_baseline(self) -> None:
        self._last_chat_text = self.read_chat()
        log.info("[SNAPSHOT] Chat referansı alındı.")

    def _normalize(self, text: str) -> str:
        """OCR hata duzeltme: rakam/sembol harf donusumu + kucuk harf."""
        t = text.lower()
        for k, v in [("1","l"),("0","o"),("5","s"),("$","s"),("4","a"),("@","a"),
                     ("3","e"),("!","i"),("I","i"),("s","s"),("g","g"),("u","u"),("o","o"),("c","c")]:
            t = t.replace(k, v)
        return t

    def _best_score(self, keyword: str, text: str) -> float:
        """Normalize edilmis keyword ile text arasindaki en iyi benzerlik skorunu dondurur."""
        norm_text = self._normalize(text)
        norm_kw   = self._normalize(keyword)

        if norm_kw in norm_text:
            return 1.0

        words    = norm_text.split()
        kw_words = norm_kw.split()
        best     = 0.0

        if len(kw_words) == 1:
            for w in words:
                s = difflib.SequenceMatcher(None, norm_kw, w).ratio()
                if s > best:
                    best = s
        else:
            for i in range(max(1, len(words) - len(kw_words) + 1)):
                window = " ".join(words[i:i+len(kw_words)])
                s = difflib.SequenceMatcher(None, norm_kw, window).ratio()
                if s > best:
                    best = s
        return best

    def check_fish_bite(self, text: str) -> str:
        if not text:
            log.warning("[WHITELIST UNKNOWN] Chat bos okundu! Garantiyeye alip OYNUYORUZ!")
            return "play"

        MIN_THRESHOLD = 0.72

        best_keyword      = None
        best_should_catch = False
        best_score        = 0.0

        for keyword, should_catch in self.config.whitelist.items():
            score = self._best_score(keyword, text)
            log.debug(f"[SCORE] '{keyword}' -> {score:.3f}")
            if score > best_score:
                best_score        = score
                best_keyword      = keyword
                best_should_catch = should_catch

        if best_keyword and best_score >= MIN_THRESHOLD:
            if best_should_catch:
                log.info(f"[WHITELIST MATCH] '{best_keyword}' (skor: {best_score:.2f}) yakalanacak!")
                return "play"
            else:
                log.info(f"[WHITELIST SKIP] '{best_keyword}' (skor: {best_score:.2f}) istenmiyor. Iptal.")
                return "skip"

        log.warning(f"[WHITELIST UNKNOWN] Tanimlanamayan metin (en iyi: '{best_keyword}' @ {best_score:.2f}). OYNUYORUZ! ({text[:50]})")
        return "play"

# ===========================================================================
# MULTI-THREAD MINIGAME CORE (Producer-Consumer)
# ===========================================================================
class MinigameController:
    """Manages the 4-thread producer-consumer architecture for the minigame."""
    def __init__(self, config: FisherConfig, chat_reader: Optional[ChatReader] = None):
        self.config = config
        self.chat_reader = chat_reader
        self.skip_fish = False
        self.mouse = HumanMouse()
        self.frame_queue = queue.Queue(maxsize=3)
        self.current_fish_pos = None
        self.fish_velocity = (0.0, 0.0)
        self.last_fish_pos = None
        self.last_fish_time = 0.0
        self.fish_pos_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.hits = 0
        self.hits_lock = threading.Lock()
        self.latest_hsv = None
        
    def _is_minigame_closed(self, hsv: np.ndarray) -> bool:
        """Checks if the window is closed based on golden UI icons (Clock & Fish)."""
        fh, fw = hsv.shape[:2]
        gold_lower = np.array([10, 80, 80])
        gold_upper = np.array([40, 255, 255])
        
        # Sadece ikonların olduğu köşelere bak (Sağ üst ve Sol alt)
        roi_top_right = hsv[:int(fh*0.25), int(fw*0.65):]
        roi_bottom_left = hsv[int(fh*0.75):, :int(fw*0.35)]
        
        gold_pixels = cv2.countNonZero(cv2.inRange(roi_top_right, gold_lower, gold_upper))
        gold_pixels += cv2.countNonZero(cv2.inRange(roi_bottom_left, gold_lower, gold_upper))
        
        # Altın sarısı ikon pikselleri kaybolduysa oyun bitmiştir
        return gold_pixels < 30
        
    def thread1_producer(self):
        """Thread 1 (Producer): Captures minigame ROI with mss continually."""
        log.info("[THREAD 1] Görüntü Yakalayıcı (Producer) başlatıldı.")
        with mss.mss() as sct:
            monitor = self.config.minigame_roi
            interval = 1.0 / self.config.minigame_fps_target
            while not self.stop_event.is_set():
                start = time.time()
                frame = np.array(sct.grab(monitor))
                bgr = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
                
                if self.frame_queue.full():
                    try: self.frame_queue.get_nowait()
                    except queue.Empty: pass
                self.frame_queue.put(bgr)
                
                elapsed = time.time() - start
                time.sleep(max(0, interval - elapsed))

    def thread2_consumer1(self):
        """Thread 2 (Consumer 1): Extracts fish coordinates (X, Y) via Grayscale."""
        log.info("[THREAD 2] Koordinat İzleyici (Consumer 1) başlatıldı. (Grayscale FOOLPROOF Modu)")
        
        while not self.stop_event.is_set():
            try:
                frame = self.frame_queue.get(timeout=0.5)
            except queue.Empty:
                continue
                
            hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
            self.latest_hsv = hsv  # Share for Thread 3
            
            # Check if closed
            if self._is_minigame_closed(hsv):
                log.info("[THREAD 2] Minigame penceresi kapandı (Mavi su yok).")
                self.stop_event.set()
                break
                
            # FOOLPROOF FISH DETECTION (Value Channel Thresholding)
            # Grayscale hatalıydı çünkü Mavi ve Kırmızı renkleri griye çevirirken karanlıklaştırıyordu.
            # Bunun yerine HSV'nin 'Value' (Parlaklık - Max(R,G,B)) kanalını kullanıyoruz.
            # Koyu mavi suyun Value değeri ~100'dür (Mavisi parlaktır).
            # Kırmızı çemberin Value değeri ~255'tir (Kırmızısı parlaktır).
            # Balığın Value değeri ise ~30'dur (Hiçbir rengi yoktur, simsiyahtır).
            v_channel = hsv[:, :, 2]
            fh, fw = v_channel.shape
            
            # Sadece suyun olduğu GÜVENLİ BÖLGEYİ belirle (Kenarlıklar ve arayüz kesinlikle hariç)
            valid_area = np.zeros_like(v_channel)
            valid_area[35:int(fh * 0.80), 30:fw-30] = 255  # Sol üst/sağ üst köşelerdeki gölgeler girmesin diye iyice daralttık
            valid_area[:int(fh * 0.32), int(fw * 0.60):] = 0 # Sağ üstteki 0/3 skor tablosunu tamamen sil
            
            # Güvenli bölge içindeki EN KARANLIK pikselin Value değerini bul (Kesinlikle balıktır)
            min_val, _, _, _ = cv2.minMaxLoc(v_channel, mask=valid_area)
            
            # Balık gerçekten simsiyah olduğu için toleransı 35'ten 20'ye düşürdük (Sadece tam siyahları alacak)
            _, fish_mask = cv2.threshold(v_channel, min_val + 20, 255, cv2.THRESH_BINARY_INV)
            fish_mask = cv2.bitwise_and(fish_mask, valid_area)
            
            # Ufak gürültüleri temizle
            kernel = np.ones((3, 3), np.uint8)
            fish_mask = cv2.erode(fish_mask, kernel, iterations=1)
            fish_mask = cv2.dilate(fish_mask, kernel, iterations=2)
            
            contours, _ = cv2.findContours(fish_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            
            if contours:
                # Sadece makul boyutlardaki gölgeleri (balığı) dikkate al. (Çok küçük gürültüleri veya çok büyük dalgaları yoksay)
                valid_contours = [c for c in contours if 30 < cv2.contourArea(c) < 1200]
                if valid_contours:
                    c = max(valid_contours, key=cv2.contourArea)
                    
                    # Bounding Box (Kutu) Merkezi: Moments(ağırlık merkezi) kuyruk oynadığında kayabilir.
                    # Kutu merkezi ise balığın tam gövdesine odaklanır.
                    x, y, w, h = cv2.boundingRect(c)
                    cx = x + w // 2
                    cy = y + h // 2
                    now_t = time.time()
                    with self.fish_pos_lock:
                        if self.last_fish_pos and (now_t - self.last_fish_time) < 0.5:
                            dt = now_t - self.last_fish_time
                            if dt > 0.005:  # Aşırı küçük zaman farklarında hız patlamasını engelle
                                raw_vx = (cx - self.last_fish_pos[0]) / dt
                                raw_vy = (cy - self.last_fish_pos[1]) / dt
                                
                                # Hız Yumuşatma (Exponential Moving Average - Low Pass Filter)
                                # 1 piksellik kamera/algılama titremelerinin hızı bozmasını engeller
                                alpha = 0.4
                                vx = (1.0 - alpha) * self.fish_velocity[0] + alpha * raw_vx
                                vy = (1.0 - alpha) * self.fish_velocity[1] + alpha * raw_vy
                            else:
                                vx, vy = self.fish_velocity
                        else:
                            vx, vy = 0.0, 0.0
                            
                        self.current_fish_pos = (cx, cy)
                        self.fish_velocity = (vx, vy)
                        self.last_fish_pos = (cx, cy)
                        self.last_fish_time = now_t

    def thread3_consumer2(self):
        """Thread 3 (Consumer 2): Watches for red circle and clicks the fish."""
        log.info("[THREAD 3] Tetikleyici & Tıklayıcı (Consumer 2) başlatıldı.")
        # Red/Orange ranges - 2. fotoğraftaki çok açık pembe/kırmızı renk için iyice esnetildi
        red_lower1 = np.array([0, 50, 120])
        red_upper1 = np.array([20, 255, 255])
        red_lower2 = np.array([160, 50, 120])
        red_upper2 = np.array([180, 255, 255])
        
        last_click = 0.0
        click_cooldown = 0.4  # Her kırmızı çember yandığında (ıskalasa bile) çabuk toparlaması için düşürüldü
        
        while not self.stop_event.is_set():
            hsv = getattr(self, "latest_hsv", None)
            if hsv is None:
                time.sleep(0.01)
                continue
                
            # Alt kısımdaki kırmızı süre barını (time bar) görmezden gelmek için maskeyi kırpıyoruz
            safe_h = int(hsv.shape[0] * 0.82)
            hsv_safe = hsv[:safe_h, :]
            
            mask1 = cv2.inRange(hsv_safe, red_lower1, red_upper1)
            mask2 = cv2.inRange(hsv_safe, red_lower2, red_upper2)
            red_mask = cv2.bitwise_or(mask1, mask2)
            
            # Üst köşedeki sarı/turuncu hit sayacını (0/3 yazan ikon) yoksaymak için üst %20'yi kırpıyoruz
            red_mask[:int(hsv.shape[0] * 0.20), :] = 0
            
            # ROI içinde (süre barı hariç) kırmızı çember yandı mı?
            red_pixels = cv2.countNonZero(red_mask)
            is_red = red_pixels > 15  # Eşik değeri düşürüldü
            
            with self.fish_pos_lock:
                fish_pos = self.current_fish_pos
                fish_vel = getattr(self, "fish_velocity", (0.0, 0.0))
            
            if is_red:
                now = time.time()
                if now - last_click >= click_cooldown:
                    if fish_pos is not None:
                        # Aimbot Tahmini: Hedefi ucundan kaçırmaması için tahmin süresi (0.03 sn) iyice kısaltıldı
                        pred_delay = 0.03
                        tgt_x = int(fish_pos[0] + fish_vel[0] * pred_delay)
                        tgt_y = int(fish_pos[1] + fish_vel[1] * pred_delay)
                        
                        # Sınır dışına çıkmasını engelle
                        tgt_x = max(0, min(self.config.minigame_roi["width"], tgt_x))
                        tgt_y = max(0, min(self.config.minigame_roi["height"], tgt_y))
                        
                        click_type = "Tahminli Balık"
                    else:
                        # Balık takip edilemediyse kırmızı çemberin merkezine tıkla (B planı)
                        M = cv2.moments(red_mask)
                        if M["m00"] > 0:
                            tgt_x = int(M["m10"] / M["m00"])
                            tgt_y = int(M["m01"] / M["m00"])
                        else:
                            tgt_x = self.config.minigame_roi["width"] // 2
                            tgt_y = self.config.minigame_roi["height"] // 2
                        click_type = "Çember Merkezi"
                        
                    screen_x = self.config.minigame_roi["left"] + tgt_x
                    screen_y = self.config.minigame_roi["top"] + tgt_y
                    
                    log.info(f"[THREAD 3] TIKLANIYOR! Hedef: {click_type}, Koordinat: ({tgt_x}, {tgt_y})")
                    self.mouse.click(screen_x, screen_y, fast=True)
                    last_click = time.time()
                    
                    with self.hits_lock:
                        self.hits += 1
                        # Oyun kapanana kadar tıklamaya devam edecek. (Önceden 3 miss atınca çıkıyordu)
                        log.info(f"[THREAD 3] Tıklama yapıldı (Toplam deneme: {self.hits})")
                            
            time.sleep(0.01)

    def thread4_chat_reader(self):
        """Thread 4: Reads chat in parallel to avoid delaying minigame start."""
        if not self.chat_reader: return
        log.info("[THREAD 4] Minigame oynanırken arka planda Chat okunuyor...")
        time.sleep(0.5) # Yazının netleşmesi için ufak bekleme
        current_text = self.chat_reader.read_chat()
        if not current_text.strip(): return
        
        log.info(f"[OCR] Okunan Chat: '{current_text.replace(chr(10), ' ')}'")
        decision = self.chat_reader.check_fish_bite(current_text)
        if decision == "skip":
            log.info("[THREAD 4] DİKKAT: İstenmeyen balık! Minigame anında iptal ediliyor.")
            self.skip_fish = True
            self.stop_event.set()

    def play(self):
        """Starts all 4 threads and orchestrates the Minigame."""
        self.stop_event.clear()
        self.hits = 0
        self.current_fish_pos = None
        self.latest_hsv = None
        self.skip_fish = False
        
        while not self.frame_queue.empty():
            try: self.frame_queue.get_nowait()
            except queue.Empty: pass
            
        t1 = threading.Thread(target=self.thread1_producer, daemon=True)
        t2 = threading.Thread(target=self.thread2_consumer1, daemon=True)
        t3 = threading.Thread(target=self.thread3_consumer2, daemon=True)
        t4 = threading.Thread(target=self.thread4_chat_reader, daemon=True)
        
        t1.start(); t2.start(); t3.start(); t4.start()
        
        self.stop_event.wait(timeout=16.0)
        if not self.stop_event.is_set():
            log.warning("[MINIGAME] Zaman aşımı (16sn)! Thread'ler zorla durduruluyor.")
            self.stop_event.set()
            
        t1.join(); t2.join(); t3.join(); t4.join()
        log.info(f"[MINIGAME] Sonlandırıldı. Toplam başarılı vuruş: {self.hits}")

# ===========================================================================
# BÖLÜM 6: DURUM MAKİNESİ (STATE MACHINE)
# ===========================================================================
class FishingHelper:
    def __init__(self, config: Optional[FisherConfig] = None):
        self.config = config or FisherConfig()
        self.state = AppState.CAST
        self.running = False
        self.chat_reader = ChatReader(self.config)
        self.keyboard = HumanKeyboard()
        self.stats = {"total_casts": 0, "hits": 0, "misses": 0, "valuable_fish": 0, "worthless_fish": 0, "session_start": time.time()}
        self._farm_start_time = time.time()

    def _is_farm_time_up(self) -> bool:
        elapsed_min = (time.time() - self._farm_start_time) / 60.0
        return elapsed_min >= self.config.farm_duration_minutes

    def run(self):
        self.running = True
        log.info("Metin2 Fishing Bot (Gelişmiş Multi-Thread Mimari) başlatıldı.")
        while self.running:
            try:
                if self.state == AppState.CAST:
                    self.state = self.state_cast()
                elif self.state == AppState.DECISION:
                    self.state = self.state_decision()
                elif self.state == AppState.MINIGAME:
                    controller = MinigameController(self.config, self.chat_reader)
                    controller.play()
                    if controller.skip_fish:
                        self.stats["worthless_fish"] += 1
                        self.keyboard.press(self.config.fishing_close_key)
                    else:
                        self.stats["valuable_fish"] += 1
                        self.stats["hits"] += controller.hits
                    self.state = AppState.ANIM_CANCEL
                elif self.state == AppState.ANIM_CANCEL:
                    self.state = self.state_anim_cancel()
                elif self.state == AppState.REST:
                    log.info(f"[REST] {self.config.rest_duration_minutes} dakika dinleniyor...")
                    time.sleep(self.config.rest_duration_minutes * 60)
                    self._farm_start_time = time.time()
                    self.state = AppState.CAST
                elif self.state == AppState.STOPPED:
                    break
                time.sleep(0.1)
            except KeyboardInterrupt:
                break
            except Exception as e:
                log.error(f"[HATA] Beklenmeyen hata: {e}", exc_info=True)
                break
        self.running = False
        log.info(f"Bot kapatıldı. Stats: {self.stats}")

    def state_cast(self) -> AppState:
        log.info("[STATE 1] Olta atılıyor... (Mod: Whitelist)")
        self.stats["total_casts"] += 1
        self.keyboard.press(self.config.bait_key)
        time.sleep(random.uniform(0.3, 0.6))
        
        self.keyboard.press(self.config.cast_key)
        
        time.sleep(2.0 + random.uniform(0.2, 0.6))
        return AppState.DECISION

    def state_decision(self) -> AppState:
        log.info("[STATE 2] Balık bekleniyor (Minigame penceresi aranıyor)...")
        wait_start = time.time()
        
        with mss.mss() as sct:
            while time.time() - wait_start < 60.0:
                if not self.running: return AppState.STOPPED
                if self._is_farm_time_up(): return AppState.REST
                
                # Minigame ekranının açılıp açılmadığını altın sarısı UI ikonlarından anlıyoruz
                img = np.array(sct.grab(self.config.minigame_roi))
                hsv = cv2.cvtColor(cv2.cvtColor(img, cv2.COLOR_BGRA2BGR), cv2.COLOR_BGR2HSV)
                
                fh, fw = hsv.shape[:2]
                gold_lower = np.array([10, 80, 80])
                gold_upper = np.array([40, 255, 255])
                
                roi_top_right = hsv[:int(fh*0.25), int(fw*0.65):]
                roi_bottom_left = hsv[int(fh*0.75):, :int(fw*0.35)]
                
                gold_pixels = cv2.countNonZero(cv2.inRange(roi_top_right, gold_lower, gold_upper))
                gold_pixels += cv2.countNonZero(cv2.inRange(roi_bottom_left, gold_lower, gold_upper))
                
                if gold_pixels > 50: # Altın sarısı ikonlar belirdiyse minigame açılmış demektir
                    log.info("[STATE 2] Minigame açıldı! ZAMAN KAYBETMEDEN OYUNA BAŞLANIYOR!")
                    # Chat okuma işi oyun oynanırken arka planda (Thread 4) yapılacak!
                    return AppState.MINIGAME
                        
                time.sleep(0.5)
                
        log.warning("[STATE 2] 60sn geçti, balık vurmadı. İptal.")
        self.keyboard.press(self.config.fishing_close_key)
        return AppState.ANIM_CANCEL

    def state_anim_cancel(self) -> AppState:
        log.info("[STATE 4] Animasyon iptali yapılıyor (Ctrl+G)...")
        # Minigame biter bitmez oltayı çekme animasyonunu İPTAL ETMEK için hiç beklemeden bineğe biniyoruz
        time.sleep(random.uniform(0.15, 0.3))
        
        # Hızlıca bineğe bin
        self.keyboard.hotkey("ctrl", self.config.mount_key)
        
        # Animasyonun iptal olması için ufak bir bekleme
        time.sleep(random.uniform(0.15, 0.25))
        
        # Tekrar binekden in
        self.keyboard.hotkey("ctrl", self.config.mount_key)
        
        # Karakterin yere basıp oltayı tekrar atabilmesi için kısa bekleme
        time.sleep(random.uniform(0.4, 0.6))
        return AppState.CAST

if __name__ == "__main__":
    pass
