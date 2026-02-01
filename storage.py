import csv
import os
from typing import List, Optional

MAX_30D = 48 * 30
MAX_15D = 48 * 15

CSV_FILE = "data.csv"


def load_data() -> List[float]:
    if not os.path.exists(CSV_FILE):
        return []
    with open(CSV_FILE, newline="", encoding="utf-8") as f:
        return [float(row[0]) for row in csv.reader(f) if row]


def append_and_trim(data: List[float], value: float) -> List[float]:
    data.append(value)
    return data[-MAX_30D:]


def save_data(data: List[float]) -> None:
    with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        for v in data:
            w.writerow([v])


def avg_last(data: List[float], n: int) -> Optional[float]:
    if len(data) < n:
        return None
    return sum(data[-n:]) / n


def avg_last_partial(data: List[float], n: int) -> Optional[float]:
    if not data:
        return None
    return sum(data[-min(len(data), n):]) / min(len(data), n)
