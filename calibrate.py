"""
=============================================================================
Koordinat Bulucu - Fisherman ROI Kalibrasyon Aracı
=============================================================================
Bu script, program için gerekli koordinatları (ROI, buton pozisyonları vb.)
interaktif olarak belirlemenizi sağlar.

Çalıştır: python calibrate.py
=============================================================================
"""

import time
import sys
import cv2
import numpy as np
import mss
import win32gui
import win32con
from dataclasses import dataclass


def get_mouse_position():
    """Anlık fare pozisyonunu döndürür (win32api olmadan)."""
    import ctypes
    class POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
    pt = POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def live_coordinate_tracker():
    """
    Fare konumunu gerçek zamanlı gösterir.
    Fareyi ilgili bölgeye götürüp koordinatı not edin.
    """
    print("=" * 50)
    print("FARE KOORDİNAT TAKİPÇİSİ")
    print("=" * 50)
    print("Fareyi ilgili bölgeye götürün ve koordinatı not edin.")
    print("Çıkmak için: Ctrl+C")
    print()

    try:
        while True:
            x, y = get_mouse_position()
            print(f"\r  Fare Pozisyonu: X={x:4d}, Y={y:4d}  ", end="", flush=True)
            time.sleep(0.05)
    except KeyboardInterrupt:
        print("\n\nTracker durduruldu.")


def capture_roi_selector():
    """
    Ekranı yakalar ve mouse ile ROI seçmenizi sağlar.
    OpenCV'nin selectROI fonksiyonunu kullanır.
    """
    print("=" * 50)
    print("ROI SEÇİCİ")
    print("=" * 50)
    print("Ekran yakalaması yapılıyor...")

    with mss.mss() as sct:
        # Tüm ekranı yakala
        monitor = sct.monitors[1]  # Birincil monitör
        screenshot = sct.grab(monitor)
        frame = np.array(screenshot)
        frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)

    print("Ekran yakalandı!")
    print("Talimatlar:")
    print("  1. Fare ile ROI alanını seçin (sürükleyin)")
    print("  2. Enter veya Space ile onaylayın")
    print("  3. C veya Esc ile iptal edin")
    print()

    # Ekranı küçült (çok büyükse)
    h, w = frame.shape[:2]
    scale = min(1.0, 1200 / w)
    display = cv2.resize(frame, (int(w * scale), int(h * scale)))

    roi = cv2.selectROI("ROI Seçici - Enter ile onayla, C ile iptal", display, False)
    cv2.destroyAllWindows()

    if roi[2] > 0 and roi[3] > 0:
        # Seçilen ROI'yi orijinal koordinatlara çevir
        x = int(roi[0] / scale)
        y = int(roi[1] / scale)
        w_roi = int(roi[2] / scale)
        h_roi = int(roi[3] / scale)

        print(f"\nSeçilen ROI (Orijinal Koordinatlar):")
        print(f'  dict: {{"top": {y}, "left": {x}, "width": {w_roi}, "height": {h_roi}}}')
        print(f"  Tuple: ({x}, {y}, {w_roi}, {h_roi})")
        return {"top": y, "left": x, "width": w_roi, "height": h_roi}
    else:
        print("ROI seçilmedi.")
        return None


def hsv_color_picker():
    """
    Ekran üzerinde bir bölge seçip içindeki renk değerlerini (HSV) gösterir.
    Balık rengi ve daire rengi tespiti için kullanın.
    """
    print("=" * 50)
    print("HSV RENK SEÇICI")
    print("=" * 50)
    print("Renk analizi yapılacak bölgeyi seçin...")

    with mss.mss() as sct:
        monitor = sct.monitors[1]
        screenshot = sct.grab(monitor)
        frame = np.array(screenshot)
        frame_bgr = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)

    h, w = frame_bgr.shape[:2]
    scale = min(1.0, 1200 / w)
    display = cv2.resize(frame_bgr, (int(w * scale), int(h * scale)))

    roi = cv2.selectROI("Renk analizi için bölge seçin", display, False)
    cv2.destroyAllWindows()

    if roi[2] > 0 and roi[3] > 0:
        x, y, wr, hr = roi
        x, y, wr, hr = int(x/scale), int(y/scale), int(wr/scale), int(hr/scale)

        # Seçilen bölgeyi HSV'ye çevir
        cropped = frame_bgr[y:y+hr, x:x+wr]
        hsv_crop = cv2.cvtColor(cropped, cv2.COLOR_BGR2HSV)

        # İstatistikleri hesapla
        h_min, h_max = hsv_crop[:,:,0].min(), hsv_crop[:,:,0].max()
        s_min, s_max = hsv_crop[:,:,1].min(), hsv_crop[:,:,1].max()
        v_min, v_max = hsv_crop[:,:,2].min(), hsv_crop[:,:,2].max()
        h_mean = hsv_crop[:,:,0].mean()
        s_mean = hsv_crop[:,:,1].mean()
        v_mean = hsv_crop[:,:,2].mean()

        print(f"\nSeçilen Bölge HSV Değerleri:")
        print(f"  H (Ton)       : min={h_min}, max={h_max}, ortalama={h_mean:.1f}")
        print(f"  S (Doygunluk) : min={s_min}, max={s_max}, ortalama={s_mean:.1f}")
        print(f"  V (Parlaklık) : min={v_min}, max={v_max}, ortalama={v_mean:.1f}")
        print()
        print(f"  Önerilen lower: np.array([{max(0,int(h_mean-15))}, {max(0,int(s_mean-40))}, {max(0,int(v_mean-40))}])")
        print(f"  Önerilen upper: np.array([{min(180,int(h_mean+15))}, {min(255,int(s_mean+40))}, {min(255,int(v_mean+40))}])")

        # Görselleştir
        cv2.imshow("Seçilen Bölge (BGR)", cropped)
        cv2.imshow("Seçilen Bölge (HSV)", hsv_crop)
        cv2.waitKey(0)
        cv2.destroyAllWindows()


def live_minigame_monitor():
    """
    Minigame penceresini gerçek zamanlı izler ve analiz sonuçlarını gösterir.
    Koordinatların doğruluğunu test etmek için kullanın.
    """
    print("=" * 50)
    print("CANLI MİNİGAME MONİTÖRÜ")
    print("=" * 50)

    # Minigame ROI'yi buraya girin
    minigame_roi = {"top": 222, "left": 365, "width": 250, "height": 230}
    print(f"İzlenen ROI: {minigame_roi}")
    print("Çıkmak için: 'q' tuşuna basın")

    with mss.mss() as sct:
        while True:
            screenshot = sct.grab(minigame_roi)
            frame = np.array(screenshot)
            frame_bgr = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)

            # 3x büyütülmüş görüntü
            big = cv2.resize(frame_bgr, (750, 690), interpolation=cv2.INTER_NEAREST)

            # Merkez dairesi çiz
            cx, cy = 125 * 3, 100 * 3  # 3x büyütülmüş koordinat
            r = 70 * 3
            cv2.circle(big, (cx, cy), r, (255, 255, 255), 2)
            cv2.circle(big, (cx, cy), 5, (0, 0, 255), -1)

            cv2.putText(
                big,
                "Minigame Monitor - Q ile cik",
                (10, 30),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.7,
                (0, 255, 0),
                2,
            )

            cv2.imshow("Minigame Monitor", big)
            if cv2.waitKey(1) & 0xFF == ord("q"):
                break

    cv2.destroyAllWindows()


def main_menu():
    """Ana menü."""
    print("\n" + "=" * 60)
    print("  Metin2 Fishing Bot - Koordinat Kalibrasyon Aracı")
    print("=" * 60)
    print()
    print("  [1] Fare Koordinat Takipçisi (anlık X,Y göster)")
    print("  [2] ROI Seçici (sürükle-bırak ile bölge seç)")
    print("  [3] HSV Renk Seçici (renk aralığı bul)")
    print("  [4] Canlı Minigame Monitörü")
    print("  [0] Çıkış")
    print()

    choice = input("Seçiminiz: ").strip()

    if choice == "1":
        live_coordinate_tracker()
    elif choice == "2":
        capture_roi_selector()
    elif choice == "3":
        hsv_color_picker()
    elif choice == "4":
        live_minigame_monitor()
    elif choice == "0":
        print("Çıkılıyor...")
        sys.exit(0)
    else:
        print("Geçersiz seçim!")

    main_menu()  # Tekrar menüye dön


if __name__ == "__main__":
    main_menu()
