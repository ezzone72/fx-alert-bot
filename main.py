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
TREND_IMAGE_PATH = os.path.join(ASSET_DIR, "trend.png")

URGENT_PCT = 0.5  # 직전 대비 ±0.5% 이상이면 긴급

# ✅ 추가 통화
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
            return {"deal": deal, "mid": mid, "ttb": ttb, "tts": tts}
    return None


def fetch_latest_rates_with_date() -> Tuple[Dict[str, Dict[str, Optional[float]]], str]:
    """
    최근 7일(주말/공휴일 포함) 중 데이터 있는 날짜를 찾아,
    지정 통화들의 deal/mid를 한 번에 가져옵니다.
    """
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
    """
    각 통화별 CSV가 부족하면:
    과거 '일 단위 deal'을 가져와 하루값을 48번 반복하여 1440개로 채움.
    """
    authkey = os.getenv("EXIMBANK_API_KEY", "").strip()
    if not authkey:
        raise RuntimeError("EXIMBANK_API_KEY가 비어 있습니다.")

    # 어떤 통화가 부족한지 체크
    need_codes = [code for _, code in CURRENCIES if len(series_map.get(code, [])) < MAX_30D]
    if not need_codes:
        return series_map

    kst = ZoneInfo("Asia/Seoul")
    today = datetime.now(kst).date()

    prefix_map: Dict[str, List[float]] = {code: [] for code in need_codes}

    # 주말/공휴일 고려해 넉넉히 120일 범위
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
                if code not in need_codes:
                    continue
                if len(prefix_map[code]) >= MAX_30D:
                    continue

                r = _extract_rates(items, cur_unit)
                if r and r.get("deal") is not None:
                    prefix_map[code].extend([float(r["deal"])] * 48)

            time.sleep(0.12)
        except Exception:
            time.sleep(0.25)
            continue

    # prefix_map은 "최근 과거 → 더 과거" 순으로 쌓였으니 뒤집어서 과거→최근 정렬
    for code in need_codes:
        prefix_map[code] = list(reversed(prefix_map[code]))
        existing = series_map.get(code, [])
        series_map[code] = (prefix_map[code] + existing)[-MAX_30D:]

    return series_map


# ================= 신호 판정 =================

def decide_signal(price: float, a15: Optional[float], a30: Optional[float], th: float):
    # 30D 우선
    if a30 is not None and price < a30 * th:
        return "BUY30", "📉 매수 신호 (30D)"
    if a15 is not None and price < a15 * th:
        return "BUY15", "📉 매수 신호 (15D)"
    if a30 is not None and price > a30 * th:
        return "SELL30", "📈 매도 신호 (30D)"
    if a15 is not None and price > a15 * th:
        return "SELL15", "📈 매도 신호 (15D)"
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

    # +각도=위, -각도=아래
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


def _build_multi_trend_panel(angle_map_15: Dict[str, float], angle_map_30: Dict[str, float]) -> str:
    """
    4개 통화를 한 장에 표처럼 구성:
    rows: 통화, cols: 15D / 30D
    """
    _ensure_dirs()

    rows = [code for _, code in CURRENCIES]
    W, H = 520, 70 + 120 * len(rows)
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

    d.text((16, 12), "Trend Direction (10° buckets)", fill=(255, 255, 255, 255), font=font)
    d.text((220, 42), "15D", fill=(255, 255, 255, 255), font=font_mid)
    d.text((390, 42), "30D", fill=(255, 255, 255, 255), font=font_mid)

    y = 70
    for code in rows:
        d.text((16, y + 42), code, fill=(255, 255, 255, 255), font=font_mid)

        a15 = angle_map_15.get(code, 0.0)
        a30 = angle_map_30.get(code, 0.0)

        p15 = _get_arrow_image_path_10(a15)
        p30 = _get_arrow_image_path_10(a30)

        if p15:
            img15 = Image.open(p15).convert("RGBA")
            panel.alpha_composite(img15, (200, y))
        else:
            d.text((240, y + 46), "→", fill=(255, 255, 255, 255), font=font_mid)

        if p30:
            img30 = Image.open(p30).convert("RGBA")
            panel.alpha_composite(img30, (370, y))
        else:
            d.text((410, y + 46), "→", fill=(255, 255, 255, 255), font=font_mid)

        # 구분선
        d.line((16, y + 118, W - 16, y + 118), fill=(60, 60, 60, 255), width=1)
        y += 120

    d.text((16, H - 22), "Up=+angle  Down=-angle  Flat(|angle|<5°)=→", fill=(200, 200, 200, 255), font=font_small)

    panel.save(TREND_IMAGE_PATH, "PNG")
    return TREND_IMAGE_PATH


# ================= 메인 =================

def main():
    th = _get_threshold()

    # 1) 통화별 CSV 로드
    series_map: Dict[str, List[float]] = {}
    for _, code in CURRENCIES:
        series_map[code] = load_data(_csv_name(code))

    # 2) 부족하면 30일(1440개) 부트스트랩
    if any(len(series_map[code]) < MAX_30D for _, code in CURRENCIES):
        try:
            series_map = bootstrap_fill_30d_if_needed(series_map)
            for _, code in CURRENCIES:
                save_data(series_map[code], _csv_name(code))
        except Exception as e:
            # 부트스트랩 실패는 긴급이 아니라서 텍스트 1회만
            send_message(f"⚠️ 부트스트랩 실패(과거데이터 채우기)\n{e}")

    # 3) 최신 환율 가져오기(한 번에)
    try:
        latest_map, used_date = fetch_latest_rates_with_date()
    except Exception as e:
        send_message(f"⚠️ 환율 수신 실패\n{e}")
        return

    # 4) 긴급 체크(직전 대비 ±0.5%) — 통화별로
    urgent_lines: List[str] = []
    urgent_any = False

    for cur_unit, code in CURRENCIES:
        if code not in latest_map:
            continue

        price = latest_map[code]["deal"]
        if price is None:
            continue

        series = series_map.get(code, [])
        prev_price = series[-1] if series else None

        if prev_price is not None and prev_price != 0:
            pct = (price - prev_price) / prev_price * 100.0
            if abs(pct) >= URGENT_PCT:
                urgent_any = True
                direction = "상승" if pct > 0 else "하락"
                urgent_lines.append(
                    f"- {code}: {prev_price:.4f} → {price:.4f} ({pct:+.3f}%, {direction})"
                )

    # 5) 데이터 반영(30분마다 1개 append) + 저장
    for cur_unit, code in CURRENCIES:
        if code not in latest_map:
            continue
        price = latest_map[code]["deal"]
        if price is None:
            continue
        series_map[code] = append_and_trim(series_map.get(code, []), float(price), MAX_30D)
        save_data(series_map[code], _csv_name(code))

    # 6) 긴급이 있으면: “긴급만” 발송하고 리포트는 스킵 (주인님 요청)
    if urgent_any:
        msg = [
            "🚨 긴급 환율 변동",
            "----------------------",
            f"기준일: {used_date}",
            "기준: 매매기준율(deal_bas_r)",
            "",
            *urgent_lines
        ]
        send_message("\n".join(msg))
        return

    # 7) 신호(매수/매도) 통화만 모아서 리포트 1건 발송
    state_map = load_state()
    report_lines: List[str] = []
    angle15_map: Dict[str, float] = {}
    angle30_map: Dict[str, float] = {}

    any_signal = False

    for cur_unit, code in CURRENCIES:
        if code not in latest_map:
            continue

        series = series_map.get(code, [])
        if not series:
            continue

        price = float(latest_map[code]["deal"])
        mid = latest_map[code].get("mid")

        a15_show = avg_last_partial(series, MAX_15D)
        a30_show = avg_last_partial(series, MAX_30D)

        a15 = avg_last(series, MAX_15D)
        a30 = avg_last(series, MAX_30D)

        t15 = _trend_window(series, MAX_15D, half_days=7.5)
        t30 = _trend_window(series, MAX_30D, half_days=15.0)

        state, sig = decide_signal(price, a15, a30, th)
        if not sig:
            # 이미지에는 방향을 계속 찍고 싶으면 여기서도 넣을 수 있지만,
            # 주인님은 “신호 있을 때만” 리포트라서 이미지도 신호 있을 때만 보냅니다.
            continue

        any_signal = True

        # state 중복 방지(통화별)
        prev_state = state_map.get(code, "NONE")
        if state != prev_state:
            state_map[code] = state

        report_lines.append(f"## {code}")
        report_lines.append(f"- 현재(deal): {price:.4f}")
        report_lines.append(f"- 중간값(mid): {mid:.4f}" if mid is not None else "- 중간값(mid): N/A")
        report_lines.append(f"- 15D 평균: {a15_show:.4f}" if a15_show is not None else "- 15D 평균: N/A")
        report_lines.append(f"- 30D 평균: {a30_show:.4f}" if a30_show is not None else "- 30D 평균: N/A")
        report_lines.append(f"- 데이터: {len(series)}/{MAX_30D}")

        if t15 is not None:
            direction15 = "하락추세" if t15["a_last"] < t15["a_first"] else "상승/횡보"
            report_lines.append(
                f"- 추세(15D): {direction15} | 전반7.5D {t15['a_first']:.4f} → 후반7.5D {t15['a_last']:.4f}"
            )
            report_lines.append(
                f"  - 기울기(15D): {t15['slope_per_day']:+.4f} 원/일 ({t15['pct_per_day']:+.3f}%/일) | 각도: {t15['angle_deg']:+.2f}°"
            )
            angle15_map[code] = float(t15["angle_deg"])
        else:
            report_lines.append("- 추세(15D): 데이터 부족")
            angle15_map[code] = 0.0

        if t30 is not None:
            direction30 = "하락추세" if t30["a_last"] < t30["a_first"] else "상승/횡보"
            report_lines.append(
                f"- 추세(30D): {direction30} | 전반15D {t30['a_first']:.4f} → 후반15D {t30['a_last']:.4f}"
            )
            report_lines.append(
                f"  - 기울기(30D): {t30['slope_per_day']:+.4f} 원/일 ({t30['pct_per_day']:+.3f}%/일) | 각도: {t30['angle_deg']:+.2f}°"
            )
            angle30_map[code] = float(t30["angle_deg"])
        else:
            report_lines.append("- 추세(30D): 데이터 부족")
            angle30_map[code] = 0.0

        report_lines.append(f"- 신호: {sig}")
        report_lines.append("")

    if any_signal:
        # state 저장
        save_state_map(state_map)

        header = [
            "📊 환율 신호 리포트 (신호 발생 통화만)",
            "----------------------",
            f"기준일: {used_date}",
            "기준: 매매기준율(deal_bas_r)",
            ""
        ]
        msg = "\n".join(header + report_lines).strip()

        # 신호 통화들만 이미지 구성(표 형태)
        try:
            # angle_map에 없는 통화는 0으로
            for _, code in CURRENCIES:
                angle15_map.setdefault(code, 0.0)
                angle30_map.setdefault(code, 0.0)

            img_path = _build_multi_trend_panel(angle15_map, angle30_map)
            send_message(msg, file_path=img_path, filename="trend.png")
        except Exception as e:
            send_message(msg + f"\n(이미지 생성 실패: {e})")


if __name__ == "__main__":
    main()
