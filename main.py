import os
import json
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Optional, Tuple, Dict, Any, List

import requests
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont

from notifier import send_message
from storage import (
    load_data, append_and_trim, save_data,
    avg_last, avg_last_partial,
    MAX_15D, MAX_30D
)

load_dotenv()

STATE_FILE = "state.json"
ASSET_DIR = "assets"
ARROW_DIR = os.path.join(ASSET_DIR, "arrows")
TREND_IMAGE_PATH = os.path.join(ASSET_DIR, "trend.png")

URGENT_PCT = 0.5  # 직전 대비 ±0.5% 이상 변동 시 긴급


# ================= 기본 유틸 =================

def _get_threshold() -> float:
    try:
        return float(os.getenv("THRESHOLD", "1.1"))
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


def _to_float(x: Any) -> Optional[float]:
    try:
        return float(str(x).replace(",", "").strip())
    except Exception:
        return None


# ================= 추세 계산 =================

def _trend_window(data: List[float], window_n: int, half_days: float) -> Optional[Dict[str, float]]:
    if len(data) < window_n:
        return None

    w = data[-window_n:]
    half = window_n // 2
    first, last = w[:half], w[half:]

    a_first = sum(first) / len(first)
    a_last = sum(last) / len(last)

    delta = a_last - a_first
    slope_per_day = delta / half_days

    import math
    angle_deg = math.degrees(math.atan(slope_per_day))
    pct_per_day = (slope_per_day / a_first) * 100.0 if a_first else 0.0

    return {
        "a_first": a_first,
        "a_last": a_last,
        "delta": delta,
        "slope_per_day": slope_per_day,
        "angle_deg": angle_deg,
        "pct_per_day": pct_per_day,
    }


# ================= 환율 API =================

def _fetch_ap01(searchdate: str, authkey: str) -> List[Dict[str, Any]]:
    url = "https://oapi.koreaexim.go.kr/site/program/financial/exchangeJSON"
    params = {"authkey": authkey, "searchdate": searchdate, "data": "AP01"}
    r = requests.get(url, params=params, timeout=25)
    r.raise_for_status()
    return r.json() if isinstance(r.json(), list) else []


def _fetch_jpy100_exact(searchdate: str, authkey: str) -> Optional[Dict[str, float]]:
    for item in _fetch_ap01(searchdate, authkey):
        if item.get("cur_unit") == "JPY(100)":
            deal = _to_float(item.get("deal_bas_r"))
            if deal is None:
                return None
            ttb = _to_float(item.get("ttb"))
            tts = _to_float(item.get("tts"))
            mid = (ttb + tts) / 2 if (ttb and tts) else None
            return {"deal": deal, "mid": mid}
    return None


def fetch_latest_jpy100() -> Tuple[Dict[str, float], str]:
    authkey = os.getenv("EXIMBANK_API_KEY", "")
    if not authkey:
        raise RuntimeError("EXIMBANK_API_KEY 없음")

    kst = ZoneInfo("Asia/Seoul")
    today = datetime.now(kst).date()

    for back in range(7):
        d = today - timedelta(days=back)
        sd = d.strftime("%Y%m%d")
        item = _fetch_jpy100_exact(sd, authkey)
        if item:
            return item, sd

    raise RuntimeError("최근 7일 환율 조회 실패")


# ================= 부트스트랩 =================

def bootstrap_fill_30d(data: List[float]) -> List[float]:
    if len(data) >= MAX_30D:
        return data

    authkey = os.getenv("EXIMBANK_API_KEY", "")
    kst = ZoneInfo("Asia/Seoul")
    today = datetime.now(kst).date()

    prefix: List[float] = []

    for back in range(1, 120):
        if len(prefix) >= MAX_30D:
            break
        d = today - timedelta(days=back)
        sd = d.strftime("%Y%m%d")
        item = _fetch_jpy100_exact(sd, authkey)
        if item:
            prefix.extend([item["deal"]] * 48)

    prefix = prefix[::-1]
    return (prefix + data)[-MAX_30D:]


# ================= 신호 =================

def decide_signal(price: float, a15: Optional[float], a30: Optional[float], th: float):
    if a30 and price < a30 * th:
        return "BUY30", "📉 매수 신호 (30D)"
    if a15 and price < a15 * th:
        return "BUY15", "📉 매수 신호 (15D)"
    if a30 and price > a30 * th:
        return "SELL30", "📈 매도 신호 (30D)"
    if a15 and price > a15 * th:
        return "SELL15", "📈 매도 신호 (15D)"
    return "NONE", None


# ================= 메인 =================

def main():
    th = _get_threshold()
    data = load_data()

    if len(data) < MAX_30D:
        data = bootstrap_fill_30d(data)
        save_data(data)

    rates, used_date = fetch_latest_jpy100()
    price = rates["deal"]

    prev_price = data[-1] if data else None
    urgent_trigger = False
    urgent_msg = None

    if prev_price:
        pct = (price - prev_price) / prev_price * 100
        if abs(pct) >= URGENT_PCT:
            urgent_trigger = True
            urgent_msg = (
                "🚨 긴급 환율 변동\n"
                "----------------------\n"
                f"JPY(100)/KRW (매매기준율)\n"
                f"기준일: {used_date}\n"
                f"직전: {prev_price:.4f}\n"
                f"현재: {price:.4f}\n"
                f"변동: {pct:+.3f}%"
            )

    data = append_and_trim(data, price)
    save_data(data)

    a15 = avg_last(data, MAX_15D)
    a30 = avg_last(data, MAX_30D)
    state, sig = decide_signal(price, a15, a30, th)

    # 🚨 긴급이 있으면 → 긴급만 발송
    if urgent_trigger and urgent_msg:
        send_message(urgent_msg)
        return

    # 📩 신호가 있을 때만 리포트
    if sig:
        t15 = _trend_window(data, MAX_15D, 7.5)
        t30 = _trend_window(data, MAX_30D, 15.0)

        lines = [
            "📊 엔화 환율 신호 리포트 (기준환율)",
            "----------------------",
            f"JPY(100)/KRW | {used_date}",
            f"현재: {price:.4f}",
            f"15D 평균: {a15:.4f}",
            f"30D 평균: {a30:.4f}",
            "",
            f"추세(15D): 각도 {t15['angle_deg']:+.2f}°",
            f"추세(30D): 각도 {t30['angle_deg']:+.2f}°",
            "",
            sig
        ]

        prev = load_state()
        if state != prev:
            save_state(state)

        send_message("\n".join(lines))


if __name__ == "__main__":
    main()
