import os
import requests
import time

items = [
    "Minik Balık",
    "Tatlı Su Karidesi",
    "Sudak",
    "Sazan",
    "Somon",
    "Yılanbalığı",
    "Gökkuşağı Alabalığı",
    "Ringa",
    "Palamut",
    "Yabbie Yengeci",
    "Zargana",
    "Altın Sudak",
    "İstiridye",
    "Bilge Kralın Eldiveni",
    "Bilge Kralın Sembolü",
    "Altın Yüzük",
    "Soru işareti"
]

os.makedirs("assets", exist_ok=True)
headers = {"User-Agent": "Mozilla/5.0"}

for name in items:
    filename = name.lower().replace(" ", "_") + ".png"
    filepath = os.path.join("assets", filename)
    if os.path.exists(filepath):
        continue
        
    print(f"Fetching API for {name}...")
    try:
        # MediaWiki API to get image URL
        api_url = f"https://tr-wiki.metin2.gameforge.com/api.php?action=query&titles=Dosya:{name}.png&prop=imageinfo&iiprop=url&format=json"
        resp = requests.get(api_url, headers=headers).json()
        pages = resp.get("query", {}).get("pages", {})
        
        image_url = None
        for page_id, page_data in pages.items():
            if "imageinfo" in page_data:
                image_url = page_data["imageinfo"][0]["url"]
                
        if image_url:
            print(f"Downloading from {image_url}...")
            img_data = requests.get(image_url, headers=headers).content
            with open(filepath, "wb") as f:
                f.write(img_data)
        else:
            print(f"Could not find URL for {name}")
    except Exception as e:
        print(f"Error for {name}: {e}")
    time.sleep(0.5)

print("Done downloading assets.")
