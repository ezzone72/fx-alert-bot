import json
import os
from typing import Optional, Tuple

import requests
from dotenv import load_dotenv

from notifier import send_telegram
from storage import (
    load_data, append_and_trim, save_data,
    avg_last, avg_last_partial,
    MAX_15D, MAX_30D
)

load_dotenv()

STATE_FILE = "state.json"


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


def fetch_jpy100_krw() -> Optional[float]:
    authkey = os.getenv("EXIMBANK_API_KEY", "").strip()
    if not authkey:
        raise RuntimeError("EXIMBANK_API_KEY가 비어 있습니다.")

    # 도메인 변경 대응
    url = "https://oapi.koreaexim.go.kr/site/program/financial/exchangeJSON"
    params = {"authkey": authkey, "searchdate": "", "data": "AP01"}
    headers = {"User-Agent": "fx-alert-bot/avg-and-signal"}

    r = requests.get(url, params=params, headers=headers, timeout=25)
    r.raise_for_status()
    data = r.json()

    for item in data:
        if item.get("cur_unit") == "JPY(100)":
            s = str(item.get("deal_bas_r", "")).replace(",", "").strip()
            try:
                return float(s)
            except ValueError:
                return None
    return None


def decide_signal(price: float, a15: Optional[float], a30: Optional[float], th: float) -> Tuple[str, Optional[str]]:
    """
    기존 로직 그대로:
    - 30일 평균 조건 우선, 그 다음 15일
    - a15/a30이 None(데이터 부족)이면 판정하지 않음
    """
    if a30 is not None and price < a30 * th:
        return "BUY30", f"📉 매수 신호 (30D)\n가격: {price}\n30D 평균: {a30:.4f}\n기준: avg*{th}"
    if a15 is not None and price < a15 * th:
        return "BUY15", f"📉 매수 신호 (15D)\n가격: {price}\n15D 평균: {a15:.4f}\n기준: avg*{th}"
    if a30 is not None and price > a30 * th:
        return "SELL30", f"📈 매도 신호 (30D)\n가격: {price}\n30D 평균: {a30:.4f}\n기준: avg*{th}"
    if a15 is not None and price > a15 * th:
        return "SELL15", f"📈 매도 신호 (15D)\n가격: {price}\n15D 평균: {a15:.4f}\n기준: avg*{th}"
    return "NONE", None


def main() -> None:
    th = _get_threshold()

    price = fetch_jpy100_krw()
    if price is None:
        send_telegram("⚠️ 환율 데이터 수신 실패")
        return

    # 데이터 누적/슬라이딩(최근 30일=1440개 유지)
    data = load_data()
    data = append_and_trim(data, price)
    save_data(data)

    # 표시용 평균(부분 평균): 지금까지 모인 데이터로 계산해서 항상 보여줌
    a15_show = avg_last_partial(data, MAX_15D)
    a30_show = avg_last_partial(data, MAX_30D)

    # 판정용 평균(정식): 데이터가 충분할 때만 계산(기존 로직 유지)
    a15 = avg_last(data, MAX_15D)   # 720개 있어야 값 생김
    a30 = avg_last(data, MAX_30D)   # 1440개 있어야 값 생김

    # 🧪TEST 표시는 15일치 쌓이기 전까지만 붙임 (720개 넘으면 자동으로 사라짐)
    is_test = len(data) < MAX_15D

    # 메시지(항상 가격 + 현재까지 평균 표시)
    lines = []
    if is_test:
        lines.append("🧪 TEST(수집중) — 15일 데이터 미만")
    lines.append("📊 엔화 환율 알림")
    lines.append("----------------------")
    lines.append("JPY(100) / KRW")
    lines.append(f"현재가: {price}")
    lines.append(f"15D 평균(현재까지): {a15_show:.4f}" if a15_show is not None else "15D 평균(현재까지): N/A")
    lines.append(f"30D 평균(현재까지): {a30_show:.4f}" if a30_show is not None else "30D 평균(현재까지): N/A")
    lines.append(f"데이터: {len(data)}/{MAX_30D} (30분봉)")

    # 판정(기존 로직 그대로): 데이터 충분할 때만 신호
    prev = load_state()
    state, sig_msg = decide_signal(price, a15, a30, th)

    # 가격/평균 알림은 매번 보내기 (원하신 방향)
    # + 신호가 있으면 본문에 같이 포함
    if sig_msg:
        lines.append("")
        lines.append(sig_msg)

    send_telegram("\n".join(lines))

    # 상태는 신호가 있을 때만 갱신 (중복 신호 관리용)
    # (가격 알림은 매번 가니까 state로 막지 않습니다)
    if sig_msg and state != prev:
        save_state(state)


if __name__ == "__main__":
    main()
