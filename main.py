import os
import json
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Optional, Tuple

import requests
from dotenv import load_dotenv

from notifier import send_message
from storage import (
    load_data, append_and_trim, save_data,
    avg_last, avg_last_partial,
    MAX_15D, MAX_30D
)

load_dotenv()

STATE_FILE = "state.json"


def load_state() -> str:
    if not os.path.exists(STATE_FILE):
        return "NONE"
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f).get("state", "NONE")
    except Exception:
        return "NONE"


def save_state(state: str) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"state": state}, f, ensure_ascii=False)


def fetch_jpy100_krw_with_date() -> Tuple[float, str]:
    authkey = os.getenv("EXIMBANK_API_KEY", "").strip()
    if not authkey:
        raise RuntimeError("EXIMBANK_API_KEY가 비어 있습니다.")

    url = "https://oapi.koreaexim.go.kr/site/program/financial/exchangeJSON"
    headers = {"User-Agent": "fx-alert-bot"}

    kst = ZoneInfo("Asia/Seoul")
    today = datetime.now(kst).date()

    for day_back in range(0, 7):
        d = today - timedelta(days=day_back)
        searchdate = d.strftime("%Y%m%d")

        params = {
            "authkey": authkey,
            "searchdate": searchdate,
            "data": "AP01",
        }

        try:
            r = requests.get(url, params=params, headers=headers, timeout=25)
            r.raise_for_status()
            data = r.json()

            if not isinstance(data, list):
                continue

            for item in data:
                if item.get("cur_unit") == "JPY(100)":
                    price = float(str(item.get("deal_bas_r")).replace(",", ""))
                    return price, searchdate

        except Exception:
            time.sleep(1)

    raise RuntimeError("최근 7일 내 환율 데이터를 찾지 못했습니다.")


def decide_signal(price, a15, a30, th=1.1):
    if a30 is not None and price < a30 * th:
        return "BUY30", "📉 매수 신호 (30D)"
    if a15 is not None and price < a15 * th:
        return "BUY15", "📉 매수 신호 (15D)"
    if a30 is not None and price > a30 * th:
        return "SELL30", "📈 매도 신호 (30D)"
    if a15 is not None and price > a15 * th:
        return "SELL15", "📈 매도 신호 (15D)"
    return "NONE", None


def main():
    try:
        price, used_date = fetch_jpy100_krw_with_date()
    except Exception as e:
        send_message(f"⚠️ 환율 수신 실패\n{e}")
        return

    data = load_data()
    data = append_and_trim(data, price)
    save_data(data)

    a15_show = avg_last_partial(data, MAX_15D)
    a30_show = avg_last_partial(data, MAX_30D)

    a15 = avg_last(data, MAX_15D)
    a30 = avg_last(data, MAX_30D)

    is_test = len(data) < MAX_15D

    lines = []
    if is_test:
        lines.append("🧪 TEST (수집중)")
    lines.append("📊 엔화 환율")
    lines.append(f"기준일: {used_date}")
    lines.append(f"현재가: {price}")
    lines.append(f"15D 평균(현재까지): {a15_show:.4f}")
    lines.append(f"30D 평균(현재까지): {a30_show:.4f}")
    lines.append(f"데이터 수: {len(data)}/{MAX_30D}")

    state, sig = decide_signal(price, a15, a30)
    if sig:
        lines.append(sig)
        prev = load_state()
        if state != prev:
            save_state(state)

    send_message("\n".join(lines))


if __name__ == "__main__":
    main()
