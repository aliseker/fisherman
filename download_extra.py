import os
import requests
from PIL import Image, ImageDraw, ImageFont

items = [
    "Saç Boyası", "Boya Çıkarıcı", "Lucy'nin Yüzüğü"
]

os.makedirs("assets", exist_ok=True)
headers = {"User-Agent": "Mozilla/5.0"}
base_api = "https://tr-wiki.metin2.gameforge.com/api.php"

for name in items:
    save_name = name.lower().replace(" ", "_")
    filepath = os.path.join("assets", f"{save_name}.png")
    
    # We will search the wiki for the item name to find its image
    print(f"Searching for {name}...")
    try:
        file_title = f"Dosya:{name.replace(' ', '_')}.png"
        if name == "Saç Boyası":
            file_title = "Dosya:Saç_Boyası_(Kırmızı).png"
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

print("Done.")
