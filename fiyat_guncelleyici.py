"""
Metin2 [RUBY] Lucifer Canlı Pazar Fiyatı Güncelleyici (DostGame)
-----------------------------------------------------------------------------
DostGame Ticaret Camı'nda (Ruby Lucifer) her balığı arar, 'Miktar' sütunundaki
birim fiyatlardan (1x = 2.19m gibi) en ucuz ilk 3 ilanın ortalamasını alır ve
fiyatlar.json'a yazar.

Cloudflare/proof engeli: gerçek Chrome + kalıcı profil (dostgame_profile/) ile
başsız (headless) OLMADAN çalışır. Pencere ekran dışında açılır, odak çalmaz.
-----------------------------------------------------------------------------
"""

import os
import re
import json
import time
import logging
from typing import Dict, Any, Optional, List

log = logging.getLogger("FiyatGuncelleyici")

URL = "https://www.dostgame.com/tr/metin2-ticaret-cami/ruby-lucifer"
_BASE = os.path.dirname(os.path.abspath(__file__))
PROFILE_DIR = os.path.join(_BASE, "dostgame_profile")
PRICE_FILE = os.path.join(_BASE, "fiyatlar.json")
TOP_N = 3

# key = fiyatlar.json / ayarlar.json anahtarı, queries = sitede aranacak metinler (sırayla denenir)
TARGET_ITEMS = [
    {"key": "palamut",             "queries": ["Izgara palamut"],            "check": "palamut"},
    {"key": "yabbie yengeci",      "queries": ["Izgara yabbie"],             "check": "yabbie"},
    {"key": "zargana",             "queries": ["Izgara zargana"],            "check": "zargana"},
    {"key": "altın sudak",         "queries": ["altın sudak", "sudak"],      "check": "sudak"},
    {"key": "hamsi",               "queries": ["Izgara hamsi"],              "check": "hamsi"},
    {"key": "kral yengeç",         "queries": ["Izgara kral", "kral yengeç"], "check": "kral"},
    {"key": "deniz kızı anahtarı", "queries": ["deniz kızı anahtar", "deniz kızı"], "check": "deniz"},
]


def _to_int(s: str) -> int:
    return int(re.sub(r"[^\d]", "", s) or 0)


def _parse_unit_token(num: str, suffix: str) -> Optional[int]:
    num = num.strip()
    if "," in num and "." in num:
        num = num.replace(".", "").replace(",", ".")
    else:
        num = num.replace(",", ".")
    try:
        mult = {"m": 1_000_000, "k": 1_000, "kk": 1_000_000, "b": 1_000_000_000}[suffix.lower()]
        return int(float(num) * mult)
    except Exception:
        return None


def parse_row_unit_price(cells: List[str]) -> Optional[int]:
    """Bir ilan satırının BİRİM Yang fiyatını döner. cells = td metinleri."""
    row_text = " ".join(cells)
    # 1) "(1x = 2.19m)" -> site zaten birim fiyatı hesaplamış
    m = re.search(r"\(\s*1x\s*=\s*([\d.,]+)\s*(kk|[mkb])\s*\)", row_text, re.IGNORECASE)
    if m:
        return _parse_unit_token(m.group(1), m.group(2))

    # 2) Tek adet (birim satırı yok): Miktar=1, fiyat = Won*100M + Yang
    qty = 1
    if len(cells) > 1:
        qm = re.match(r"\s*(\d[\d.]*)", cells[1])
        if qm:
            qty = max(1, _to_int(qm.group(1)))
    won, yang = 0, 0
    if len(cells) > 3:
        wm = re.search(r"(\d+)", cells[3])
        if wm:
            won = int(wm.group(1))
    if len(cells) > 2:
        # "2.980.000" veya "1 Won + 3M" biçimleri
        ydot = re.search(r"\d{1,3}(?:\.\d{3})+", cells[2])
        if ydot:
            yang = _to_int(ydot.group(0))
        else:
            tm = re.search(r"([\d.,]+)\s*(kk|[mkb])\b", cells[2], re.IGNORECASE)
            if tm:
                yang = _parse_unit_token(tm.group(1), tm.group(2)) or 0
    total = won * 100_000_000 + yang
    if total <= 0:
        return None
    return total // qty


def _wait_gate(page, timeout=60) -> bool:
    """Cloudflare/proof engeli (atlas-proof-backdrop) kalkana kadar bekler."""
    end = time.time() + timeout
    while time.time() < end:
        if page.locator(".atlas-proof-backdrop").count() == 0 and page.locator(".atlas-apply").count():
            time.sleep(0.6)
            if page.locator(".atlas-proof-backdrop").count() == 0:
                return True
        time.sleep(0.5)
    return False


def _search_item(page, item: dict) -> List[int]:
    """Eşyayı arar, ilk TOP_N ilanın birim fiyatlarını döndürür."""
    for q in item["queries"]:
        if not _wait_gate(page, 45):
            raise RuntimeError("Doğrulama engeli kalkmadı")
        inp = page.locator("input[placeholder*='eşya']").first
        inp.click()
        inp.fill("")
        inp.fill(q)
        time.sleep(1.2)
        opts = page.locator(".atlas-item-option")
        texts = [opts.nth(i).inner_text().strip() for i in range(opts.count())]
        pick = None
        # önce 'Izgara' ile başlayan + doğru isim, sonra sadece doğru isim
        for pref in (True, False):
            for i, t in enumerate(texts):
                tl = t.lower()
                if item["check"] in tl and (not pref or tl.startswith("izgara")):
                    pick = i
                    break
            if pick is not None:
                break
        if pick is None:
            continue
        opts.nth(pick).click()
        time.sleep(0.5)
        if not _wait_gate(page, 45):
            raise RuntimeError("Doğrulama engeli kalkmadı")
        page.locator(".atlas-apply").first.click(timeout=15000)

        prices: List[int] = []
        for _ in range(25):
            time.sleep(0.8)
            rows = page.locator("tbody[data-atlas-rows] tr")
            n = rows.count()
            if not n:
                continue
            first = rows.first.inner_text().lower()
            if "ükleniyor" in first or "kategori" in first:
                continue
            if "bulunamad" in first or "ilan yok" in first:
                break
            for r in range(min(n, 8)):
                tds = rows.nth(r).locator("td")
                cells = [tds.nth(c).inner_text().replace("\n", " ").strip() for c in range(tds.count())]
                if not cells or item["check"] not in cells[0].lower():
                    continue
                p = parse_row_unit_price(cells)
                if p and p > 1000:
                    prices.append(p)
                if len(prices) >= TOP_N:
                    break
            break
        if prices:
            log.info(f"[FIYAT] {texts[pick]} -> ilk {len(prices)} birim fiyat: {prices}")
            return prices[:TOP_N]
    return []


def fetch_live_prices(progress_callback=None) -> Dict[str, Any]:
    """DostGame'den canlı fiyatları çeker, fiyatlar.json'u günceller, sonucu döndürür."""
    from patchright.sync_api import sync_playwright

    def say(msg):
        log.info(msg)
        if progress_callback:
            progress_callback(msg)

    current: Dict[str, Any] = {}
    if os.path.exists(PRICE_FILE):
        try:
            with open(PRICE_FILE, "r", encoding="utf-8") as f:
                current = json.load(f)
        except Exception:
            current = {}

    say("[FIYAT] DostGame Ruby Lucifer pazarina baglaniliyor...")
    updated = dict(current)
    ok_count = 0
    os.makedirs(PROFILE_DIR, exist_ok=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            PROFILE_DIR, channel="chrome", headless=False, no_viewport=True,
            args=["--window-position=-32000,-32000", "--window-size=1280,900",
                  "--disable-blink-features=AutomationControlled"],
        )
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(URL, wait_until="domcontentloaded", timeout=45000)
            for item in TARGET_ITEMS:
                key = item["key"]
                say(f"[FIYAT] {key.title()} araniyor...")
                try:
                    prices = _search_item(page, item)
                except Exception as e:
                    say(f"[HATA] {key}: {e}")
                    prices = []
                if prices:
                    avg = int(sum(prices) / len(prices))
                    updated[key] = {
                        "birim_yang": avg,
                        "not": f"DostGame Lucifer - en ucuz {len(prices)} ilan ortalamasi "
                               f"({', '.join(f'{x/1e6:.2f}m' for x in prices)})",
                        "guncelleme": time.strftime("%Y-%m-%d %H:%M"),
                    }
                    ok_count += 1
                    say(f"[OK] {key.title()}: {avg/1e6:.2f}M Yang")
                else:
                    say(f"[UYARI] {key.title()} sitede bulunamadi/okunamadi, eski fiyat korundu.")
        finally:
            ctx.close()

    with open(PRICE_FILE, "w", encoding="utf-8") as f:
        json.dump(updated, f, ensure_ascii=False, indent=4)
    say(f"[TAMAMLANDI] {ok_count}/{len(TARGET_ITEMS)} fiyat siteden guncellendi.")
    return updated


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    res = fetch_live_prices()
    for k, v in res.items():
        print(f"{k}: {v.get('birim_yang', 0):,} Yang | {v.get('not', '')}".encode("ascii", "replace").decode())
