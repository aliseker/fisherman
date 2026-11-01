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
import json
import urllib.request
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

# Uzak OCR servisi (ocr_server.py). Sanal makinelerde ekran kartı olmadığı için
# sohbet resmi ana bilgisayardaki GPU'ya gönderilir. Örn: FISHER_OCR_URL=http://192.168.50.1:8765
_REMOTE_OCR_URL = os.environ.get("FISHER_OCR_URL", "").strip().rstrip("/")
if not _REMOTE_OCR_URL and os.path.exists("settings.json"):
    try:
        with open("settings.json", "r", encoding="utf-8") as _sf:
            _sdata = json.load(_sf)
            _REMOTE_OCR_URL = _sdata.get("ocr_url", "").strip().rstrip("/")
    except Exception:
        pass

if _REMOTE_OCR_URL:
    _OCR_BACKEND = "remote"

# EasyOCR modeli modül genelinde tek sefer yüklenir (Singleton).
# Bot kaç kez durdurulup başlatılırsa başlatılsın model yeniden yüklenmez.
_EASYOCR_READER = None

def _get_ocr_reader():
    """EasyOCR Reader singleton'ı döndürür; ilk çağrıda yükler."""
    global _EASYOCR_READER
    if _OCR_BACKEND != "easyocr" or easyocr is None:
        return None
    if _EASYOCR_READER is None:
        log_init = logging.getLogger("FisherAppMT")
        log_init.info("[OCR] EasyOCR modeli ilk kez yükleniyor...")
        
        try:
            import torch
            is_cuda = torch.cuda.is_available()
            log_init.info(f"[OCR] Ekran Karti (CUDA) Aktif Mi: {'EVET (Hizli)' if is_cuda else 'HAYIR (Yavas)'}")
        except:
            pass
            
        _EASYOCR_READER = easyocr.Reader(["tr", "en"], gpu=True, verbose=False)
        log_init.info("[OCR] EasyOCR modeli yüklendi.")
    return _EASYOCR_READER

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
    bait_keys: list = field(default_factory=lambda: ["1", "2", "3", "4", "f1", "f2", "f3", "f4"])
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
        self._reader = _get_ocr_reader()  # Singleton — model sadece ilk seferde yüklenir
        self._last_chat_text = ""

    def read_chat(self) -> str:
        frame = self.capture.capture(self.config.chat_roi)
        # 1. Görüntüyü 1.5 kat büyüt (Önceden 3x idi, hızı aşırı düşürüyordu. 1.5x GPU'yu uçurur.)
        resized = cv2.resize(frame, None, fx=1.5, fy=1.5, interpolation=cv2.INTER_CUBIC)
        # 2. Gri tonlamaya çevir
        gray = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY)
        # 3. Metni siyah, arkaplanı beyaz yapmak için Otsu & Inverted Threshold
        _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
        if self._backend == "remote":
            return self._read_remote(thresh)
        if self._backend == "easyocr" and self._reader:
            results = self._reader.readtext(thresh, detail=1)
            results.sort(key=lambda r: r[0][0][1])
            return "\n".join(r[1].lower().strip() for r in results if r[1].strip())
        return ""

    def _read_remote(self, img: np.ndarray) -> str:
        """Resmi ana bilgisayardaki OCR servisine (ocr_server.py) gönderir."""
        ok, buf = cv2.imencode(".png", img)
        if not ok:
            return ""
        req = urllib.request.Request(
            _REMOTE_OCR_URL + "/ocr", data=buf.tobytes(),
            headers={"Content-Type": "image/png"}, method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return json.loads(resp.read().decode("utf-8")).get("text", "")
        except Exception as e:
            log.warning(f"[OCR-REMOTE] Servise ulaşılamadı ({_REMOTE_OCR_URL}): {e}")
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
        # "taktin" kelimesi "altin" ile 0.73 oranında eşleşiyordu!
        garbage_words = [
            "cengele", "cengek", "engele", "yem", "olarak", "solucan", "sokucan", "hamur", "taktin", "taklin", "taktn",
            "baligi", "bal", "olaya", "oltaya", "oltaniza", "takildi", "takild", "takdm", "yakaladin", "yakalandi", 
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
        # Her chat kelimesi keyword içinde SADECE BİR KEZ kullanılabilir.
        # (Aksi halde "altın yüzük" yazısındaki 'altın' kelimesi, "altın ton" keyword'ündeki
        #  'ton' ile de 0.50 eşleşip yanlışlıkla "altın ton balığı" sanılıyordu.)
        used_idx = set()
        # Uzun (ayırt edici) kelimeler önce eşleşsin
        for kw_w in sorted(kw_words, key=len, reverse=True):
            best_match_for_word = 0.0
            best_idx = -1
            # Okunan yazıdaki henüz kullanılmamış kelimelerle tek tek kıyasla
            for i, w in enumerate(words):
                if i in used_idx:
                    continue
                s = difflib.SequenceMatcher(None, kw_w, w).ratio()
                if s > best_match_for_word:
                    best_match_for_word = s
                    best_idx = i
            # Kısa kelimelerde (ton, kral vb.) tesadüfi benzerlik kolay olduğundan eşik daha yüksek.
            min_word_score = 0.66 if len(kw_w) <= 3 else 0.50
            # Eğer bu kelime için bulunan en iyi eşleşme eşiğin altındaysa,
            # demek ki bu kelime chatte HİÇ YOK. O zaman direkt elenmesi için 0 döndür.
            if best_match_for_word < min_word_score:
                return 0.0
            used_idx.add(best_idx)
            total_score += best_match_for_word

        # Tüm kelimelerin eşleşme oranlarının ortalamasını döndür
        return total_score / len(kw_words)

    def identify_fish_bite(self, text: str) -> Tuple[str, Optional[str]]:
        catch_unknown = self.config.whitelist.get("belli değil", False)
        if not text or not text.strip():
            if catch_unknown:
                log.info("[WHITELIST] Chat metni boş/okunamadı, 'belli değil' AÇIK -> Yakalanıyor.")
                return ("play", "belli değil")
            else:
                log.warning("[WHITELIST] Chat metni boş/okunamadı, 'belli değil' KAPALI -> İptal ediliyor (Skip).")
                return ("skip", "belli değil")

        MIN_THRESHOLD = 0.72

        best_keyword      = None
        best_should_catch = False
        best_ratio        = 0.0
        best_weight       = 0.0

        for keyword, should_catch in self.config.whitelist.items():
            if keyword == "belli değil":
                continue
            ratio = self._best_score(keyword, text)
            log.debug(f"[SCORE] '{keyword}' -> {ratio:.3f}")
            
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
                log.info(f"[WHITELIST SKIP] '{best_keyword}' (oran: {best_ratio:.2f}) istenmiyor. Iptal.")
                return ("skip", best_keyword)

        # Tanımlanamayan metin geldiğinde kullanıcının 'belli değil' ayarını uygula
        if catch_unknown:
            log.info(f"[WHITELIST UNKNOWN] Tanımlanamayan metin, 'belli değil' AÇIK -> Yakalanıyor. ({text[:50]})")
            return ("play", "belli değil")
        else:
            log.warning(f"[WHITELIST UNKNOWN] Tanımlanamayan metin, 'belli değil' KAPALI -> İptal ediliyor (Skip). ({text[:50]})")
            return ("skip", "belli değil")

    def check_fish_bite(self, text: str) -> str:
        decision, _ = self.identify_fish_bite(text)
        return decision

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
        self.hits_lock = threading.Lock()
        self.latest_hsv = None
        self.detected_fish: Optional[str] = None

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
            # Sol/Sağ kenarlardan 35 piksel, üstten 40, alttan 45 piksel kırpıyoruz (Balığı kesmemek için esnetildi)
            safe_top = 40
            safe_bottom = fh - 45
            safe_left = 35
            safe_right = fw - 35
            valid_area[safe_top:safe_bottom, safe_left:safe_right] = 255
            # Sağ üstteki 0/3 skor tablosunu tamamen sil
            valid_area[:int(fh * 0.32), int(fw * 0.60):] = 0
            
            # Güvenli bölge içindeki EN KARANLIK pikselin Value değerini bul (Kesinlikle balıktır)
            min_val, _, _, _ = cv2.minMaxLoc(v_channel, mask=valid_area)
            
            # Oyun içi gece döngüsünde sular karardığı için +20 tolerans suyu da balık sanıyordu.
            # Bunu +10'a düşürerek sadece ve sadece simsiyah balık gövdesini almasını garantiledik.
            _, fish_mask = cv2.threshold(v_channel, min_val + 10, 255, cv2.THRESH_BINARY_INV)
            fish_mask = cv2.bitwise_and(fish_mask, valid_area)
            
            # Ufak gürültüleri temizle
            kernel = np.ones((3, 3), np.uint8)
            fish_mask = cv2.erode(fish_mask, kernel, iterations=1)
            fish_mask = cv2.dilate(fish_mask, kernel, iterations=2)
            
            contours, _ = cv2.findContours(fish_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            
            if contours:
                # Toleransı çok düşürdüğümüz için balık bazen küçük görünebilir, alt limiti 10'a çektik.
                valid_contours = [c for c in contours if 10 < cv2.contourArea(c) < 3000]
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
        # Kırmızı çemberi daha iyi algılaması için aralığı biraz esnetiyoruz (Önceki 0-5 çok dar kalmış, 0-10 yapıyoruz)
        # Kahverengilere tıklamasın diye eski 20 yerine 10'da bırakıyoruz.
        red_lower1 = np.array([0, 70, 120])
        red_upper1 = np.array([10, 255, 255])
        red_lower2 = np.array([170, 70, 120])
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
        
        current_text = ""
        # Chat yazısının ekranda belirmesi için 0.1s - 0.2s arayla deneme yap
        for _ in range(3):
            time.sleep(0.12)
            if self.stop_event.is_set():
                return
            current_text = self.chat_reader.read_chat()
            if current_text.strip():
                break

        log.info(f"[OCR] Okunan Chat: '{current_text.replace(chr(10), ' ')}'")
        decision, fish_name = self.chat_reader.identify_fish_bite(current_text)
        self.detected_fish = fish_name
        if decision == "skip":
            log.info(f"[THREAD 4] DİKKAT: İstenmeyen balık/eşya ({fish_name})! Minigame anında iptal ediliyor.")
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


class FishingHelper:
    def __init__(self, config: Optional[FisherConfig] = None):
        self.config = config or FisherConfig()
        self.state = AppState.CAST
        self.running = False
        self.chat_reader = ChatReader(self.config)
        self.keyboard = HumanKeyboard()
        saved = load_session()
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
            # Önceki oturumların toplam çalışma süresi kadar geriden başlat -> sayaç kaldığı yerden devam eder
            "session_start": time.time() - float(saved.get("active_seconds", 0)),
        }
        for k in _SESSION_KEYS:
            if k in saved:
                self.stats[k] = saved[k]
        self._farm_start_time = time.time()
        self.current_bait_index = 0
        self.last_controller: Optional[MinigameController] = None
        # Slot değişiminden sonraki ilk cast'te eski 'yem bitti' yazısı chatte kalıcı durabilir.
        self._just_switched_bait = False
        self._last_save = 0.0
        self._update_earnings()

    def save_session(self) -> None:
        """Sayaçları ve toplam aktif çalışma süresini oturum.json'a yazar."""
        try:
            data = {k: self.stats[k] for k in _SESSION_KEYS}
            data["active_seconds"] = int(time.time() - self.stats["session_start"])
            tmp = SESSION_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, SESSION_FILE)
            self._last_save = time.time()
        except Exception as e:
            log.warning(f"[OTURUM] Kaydedilemedi: {e}")

    def _update_earnings(self) -> None:
        """fiyatlar.json üzerinden tutulan balıkların toplam Yang ve Won değerini hesaplar."""
        fiyatlar = {}
        if os.path.exists("fiyatlar.json"):
            try:
                with open("fiyatlar.json", "r", encoding="utf-8") as f:
                    fiyatlar = json.load(f)
            except Exception:
                pass

        total_yang = 0
        for fish_name, count in self.stats["caught_fish"].items():
            fn = fish_name.lower().strip()
            item_info = fiyatlar.get(fn, {})
            if not item_info:
                for k, v in fiyatlar.items():
                    if k in fn or fn in k:
                        item_info = v
                        break
            birim_yang = item_info.get("birim_yang", 0) if isinstance(item_info, dict) else int(item_info or 0)
            total_yang += count * birim_yang

        self.stats["total_yang"] = total_yang
        self.stats["total_won"] = round(total_yang / 100_000_000, 3)

    def _check_catch_outcome(self, chat_text: str) -> None:
        """Minigame sonrası chatte 'uzunluğu xx cm' veya 'yemi kaybettin' yazısını denetler."""
        if not chat_text:
            return
        t = self._fold_tr(chat_text.lower())
        fish_name = (self.last_controller.detected_fish if self.last_controller else None) or "Bilinmeyen Balık"

        # Yakalanma belirtileri: 'yakalanan', 'uzunlu', 'cm', 'boyu'
        if any(w in t for w in ("yakalanan", "uzunlu", "cm.", "cm,", "cm ")):
            self.stats["caught_count"] += 1
            cf = self.stats["caught_fish"]
            cf[fish_name] = cf.get(fish_name, 0) + 1
            self._update_earnings()
            self.save_session()
            log.info(f"🎣 [TUTULDU] {fish_name.title()} yakalandı! (Adet: {cf[fish_name]}, Toplam Tutulan: {self.stats['caught_count']}) | Tahmini Kazanç: {self.stats['total_won']:.2f} Won ({self.stats['total_yang']:,} Yang)")
        elif any(w in t for w in ("kaybettin", "kayboldu", "kacti", "kact")):
            self.stats["escaped_count"] += 1
            log.warning(f"💨 [KAÇTI] Balık kaçtı (Yemi kaybettin)! (Toplam kaçan: {self.stats['escaped_count']})")
            self.save_session()

    @staticmethod
    def _fold_tr(text: str) -> str:
        for k, v in (("ö","o"),("ç","c"),("ş","s"),("ı","i"),("ü","u"),("ğ","g"),("İ","i")):
            text = text.replace(k, v)
        return text

    def _chat_last_event(self, chat_text: str) -> str:
        """Sohbet kalıcı olduğundan eski mesajlar silinmez. Bu yüzden metindeki EN SON olaya bakılır
        (OCR çıktısı yukarıdan aşağıya sıralı, yani sondaki = en yeni).
        Dönüş: 'bait_empty' | 'mount' | 'ok' (yem takıldı mesajı en son) | 'none'"""
        t = re.sub(r"\s+", " ", self._fold_tr(chat_text.lower()))
        events = {}
        for name, pats in (
            ("bait_empty", (r"once yemi", r"yemi cengel")),
            ("mount", (r"binek", r"uzerindeyken", r"tutamazs")),
            # 'Çengele yem olarak Solucan taktın' (OCR bozuk halleri: olbrak, solusc, takbn ...)
            ("ok", (r"ol\w{0,2}rak", r"yem ol", r"solu", r"hamur", r"taktin", r"takt")),
        ):
            pos = -1
            for p in pats:
                for m in re.finditer(p, t):
                    pos = max(pos, m.start())
            if pos >= 0:
                events[name] = pos
        if not events:
            return "none"
        return max(events, key=events.get)

    def _is_farm_time_up(self) -> bool:
        elapsed_min = (time.time() - self._farm_start_time) / 60.0
        return elapsed_min >= self.config.farm_duration_minutes

    def run(self):
        self.running = True
        log.info("Oyun penceresine geçmek için 5 saniyeniz var...")
        for i in range(5, 0, -1):
            if not self.running: return
            log.info(f"{i} saniye...")
            time.sleep(1)
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
                    self.last_controller = controller
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
                if time.time() - self._last_save > 15:
                    self.save_session()
            except KeyboardInterrupt:
                break
            except Exception as e:
                log.error(f"[HATA] Beklenmeyen hata: {e}", exc_info=True)
                break
        self.running = False
        self.save_session()
        log.info(f"Bot kapatıldı. Stats: {self.stats}")

    def state_cast(self) -> AppState:
        current_bait = self.config.bait_keys[self.current_bait_index]
        log.info(f"[STATE 1] Olta atılıyor... (Yem Slotu: {current_bait})")
        self.stats["total_casts"] += 1
        self.keyboard.press(current_bait)
        time.sleep(random.uniform(0.1, 0.2))  # Yem ikonunun işlenmesi için kısa bekleme
        
        self.keyboard.press(self.config.cast_key)
        
        # Oltayı attıktan sonra "Yem yok" uyarısı için chat'i kontrol et
        time.sleep(0.3) # Yazının belirmesi için çok kısa bekle
        chat_text = self.chat_reader.read_chat().lower()
        last_event = self._chat_last_event(chat_text)
        
        # "Önce yemi çengele geçir" mesajı sohbette KALICI olduğu için, mesaj varsa bile
        # ondan sonra 'çengele yem taktın' satırı geldiyse eski mesajdır (last_event == 'ok').
        if last_event == "bait_empty":
            if self._just_switched_bait:
                # Yeni slotun ilk denemesi: eski mesaj OCR'da kalmış olabilir. Atlamadan önce 1 kez daha dene.
                log.warning(f"[YEM DOĞRULAMA] '{current_bait}' slotunda 'yem yok' görüldü ama yeni geçildi, tekrar deneniyor...")
                self._just_switched_bait = False
                time.sleep(0.8)
                return AppState.CAST
            log.warning(f"[YEM BİTTİ] '{current_bait}' slotundaki yem tükendi!")
            self.current_bait_index += 1
            if self.current_bait_index >= len(self.config.bait_keys):
                log.error("[YEM BİTTİ] Tüm yem slotları tükendi! Bot durduruluyor.")
                return AppState.STOPPED
            
            next_bait = self.config.bait_keys[self.current_bait_index]
            log.info(f"[YEM DEĞİŞİMİ] Yeni yem slotuna geçiliyor: {next_bait}")
            self._just_switched_bait = True
            return AppState.CAST
        
        # Bu cast'te hata yok (ya da yem takıldı) -> doğrulama bayrağını kapat
        self._just_switched_bait = False
            
        if last_event == "mount":
            log.warning("[BİNEK HATASI] Binek üzerindeyken balık tutulamaz! Binekten iniliyor...")
            self.keyboard.hotkey('ctrl', 'g')
            time.sleep(0.6)
            return AppState.CAST
            
        time.sleep(random.uniform(0.3, 0.6))
        return AppState.DECISION

    def state_decision(self) -> AppState:
        log.info("[STATE 2] Balık bekleniyor (Minigame penceresi aranıyor)...")
        wait_start = time.time()
        # Loglara göre ısırma süresi: medyan ~0sn, p99 ~3sn. 60sn beklemek takılma gibi görünüyordu.
        bite_timeout = 15.0
        
        with mss.mss() as sct:
            while time.time() - wait_start < bite_timeout:
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
                        
                time.sleep(0.05)
                
        log.warning(f"[STATE 2] {bite_timeout:.0f}sn geçti, balık vurmadı (olta atılmamış olabilir). İptal, yeniden denenecek.")
        self.keyboard.press(self.config.fishing_close_key)
        return AppState.ANIM_CANCEL

    def state_anim_cancel(self) -> AppState:
        log.info("[STATE 4] Animasyon iptali yapılıyor (Ctrl+G)...")
        # Minigame biter bitmez oltayı çekme animasyonunu İPTAL ETMEK için hiç beklemeden bineğe biniyoruz
        time.sleep(random.uniform(0.05, 0.12))
        
        # Hızlıca bineğe bin
        self.keyboard.hotkey("ctrl", self.config.mount_key)
        
        # Sunucu gecikmesi için kısa bekleme (eskiden 0.4-0.6 sn)
        time.sleep(random.uniform(0.22, 0.32))
        
        # Tekrar binekden in
        self.keyboard.hotkey("ctrl", self.config.mount_key)
        
        # Karakterin yere basıp oltayı tekrar atabilmesi için kısa bekleme (eskiden 0.4-0.6 sn)
        time.sleep(random.uniform(0.2, 0.3))

        # Sonuç kontrolü (Balık tutuldu mu / kaçtı mı)
        chat_text = self.chat_reader.read_chat()
        self._check_catch_outcome(chat_text)

        return AppState.CAST

if __name__ == "__main__":
    pass
