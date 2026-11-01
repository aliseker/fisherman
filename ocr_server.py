"""
=============================================================================
Metin2 Fisher - OCR Servisi (ana bilgisayarda, ekran kartıyla çalışır)
=============================================================================
Sanal makinelerdeki botlar sohbet resmini buraya gönderir, metni geri alır.

Başlatma (ana bilgisayarda):   ocr_servisi_baslat.bat
Adres:  http://0.0.0.0:8765   (sanal makineden:  http://<ana-pc-vmware-ip>:8765)

Güvenlik: Sadece --host ile verilen adreste dinler. VMware ağ kartının IP'sini
verirsen (örn. 192.168.xxx.1) servis dışarıdaki ağdan erişilemez, yalnızca
sanal makinelerden erişilir.
=============================================================================
"""

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2
import numpy as np
import easyocr

_reader = None
_lock = threading.Lock()  # GPU'ya aynı anda tek okuma (istekler sıraya girer)


def get_reader():
    global _reader
    if _reader is None:
        print("[OCR] Model yükleniyor (GPU)...")
        _reader = easyocr.Reader(["tr", "en"], gpu=True, verbose=False)
        print("[OCR] Model hazır.")
    return _reader


def read_image(img: np.ndarray) -> str:
    with _lock:
        results = get_reader().readtext(img, detail=0)
    return "\n".join(r.lower().strip() for r in results if r.strip())


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"status": "ok"})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/ocr":
            self._send(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length <= 0 or length > 5_000_000:
                self._send(400, {"error": "bad length"})
                return
            data = np.frombuffer(self.rfile.read(length), dtype=np.uint8)
            img = cv2.imdecode(data, cv2.IMREAD_GRAYSCALE)
            if img is None:
                self._send(400, {"error": "bad image"})
                return
            t0 = time.time()
            text = read_image(img)
            print(f"[OCR] {self.client_address[0]} {time.time() - t0:.2f}s -> {text.replace(chr(10), ' | ')[:90]}")
            self._send(200, {"text": text})
        except Exception as e:
            print(f"[OCR] Hata: {e}")
            self._send(500, {"error": str(e)})

    def log_message(self, fmt, *args):  # varsayılan erişim logunu sustur
        pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1", help="Dinlenecek IP. Sanal makineler için VMware ağ kartının IP'sini ver (0.0.0.0 tüm ağa açar!)")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()

    get_reader()  # başlangıçta modeli yükle, ilk istek gecikmesin
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[OCR] Servis hazır: http://{args.host}:{args.port}  (durdurmak için Ctrl+C)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
