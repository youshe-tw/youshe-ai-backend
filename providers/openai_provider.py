import os, base64, httpx

def map_size(aspect_ratio: str):
    if aspect_ratio == "1:1":
        return "1024x1024"
    return "1536x1024"

async def render(main_image: bytes, main_mime: str, references, prompt: str, aspect_ratio: str, quality: str):
    key = os.getenv("OPENAI_API_KEY","").strip()
    if not key:
        raise RuntimeError("尚未設定 OPENAI_API_KEY。")

    model = os.getenv("OPENAI_IMAGE_MODEL","gpt-image-1")

    multipart = [("image[]", ("sketchup.png", main_image, main_mime))]
    for idx, (raw, mime) in enumerate(references[:4], start=1):
        ext = "png" if "png" in mime else "jpg"
        multipart.append(("image[]", (f"reference_{idx}.{ext}", raw, mime)))

    data = {
        "model": model,
        "prompt": prompt,
        "size": map_size(aspect_ratio),
        "quality": "high" if quality in ("2K","4K") else "medium",
        "input_fidelity": "high"
    }

    async with httpx.AsyncClient(timeout=900) as client:
        r = await client.post(
            "https://api.openai.com/v1/images/edits",
            headers={"Authorization":f"Bearer {key}"},
            files=multipart,
            data=data
        )

    if r.status_code >= 300:
        raise RuntimeError(f"OpenAI API HTTP {r.status_code}: {r.text[:1800]}")

    payload = r.json()
    item = (payload.get("data") or [{}])[0]
    if item.get("b64_json"):
        return item["b64_json"], "image/png"

    if item.get("url"):
        async with httpx.AsyncClient(timeout=180) as client:
            img = await client.get(item["url"])
            img.raise_for_status()
        return base64.b64encode(img.content).decode("ascii"), img.headers.get("content-type","image/png")

    raise RuntimeError("OpenAI 已回應，但沒有找到輸出圖片。")