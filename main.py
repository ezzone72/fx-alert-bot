import os
import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Optional, Tuple, Dict, Any, List

import yfinance as yf
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont

from notifier import send_message
from storage import (
    load_data, save_data, append_and_trim,
    avg_last, MAX_15D, MAX_30D
)

load_dotenv()

STATE_FILE = "state.json"
NEWS_STATE_FILE = "sent_news.json"
ASSET_DIR = "assets"
ARROW_DIR = os.path.join(ASSET_DIR, "arrows")

URGENT_PCT = 0.5

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

def load_state(file_path=STATE_FILE) -> Dict:
    if not os.path.exists(file_path): return {}
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except: return {}

def save_state(data: Dict, file_path=STATE_FILE):
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)

def _normalize_side(x: Optional[str]) -> Optional[str]:
    if not x: return None
    s = str(x).upper().strip()
    if s.startswith("BUY"): return "BUY"
    if s.startswith("SELL"): return "SELL"
    return None

# ================= 뉴스 기능 =================

def fetch_currency_news() -> List[str]:
    keywords = ["달러", "엔화", "원화", "스위스", "호주달러","환율"]
    sent_news = load_state(NEWS_STATE_FILE)
    
    # 네이버 뉴스 최신순 검색 (쿼리: 환율)
    url = "https://search.naver.com/search.naver?where=news&query=환율&sort=1"
    headers = {"User-Agent": "Mozilla/5.0"}
    
    news_to_send = []
    try:
        res = requests.get(url, headers=headers)
        soup = BeautifulSoup(res.text, 'html.parser')
        items = soup.select(".news_tit")
        
        for item in items:
            title = item.get_text()
            link = item['href']
            
            # 키워드 포함 여부 및 중복 체크
            if any(kw in title for kw in keywords) and link not in sent_news:
                news_to_send.append(f"📰 **{title}**\n{link}")
                sent_news[link] = datetime.now().isoformat() # 중복 방지 기록
                
        # 최근 50개만 유지 (파일 크기 관리)
        if len(sent_news) > 50:
            keys = list(sent_news.keys())[-50:]
            sent_news = {k: sent_news[k] for k in keys}
            
        save_state(sent_news, NEWS_STATE_FILE)
    except Exception as e:
        print(f"News fetch error: {e}")
        
    return news_to_send

# ================= 추세 및 시각화 (기존 동일) =================

def _trend_window(data: List[float], window_n: int, half_days: float) -> Optional[Dict[str, float]]:
    if len(data) < window_n: return None
    w = data[-window_n:]; half = window_n // 2
    f, l = w[:half], w[half:]
    af, al = sum(f)/len(f), sum(l)/len(l)
    delta = al - af; slope = delta / half_days
    import math
    angle = math.degrees(math.atan(slope))
    pct = (slope / af) * 100.0 if af else 0.0
    return {"angle_deg": angle, "pct_per_day": pct}

def _fmt_pct(p: Optional[float]) -> str:
    if p is None: return "N/A"
    return f"{'+' if p >= 0 else '-'}{abs(p):.3f}%/day"

def _interpretation_label_7_en(t15, t30):
    if not t15 or not t30: return "Flat"
    def _s(p): return 1 if p > 0.01 else (-1 if p < -0.01 else 0)
    s15, s30 = _s(t15["pct_per_day"]), _s(t30["pct_per_day"])
    if s15 == 0 and s30 == 0: return "Flat"
    if s30 > 0 and s15 < 0: return "Turning Down"
    if s30 < 0 and s15 > 0: return "Turning Up"
    if s30 > 0 and s15 > 0: return "Uptrend Sustained" if t15["pct_per_day"] >= t30["pct_per_day"] else "Uptrend Slowing"
    if s30 < 0 and s15 < 0: return "Downtrend Sustained" if abs(t15["pct_per_day"]) >= abs(t30["pct_per_day"]) else "Downtrend Slowing"
    return "Flat"

def fetch_latest_rates_yahoo():
    kst = ZoneInfo("Asia/Seoul"); now = datetime.now(kst)
    out = {}
    for code, ticker in CURRENCY_TICKERS:
        try:
            yt = yf.Ticker(ticker)
            df = yt.history(period="1d", interval="1m")
            if df.empty: df = yt.history(period="5d", interval="1m")
            if not df.empty:
                cp = float(df['Close'].iloc[-1])
                if code == "JPY100": cp *= 100
                out[code] = {"deal": cp}
        except Exception as e: print(f"Error {ticker}: {e}")
    if not out: raise RuntimeError("Yahoo Fetch Fail")
    return out, now.strftime("%Y-%m-%d %H:%M")

def decide_signal(price, a15, a30, th):
    if a30 and price < a30 * (2 - th): return "BUY30"
    if a15 and price < a15 * (2 - th): return "BUY15"
    if a30 and price > a30 * th: return "SELL30"
    if a15 and price > a15 * th: return "SELL15"
    return None

def _sig_to_emoji(sig):
    side = "BUY" if sig.startswith("BUY") else "SELL"
    basis = "30D" if sig.endswith("30") else "15D"
    return ("🟢", "BUY", basis) if side == "BUY" else ("🔴", "SELL", basis)

def _build_currency_trend_panel(code, a15, a30):
    os.makedirs(ASSET_DIR, exist_ok=True)
    W, H = 420, 220
    panel = Image.new("RGBA", (W, H), (20, 20, 20, 255))
    d = ImageDraw.Draw(panel)
    try: f, fm = ImageFont.truetype("DejaVuSans.ttf", 22), ImageFont.truetype("DejaVuSans.ttf", 18)
    except: f = fm = ImageFont.load_default()
    d.text((16, 12), f"{code} Trend", fill=(255, 255, 255), font=f)
    d.text((70, 60), f"30D ({a30:+.1f}°)", fill=(255, 255, 255), font=fm)
    d.text((265, 60), f"15D ({a15:+.1f}°)", fill=(255, 255, 255), font=fm)
    path = os.path.join(ASSET_DIR, f"trend_{code}.png")
    panel.save(path, "PNG"); return path

# ================= 메인 =================

def main():
    th = _get_threshold()
    is_manual = os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch"
    
    # 1. 뉴스 체크 (있을 때만 별도 메시지)
    news_list = fetch_currency_news()
    for news_msg in news_list:
        send_message(news_msg)

    # 2. 환율 체크
    series_map = {code: load_data(_csv_name(code)) for _, code in CURRENCY_TICKERS}
    try:
        latest_map, used_date = fetch_latest_rates_yahoo()
    except Exception as e:
        send_message(f"⚠️ Yahoo FX fetch failed: {e}"); return

    state_changed = False
    state_map = load_state(STATE_FILE)
    manual_report_lines = []

    for code, _ in CURRENCY_TICKERS:
        r = latest_map.get(code); price = r["deal"]
        prev = series_map.get(code, [])
        
        if not prev or prev[-1] != price:
            if prev and prev[-1] != 0:
                pct = (price - prev[-1]) / prev[-1] * 100.0
                if abs(pct) >= URGENT_PCT:
                    series_temp = append_and_trim(prev, price, MAX_30D)
                    t15_u = _trend_window(series_temp, MAX_15D, 7.5)
                    t30_u = _trend_window(series_temp, MAX_30D, 15.0)
                    urgent_text = f"🚨 **[URGENT] {code} 변동 감지**\n이전: {prev[-1]:.2f} → 현재: **{price:.2f}** ({pct:+.3f}%)\n일시: {used_date}"
                    u_img = _build_currency_trend_panel(code, t15_u["angle_deg"] if t15_u else 0.0, t30_u["angle_deg"] if t30_u else 0.0)
                    send_message(urgent_text, file_path=u_img, filename=f"urgent_{code}.png")
            series_map[code] = append_and_trim(prev, price, MAX_30D)
            save_data(series_map[code], _csv_name(code))
            state_changed = True

        series = series_map[code]
        a15, a30 = avg_last(series, MAX_15D), avg_last(series, MAX_30D)
        sig = decide_signal(price, a15, a30, th)
        t15, t30 = _trend_window(series, MAX_15D, 7.5), _trend_window(series, MAX_30D, 15.0)
        curr_side = _normalize_side(sig) if sig else "NONE"
        prev_side = _normalize_side(state_map.get(code))
        
        emoji, side, basis = _sig_to_emoji(sig) if sig else ("ℹ️", "KEEP", "N/A")
        indicator = _interpretation_label_7_en(t15, t30)
        status_text = f"**{code}: {price:.2f}** | {emoji} {side} ({basis})\n└ {indicator} | 30d: {_fmt_pct(t30['pct_per_day'] if t30 else None)}"

        if sig and curr_side != prev_side:
            img_path = _build_currency_trend_panel(code, t15["angle_deg"] if t15 else 0.0, t30["angle_deg"] if t30 else 0.0)
            send_message(status_text, file_path=img_path, filename=f"trend_{code}.png")
            state_map[code] = curr_side; state_changed = True

        if is_manual: manual_report_lines.append(status_text)

    if is_manual:
        send_message(f"🔍 **Manual Status Report** ({used_date})\n\n" + "\n\n".join(manual_report_lines))

    if state_changed: save_state(state_map, STATE_FILE)

if __name__ == "__main__":
    main()
