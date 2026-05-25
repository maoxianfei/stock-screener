"""
多市场 K 线数据拉取模块
========================
支持 A 股 (mootdx)、港股 (Yahoo/腾讯)、美股 (新浪/Yahoo)
日线/周线/月线三维度 K 线数据获取。
"""

from datetime import datetime, timedelta
from typing import Optional
import requests
import re
import json

from hexagram_engine import KLine


# ============================================================
# A股 — mootdx K线
# ============================================================

def fetch_a_share_klines(
    code: str,
    period: str = "daily",
    count: int = 120,
) -> list[KLine]:
    """
    拉取 A 股 K 线数据 (mootdx TCP)

    Args:
        code: 6位股票代码, 如 "000001", "600519"
        period: "daily" (日线) | "weekly" (周线) | "monthly" (月线)
        count: 拉取数量 (建议至少72根日线, 可生成67个卦象)

    Returns:
        按时间升序排列的 KLine 列表
    """
    from mootdx.quotes import Quotes

    # mootdx frequency 映射 (注意: 参数名是 frequency 不是 category)
    # frequency: 9=日线, 5=周线, 6=月线
    frequency_map = {
        "daily": 9,
        "weekly": 5,
        "monthly": 6,
    }
    freq = frequency_map.get(period, 9)

    # market: 0=深圳, 1=上海
    if code.startswith(("6", "9")):
        market = 1
    else:
        market = 0

    client = Quotes.factory(market="std")
    raw = client.bars(symbol=code, frequency=freq, offset=count)

    if raw is None or len(raw) == 0:
        return []

    klines = []
    for _, row in raw.iterrows():
        dt = str(row.get("datetime", ""))
        date = str(dt)[:10] if dt else ""
        if not date:
            continue

        klines.append(KLine(
            date=date,
            open=float(row.get("open", 0)),
            high=float(row.get("high", 0)),
            low=float(row.get("low", 0)),
            close=float(row.get("close", 0)),
            volume=float(row.get("vol", 0)),
        ))

    # 按日期升序排序
    klines.sort(key=lambda k: k.date)
    return klines


# ============================================================
# 港股 — Yahoo Finance K线
# ============================================================

def fetch_hk_klines_yahoo(
    code: str,
    period: str = "daily",
    count: int = 120,
) -> list[KLine]:
    """
    拉取港股 K 线 (Yahoo Finance chart API)

    Args:
        code: 港股代码, 如 "00700", "09988", "01810"
        period: "daily" | "weekly" | "monthly"
        count: 数量 (Yahoo range 方式: 1y/2y/5y/max)

    Returns:
        KLine 列表
    """
    symbol = f"{int(code):04d}.HK"

    # interval 映射
    interval_map = {
        "daily": "1d",
        "weekly": "1wk",
        "monthly": "1mo",
    }
    interval = interval_map.get(period, "1d")

    # range 映射 (根据需要的数量近似)
    if count <= 30:
        range_ = "1mo"
    elif count <= 90:
        range_ = "3mo"
    elif count <= 250:
        range_ = "1y"
    elif count <= 500:
        range_ = "2y"
    elif count <= 1250:
        range_ = "5y"
    else:
        range_ = "max"

    url = f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"interval": interval, "range": range_}
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
    }

    try:
        r = requests.get(url, params=params, headers=headers, timeout=15)
        r.raise_for_status()
    except Exception as e:
        print(f"[ERROR] Yahoo K线请求失败: {e}")
        return []

    d = r.json()
    chart = d.get("chart", {}).get("result", [{}])
    if not chart or not chart[0]:
        return []

    result = chart[0]
    timestamps = result.get("timestamp", [])
    quote = result.get("indicators", {}).get("quote", [{}])[0]

    if not timestamps:
        return []

    klines = []
    for i, ts in enumerate(timestamps):
        dt = datetime.fromtimestamp(ts)
        if period == "daily":
            date_str = dt.strftime("%Y-%m-%d")
        else:
            date_str = dt.strftime("%Y-%m-%d")

        o = quote["open"][i]
        h = quote["high"][i]
        l = quote["low"][i]
        c = quote["close"][i]
        v = quote["volume"][i] or 0

        # 跳过空值
        if o is None or c is None:
            continue

        klines.append(KLine(
            date=date_str,
            open=round(float(o), 3),
            high=round(float(h), 3) if h else round(float(o), 3),
            low=round(float(l), 3) if l else round(float(o), 3),
            close=round(float(c), 3),
            volume=float(v),
        ))

    klines.sort(key=lambda k: k.date)
    return klines


# ============================================================
# 美股 — Yahoo Finance K线
# ============================================================

def fetch_us_klines_yahoo(
    ticker: str,
    period: str = "daily",
    count: int = 120,
) -> list[KLine]:
    """
    拉取美股 K 线 (Yahoo Finance)

    Args:
        ticker: 美股代码, 如 "AAPL", "TSLA"
        period: "daily" | "weekly" | "monthly"
        count: 数量

    Returns:
        KLine 列表
    """
    interval_map = {
        "daily": "1d",
        "weekly": "1wk",
        "monthly": "1mo",
    }
    interval = interval_map.get(period, "1d")

    if count <= 30:
        range_ = "1mo"
    elif count <= 90:
        range_ = "3mo"
    elif count <= 250:
        range_ = "1y"
    elif count <= 500:
        range_ = "2y"
    elif count <= 1250:
        range_ = "5y"
    else:
        range_ = "max"

    url = f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}"
    params = {"interval": interval, "range": range_}
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
    }

    try:
        r = requests.get(url, params=params, headers=headers, timeout=15)
        r.raise_for_status()
    except Exception as e:
        print(f"[ERROR] Yahoo K线请求失败: {e}")
        return []

    d = r.json()
    chart = d.get("chart", {}).get("result", [{}])
    if not chart or not chart[0]:
        return []

    result = chart[0]
    timestamps = result.get("timestamp", [])
    quote = result.get("indicators", {}).get("quote", [{}])[0]

    if not timestamps:
        return []

    klines = []
    for i, ts in enumerate(timestamps):
        dt = datetime.fromtimestamp(ts)
        date_str = dt.strftime("%Y-%m-%d")

        o = quote["open"][i]
        h = quote["high"][i]
        l = quote["low"][i]
        c = quote["close"][i]
        v = quote["volume"][i] or 0

        if o is None or c is None:
            continue

        klines.append(KLine(
            date=date_str,
            open=round(float(o), 2),
            high=round(float(h), 2) if h else round(float(o), 2),
            low=round(float(l), 2) if l else round(float(o), 2),
            close=round(float(c), 2),
            volume=float(v),
        ))

    klines.sort(key=lambda k: k.date)
    return klines


# ============================================================
# 统一接口
# ============================================================

def fetch_klines(
    code: str,
    market: str = "a",
    period: str = "daily",
    count: int = 120,
) -> list[KLine]:
    """
    统一 K 线拉取接口

    Args:
        code: 股票代码
        market: "a" (A股), "hk" (港股), "us" (美股)
        period: "daily" | "weekly" | "monthly"
        count: 拉取数量

    Returns:
        KLine 列表
    """
    if market == "a":
        return fetch_a_share_klines(code, period, count)
    elif market == "hk":
        return fetch_hk_klines_yahoo(code, period, count)
    elif market == "us":
        return fetch_us_klines_yahoo(code, period, count)
    else:
        raise ValueError(f"不支持的市场: {market}")
