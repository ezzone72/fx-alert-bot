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
    """
    window_n: 15D=720, 30D=1440
    half_days: 15D는 7.5, 30D는 15.0 (전반/후반 비교 구간 길이)
    """
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


# ========= 화살표 이미지 =========

def _ensure_dirs():
    os.makedirs(ARROW_DIR, exist_ok=True)


def _bucket_angle_deg(angle_deg: float) -> Tuple[int, int]:
    """
    5°~70°만 사용(총 14개). 각도는 숫자 출력은 그대로 유지하되,
    이미지는 이 버킷으로 매핑.
    반환: (sign, bucket) where sign=+1 or -1 or 0, bucket in {5,10,...,70}
    """
    if angle_deg > 0:
        sign = 1
    elif angle_deg < 0:
        sign = -1
    else:
        sign = 0

    a = abs(angle_deg)
    if a < 2.5:
        # 너무 작은 건 5도로 올리지 않고 "거의 횡보"로 취급
        return sign, 5  # 요청상 14개만 쓰기 위해 5도로 붙입니다(거의 flat도 약한 화살표)
    # 가장 가까운 5도
    bucket = int(round(a / 5.0) * 5)
    if bucket < 5:
        bucket = 5
    if bucket > 70:
        bucket = 70
    return sign, bucket


def _draw_arrow_png(path: str, angle_deg: float, size: int = 160) -> None:
    """
    투명 배경 PNG에 오른쪽 방향 화살표를 그리고 angle_deg만큼 회전한 모양을 저장.
    (각도는 +면 위로, -면 아래로)
    """
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    cx, cy = size // 2, size // 2
    length = int(size * 0.35)
    thickness = max(3, size // 28)

    # 기본: 오른쪽 화살표(0도)
    x1, y1 = cx - length, cy
    x2, y2 = cx + length, cy

    # 선
    d.line((x1, y1, x2, y2), fill=(255, 255, 255, 255), width=thickness)

    # 화살촉
    head = int(size * 0.12)
    d.line((x2, y2, x2 - head, y2 - head // 2), fill=(255, 255, 255, 255), width=thickness)
    d.line((x2, y2, x2 - head, y2 + head // 2), fill=(255, 255, 255, 255), width=thickness)

    # 회전(중심 기준)
    img = img.rotate(-angle_deg, resample=Image.Resampling.BICUBIC, center=(cx, cy), expand=False)
    img.save(path, "PNG")


def _get_arrow_image_path(angle_deg: float) -> str:
    """
    버킷(5~70)과 부호에 따라 캐시된 PNG를 반환.
    없으면 생성.
    """
    _ensure_dirs()
    sign, bucket = _bucket_angle_deg(angle_deg)
    # 부호별 파일명(실제로는 14개(버킷) + 필요시 down도 생성)
    # 사용자가 "14개"를 원한 취지를 살려, 기본은 magnitude 14개를 만들고,
    # down은 필요할 때만 생성(파일 수는 늘 수 있음)
    tag = "up" if sign >= 0 else "down"
    fname = f"{tag}_{bucket:02d}.png"
    path = os.path.join(ARROW_DIR, fname)

    if not os.path.exists(path):
        # up: +bucket도, down: -bucket도
        ang = float(bucket) if sign >= 0 else -float(bucket)
        _draw_arrow_png(path, ang)

    return path


def _build_trend_panel(angle15: float, angle30: float) -> str:
    """
    15D/30D 화살표 2개를 한 이미지로 합쳐서 저장.
    """
    _ensure_dirs()
    img15 = Image.open(_get_arrow_image_path(angle15)).convert("RGBA")
    img30 = Image.open(_get_arrow_image_path(angle30)).convert("RGBA")

    W, H = 420, 220
    panel = Image.new("RGBA", (W, H), (20, 20, 20, 255))
    d = ImageDraw.Draw(panel)

    # 폰트(시스템 기본)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 22)
        font_small = ImageFont.truetype("DejaVuSans.ttf", 16)
    except Exception:
        font = ImageFont.load_default()
        font_small = ImageFont.load_default()

    # 타이틀
    d.text((16, 12), "Trend Direction", fill=(255, 255, 255, 255), font=font)

    # 라벨 + 이미지 배치
    d.text((40, 70), "15D", fill=(255, 255, 255, 255), font=font)
    d.text((250, 70), "30D", fill=(255, 255, 255, 255), font=font)

    panel.alpha_composite(img15, (30, 95))
    panel.alpha_composite(img30, (240, 95))

    # 안내(각도 숫자는 메시지에 그대로 있고, 여기서는 방향만)
    d.text((16, 195), "Arrows are bucketed to 5° steps (5°~70°).", fill=(200, 200, 200, 255), font=font_small)

    os.makedirs(ASSET_DIR, exist_ok=True)
    panel.save(TREND_IMAGE_PATH, "PNG")
    return TREND_IMAGE_PATH


def main():
    th = _get_threshold()

    data = load_data()

    if len(data) < MAX_30D:
        try:
            data = bootstrap_fill_30d_if_needed(data)
            save_data(data)
        except Exception as e:
            send_message(f"⚠️ 부트스트랩 실패(과거데이터 채우기)\n{e}")

    try:
        rates, used_date = fetch_jpy100_item_with_date_fallback()
    except Exception as e:
        send_message(f"⚠️ 환율 수신 실패\n{e}")
        return

    # 기준환율: deal_bas_r
    price = rates["deal"]
    if price is None:
        send_message("⚠️ deal_bas_r(매매기준율) 값이 없습니다.")
        return

    data = append_and_trim(data, price)
    save_data(data)

    a15_show = avg_last_partial(data, MAX_15D)
    a30_show = avg_last_partial(data, MAX_30D)

    a15 = avg_last(data, MAX_15D)
    a30 = avg_last(data, MAX_30D)

    is_test = len(data) < MAX_15D

    # 15D 추세(전반7.5D vs 후반7.5D)
    t15 = _trend_window(data, MAX_15D, half_days=7.5)
    # 30D 추세(전반15D vs 후반15D)
    t30 = _trend_window(data, MAX_30D, half_days=15.0)

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

    # 중간값 한 줄
    if rates.get("mid") is not None:
        lines.append(f"중간값(mid=(TTB+TTS)/2): {rates['mid']:.4f}")
    else:
        lines.append("중간값(mid=(TTB+TTS)/2): N/A")

    lines.append(f"15D 평균(현재까지): {a15_show:.4f}" if a15_show is not None else "15D 평균(현재까지): N/A")
    lines.append(f"30D 평균(현재까지): {a30_show:.4f}" if a30_show is not None else "30D 평균(현재까지): N/A")
    lines.append(f"데이터: {len(data)}/{MAX_30D} (30분봉)")

    # 추세 15D
    if t15 is None:
        lines.append("추세(15D): 데이터 부족(15일 필요)")
        angle15 = 0.0
    else:
        direction15 = "하락추세" if t15["a_last"] < t15["a_first"] else "상승/횡보"
        lines.append(
            f"추세(15D): {direction15} | 전반7.5D {t15['a_first']:.4f} → 후반7.5D {t15['a_last']:.4f}"
        )
        lines.append(
            f"기울기(15D): {t15['slope_per_day']:+.4f} 원/일 ({t15['pct_per_day']:+.3f}%/일) | 각도: {t15['angle_deg']:+.2f}°"
        )
        angle15 = t15["angle_deg"]

    # 추세 30D
    if t30 is None:
        lines.append("추세(30D): 데이터 부족(30일 필요)")
        angle30 = 0.0
    else:
        direction30 = "하락추세" if t30["a_last"] < t30["a_first"] else "상승/횡보"
        lines.append(
            f"추세(30D): {direction30} | 전반15D {t30['a_first']:.4f} → 후반15D {t30['a_last']:.4f}"
        )
        lines.append(
            f"기울기(30D): {t30['slope_per_day']:+.4f} 원/일 ({t30['pct_per_day']:+.3f}%/일) | 각도: {t30['angle_deg']:+.2f}°"
        )
        angle30 = t30["angle_deg"]

    if chart:
        lines.append(f"최근 24h: {chart}")

    state, sig = decide_signal(price, a15, a30, th)
    if sig:
        lines.append(sig)
        prev = load_state()
        if state != prev:
            save_state(state)

    # ✅ 방향성 이미지를 생성해서 첨부(15D/30D 둘 다)
    try:
        img_path = _build_trend_panel(angle15, angle30)
        send_message("\n".join(lines), file_path=img_path, filename="trend.png")
    except Exception as e:
        # 이미지 실패해도 텍스트는 보내기
        send_message("\n".join(lines) + f"\n(이미지 생성 실패: {e})")


if __name__ == "__main__":
    main()
