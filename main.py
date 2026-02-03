import os
import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Optional, List

import yfinance as yf
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from notifier import send_message
from storage import (
    load_data, save_data, append_and_trim,
    avg_last, MAX_15D, MAX_30D
)

load_dotenv()

STATE_FILE = "state.json"
NEWS_STATE_FILE = "sent_news.json"
URGENT_PCT = 0.5 
CURRENCY_TICKERS = [("JPY100", "JPYKRW=X"), ("USD", "USDKRW=X"), ("AUD", "AUDKRW=X"), ("CHF", "CHFKRW=X")]

def load_state(path):
    if not os.path.exists(path): return {}
    try:
        with open(path, "r", encoding="utf-8") as f: return json.load(f)
    except: return {}

def save_state(data, path):
    with open(path, "w", encoding="utf-8") as f: json.dump(data, f, ensure_ascii=False)

def main():
    is_manual = os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch"
    state_map = load_state(STATE_FILE)
    state_changed = False
    status_lines = []

    # 1. 환율 데이터 수집 및 판정
    for code, ticker in CURRENCY_TICKERS:
        try:
            yt = yf.Ticker(ticker)
            df = yt.history(period="1d", interval="1m")
            if df.empty: df = yt.history(period="5d", interval="1m")
            if df.empty: continue
            
            cp = float(df['Close'].iloc[-1])
            price = cp * 100 if code == "JPY100" else cp
            
            # 기존 데이터 불러오기
            csv_file = f"data_{code}.csv"
            series = load_data(csv_file)
            
            # 방향성(화살표) 판단
            prev_price = series[-1] if series else price
            diff = price - prev_price
            arrow = "🔺" if diff > 0 else "🔻" if diff < 0 else "➖"
            
            # 기준값(30일 평균) 대비 BUY/SELL 판단
            a30 = avg_last(series, MAX_30D)
            if not a30: a30 = price
            
            sig = "BUY" if price < a30 else "SELL"
            curr_side = sig
            prev_side = state_map.get(code)

            # 알림 조건: 신호가 바뀌었거나, 수동 실행일 때만
            if curr_side != prev_side or is_manual:
                action_icon = "🟢" if sig == "BUY" else "🔴"
                msg = (
                    f"🚨 **{code} 신호 발생**\n"
                    f"현재가: **{price:.2f}** {arrow} ({diff:+.2f})\n"
                    f"평균가: {a30:.2f}\n"
                    f"판단: {action_icon} **{sig} 타점**"
                )
                send_message(msg) # 가짜 이미지 없이 텍스트로만 정확히 전달
                state_map[code] = curr_side
                state_changed = True

            # 데이터 적립 (SVC 유지)
            series = append_and_trim(series, price, MAX_30D)
            save_data(series, csv_file)
            state_changed = True
            status_lines.append(f"{code}: {price:.2f} {arrow}")

        except Exception as e:
            print(f"Error processing {code}: {e}")

    # 2. 수동 실행 시 전체 요약 보고
    if is_manual and status_lines:
        send_message("🔍 **현재 환율 모니터링 요약**\n" + "\n".join(status_lines))

    if state_changed:
        save_state(state_map, STATE_FILE)

if __name__ == "__main__":
    main()
