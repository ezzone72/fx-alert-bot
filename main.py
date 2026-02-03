import os
import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Optional, Tuple, Dict, Any, List

import yfinance as yf
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont

from notifier import send_message
from storage import (
    load_data, save_data, append_and_trim,
    avg_last, MAX_15D, MAX_30D
)

load_dotenv()

STATE_FILE = "state.json"
ASSET_DIR = "assets"
ARROW_DIR = os.path.join(ASSET_DIR, "arrows")

URGENT_PCT = 0.5  # 직전 대비 ±0.5% 이상이면 긴급

# 야후 파이낸스 티커 매핑
# (대상 코드, 야후 티커)
CURRENCY_TICKERS = [
    ("JPY100", "JPYKRW=X"),
    ("USD", "USDKRW=X"),
    ("AUD", "AUDKRW=X"),
    ("CHF", "CHFKRW=X"),
]

# ================= 기본 유틸 =================

def _get_threshold() -> float:
    v = os.getenv("THRESHOLD", "1.01").strip()
    try:
        return float(v)
    except ValueError:
        return 1.01

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

def _normalize_side(x: Optional[str]) -> Optional[str]:
    if not x: return None
    s = str(x).upper().strip()
    if s.startswith("BUY"): return "BUY"
    if s.startswith("SELL"): return "SELL"
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
        "a_first": a_first, "a_last": a_last, "delta": delta,
        "slope_per_day": slope_per_day, "angle_deg": angle_deg, "pct_per_day": pct_per_day,
    }

def _sign_pct(p: Optional[float], eps: float = 0.01) -> int:
    if p is None: return 0
    if p > eps: return 1
    if p < -eps: return -1
    return 0

def _interpretation_label_7_en(t15: Optional[Dict[str, float]], t30: Optional[Dict[str, float]]) -> str:
    if t15 is None or t30 is None: return "Flat"
    p15, p30 = t15.get("pct_per_day"), t30.get("pct_per_day")
    s15, s30 = _sign_pct(p15), _sign_pct(p30)

    if s15 == 0 and s30 == 0: return "Flat"
    if s15 == 0 and s30 != 0: return "Uptrend Slowing" if s30 > 0 else "Downtrend Slowing"
    if s30 == 0 and s15 != 0: return "Turning Up" if s15 > 0 else "Turning Down"
    if s30 > 0 and s15 < 0: return "Turning Down"
    if s30 < 0 and s15 > 0: return "Turning Up"
    if s30 > 0 and s15 > 0:
        return "Uptrend Slowing" if (p15 < p30) else "Uptrend Sustained"
    if s30 < 0 and s15 < 0:
        return "Downtrend Slowing" if (abs(p15) < abs(p30)) else "Downtrend Sustained"
    return "Flat"

def _fmt_pct(p: Optional[float]) -> str:
    if p is None: return "N/A"
    return f"{'+' if p >= 0 else '-'}{abs(p):.3f}%/day"

# ================= 야후 파이낸스 환율 API (핵심 교체) =================

def fetch_latest_rates_yahoo() -> Tuple[Dict[str, Dict[str, float]], str]:
    """
    야후 파이낸스를 통해 실시간 환율을 가져옵니다.
    """
    kst = ZoneInfo("Asia/Seoul")
    now_kst = datetime.now(kst)
    out: Dict[str, Dict[str, float]] = {}

    for code, ticker in CURRENCY_TICKERS:
        try:
            yt = yf.Ticker(ticker)
            # 최신 1일치 데이터를 분 단위로 가져와 마지막 가격 선택
            df = yt.history(period="1d", interval="1m")
            if df.empty:
                # 1일 데이터가 없으면 5일치에서 마지막 데이터를 가져옴 (주말 대비)
                df = yt.history(period="5d", interval="1m")
            
            if not df.empty:
                current_price = float(df['Close'].iloc[-1])
                # JPY100 단위 보정
                if code == "JPY100":
                    current_price *= 100
                out[code] = {"deal": current_price}
            else:
                print(f"Warning: No data found for {ticker}")
        except Exception as e:
            print(f"Error fetching {ticker}: {e}")
            continue

    if not out:
        raise RuntimeError("모든 통화에 대한 야후 파이낸스 데이터 호출에 실패했습니다.")

    return out, now_kst.strftime("%Y%m%d")

# ================= 신호 및 시각화 (기존 유지) =================

def decide_signal(price: float, a15: Optional[float], a30: Optional[float], th: float) -> Optional[str]:
    if a30 is not None and price < a30 * (2 - th): return "BUY30" # th가 1.01이면 0.99
    if a15 is not None and price < a15 * (2 - th): return "BUY15"
    if a30 is not None and price > a30 * th: return "SELL30"
    if a15 is not None and price > a15 * th: return "SELL15"
    return None

def _sig_to_side(sig: str) -> str:
    return "BUY" if sig.startswith("BUY") else "SELL"

def _sig_to_emoji(sig: str) -> Tuple[str, str, str]:
    side = _sig_to_side(sig)
    basis = "30D" if sig.endswith("30") else "15D"
    return ("🟢", "BUY", basis) if side == "BUY" else ("🔴", "SELL", basis)

def _ensure_dirs():
    os.makedirs(ARROW_DIR, exist_ok=True)
    os.makedirs(ASSET_DIR, exist_ok=True)

def _bucket_angle_deg_10(angle_deg: float) -> Tuple[int, int]:
    if abs(angle_deg) < 5.0: return 0, 0
    sign = 1 if angle_deg > 0 else -1
    bucket = int(round(abs(angle_deg) / 10.0) * 10)
    bucket = max(10, min(90, bucket))
    return sign, bucket

def _draw_arrow_png(path: str, angle_deg: float, size: int = 120) -> None:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    cx, cy = size // 2, size // 2
    length = int(size * 0.35)
    thickness = max(3, size // 26)
    x1, y1, x2, y2 = cx - length, cy, cx + length, cy
    d.line((x1, y1, x2, y2), fill=(255, 255, 255, 255), width=thickness)
    head = int(size * 0.12)
    d.line((x2, y2, x2 - head, y2 - head // 2), fill=(255, 255, 255, 255), width=thickness)
    d.line((x2, y2, x2 - head, y2 + head // 2), fill=(255, 255, 255, 255), width=thickness)
    img = img.rotate(angle_deg, resample=Image.Resampling.BICUBIC, center=(cx, cy), expand=False)
    img.save(path, "PNG")

def _get_arrow_image_path_10(angle_deg: float) -> Optional[str]:
    _ensure_dirs()
    sign, bucket = _bucket_angle_deg_10(angle_deg)
    if sign == 0: return None
    fname = f"{'up' if sign > 0 else 'down'}_{bucket:02d}.png"
    path = os.path.join(ARROW_DIR, fname)
    if not os.path.exists(path):
        _draw_arrow_png(path, float(bucket) if sign > 0 else -float(bucket))
    return path

def _build_currency_trend_panel(code: str, angle15: float, angle30: float) -> str:
    _ensure_dirs()
    p15, p30 = _get_arrow_image_path_10(angle15), _get_arrow_image_path_10(angle30)
    W, H = 420, 220
    panel = Image.new("RGBA", (W, H), (20, 20, 20, 255))
    d = ImageDraw.Draw(panel)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 22)
        font_mid = ImageFont.truetype("DejaVuSans.ttf", 18)
        font_small = ImageFont.truetype("DejaVuSans.ttf", 15)
    except:
        font = font_mid = font_small = ImageFont.load_default()

    d.text((16, 12), f"{code} Trend", fill=(255, 255, 255, 255), font=font)
    d.text((70, 60), "30D", fill=(255, 255, 255, 255), font=font_mid)
    d.text((265, 60), "15D", fill=(255, 255, 255, 255), font=font_mid)
    if p30: panel.alpha_composite(Image.open(p30).convert("RGBA"), (45, 85))
    else: d.text((105, 110), "→", fill=(255, 255, 255, 255), font=font_mid)
    if p15: panel.alpha_composite(Image.open(p15).convert("RGBA"), (240, 85))
    else: d.text((300, 110), "→", fill=(255, 255, 255, 255), font=font_mid)
    d.text((16, 195), "Up=+angle  Down=-angle  Flat(|angle|<5°)=→", fill=(200, 200, 200, 255), font=font_small)
    path = os.path.join(ASSET_DIR, f"trend_{code}.png")
    panel.save(path, "PNG")
    return path

# ================= 메인 =================

def main():
    th = _get_threshold()
    series_map = {code: load_data(_csv_name(code)) for _, code in CURRENCY_TICKERS}

    # 1) 야후 파이낸스로 환율 데이터 가져오기 (수은 로직 대체)
    try:
        latest_map, used_date = fetch_latest_rates_yahoo()
    except Exception as e:
        send_message(f"⚠️ Yahoo FX fetch failed\n{e}")
        return

    # 2) 긴급 체크 및 데이터 반영
    urgent_lines, urgent_any = [], False
    state_changed = False

    for code, _ in CURRENCY_TICKERS:
        r = latest_map.get(code)
        if not r: continue
        price = r["deal"]
        prev_series = series_map.get(code, [])
        
        # 중복 데이터 방지 (마지막 가격과 동일하면 스킵)
        if prev_series and prev_series[-1] == price:
            continue

        # 긴급 변동성 체크
        if prev_series and prev_series[-1] != 0:
            pct = (price - prev_series[-1]) / prev_series[-1] * 100.0
            if abs(pct) >= URGENT_PCT:
                urgent_any = True
                urgent_lines.append(f"- {code}: {prev_series[-1]:.2f} → {price:.2f} ({pct:+.3f}%)")

        # 데이터 저장
        series_map[code] = append_and_trim(prev_series, price, MAX_30D)
        save_data(series_map[code], _csv_name(code))
        state_changed = True

    if urgent_any:
        send_message(f"🚨 URGENT FX MOVE\ndate: {used_date}\n" + "\n".join(urgent_lines))

    # 3) 신호 판정 및 디스코드 발송
    state_map = load_state()
    for code, _ in CURRENCY_TICKERS:
        r = latest_map.get(code)
        if not r: continue
        series = series_map.get(code, [])
        if not series: continue

        price = r["deal"]
        a15, a30 = avg_last(series, MAX_15D), avg_last(series, MAX_30D)
        sig = decide_signal(price, a15, a30, th)
        
        if sig:
            curr_side = _normalize_side(_sig_to_side(sig))
            prev_side = _normalize_side(state_map.get(code))
            
            if curr_side and curr_side != prev_side:
                t15 = _trend_window(series, MAX_15D, 7.5)
                t30 = _trend_window(series, MAX_30D, 15.0)
                emoji, side, basis = _sig_to_emoji(sig)
                
                text = (f"{code}: {price:.2f} | {emoji} {side} ALERT ({basis}) | {_interpretation_label_7_en(t15, t30)}\n"
                        f"30day: {_fmt_pct(t30['pct_per_day'] if t30 else None)}\n"
                        f"15day: {_fmt_pct(t15['pct_per_day'] if t15 else None)}")
                
                img_path = _build_currency_trend_panel(code, t15["angle_deg"] if t15 else 0.0, t30["angle_deg"] if t30 else 0.0)
                send_message(text, file_path=img_path, filename=f"trend_{code}.png")
                
                state_map[code] = curr_side
                state_changed = True

    if state_changed:
        save_state_map(state_map)

if __name__ == "__main__":
    main()
