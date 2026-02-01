import os
import requests

def send_telegram(text: str) -> None:
    webhook = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook:
        raise RuntimeError("DISCORD_WEBHOOK_URL이 비어 있습니다.")

    r = requests.post(webhook, json={"content": text}, timeout=15)
    r.raise_for_status()
