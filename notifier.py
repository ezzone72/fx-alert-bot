import os
import requests


def send_message(text: str) -> None:
    url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    if not url:
        raise RuntimeError("DISCORD_WEBHOOK_URL이 비어 있습니다.")

    r = requests.post(url, json={"content": text}, timeout=15)
    r.raise_for_status()
