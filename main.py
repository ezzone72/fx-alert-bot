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
    """
    텍스트 그래프(스파크라인). width=48이면 최근 24시간(30분봉) 느낌으로 보기 좋음.
    series가 길면 다운샘플링합니다.
    """
    if not series:
        return ""

    # 다운샘플: 원하는 폭으로 줄이기
    if len(series) > width:
        step = len(series) / width
        sampled = []
        for i in range(width):
            idx = int(i * step)
            sampled.append(series[idx])
    else:
        sampled = series[:]

    mn, mx = min(sampled), max(sampled)
    if mx - mn < 1e-9:
        return "▁" * len(sampled)

    blocks = "▁▂▃▄▅▆▇█"
    out = []
    for v in sampled:
        t = (v - mn) / (mx - mn)  # 0..1
        out.append(blocks[int(t * (len(blocks) - 1))])
    return "".join(out)


def _trend_15d(data: List[float]) -> Optional[Dict[str, float]]:
    """
    15일(720개) 기준:
    - 전반 7.5일(360개) 평균 vs 후반 7.5일(360개) 평균 비교
    - 후반 < 전반이면 하락추세
    - 기울기(원/일), 각도(도)를 대략 계산
    """
    if len(data) < MAX_15D:
        return None

    w = data[-MAX_15D:]           # 최근 15일
    first = w[: MAX_15D // 2]     # 7.5일
    last = w[MAX_15D // 2 :]      # 7.5일

    a_first = sum(first) / len(first)
    a_last = sum(last) / len(last)

    # 7.5일 동안 평균이 얼마나 변했는지
    delta = a_last - a_first
    days = 7.5
    slope_per_day = delta / days  # 원/일 (JPY100 기준)

    # 각도: x=일(day), y=원(KRW) 기준의 기울기 각도(참고용)
    import math
    angle_deg = math.degrees(math.atan(slope_per_day))

    # 퍼센트(일): 기준을 전반 평균으로 둠
    pct_per_day = (slope_per_day / a_first) * 100.0 if a_first != 0 else 0.0

    return {
        "a_first": a_first,
        "a_last": a_last,
        "delta": delta,
        "slope_per_day": slope_per_day,
        "angle_deg": angle_deg,
        "pct_per_day": pct_per_day,
    }


def fetch_jpy100_item_with_date() -> Tuple[Dict[str, Optional[float]], str]:
    """
    최근 7일 중 데이터 있는 날짜를 찾아 JPY(100) 항목(가능하면 ttb/tts/deal)을 반환
    """
    authkey = os.getenv("EXIMBANK_API_KEY", "").strip()
    if not authkey:
        raise RuntimeError("EXIMBANK_API_KEY가 비어 있습니다.")

    url = "https://oapi.koreaexim.go.kr/site/program/financial/exchangeJSON"
    headers = {"User-Agent": "fx-alert-bot"}

    kst = ZoneInfo("Asia/Seoul")
    today = datetime.now(kst).date()

    last_err = None

    for day_back in range(0, 7):
        d = today - timedelta(days=day_back)
        searchdate = d.strftime("%Y%m%d")
        params = {"authkey": authkey, "searchdate": searchdate, "data": "AP01"}

        for i in range(3):
            try:
                r = requests.get(url, params=params, headers=headers, timeout=25)
                r.raise_for_status()
                data = r.json()

                if not isinstance(data, list) or len(data) == 0:
                    break

                for item in data:
                    if item.get("cur_unit") == "JPY(100)":
                        ttb = _to_float(item.get("ttb"))
                        tts = _to_float(item.get("tts"))
                        deal = _to_float(item.get("deal_bas_r"))

                        if deal is None:
                            raise RuntimeError("deal_bas_r(매매기준율) 파싱 실패")

                        mid = (ttb + tts) / 2.0 if (ttb is not None and tts is not None) else None

                        return {"deal": deal, "ttb": ttb, "tts": tts, "mid": mid}, searchdate

                break

            except Exception as e:
                last_err = e
                time.sleep(1.5 * (i + 1))

    raise RuntimeError(f"최근 7일 내 환율 데이터를 찾지 못했습니다: {last_err}")


def decide_signal(price: float, a15: Optional[float], a30: Optional[float], th: float):
    # 기존 로직 유지(30D 우선)
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

    try:
        rates, used_date = fetch_jpy100_item_with_date()
    except Exception as e:
        send_message(f"⚠️ 환율 수신 실패\n{e}")
        return

    # ✅ 기준 환율로 통일: deal_bas_r(매매기준율)
    price = rates["deal"]

    # 데이터 누적/슬라이딩
    data = load_data()
    data = append_and_trim(data, price)
    save_data(data)

    # 표시용 평균(부분)
    a15_show = avg_last_partial(data, MAX_15D)
    a30_show = avg_last_partial(data, MAX_30D)

    # 판정용 평균(정식: 충분할 때만)
    a15 = avg_last(data, MAX_15D)
    a30 = avg_last(data, MAX_30D)

    is_test = len(data) < MAX_15D

    # 추세 분석(15일 이상일 때만)
    trend = _trend_15d(data)

    # 최근 24시간 텍스트 그래프(48개 = 24시간)
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

    # ✅ 중간값은 “한 줄”만
    if rates.get("mid") is not None:
        lines.append(f"중간값(mid=(TTB+TTS)/2): {rates['mid']:.4f}")
    else:
        lines.append("중간값(mid=(TTB+TTS)/2): N/A")

    lines.append(f"15D 평균(현재까지): {a15_show:.4f}" if a15_show is not None else "15D 평균(현재까지): N/A")
    lines.append(f"30D 평균(현재까지): {a30_show:.4f}" if a30_show is not None else "30D 평균(현재까지): N/A")
    lines.append(f"데이터: {len(data)}/{MAX_30D} (30분봉)")

    # ✅ 추세 안내 문구
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

    # ✅ 텍스트 그래프(최근 24h)
    if chart:
        lines.append(f"최근 24h: {chart}")

    # 매수/매도 신호(기존 로직)
    state, sig = decide_signal(price, a15, a30, th)
    if sig:
        lines.append(sig)
        prev = load_state()
        if state != prev:
            save_state(state)

    send_message("\n".join(lines))


if __name__ == "__main__":
    main()
