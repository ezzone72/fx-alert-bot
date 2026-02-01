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
    load_data, save_data, append_and_trim,
    avg_last, avg_last_partial,
    MAX_15D, MAX_30D
)

load_dotenv()

STATE_FILE = "state.json"
ASSET_DIR = "assets"
ARROW_DIR = os.path.join(ASSET_DIR, "arrows")

URGENT_PCT = 0.5  # 직전 대비 ±0.5% 이상이면 긴급

# ✅ 통화
CURRENCIES = [
    ("JPY(100)", "JPY100"),
    ("USD", "USD"),
    ("AUD", "AUD"),
    ("CHF", "CHF"),
]


# ================= 기본 유틸 =================

def _get_threshold() -> float:
    v = os.getenv("THRESHOLD", "1.1").strip()
    try:
        return float(v)
    except ValueError:
        return 1.1


def _to_float(x: Any) -> Optional[float]:
    if x is None:
        return None
    try:
        return float(str(x).replace(",", "").strip())
    except Exception:
        return None


def _csv_name(code: str) -> str:
    return f"data_{code}.csv"


def load_state() -> Dict[str, str]:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            obj = json.load(f)
            return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def save_state_map(state_map: Dict[str, str]) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state_map, f, ensure_ascii=False)


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


def _sign_pct(p: Optional[float], eps: float = 0.01) -> int:
    """
    pct_per_day 기준으로 상승/하락/보합 판정.
    eps=0.01%/day 이하를 보합으로 봄(노이즈 컷)
    """
    if p is None:
        return 0
    if p > eps:
        return 1
    if p < -eps:
        return -1
    return 0


def _interpretation_label_7(t15: Optional[Dict[str, float]], t30: Optional[Dict[str, float]]) -> str:
    """
    해석지표 7개:
    - 상승유지
    - 상승율 둔화
    - 하락 전환
    - 보합
    - 상승전환
    - 하락 유지
    - 하락율 둔화
    """
    if t15 is None or t30 is None:
        return "보합"

    p15 = t15.get("pct_per_day")
    p30 = t30.get("pct_per_day")

    s15 = _sign_pct(p15)
    s30 = _sign_pct(p30)

    # 보합(둘 중 하나라도 거의 0이면 보합으로 처리)
    if s15 == 0 and s30 == 0:
        return "보합"
    if s15 == 0 and s30 != 0:
        # 장기방향 있는데 단기는 멈춤 -> 둔화로 보는 게 자연스러움
        return "상승율 둔화" if s30 > 0 else "하락율 둔화"
    if s30 == 0 and s15 != 0:
        # 장기는 flat인데 단기가 움직임 -> 전환 성격
        return "상승전환" if s15 > 0 else "하락 전환"

    # 변곡(주인님 조건)
    if s30 > 0 and s15 < 0:
        return "하락 전환"
    if s30 < 0 and s15 > 0:
        return "상승전환"

    # 같은 방향이면 "유지" vs "둔화"를 비교로 나눔
    # - 상승: 단기 상승률이 장기보다 약하면 둔화, 강하면 유지
    # - 하락: 단기 하락률(|p15|)이 장기보다 약하면 둔화, 강하면 유지
    if s30 > 0 and s15 > 0:
        if (p15 is not None) and (p30 is not None) and (p15 < p30):
            return "상승율 둔화"
        return "상승유지"

    if s30 < 0 and s15 < 0:
        ap15 = abs(p15) if p15 is not None else 0.0
        ap30 = abs(p30) if p30 is not None else 0.0
        if ap15 < ap30:
            return "하락율 둔화"
        return "하락 유지"

    return "보합"


def _fmt_pct(p: Optional[float]) -> str:
    if p is None:
        return "N/A"
    sign = "+" if p >= 0 else "-"
    return f"{sign}{abs(p):.3f}%/day"


# ================= 환율 API =================

def _fetch_ap01_for_date(searchdate: str, authkey: str) -> List[Dict[str, Any]]:
    url = "https://oapi.koreaexim.go.kr/site/program/financial/exchangeJSON"
    params = {"authkey": authkey, "searchdate": searchdate, "data": "AP01"}
    headers = {"User-Agent": "fx-alert-bot"}
    r = requests.get(url, params=params, headers=headers, timeout=25)
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else []


def _extract_rates(items: List[Dict[str, Any]], cur_unit: str) -> Optional[Dict[str, Optional[float]]]:
    for item in items:
        if item.get("cur_unit") == cur_unit:
            deal = _to_float(item.get("deal_bas_r"))
            if deal is None:
                return None
            ttb = _to_float(item.get("ttb"))
            tts = _to_float(item.get("tts"))
            mid = (ttb + tts) / 2.0 if (ttb is not None and tts is not None) else None
            return {"deal": deal, "mid": mid}
    return None


def fetch_latest_rates_with_date() -> Tuple[Dict[str, Dict[str, Optional[float]]], str]:
    authkey = os.getenv("EXIMBANK_API_KEY", "").strip()
    if not authkey:
        raise RuntimeError("EXIMBANK_API_KEY가 비어 있습니다.")

    kst = ZoneInfo("Asia/Seoul")
    today = datetime.now(kst).date()

    last_err = None
    for back in range(0, 7):
        d = today - timedelta(days=back)
        searchdate = d.strftime("%Y%m%d")
        try:
            items = _fetch_ap01_for_date(searchdate, authkey)
            if not items:
                continue

            out: Dict[str, Dict[str, Optional[float]]] = {}
            for cur_unit, code in CURRENCIES:
                r = _extract_rates(items, cur_unit)
                if r is not None:
                    out[code] = r

            if out:
                return out, searchdate

        except Exception as e:
            last_err = e
            time.sleep(1.0)

    raise RuntimeError(f"최근 7일 내 환율 데이터를 찾지 못했습니다: {last_err}")


# ================= 부트스트랩 (초기 30일 채우기) =================

def bootstrap_fill_30d_if_needed(series_map: Dict[str, List[float]]) -> Dict[str, List[float]]:
    authkey = os.getenv("EXIMBANK_API_KEY", "").strip()
    if not authkey:
        raise RuntimeError("EXIMBANK_API_KEY가 비어 있습니다.")

    need_codes = [code for _, code in CURRENCIES if len(series_map.get(code, [])) < MAX_30D]
    if not need_codes:
        return series_map

    kst = ZoneInfo("Asia/Seoul")
    today = datetime.now(kst).date()

    prefix_map: Dict[str, List[float]] = {code: [] for code in need_codes}

    for day_back in range(1, 120):
        if all(len(prefix_map[c]) >= MAX_30D for c in need_codes):
            break

        d = today - timedelta(days=day_back)
        searchdate = d.strftime("%Y%m%d")

        try:
            items = _fetch_ap01_for_date(searchdate, authkey)
            if not items:
                continue

            for cur_unit, code in CURRENCIES:
                if code not in need_codes or len(prefix_map[code]) >= MAX_30D:
                    continue
                r = _extract_rates(items, cur_unit)
                if r and r.get("deal") is not None:
                    prefix_map[code].extend([float(r["deal"])] * 48)

            time.sleep(0.12)
        except Exception:
            time.sleep(0.25)
            continue

    for code in need_codes:
        prefix_map[code] = list(reversed(prefix_map[code]))
        existing = series_map.get(code, [])
        series_map[code] = (prefix_map[code] + existing)[-MAX_30D:]

    return series_map


# ================= 신호 판정 =================

def decide_signal(price: float, a15: Optional[float], a30: Optional[float], th: float):
    if a30 is not None and price < a30 * th:
        return "BUY30", "BUY30"
    if a15 is not None and price < a15 * th:
        return "BUY15", "BUY15"
    if a30 is not None and price > a30 * th:
        return "SELL30", "SELL30"
    if a15 is not None and price > a15 * th:
        return "SELL15", "SELL15"
    return "NONE", None


# ================= 화살표 이미지 (10도 단위, 상하 18장) =================

def _ensure_dirs():
    os.makedirs(ARROW_DIR, exist_ok=True)
    os.makedirs(ASSET_DIR, exist_ok=True)


def _bucket_angle_deg_10(angle_deg: float) -> Tuple[int, int]:
    if abs(angle_deg) < 5.0:
        return 0, 0
    sign = 1 if angle_deg > 0 else -1
    a = abs(angle_deg)
    bucket = int(round(a / 10.0) * 10)
    if bucket < 10:
        bucket = 10
    if bucket > 90:
        bucket = 90
    return sign, bucket


def _draw_arrow_png(path: str, angle_deg: float, size: int = 120) -> None:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    cx, cy = size // 2, size // 2
    length = int(size * 0.35)
    thickness = max(3, size // 26)

    x1, y1 = cx - length, cy
    x2, y2 = cx + length, cy

    d.line((x1, y1, x2, y2), fill=(255, 255, 255, 255), width=thickness)

    head = int(size * 0.12)
    d.line((x2, y2, x2 - head, y2 - head // 2), fill=(255, 255, 255, 255), width=thickness)
    d.line((x2, y2, x2 - head, y2 + head // 2), fill=(255, 255, 255, 255), width=thickness)

    img = img.rotate(angle_deg, resample=Image.Resampling.BICUBIC, center=(cx, cy), expand=False)
    img.save(path, "PNG")


def _get_arrow_image_path_10(angle_deg: float) -> Optional[str]:
    _ensure_dirs()
    sign, bucket = _bucket_angle_deg_10(angle_deg)
    if sign == 0:
        return None
    tag = "up" if sign > 0 else "down"
    fname = f"{tag}_{bucket:02d}.png"
    path = os.path.join(ARROW_DIR, fname)
    if not os.path.exists(path):
        ang = float(bucket) if sign > 0 else -float(bucket)
        _draw_arrow_png(path, ang)
    return path


def _build_currency_trend_panel(code: str, angle15: float, angle30: float) -> str:
    """
    통화 1개용 이미지: (30D, 15D) 화살표를 한 장에 배치
    """
    _ensure_dirs()

    p15 = _get_arrow_image_path_10(angle15)
    p30 = _get_arrow_image_path_10(angle30)

    W, H = 420, 220
    panel = Image.new("RGBA", (W, H), (20, 20, 20, 255))
    d = ImageDraw.Draw(panel)

    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 22)
        font_mid = ImageFont.truetype("DejaVuSans.ttf", 18)
        font_small = ImageFont.truetype("DejaVuSans.ttf", 15)
    except Exception:
        font = ImageFont.load_default()
        font_mid = ImageFont.load_default()
        font_small = ImageFont.load_default()

    d.text((16, 12), f"{code} Trend (10° buckets)", fill=(255, 255, 255, 255), font=font)

    # 라벨: 주인님 요청대로 30D / 15D
    d.text((70, 60), "30D", fill=(255, 255, 255, 255), font=font_mid)
    d.text((265, 60), "15D", fill=(255, 255, 255, 255), font=font_mid)

    # 30D (왼쪽)
    if p30:
        img30 = Image.open(p30).convert("RGBA")
        panel.alpha_composite(img30, (45, 85))
    else:
        d.text((105, 110), "→", fill=(255, 255, 255, 255), font=font_mid)

    # 15D (오른쪽)
    if p15:
        img15 = Image.open(p15).convert("RGBA")
        panel.alpha_composite(img15, (240, 85))
    else:
        d.text((300, 110), "→", fill=(255, 255, 255, 255), font=font_mid)

    d.text((16, 195), "Up=+angle  Down=-angle  Flat(|angle|<5°)=→", fill=(200, 200, 200, 255), font=font_small)

    path = os.path.join(ASSET_DIR, f"trend_{code}.png")
    panel.save(path, "PNG")
    return path


# ================= 메인 =================

def main():
    th = _get_threshold()

    # 1) 통화별 CSV 로드
    series_map: Dict[str, List[float]] = {}
    for _, code in CURRENCIES:
        series_map[code] = load_data(_csv_name(code))

    # 2) 부족하면 30일 부트스트랩
    if any(len(series_map[code]) < MAX_30D for _, code in CURRENCIES):
        try:
            series_map = bootstrap_fill_30d_if_needed(series_map)
            for _, code in CURRENCIES:
                save_data(series_map[code], _csv_name(code))
        except Exception as e:
            send_message(f"⚠️ 부트스트랩 실패(과거데이터 채우기)\n{e}")

    # 3) 최신 환율(한 번에)
    try:
        latest_map, used_date = fetch_latest_rates_with_date()
    except Exception as e:
        send_message(f"⚠️ 환율 수신 실패\n{e}")
        return

    # 4) 긴급 체크(직전 대비)
    urgent_lines: List[str] = []
    urgent_any = False
    for cur_unit, code in CURRENCIES:
        r = latest_map.get(code)
        if not r or r.get("deal") is None:
            continue

        price = float(r["deal"])
        prev_series = series_map.get(code, [])
        prev_price = prev_series[-1] if prev_series else None

        if prev_price is not None and prev_price != 0:
            pct = (price - prev_price) / prev_price * 100.0
            if abs(pct) >= URGENT_PCT:
                urgent_any = True
                direction = "UP" if pct > 0 else "DOWN"
                urgent_lines.append(f"- {code}: {prev_price:.4f} → {price:.4f} ({pct:+.3f}%, {direction})")

    # 5) 데이터 반영 + 저장
    for cur_unit, code in CURRENCIES:
        r = latest_map.get(code)
        if not r or r.get("deal") is None:
            continue
        price = float(r["deal"])
        series_map[code] = append_and_trim(series_map.get(code, []), price, MAX_30D)
        save_data(series_map[code], _csv_name(code))

    # 6) 긴급이면: 긴급만 1건 (기존 정책 유지)
    if urgent_any:
        msg = "\n".join([
            "🚨 URGENT FX MOVE",
            "----------------------",
            f"date: {used_date}",
            "basis: deal_bas_r",
            "",
            *urgent_lines
        ])
        send_message(msg)
        return

    # 7) 신호가 있는 통화만 “통화별 메시지 + 통화별 이미지”로 발송
    state_map = load_state()

    for cur_unit, code in CURRENCIES:
        r = latest_map.get(code)
        if not r or r.get("deal") is None:
            continue

        series = series_map.get(code, [])
        if not series:
            continue

        price = float(r["deal"])

        a15 = avg_last(series, MAX_15D)
        a30 = avg_last(series, MAX_30D)

        # 신호
        state, sig = decide_signal(price, a15, a30, th)
        if sig is None:
            continue  # ✅ 신호 있을 때만 발송(스팸 방지)

        # 추세(15/30)
        t15 = _trend_window(series, MAX_15D, half_days=7.5)
        t30 = _trend_window(series, MAX_30D, half_days=15.0)

        # 해석지표(7개 중 1개)
        indicator = _interpretation_label_7(t15, t30)

        # 30D/15D “상승/하락율” = pct_per_day 사용
        p30 = t30["pct_per_day"] if t30 else None
        p15 = t15["pct_per_day"] if t15 else None

        # ✅ 주인님 요청: 텍스트 3줄만
        # 1) CODE: 현재가 (해석지표 1개 + BUY/SELL)
        # 2) 30day: +/- 하락율
        # 3) 15day: +/- 하락율
        line1 = f"{code}: {price:.4f} ({indicator}, {sig})"
        line2 = f"30day: {_fmt_pct(p30)}"
        line3 = f"15day: {_fmt_pct(p15)}"
        text = "\n".join([line1, line2, line3])

        # 이미지(30D, 15D) — 통화별 1장
        angle15 = float(t15["angle_deg"]) if t15 else 0.0
        angle30 = float(t30["angle_deg"]) if t30 else 0.0
        img_path = _build_currency_trend_panel(code, angle15=angle15, angle30=angle30)

        # state 저장(통화별)
        prev_state = state_map.get(code, "NONE")
        if state != prev_state:
            state_map[code] = state

        send_message(text, file_path=img_path, filename=f"trend_{code}.png")

    save_state_map(state_map)


if __name__ == "__main__":
    main()
