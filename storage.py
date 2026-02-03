import os
from typing import List, Optional

DEFAULT_FILE = "data.csv"

# 30분봉 기준 10분기준으로 변경
MAX_15D = 6 * 24 * 15   # 720
MAX_30D = 6 * 24 * 30   # 1440


def load_data(filename: str = DEFAULT_FILE) -> List[float]:
    if not os.path.exists(filename):
        return []
    out: List[float] = []
    with open(filename, "r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            try:
                out.append(float(s))
            except Exception:
                continue
    return out


def save_data(data: List[float], filename: str = DEFAULT_FILE) -> None:
    with open(filename, "w", encoding="utf-8") as f:
        for v in data:
            f.write(f"{v}\n")


def append_and_trim(data: List[float], value: float, max_len: int = MAX_30D) -> List[float]:
    data.append(float(value))
    if len(data) > max_len:
        data = data[-max_len:]
    return data


def avg_last(data: List[float], n: int) -> Optional[float]:
    if len(data) < n:
        return None
    w = data[-n:]
    return sum(w) / n


def avg_last_partial(data: List[float], n: int) -> Optional[float]:
    if not data:
        return None
    w = data[-n:] if len(data) >= n else data[:]
    return sum(w) / len(w)
