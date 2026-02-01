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

URGENT_PCT = 0.5  # 직전 값 대비 0.5% 이상 변동 시 긴급 알림


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


def _trend_window(data: List[float], window_n: int, half_days: float) -> Optional[Dict[str, float]]:
    if len(data) < window_n:
        return None

    w = data[-window_n:]
    half = window_n // 2
    first = w[:half]
    last = w[half:]

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
    if len(data) >= MAX_30D:
        return data

    authkey = os.getenv("EXIMBANK_API_KEY", "").strip()
    if not authkey:
        raise RuntimeError("EXIMBANK_API_KEY가 비어 있습니다.")

    need = MAX_30D - len(data)
    days_needed = (need + 47) // 48

    kst = ZoneInfo("Asia/Seoul")
    today = datetime.now(kst).date()

    prefix: List[float] = []
    got_days = 0

    for day_back in range(1, 90 + 1):
        if got_days >= days_needed:
            break

        d = today - timedelta(days=day_back)
        searchdate = d.strftime("%Y%m%d")

        try:
            item = _fetch_jpy100_item_exact_date(searchdate, authkey)
            if item is None:
                continue

            deal = item["deal"]
            if deal is None:
                continue

            prefix.extend([deal] * 48)
            got_days += 1
            time.sleep(0.15)

        except Exception:
            time.sleep(0.3)
            continue

    if not prefix:
        return data

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


# ========= 화살표 이미지 (10도 단위, 상하 18장) =========

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


def _draw_arrow_png(path: str, angle_deg: float, size: int = 160) -> None:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    cx, cy = size // 2, size // 2
    length = int(size * 0.35)
    thickness = max(3, size // 28)

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


def _build_trend_panel(angle15: float, angle30: float) -> str:
    _ensure_dirs()

    p15 = _get_arrow_image_path_10(angle15)
    p30 = _get_arrow_image_path_10(angle30)

    W, H = 420, 230
    panel = Image.new("RGBA", (W, H), (20, 20, 20, 255))
    d = ImageDraw.Draw(panel)

    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 22)
        font_small = ImageFont.truetype("DejaVuSans.ttf", 16)
        font_mid = ImageFont.truetype("DejaVuSans.ttf", 18)
    except Exception:
        font = ImageFont.load_default()
        font_small = ImageFont.load_default()
        font_mid = ImageFont.load_default()

    d.text((16, 12), "Trend Direction (10° buckets)", fill=(255, 255, 255, 255), font=font)

    d.text((55, 65), "15D", fill=(255, 255, 255, 255), font=font)
    d.text((265, 65), "30D", fill=(255, 255, 255, 255), font=font)

    if p15:
        img15 = Image.open(p15).convert("RGBA")
        panel.alpha_composite(img15, (30, 90))
    else:
        d.text((70, 120), "→", fill=(255, 255, 255, 255), font=font_mid)

    if p30:
        img30 = Image.open(p30).convert("RGBA")
        panel.alpha_composite(img30, (240, 90))
    else:
        d.text((280, 120), "→", fill=(255, 255, 255, 255), font=font_mid)

    d.text((16, 205), "Up=+angle  Down=-angle  Flat(|angle|<5°)=→", fill=(200, 200, 200, 255), font=font_small)

    panel.save(TREND_IMAGE_PATH, "PNG")
    return TREND_IMAGE_PATH


def main():
    th = _get_threshold()

    data = load_data()

    # 부족하면 30일 채우기(최초 1회)
    if len(data) < MAX_30D:
        try:
            data = bootstrap_fill_30d_if_needed(data)
            save_data(data)
        except Exception as e:
            # 부트스트랩 실패는 긴급은 아니므로 텍스트만 1회 안내
            send_message(f"⚠️ 부트스트랩 실패(과거데이터 채우기)\n{e}")

    # 최신 값 조회
    try:
        rates, used_date = fetch_jpy100_item_with_date_fallback()
    except Exception as e:
        send_message(f"⚠️ 환율 수신 실패\n{e}")
        return

    price = rates["deal"]
    if price is None:
        send_message("⚠️ deal_bas_r(매매기준율) 값이 없습니다.")
        return

    # ===== 긴급 변동 체크(직전 값 대비) =====
    prev_price = data[-1] if data else None
    urgent_trigger = False
    urgent_msg = None
    if prev_price is not None and prev_price != 0:
        pct = (price - prev_price) / prev_price * 100.0
        if abs(pct) >= URGENT_PCT:
            urgent_trigger = True
            direction = "상승" if pct > 0 else "하락"
            urgent_msg = (
                "🚨 긴급 변동 감지\n"
                "----------------------\n"
                f"JPY(100)/KRW (기준: 매매기준율)\n"
                f"기준일: {used_date}\n"
                f"직전: {prev_price:.4f}\n"
                f"현재: {price:.4f}\n"
                f"변동: {pct:+.3f}% ({direction})"
            )

    # ===== 데이터 반영(30분마다 1개) =====
    data = append_and_trim(data, price)
    save_data(data)

    # 평균(표시/판정)
    a15_show = avg_last_partial(data, MAX_15D)
    a30_show = avg_last_partial(data, MAX_30D)
    a15 = avg_last(data, MAX_15D)
    a30 = avg_last(data, MAX_30D)

    # 추세
    t15 = _trend_window(data, MAX_15D, half_days=7.5)
    t30 = _trend_window(data, MAX_30D, half_days=15.0)

    # 신호
    state, sig = decide_signal(price, a15, a30, th)

    # ===== 1번 적용: 리포트는 “신호 있을 때만” 발송 =====
    report_trigger = sig is not None

    # 1) 긴급이면 즉시 발송(신호 없어도)
    if urgent_trigger and urgent_msg:
        send_message(urgent_msg)

    # 2) 신호가 있으면 리포트 + 이미지 발송
    if report_trigger:
        lines = []
        lines.append("📊 엔화 환율 신호 리포트 (전신환/기준환율 기준)")
        lines.append("----------------------")
        lines.append("JPY(100) / KRW")
        lines.append(f"기준일: {used_date}")
        lines.append(f"매매기준율(deal): {price:.4f}")
        if rates.get("mid") is not None:
            lines.append(f"중간값(mid=(TTB+TTS)/2): {rates['mid']:.4f}")
        else:
            lines.append("중간값(mid=(TTB+TTS)/2): N/A")

        lines.append(f"15D 평균(현재까지): {a15_show:.4f}" if a15_show is not None else "15D 평균(현재까지): N/A")
        lines.append(f"30D 평균(현재까지): {a30_show:.4f}" if a30_show is not None else "30D 평균(현재까지): N/A")
        lines.append(f"데이터: {len(data)}/{MAX_30D} (30분봉)")

        # 15D
        if t15 is None:
            lines.append("추세(15D): 데이터 부족(15일 필요)")
            angle15 = 0.0
        else:
            direction15 = "하락추세" if t15["a_last"] < t15["a_first"] else "상승/횡보"
            lines.append(f"추세(15D): {direction15} | 전반7.5D {t15['a_first']:.4f} → 후반7.5D {t15['a_last']:.4f}")
            lines.append(f"기울기(15D): {t15['slope_per_day']:+.4f} 원/일 ({t15['pct_per_day']:+.3f}%/일) | 각도: {t15['angle_deg']:+.2f}°")
            angle15 = t15["angle_deg"]

        # 30D
        if t30 is None:
            lines.append("추세(30D): 데이터 부족(30일 필요)")
            angle30 = 0.0
        else:
            direction30 = "하락추세" if t30["a_last"] < t30["a_first"] else "상승/횡보"
            lines.append(f"추세(30D): {direction30} | 전반15D {t30['a_first']:.4f} → 후반15D {t30['a_last']:.4f}")
            lines.append(f"기울기(30D): {t30['slope_per_day']:+.4f} 원/일 ({t30['pct_per_day']:+.3f}%/일) | 각도: {t30['angle_deg']:+.2f}°")
            angle30 = t30["angle_deg"]

        # 최근 24h 스파크라인(신호 리포트에서만)
        last_48 = data[-48:] if len(data) >= 48 else data[:]
        chart = _sparkline(last_48, width=min(48, len(last_48))) if last_48 else ""
        if chart:
            lines.append(f"최근 24h: {chart}")

        # 신호 문구
        lines.append(sig)

        # 상태 중복 방지(신호 바뀔 때만 갱신)
        prev = load_state()
        if state != prev:
            save_state(state)

        # 방향 이미지 첨부
        try:
            img_path = _build_trend_panel(angle15, angle30)
            send_message("\n".join(lines), file_path=img_path, filename="trend.png")
        except Exception as e:
            send_message("\n".join(lines) + f"\n(이미지 생성 실패: {e})")


if __name__ == "__main__":
    main()
