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

URGENT_PCT = 0.5  # 직전 대비 ±0.5% 이상이면 긴급 알림

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

def _fmt_pct(p: Optional[float]) -> str:
    if p is None: return "N/A"
    return f"{'+' if p >= 0 else '-'}{abs(p):.3f}%/day"

def _interpretation_label_7_en(t15: Optional[Dict[str, float]], t30: Optional[Dict[str, float]]) -> str:
    if t15 is None or t30 is None: return "Flat"
    def _sign(p, eps=0.01):
        if p is None or abs(p) <= eps: return 0
        return 1 if p > 0 else -1
    s15, s30 = _sign(t15.get("pct_per_day")), _sign(t30.get("pct_per_day"))
    if s15 == 0 and s30 == 0: return "Flat"
    if s30 > 0 and s15 < 0: return "Turning Down"
    if s30 < 0 and s15 > 0: return "Turning Up"
    if s30 > 0 and s15 > 0: return "Uptrend Sustained" if t15["pct_per_day"] >= t30["pct_per_day"] else "Uptrend Slowing"
    if s30 < 0 and s15 < 0: return "Downtrend Sustained" if abs(t15["pct_per_day"]) >= abs(t30["pct_per_day"]) else "Downtrend Slowing"
    return "Flat"

# ================= 데이터 수집 (Yahoo Finance) =================

def fetch_latest_rates_yahoo() -> Tuple[Dict[str, Dict[str, float]], str]:
    kst = ZoneInfo("Asia/Seoul")
    now_kst = datetime.now(kst)
    out: Dict[str, Dict[str, float]] = {}
    for code, ticker in CURRENCY_TICKERS:
        try:
            yt = yf.Ticker(ticker)
            df = yt.history(period="1d", interval="1m")
            if df.empty:
                df = yt.history(period="5d", interval="1m")
            if not df.empty:
                current_price = float(df['Close'].iloc[-1])
                if code == "JPY100": current_price *= 100
                out[code] = {"deal": current_price}
        except Exception as e:
            print(f"Error fetching {ticker}: {e}")
    if not out: raise RuntimeError("Yahoo Finance 데이터 수집 실패")
    return out, now_kst.strftime("%Y-%m-%d %H:%M")

# ================= 신호 및 시각화 =================

def decide_signal(price: float, a15: Optional[float], a30: Optional[float], th: float) -> Optional[str]:
    if a30 is not None and price < a30 * (2 - th): return "BUY30"
    if a15 is not None and price < a15 * (2 - th): return "BUY15"
    if a30 is not None and price > a30 * th: return "SELL30"
    if a15 is not None and price > a15 * th: return "SELL15"
    return None

def _sig_to_emoji(sig: str) -> Tuple[str, str, str]:
    side = "BUY" if sig.startswith("BUY") else "SELL"
    basis = "30D" if sig.endswith("30") else "15D"
    return ("🟢", "BUY", basis) if side == "BUY" else ("🔴", "SELL", basis)

def _ensure_dirs():
    for d in [ARROW_DIR, ASSET_DIR]: os.makedirs(d, exist_ok=True)

def _build_currency_trend_panel(code: str, angle15: float, angle30: float) -> str:
    _ensure_dirs()
    W, H = 420, 220
    panel = Image.new("RGBA", (W, H), (20, 20, 20, 255))
    d = ImageDraw.Draw(panel)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 22)
        font_mid = ImageFont.truetype("DejaVuSans.ttf", 18)
    except:
        font = font_mid = ImageFont.load_default()
    
    d.text((16, 12), f"{code} Trend", fill=(255, 255, 255), font=font)
    d.text((70, 60), f"30D ({angle30:+.1f}°)", fill=(255, 255, 255), font=font_mid)
    d.text((265, 60), f"15D ({angle15:+.1f}°)", fill=(255, 255, 255), font=font_mid)
    
    path = os.path.join(ASSET_DIR, f"trend_{code}.png")
    panel.save(path, "PNG")
    return path

# ================= 메인 =================

def main():
    th = _get_threshold()
    is_manual = os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch"
    
    series_map = {code: load_data(_csv_name(code)) for _, code in CURRENCY_TICKERS}
    try:
        latest_map, used_date = fetch_latest_rates_yahoo()
    except Exception as e:
        send_message(f"⚠️ Yahoo FX fetch failed: {e}")
        return

    state_changed = False
    state_map = load_state()
    manual_report_lines = []

    for code, _ in CURRENCY_TICKERS:
        r = latest_map.get(code)
        if not r: continue
        price = r["deal"]
        prev_series = series_map.get(code, [])
        
        # 1. 데이터 업데이트 및 긴급(Urgent) 체크
