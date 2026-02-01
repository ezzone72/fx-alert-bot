import json
import os
from typing import Optional, Tuple

import time
import requests
from dotenv import load_dotenv

from notifier import send_telegram
from storage import load_data, append_and_trim, save_data, avg_last, MAX_15D, MAX_30D

load_dotenv()

STATE_FILE = "state.json"

def _get_threshold() -> float:
    v = os.getenv("THRESHOLD", "1.1").strip()
    try:
        return float(v)
    except ValueError:
        return 1.1

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

def fetch_jpy100_krw() -> Optional[float]:
    authkey = os.getenv("EXIMBANK_API_KEY", "").strip()
    if not authkey:
        raise RuntimeError("EXIMBANK_API_KEY가 비어 있습니다.")

    # ✅ 2025-06-25 도메인 변경: www -> oapi (공공데이터포털 공지)
    url = "https://oapi.koreaexim.go.kr/site/program/financial/exchangeJSON"
    params = {"authkey": authkey, "searchdate": "", "data": "AP01"}

    headers = {
        "User-Agent": "fx-alert-bot/1.0 (+github-actions)"
    }

    last_err = None
    for i in range(5):  # 최대 5회 시도
        try:
            r = requests.get(url, params=params, headers=headers, timeout=25)
            r.raise_for_status()
            data = r.json()
            for item in data:
                if item.get("cur_unit") == "JPY(100)":
                    s = str(item.get("deal_bas_r", "")).replace(",", "").strip()
                    return float(s)
            return None
        except Exception as e:
            last_err = e
            time.sleep(2 * (i + 1))  # 2s,4s,6s,8s,10s

    raise RuntimeError(f"환율 API 호출 실패(재시도 초과): {last_err}")


def decide_signal(price: float, a15: Optional[float], a30: Optional[float], th: float) -> Tuple[str, Optional[str]]:
    # 우선순위: 30일 > 15일
    if a30 is not None and price < a30 * th:
        return "BUY30", f"📉 매수 신호 (30D)\n가격(JPY100/KRW): {price}\n30D 평균: {a30:.4f}\n기준: avg*{th}"
    if a15 is not None and price < a15 * th:
        return "BUY15", f"📉 매수 신호 (15D)\n가격(JPY100/KRW): {price}\n15D 평균: {a15:.4f}\n기준: avg*{th}"
    if a30 is not None and price > a30 * th:
        return "SELL30", f"📈 매도 신호 (30D)\n가격(JPY100/KRW): {price}\n30D 평균: {a30:.4f}\n기준: avg*{th}"
    if a15 is not None and price > a15 * th:
        return "SELL15", f"📈 매도 신호 (15D)\n가격(JPY100/KRW): {price}\n15D 평균: {a15:.4f}\n기준: avg*{th}"
    return "NONE", None

def main() -> None:
    th = _get_threshold()
    price = fetch_jpy100_krw()
    if price is None:
        return

    data = load_data()
    data = append_and_trim(data, price)
    save_data(data)

    a15 = avg_last(data, MAX_15D)
    a30 = avg_last(data, MAX_30D)

    prev = load_state()
    state, msg = decide_signal(price, a15, a30, th)

    if msg and state != prev:
        send_telegram(msg)
        save_state(state)

if __name__ == "__main__":
    main()
