import csv
import os
from typing import List, Optional

MAX_30D = 48 * 30
MAX_15D = 48 * 15

CSV_FILE = "data.csv"

def load_data() -> List[float]:
    if not os.path.exists(CSV_FILE):
        return []
    out: List[float] = []
    with open(CSV_FILE, newline="", encoding="utf-8") as f:
        for row in csv.reader(f):
            if not row:
                continue
            try:
                out.append(float(row[0]))
            except ValueError:
                continue
    return out

def append_and_trim(data: List[float], value: float) -> List[float]:
    data.append(value)
    if len(data) > MAX_30D:
        data = data[-MAX_30D:]
    return data

def save_data(data: List[float]) -> None:
    data = data[-MAX_30D:]
    with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        for v in data:
            w.writerow([v])

def avg_last(data: List[float], n: int) -> Optional[float]:
    if len(data) < n:
        return None
    window = data[-n:]
    return sum(window) / n
def avg_last_partial(data: List[float], n: int) -> Optional[float]:
    """
    데이터가 n개보다 적어도 '현재까지 모인 범위'로 평균을 계산.
    (표시용: 지금까지 평균을 보여주기 위함)
    """
    if len(data) == 0:
        return None
    m = min(len(data), n)
    window = data[-m:]
    return sum(window) / m

