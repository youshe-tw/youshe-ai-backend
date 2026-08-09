# v3.3.3 geometry preservation is enforced primarily through prompt discipline and style-strength capping.
import os, base64, httpx

async def render(main_image: bytes, main_mime: str, references, prompt: str, aspect_ratio: str, quality: str):
    key = os.getenv("GEMINI_API_KEY","").strip()
    if not key:
        raise RuntimeError("尚未設定 GEMINI_API_KEY。")

    model = os.getenv("GEMINI_IMAGE_MODEL","gemini-3.1-flash-image")
    inputs = [{"type":"image","mime_type":main_mime,"data":base64.b64encode(main_image).decode("ascii")}]

    for raw, mime in references[:5]:
        inputs.append({"type":"image","mime_type":mime,"data":base64.b64encode(raw).decode("ascii")})

    inputs.append({"type":"text","text":prompt})

    payload = {
        "model": model,
        "input": inputs,
        "response_format": {
            "type": "image",
            "mime_type": "image/jpeg",
            "aspect_ratio": aspect_ratio,
            "image_size": quality if quality in ("1K","2K","4K") else "2K"
        }
    }

    async with httpx.AsyncClient(timeout=900) as client:
        r = await client.post(
            "https://generativelanguage.googleapis.com/v1beta/interactions",
            headers={"x-goog-api-key":key,"Content-Type":"application/json"},
            json=payload
        )

    if r.status_code >= 300:
        raise RuntimeError(f"Gemini API HTTP {r.status_code}: {r.text[:1800]}")

    data = r.json()
    output_image = data.get("output_image") or {}
    if output_image.get("data"):
        return output_image["data"], output_image.get("mime_type","image/jpeg")

    def walk(x):
        if isinstance(x, dict):
            if x.get("type") == "image" and x.get("data"):
                return x["data"], x.get("mime_type","image/jpeg")
            for v in x.values():
                g = walk(v)
                if g: return g
        elif isinstance(x, list):
            for v in x:
                g = walk(v)
                if g: return g
        return None

    found = walk(data)
    if found: return found
    raise RuntimeError("Gemini 已回應，但沒有找到輸出圖片。")