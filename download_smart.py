import os
import requests
import json
import time
from PIL import Image, ImageDraw, ImageFont

items = [
    "Minik Balık", "Tatlı Su Karidesi", "Sudak", "Sazan", "Somon", 
    "Yılan Balığı", "Gökkuşağı Alabalığı", "Ringa Balığı", "Palamut", "Yabbie Yengeci", 
    "Zargana", "Altın Sudak", "Ot Sazanı", "Kadife Balığı", "Hamsi", "Kral Yengeç", 
    "Aynalı Sazan", "Deniz Kızı Anahtarı", "Altın Anahtar", "Gümüş Anahtar", 
    "Altın Parçası", "Bilge Kralın Eldiveni", "Bilge Kralın Sembolü", 
    "Altın Yüzük", "Sevimli Balık", "Soru işareti"
]

# Standardize display names vs search names
# We will search the wiki for the item name to find its image
os.makedirs("assets", exist_ok=True)
headers = {"User-Agent": "Mozilla/5.0"}
base_api = "https://tr-wiki.metin2.gameforge.com/api.php"

for name in items:
    # Save name will be lowercase, spaces to underscore
    # But for "Yılan Balığı" -> we want the key as "yılanbalığı" if user types it like that.
    # Let's map display name to save name
    save_name = name.lower().replace(" ", "_")
    if name == "Yılan Balığı": save_name = "yılanbalığı"
    if name == "Ringa Balığı": save_name = "ringa"
    if name == "Kadife Balığı": save_name = "kadife"
    if name == "Soru işareti": save_name = "belli_değil"
    
    filepath = os.path.join("assets", f"{save_name}.png")
    if os.path.exists(filepath):
        continue

    print(f"Searching for {name}...")
    try:
        # Direct file check first
        file_title = f"Dosya:{name.replace(' ', '_')}.png"
        params = {
            "action": "query",
            "titles": file_title,
            "prop": "imageinfo",
            "iiprop": "url",
            "format": "json"
        }
        res = requests.get(base_api, params=params, headers=headers).json()
        pages = res.get("query", {}).get("pages", {})
        
        img_url = None
        for pid, pdata in pages.items():
            if "imageinfo" in pdata:
                img_url = pdata["imageinfo"][0]["url"]
                
        # If not found directly, try searching for the page and getting images
        if not img_url:
            print(f"Direct file not found, searching page for {name}...")
            # We can also just try without .png or with .jpg, but mostly it's .png
            pass
            
        if img_url:
            print(f"Downloading {img_url} -> {save_name}.png")
            img_data = requests.get(img_url, headers=headers).content
            with open(filepath, "wb") as f:
                f.write(img_data)
        else:
            print(f"Could not find image for {name}. Generating placeholder.")
            img = Image.new('RGB', (40, 40), color = (73, 109, 137))
            d = ImageDraw.Draw(img)
            initials = "".join([w[0] for w in name.split()])[:2]
            d.text((10,10), initials, fill=(255,255,0))
            img.save(filepath)
    except Exception as e:
        print(f"Error on {name}: {e}. Generating placeholder.")
        img = Image.new('RGB', (40, 40), color = (73, 109, 137))
        d = ImageDraw.Draw(img)
        initials = "".join([w[0] for w in name.split()])[:2]
        d.text((10,10), initials, fill=(255,255,0))
        img.save(filepath)
        
    time.sleep(0.5)

print("Done.")
