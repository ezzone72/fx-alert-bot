import os
import json
import requests
from typing import Optional


def send_message(text: str, file_path: Optional[str] = None, filename: str = "trend.png") -> None:
    url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    if not url:
        raise RuntimeError("DISCORD_WEBHOOK_URL이 비어 있습니다.")

    if not file_path:
        r = requests.post(url, json={"content": text}, timeout=20)
        r.raise_for_status()
        return

    # Discord webhook 파일 첨부는 multipart + payload_json 형태
    payload = {"content": text}
    with open(file_path, "rb") as f:
        files = {
            "payload_json": (None, json.dumps(payload), "application/json"),
            "files[0]": (filename, f, "image/png"),
        }
        r = requests.post(url, files=files, timeout=30)
        r.raise_for_status()
