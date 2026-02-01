import os
import json
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Optional, Tuple, Dict, Any, List

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


def _to_float(x: Any) -> Optional[float]:
    if x is None:
        return None
    try:
        return float(str(x).replace(",", "").strip())
    except Exception:
        return None


def _sparkline(series: List[float], width: int = 48) -> str:
    if not series:
        return ""
    if len(series) > width:
        step = len(series) / width
        sampled = [series[int(i * step)] for i in range(width)]
    else:
        sampled = series[:]
    mn, mx = min(sampled), max(sampled)
    if mx - mn < 1e-9:
        return "▁" * len(sampled)
    blocks = "▁▂▃▄▅▆▇█"
    out = []
    for v in sampled:
        t = (v - mn) / (mx - mn)
        out.append(blocks[int(t * (len(blocks) - 1))])
    return "".join(out)


def _trend_15d(data: List[float]) -> Optional[Dict[str, float]]:
    if len(data) < MAX_15D:
        return None
    w = data[-MAX_15D:]
    first = w[: MAX_15D // 2]
    last = w[MAX_15D // 2 :]
    a_first = sum(first) / len(first)
    a_last = sum(last) / len(last)
    delta = a_last - a_first
    days = 7.5
    slope_per_day = delta / days
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


# ========= 환율 API =========

def _fetch_ap01_for_date(searchdate: str, authkey: str) -> List[Dict[str, Any]]:
    url = "https://oapi.koreaexim.go.kr/site/program/financial/exchangeJSON"
    params = {"authkey": authkey, "searchdate": searchdate, "data": "AP01"}
    headers = {"User-Agent": "fx-alert-bot"}
    r = requests.get(url, params=params, headers=headers, timeout=25)
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else []


def _fetch_jpy100_item_exact_date(searchdate: str, authkey: str) -> Optional[Dict[str, Optional[float]]]:
    """
    특정 날짜(YYYYMMDD) 하루치에서 JPY(100) 항목을 찾는다.
    그 날짜가 주말/공휴일이면 None을 리턴.
    """
    items = _fetch_ap01_for_date(searchdate, authkey)
    if not items:
        return None
    for item in items:
        if item.get("cur_unit") == "JPY(100)":
            ttb = _to_float(item.get("ttb"))
            tts = _to_float(item.get("tts"))
            deal = _to_float(item.get("deal_bas_r"))
            if deal is None:
                return None
            mid = (ttb + tts) / 2.0 if (ttb is not None and tts is not None) else None
            return {"deal": deal, "ttb": ttb, "tts": tts, "mid": mid}
    return None


def fetch_jpy100_item_with_date_fallback() -> Tuple[Dict[str, Optional[float]], str]:
    """
    실시간(현재 실행) 값: 최근 7일에서 데이터 있는 날짜를 찾아 JPY(100)을 가져온다.
    """
    authkey = os.getenv("EXIMBANK_API_KEY", "").strip()
    if not authkey:
        raise RuntimeError("EXIMBANK_API_KEY가 비어 있습니다.")

    kst = ZoneInfo("Asia/Seoul")
    today = datetime.now(kst).date()

    last_err = None
    for day_back in range(0, 7):
        d = today - timedelta(days=day_back)
        searchdate = d.strftime("%Y%m%d")
        for i in range(3):
            try:
                item = _fetch_jpy100_item_exact_date(searchdate, authkey)
                if item is None:
                    break
                return item, searchdate
            except Exception as e:
                last_err = e
                time.sleep(1.2 * (i + 1))

    raise RuntimeError(f"최근 7일 내 환율 데이터를 찾지 못했습니다: {last_err}")


# ========= 부트스트랩(초기 30일 채우기) =========

def bootstrap_fill_30d_if_needed(data: List[float]) -> List[float]:
    """
    data.csv가 비었거나 부족하면:
    - 과거 '일 단위' 환율을 가져와서 (deal_bas_r 기준)
    - 하루 값을 48번 반복하여 30분봉처럼 채운 뒤
    - 총 1440개(30일) 되도록 앞쪽(과거)에 채움
    """
    if len(data) >= MAX_30D:
        return data

    authkey = os.getenv("EXIMBANK_API_KEY", "").strip()
    if not authkey:
        raise RuntimeError("EXIMBANK_API_KEY가 비어 있습니다.")

    need = MAX_30D - len(data)
    days_needed = (need + 47) // 48  # ceil

    kst = ZoneInfo("Asia/Seoul")
    today = datetime.now(kst).date()

    prefix: List[float] = []
    got_days = 0

    # 주말/공휴일 감안해 넉넉히 90일 범위 탐색(영업일 30일 확보용)
    # (처음 한 번만 실행)
    for day_back in range(1, 90 + 1):
        if got_days >= days_needed:
            break

        d = today - timedelta(days=day_back)
        searchdate = d.strftime("%Y%m%d")

        try:
            item = _fetch_jpy100_item_exact_date(searchdate, authkey)
            if item is None:
                continue  # 데이터 없는 날(주말/공휴일) 스킵

            deal = item["deal"]
            if deal is None:
                continue

            prefix.extend([deal] * 48)
            got_days += 1

            # 너무 빠른 연속 호출 방지(서버 예의)
            time.sleep(0.15)

        except Exception:
            # 일시적 네트워크 에러면 그냥 넘어가서 다음 날 시도
            time.sleep(0.3)
            continue

    if not prefix:
        # 그래도 못 채우면 기존 data 그대로
        return data

    # prefix는 과거→최근 순으로 쌓였어야 하는데, 우리는 과거부터 훑어서 이미 그 순서입니다.
    # (today-1, today-2...로 가면 역순이므로, 실제로는 뒤집어야 “과거→최근”이 됨)
    # 현재 loop는 day_back=1부터 증가하니 prefix는 "최근 과거 → 더 과거" 순으로 들어감
    # 따라서 뒤집어서 과거→최근으로 정렬
    prefix = list(reversed(prefix))

    data = (prefix + data)[-MAX_30D:]
    return data


# ========= 신호 판정 =========

def decide_signal(price: float, a15: Optional[float], a30: Optional[float], th: float):
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
    th = _get_threshold()

    # 1) CSV 로드
    data = load_data()

    # 2) 처음엔 30일(1440개) 부트스트랩(과거 일단위 값을 48번 반복)
    if len(data) < MAX_30D:
        try:
            data = bootstrap_fill_30d_if_needed(data)
            save_data(data)
        except Exception as e:
            send_message(f"⚠️ 부트스트랩 실패(과거데이터 채우기)\n{e}")

    # 3) 최신 값(최근 7일 fallback) 가져오기
    try:
        rates, used_date = fetch_jpy100_item_with_date_fallback()
    except Exception as e:
        send_message(f"⚠️ 환율 수신 실패\n{e}")
        return

    # ✅ 기준환율: deal_bas_r(매매기준율)
    price = rates["deal"]
    if price is None:
        send_message("⚠️ deal_bas_r(매매기준율) 값이 없습니다.")
        return

    # 4) 30분마다 1개 추가(슬라이딩)
    data = append_and_trim(data, price)
    save_data(data)

    # 평균(표시용)
    a15_show = avg_last_partial(data, MAX_15D)
    a30_show = avg_last_partial(data, MAX_30D)

    # 평균(판정용)
    a15 = avg_last(data, MAX_15D)
    a30 = avg_last(data, MAX_30D)

    is_test = len(data) < MAX_15D

    trend = _trend_15d(data)
    last_48 = data[-48:] if len(data) >= 48 else data[:]
    chart = _sparkline(last_48, width=min(48, len(last_48))) if last_48 else ""

    lines = []
    if is_test:
        lines.append("🧪 TEST(수집중) — 15일 데이터 미만")
    lines.append("📊 엔화 환율 알림 (전신환/기준환율 기준)")
    lines.append("----------------------")
    lines.append("JPY(100) / KRW")
    lines.append(f"기준일: {used_date}")
    lines.append(f"매매기준율(deal): {price:.4f}")

    # ✅ 중간값 한 줄(있을 때만)
    if rates.get("mid") is not None:
        lines.append(f"중간값(mid=(TTB+TTS)/2): {rates['mid']:.4f}")
    else:
        lines.append("중간값(mid=(TTB+TTS)/2): N/A")

    lines.append(f"15D 평균(현재까지): {a15_show:.4f}" if a15_show is not None else "15D 평균(현재까지): N/A")
    lines.append(f"30D 평균(현재까지): {a30_show:.4f}" if a30_show is not None else "30D 평균(현재까지): N/A")
    lines.append(f"데이터: {len(data)}/{MAX_30D} (30분봉)")

    if trend is None:
        lines.append("추세: 데이터 부족(15일 이상 필요)")
    else:
        direction = "하락추세" if trend["a_last"] < trend["a_first"] else "상승/횡보"
        lines.append(
            f"추세(15D): {direction} | 전반7.5D {trend['a_first']:.4f} → 후반7.5D {trend['a_last']:.4f}"
        )
        lines.append(
            f"기울기: {trend['slope_per_day']:+.4f} 원/일 ({trend['pct_per_day']:+.3f}%/일) | 각도: {trend['angle_deg']:+.2f}°"
        )

    if chart:
        lines.append(f"최근 24h: {chart}")

    state, sig = decide_signal(price, a15, a30, th)
    if sig:
        lines.append(sig)
        prev = load_state()
        if state != prev:
            save_state(state)

    send_message("\n".join(lines))


if __name__ == "__main__":
    main()
