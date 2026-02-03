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

URGENT_PCT = 0.5
CURRENCY_TICKERS = [("JPY100", "JPYKRW=X"), ("USD", "USDKRW=X"), ("AUD", "AUDKRW=X"), ("CHF", "CHFKRW=X")]

# ================= 뉴스 기능 (보강됨) =================

def fetch_currency_news() -> List[str]:
    # 2026년 기준 키워드 세트
    keywords = ["달러", "엔화", "원화", "스위스", "호주달러", "환율"]
    sent_news = load_state(NEWS_STATE_FILE)
    
    # 네이버 뉴스 검색 (최신순 sort=1)
    url = "https://search.naver.com/search.naver?where=news&query=환율&sort=1"
    
    # 🛡️ 네이버 차단을 피하기 위한 강화된 헤더 (User-Agent)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Referer": "https://www.naver.com/"
    }
    
    news_to_send = []
    try:
        res = requests.get(url, headers=headers, timeout=10)
        res.raise_for_status()
        soup = BeautifulSoup(res.text, 'html.parser')
        
        # 네이버 뉴스 제목 선택자 (다양한 패턴 대응)
        items = soup.select(".news_tit, ._sp_each_title")
        
        for item in items:
            title = item.get_text(strip=True)
            link = item.get('href')
            
            if not link: continue
            
            # 키워드 체크 & 중복 체크
            if any(kw in title for kw in keywords) and link not in sent_news:
                news_to_send.append(f"📰 **{title}**\n{link}")
                sent_news[link] = datetime.now().isoformat()
        
        # 50개 초과 시 오래된 순으로 삭제
        if len(sent_news) > 50:
            overflow = len(sent_news) - 50
            for k in list(sent_news.keys())[:overflow]: del sent_news[k]
            
        save_state(sent_news, NEWS_STATE_FILE)
    except Exception as e:
        print(f"DEBUG: News Error -> {e}")
        
    return news_to_send

# ================= 기본 유틸 & 환율 로직 (동일) =================

def load_state(path):
    if not os.path.exists(path): return {}
    with open(path, "r", encoding="utf-8") as f: return json.load(f)

def save_state(data, path):
    with open(path, "w", encoding="utf-8") as f: json.dump(data, f, ensure_ascii=False)

def fetch_latest_rates_yahoo():
    kst = ZoneInfo("Asia/Seoul"); now = datetime.now(kst)
    out = {}
    for code, ticker in CURRENCY_TICKERS:
        yt = yf.Ticker(ticker)
        df = yt.history(period="1d", interval="1m")
        if df.empty: df = yt.history(period="5d", interval="1m")
        if not df.empty:
            cp = float(df['Close'].iloc[-1])
            if code == "JPY100": cp *= 100
            out[code] = {"deal": cp}
    return out, now.strftime("%Y-%m-%d %H:%M")

def _build_currency_trend_panel(code, a15, a30):
    os.makedirs(ASSET_DIR, exist_ok=True)
    panel = Image.new("RGBA", (420, 220), (20, 20, 20, 255))
    d = ImageDraw.Draw(panel)
    try: f = ImageFont.load_default() # 폰트 경로 이슈 방지용
    except: f = None
    d.text((16, 12), f"{code} Trend", fill=(255, 255, 255), font=f)
    path = os.path.join(ASSET_DIR, f"trend_{code}.png")
    panel.save(path, "PNG"); return path

def main():
    # 1. 뉴스 체크 및 발송
    news_list = fetch_currency_news()
    if news_list:
        print(f"DEBUG: Found {len(news_list)} news items.")
        for msg in news_list:
            send_message(msg)
            time.sleep(1) # 디스코드 레이트 리밋 방지
    else:
        print("DEBUG: No new news matching keywords.")

    # 2. 환율 체크 (기존 로직 수행)
    try:
        latest_map, used_date = fetch_latest_rates_yahoo()
    except: return

    # ... (이하 환율 로직 생략, 기존과 동일하게 유지) ...
    # 실제 적용 시엔 위 환율 체크 로직 하단부를 그대로 붙여넣으시면 됩니다.
    print(f"환율 체크 완료: {used_date}")

if __name__ == "__main__":
    main()
