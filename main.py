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

URGENT_PCT = 0.5  # 0.5% 이상 변동 시 긴급
CURRENCY_TICKERS = [("JPY100", "JPYKRW=X"), ("USD", "USDKRW=X"), ("AUD", "AUDKRW=X"), ("CHF", "CHFKRW=X")]

# ================= 유틸리티 =================

def _get_threshold() -> float:
    v = os.getenv("THRESHOLD", "1.01").strip()
    try: return float(v)
    except: return 1.01

def _csv_name(code: str) -> str:
    return f"data_{code}.csv"

def load_state(path):
    if not os.path.exists(path): return {}
    try:
        with open(path, "r", encoding="utf-8") as f: return json.load(f)
    except: return {}

def save_state(data, path):
    with open(path, "w", encoding="utf-8") as f: json.dump(data, f, ensure_ascii=False)

def _normalize_side(x: Optional[str]) -> Optional[str]:
    if not x: return None
    s = str(x).upper().strip()
    if "BUY" in s: return "BUY"
    if "SELL" in s: return "SELL"
    return None

# ================= 뉴스 기능 =================

def fetch_currency_news() -> List[str]:
    keywords = ["달러", "엔화", "원화", "스위스", "호주달러", "환율"]
    sent_news = load_state(NEWS_STATE_FILE)
    url = "https://search.naver.com/search.naver?where=news&query=환율&sort=1"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://www.naver.com/"
    }
    news_to_send = []
    try:
        res = requests.get(url, headers=headers, timeout=10)
        res.raise_for_status()
        soup = BeautifulSoup(res.text, 'html.parser')
        items = soup.select(".news_tit, ._sp_each_title")
        for item in items:
            title = item.get_text(strip=True)
            link = item.get('href')
            if link and any(kw in title for kw in keywords) and link not in sent_news:
                news_to_send.append(f"📰 **{title}**\n{link}")
                sent_news[link] = datetime.now().isoformat()
        if len(sent_news) > 50:
            sent_news = dict(list(sent_news.items())[-50:])
        save_state(sent_news, NEWS_STATE_FILE)
    except Exception as e: print(f"DEBUG: News Error -> {e}")
    return news_to_send

# ================= 추세 및 시각화 =================

def _trend_window(data, window_n, half_days):
    if len(data) < window_n: return None
    w = data[-window_n:]; half = window_n // 2
    f, l = w[:half], w[half:]
    af, al = sum(f)/len(f), sum(l)/len(l)
    slope = (al - af) / half_days
    import math
    angle = math.degrees(math.atan(slope))
    return {"angle_deg": angle, "pct_per_day": (slope / af * 100.0 if af else 0.0)}

def _build_currency_trend_panel(code, a15, a30):
    os.makedirs(ASSET_DIR, exist_ok=True)
    panel = Image.new("RGBA", (420, 220), (20, 20, 20, 255))
    d = ImageDraw.Draw(panel)
    try: f = ImageFont.load_default()
    except: f = None
    d.text((16, 12), f"{code} ExpertAlpha Trend", fill=(255, 255, 255), font=f)
    d.text((70, 60), f"30D ({a30:+.1f}°)", fill=(255, 255, 255), font=f)
    d.text((265, 60), f"15D ({a15:+.1f}°)", fill=(255, 255, 255), font=f)
    path = os.path.join(ASSET_DIR, f"trend_{code}.png")
    panel.save(path, "PNG"); return path

# ================= 메인 로직 =================

def main():
    th = _get_threshold()
    is_manual = os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch"
    
    # 1. 뉴스 체크
    news_list = fetch_currency_news()
    for msg in news_list:
        send_message(msg)
        time.sleep(1)

    # 2. 환율 데이터 수집
    kst = ZoneInfo("Asia/Seoul"); now = datetime.now(kst)
    used_date = now.strftime("%Y-%m-%d %H:%M")
    latest_map = {}
    for code, ticker in CURRENCY_TICKERS:
        try:
            yt = yf.Ticker(ticker); df = yt.history(period="1d", interval="1m")
            if df.empty: df = yt.history(period="5d", interval="1m")
            if not df.empty:
                cp = float(df['Close'].iloc[-1])
                latest_map[code] = cp * 100 if code == "JPY100" else cp
        except: continue

    # 3. 데이터 업데이트 및 알림 판정
    state_map = load_state(STATE_FILE)
    state_changed = False
    status_lines = []

    for code, _ in CURRENCY_TICKERS:
        price = latest_map.get(code)
        if price is None: continue
        
        series = load_data(_csv_name(code))
        
        # 방향 및 화살표 판단
        prev_price = series[-1] if series else price
        diff = price - prev_price
        arrow = "🔺" if diff > 0 else "🔻" if diff < 0 else "➖"
        
        # 평균값 계산
        a15, a30 = avg_last(series, MAX_15D), avg_last(series, MAX_30D)
        if not a30: a30 = price
        
        # BUY/SELL 및 긴급도 로직
        pct_from_avg = (price - a30) / a30 * 100.0
        is_urgent = abs(pct_from_avg) >= URGENT_PCT
        
        sig = "BUY" if price < a30 else "SELL"
        curr_side = sig
        prev_side = _normalize_side(state_map.get(code))
        
        # 알림 발송 조건: 상태 변화가 있거나, 긴급하거나, 수동 실행일 때
        if curr_side != prev_side or is_urgent or is_manual:
            t15 = _trend_window(series, MAX_15D, 7.5); t30 = _trend_window(series, MAX_30D, 15.0)
            img = _build_currency_trend_panel(code, t15["angle_deg"] if t15 else 0.0, t30["angle_deg"] if t30 else 0.0)
            
            status_tag = "⚠️ [URGENT]" if is_urgent else "📢 [REPORT]"
            action_tag = "🟢 BUY" if sig == "BUY" else "🔴 SELL"
            
            msg = (
                f"{status_tag} **{code} 보고**\n"
                f"현재가: **{price:.2f}** {arrow} ({diff:+.2f})\n"
                f"평균가 대비: {pct_from_avg:+.2f}% -> **{action_tag}**\n"
                f"━━━━━━━━━━━━━━\n"
                f"📊 추세: 15D({t15['angle_deg'] if t15 else 0:+.1f}°) | 30D({t30['angle_deg'] if t30 else 0:+.1f}°)"
            )
            send_message(msg, file_path=img)
            state_map[code] = curr_side
            state_changed = True

        # 데이터 저장
        series = append_and_trim(series, price, MAX_30D)
        save_data(series, _csv_name(code))
        state_changed = True
        status_lines.append(f"**{code}**: {price:.2f} ({arrow})")

    if is_manual:
        send_message(f"🔍 **ExpertAlpha 실시간 상태**\n" + "\n".join(status_lines))
    
    if state_changed: save_state(state_map, STATE_FILE)
    print(f"환율 체크 완료: {used_date}")

if __name__ == "__main__":
    main()
