"""
均线预筛选 + 卦象序列扫描模块 (screener.py)
=============================================
Step 1: 从全市场A股中筛选收盘价 > MA60周线 的个股，形成股票池
Step 2: 对股票池进行卦象序列扫描 (日线/周线/月线)
Step 3: 过滤只保留最近一年内触发信号的股票

MA60周线 计算方式: 取最近 60 根周线的收盘价简单移动平均 (约300日均线)
时间过滤: 信号结束日期(end_date)在最近365天内的才保留
数据源: mootdx (通达信TCP)
"""

import re
import time
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Optional

from data_fetcher import fetch_klines
from hexagram_engine import (
    KLine,
    klines_to_hexagrams,
    detect_hexagram_sequences,
    get_hexagram_info,
    SequenceMatch,
)


# ============================================================
# 数据结构
# ============================================================

@dataclass
class MAFilterResult:
    """均线筛选结果（单只股票）"""
    code: str
    name: str
    close: float             # 最新收盘价
    ma60: float              # 60周均线
    above_ma: bool           # 收盘价 > MA60周线
    klines_count: int        # 拉取到的K线数
    error: Optional[str] = None


@dataclass
class HexagramSignal:
    """卦象序列信号（扫描结果）"""
    code: str
    name: str
    period: str               # "daily" / "weekly" / "monthly"
    period_label: str         # "日线" / "周线" / "月线"
    start_date: str           # 信号起始日期
    end_date: str             # 信号结束日期
    hexagram_names: list[str] # 卦名序列
    close: float              # 信号日收盘价
    ma60: float               # 信号日MA60


@dataclass
class ScanSummary:
    """全市场扫描汇总"""
    total_stocks: int                # 全市场A股总数
    ma60_passed: int                 # MA60过滤通过数
    ma60_failed: int                 # MA60过滤未通过数
    ma60_errors: int                 # 数据异常数
    signal_count_before_filter: int  # 时间过滤前信号数
    signal_count: int                # 时间过滤后信号数
    signal_stocks: int               # 有信号的股票数
    time_filter_days: int            # 时间过滤窗口(天)
    elapsed_seconds: float           # 总耗时(秒)
    stock_pool: list[MAFilterResult] = field(default_factory=list)
    signals: list[HexagramSignal] = field(default_factory=list)


# ============================================================
# Step 1: 全市场股票列表获取
# ============================================================

def get_a_share_list() -> list[tuple[str, str, int]]:
    """
    获取全部A股股票列表 (沪深+北交所)

    Returns:
        [(code, name, market), ...]
        market: 0=深圳, 1=上海
    """
    from mootdx.quotes import Quotes
    client = Quotes.factory(market='std')

    results = []

    # 沪市
    sh_all = client.stocks(market=1)
    if sh_all is not None and len(sh_all) > 0:
        # 沪市A股: 6开头(主板60x/601/603/605) + 688开头(科创板)
        # 排除880/881/888(行业指数)、8/4开头(北交所在深市单独处理)
        mask = sh_all['code'].str.match(r'^60\d{4}$') | \
               sh_all['code'].str.match(r'^688\d{3}$')
        sh_a = sh_all[mask]
        for _, row in sh_a.iterrows():
            results.append((row['code'], row['name'], 1))

    # 深市
    sz_all = client.stocks(market=0)
    if sz_all is not None and len(sz_all) > 0:
        # 深市A股: 000/001(主板) + 002/003(中小板) + 300/301(创业板)
        # 北交所: 8开头(部分在深市列表) + 4开头
        # 排除399(指数)、395(基金/债券等)
        mask = sz_all['code'].str.match(r'^000\d{3}$') | \
               sz_all['code'].str.match(r'^001\d{3}$') | \
               sz_all['code'].str.match(r'^002\d{3}$') | \
               sz_all['code'].str.match(r'^003\d{3}$') | \
               sz_all['code'].str.match(r'^300\d{3}$') | \
               sz_all['code'].str.match(r'^301\d{3}$') | \
               sz_all['code'].str.match(r'^8\d{5}$') | \
               sz_all['code'].str.match(r'^4\d{5}$')
        sz_a = sz_all[mask]
        for _, row in sz_a.iterrows():
            results.append((row['code'], row['name'], 0))

    # 按代码排序
    results.sort(key=lambda x: x[0])
    return results


# ============================================================
# Step 2: MA60 均线筛选
# ============================================================

def calculate_ma(klines: list[KLine], period: int = 60) -> Optional[float]:
    """
    计算简单移动平均线

    Args:
        klines: 日线K线列表（时间升序）
        period: 均线周期

    Returns:
        MA值，如果K线不足则返回 None
    """
    if len(klines) < period:
        return None
    closes = [k.close for k in klines[-period:]]
    return sum(closes) / period


def fetch_and_filter_ma60(
    code: str,
    name: str,
    market: int,
    kline_count: int = 150,
) -> MAFilterResult:
    """
    拉取单只股票周线，计算MA60周线，判断是否在线上

    使用60周均线（约300日均线）替代60日均线，
    标准更宽松，避免短期回调导致趋势股被误过滤。

    Args:
        code: 6位股票代码
        name: 股票名称
        market: 0=深圳, 1=上海
        kline_count: 拉取周线数量 (需>60以计算MA60周线)
    """
    try:
        klines = fetch_klines(code, market="a", period="weekly", count=kline_count)
        if not klines or len(klines) < 60:
            return MAFilterResult(
                code=code, name=name, close=0, ma60=0,
                above_ma=False, klines_count=len(klines) if klines else 0,
                error=f"周线不足60根({len(klines) if klines else 0}根)"
            )

        close = klines[-1].close
        ma60 = calculate_ma(klines, 60)

        if ma60 is None:
            return MAFilterResult(
                code=code, name=name, close=close, ma60=0,
                above_ma=False, klines_count=len(klines),
                error="MA60周线计算失败"
            )

        return MAFilterResult(
            code=code, name=name, close=close, ma60=round(ma60, 3),
            above_ma=close > ma60, klines_count=len(klines)
        )
    except Exception as e:
        return MAFilterResult(
            code=code, name=name, close=0, ma60=0,
            above_ma=False, klines_count=0, error=str(e)[:80]
        )


def filter_by_ma60(
    stock_list: list[tuple[str, str, int]],
    max_workers: int = 10,
    kline_count: int = 150,
    progress_callback=None,
) -> list[MAFilterResult]:
    """
    并发拉取全市场周线，按MA60周线过滤

    Args:
        stock_list: [(code, name, market), ...]
        max_workers: 并发线程数
        kline_count: 拉取周线数量 (需>60)
        progress_callback: 进度回调 fn(done, total)

    Returns:
        MA60周线筛选结果列表
    """
    results = []
    total = len(stock_list)
    done = 0

    def _fetch_one(item):
        code, name, market = item
        return fetch_and_filter_ma60(code, name, market, kline_count)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_fetch_one, item): item for item in stock_list}
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            done += 1
            if progress_callback and done % 100 == 0:
                progress_callback(done, total)

    return results


# ============================================================
# Step 3: 卦象序列扫描
# ============================================================

def scan_hexagram_signals(
    stock_pool: list[MAFilterResult],
    sequence_patterns: list[list[int]],
    periods: list[str] = None,
    max_workers: int = 5,
    progress_callback=None,
) -> list[HexagramSignal]:
    """
    对MA60股票池进行多维度卦象序列扫描

    Args:
        stock_pool: MA60过滤后的股票池
        sequence_patterns: 卦象序列模式 [[40, 17], ...]
        periods: 扫描周期 ["daily", "weekly", "monthly"]
        max_workers: 并发线程数
        progress_callback: 进度回调 fn(done, total)

    Returns:
        命中的卦象信号列表
    """
    if periods is None:
        periods = ["daily", "weekly", "monthly"]

    period_labels = {"daily": "日线", "weekly": "周线", "monthly": "月线"}
    period_counts = {"daily": 200, "weekly": 120, "monthly": 60}

    all_signals = []
    total = len(stock_pool) * len(periods)
    done = 0

    def _scan_one(stock: MAFilterResult, period: str) -> list[HexagramSignal]:
        """扫描单只股票单个周期"""
        signals = []
        try:
            count = period_counts.get(period, 120)
            klines = fetch_klines(stock.code, market="a", period=period, count=count)
            if not klines or len(klines) < 7:
                return signals

            seq_matches = detect_hexagram_sequences(klines, sequence_patterns)
            for sm in seq_matches:
                signals.append(HexagramSignal(
                    code=stock.code,
                    name=stock.name,
                    period=period,
                    period_label=period_labels[period],
                    start_date=sm.start_date,
                    end_date=sm.end_date,
                    hexagram_names=sm.hexagram_names,
                    close=stock.close,
                    ma60=stock.ma60,
                ))
        except Exception:
            pass
        return signals

    # 逐股票逐周期扫描
    tasks = []
    for stock in stock_pool:
        for period in periods:
            tasks.append((stock, period))

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_scan_one, s, p): (s, p) for s, p in tasks}
        for future in as_completed(futures):
            signals = future.result()
            all_signals.extend(signals)
            done += 1
            if progress_callback and done % 50 == 0:
                progress_callback(done, total)

    # 按日期排序
    all_signals.sort(key=lambda s: (s.code, s.period, s.start_date))
    return all_signals


# ============================================================
# 全流程
# ============================================================

def run_full_scan(
    sequence_patterns: list[list[int]],
    max_workers_ma: int = 10,
    max_workers_hex: int = 5,
    kline_count: int = 150,
    time_filter_days: int = 365,
    periods: list[str] = None,
) -> ScanSummary:
    """
    全流程: 获取股票列表 → MA60过滤 → 卦象扫描 → 时间过滤

    Args:
        sequence_patterns: 卦象序列模式
        max_workers_ma: MA60过滤并发数
        max_workers_hex: 卦象扫描并发数
        kline_count: MA60周线拉取数量
        time_filter_days: 只保留最近N天内触发的信号 (默认365天)
        periods: 扫描周期

    Returns:
        ScanSummary 汇总
    """
    if periods is None:
        periods = ["daily", "weekly", "monthly"]

    t0 = time.time()

    # 1. 获取全市场股票列表
    print("=" * 70)
    print("  卦象筛选系统 — 全市场批量扫描")
    print(f"  信号模式: {' → '.join(get_hexagram_info(p[0])[0] + ' → ' + get_hexagram_info(p[-1])[0] for p in sequence_patterns)}")
    print(f"  过滤条件: MA60周线 + 最近{time_filter_days}天信号")
    print("=" * 70)

    print("\n[Step 1/3] 获取全市场A股列表...")
    stock_list = get_a_share_list()
    print(f"  全市场A股: {len(stock_list)} 只")

    # 2. MA60周线过滤
    print(f"\n[Step 2/3] MA60周线过滤 (收盘价 > MA60周线)...")
    print(f"  并发线程: {max_workers_ma} | 周线数量: {kline_count}")

    def ma_progress(done, total):
        pct = done / total * 100
        print(f"  进度: {done}/{total} ({pct:.1f}%)", end="\r")

    ma_results = filter_by_ma60(
        stock_list, max_workers=max_workers_ma,
        kline_count=kline_count, progress_callback=ma_progress,
    )

    # 统计
    ma_passed = [r for r in ma_results if r.above_ma]
    ma_failed = [r for r in ma_results if not r.above_ma and r.error is None]
    ma_errors = [r for r in ma_results if r.error is not None]

    print(f"\n  MA60周线筛选完成:")
    print(f"    线上(通过): {len(ma_passed)} 只")
    print(f"    线下(过滤): {len(ma_failed)} 只")
    print(f"    数据异常:   {len(ma_errors)} 只")

    # 3. 卦象扫描
    print(f"\n[Step 3/3] 卦象序列扫描 (日线/周线/月线)...")
    print(f"  待扫描: {len(ma_passed)} 只 × {len(periods)} 周期 = {len(ma_passed)*len(periods)} 次")
    print(f"  并发线程: {max_workers_hex}")

    def hex_progress(done, total):
        pct = done / total * 100
        print(f"  进度: {done}/{total} ({pct:.1f}%)", end="\r")

    signals_before = scan_hexagram_signals(
        ma_passed, sequence_patterns,
        periods=periods, max_workers=max_workers_hex,
        progress_callback=hex_progress,
    )

    signal_count_before = len(signals_before)

    # 4. 时间过滤: 只保留最近一年内的信号
    cutoff_date = (datetime.now() - timedelta(days=time_filter_days)).strftime("%Y-%m-%d")
    signals = [s for s in signals_before if s.end_date >= cutoff_date]

    elapsed = time.time() - t0

    # 去重信号股票
    signal_stock_codes = set(s.code for s in signals)
    filtered_count = signal_count_before - len(signals)

    print(f"\n  卦象扫描完成:")
    print(f"    扫描信号: {signal_count_before} 次")
    print(f"    时间过滤: 去除{filtered_count}次(信号结束日期早于{cutoff_date})")
    print(f"    保留信号: {len(signals)} 次 (涉及{len(signal_stock_codes)}只)")
    print(f"    总耗时: {elapsed:.1f}s")

    summary = ScanSummary(
        total_stocks=len(stock_list),
        ma60_passed=len(ma_passed),
        ma60_failed=len(ma_failed),
        ma60_errors=len(ma_errors),
        signal_count_before_filter=signal_count_before,
        signal_count=len(signals),
        signal_stocks=len(signal_stock_codes),
        time_filter_days=time_filter_days,
        elapsed_seconds=round(elapsed, 1),
        stock_pool=ma_passed,
        signals=signals,
    )

    return summary


def classify_signals(signals: list[HexagramSignal]) -> dict:
    """
    信号分层分类

    Returns:
        {
            "daily_only": [(code, name, signals), ...],      # 仅日线(短期噪音)
            "wm_only": [(code, name, signals), ...],          # 仅周线/月线(等待买点)
            "resonance": [(code, name, signals), ...],        # 日线+周线/月线共振(优质)
            "triple": [(code, name, signals), ...],           # 三重共振(日线+周线+月线)
        }
    """
    by_stock = {}
    for s in signals:
        by_stock.setdefault(s.code, []).append(s)

    daily_only = []
    wm_only = []
    resonance = []
    triple = []

    for code, sigs in by_stock.items():
        name = sigs[0].name
        has_d = any(s.period == "daily" for s in sigs)
        has_w = any(s.period == "weekly" for s in sigs)
        has_m = any(s.period == "monthly" for s in sigs)
        has_wm = has_w or has_m

        if has_d and has_wm:
            resonance.append((code, name, sigs))
            if has_w and has_m:
                triple.append((code, name, sigs))
        elif has_d and not has_wm:
            daily_only.append((code, name, sigs))
        elif not has_d and has_wm:
            wm_only.append((code, name, sigs))

    return {
        "daily_only": daily_only,
        "wm_only": wm_only,
        "resonance": resonance,
        "triple": triple,
    }


def export_to_file(summary: ScanSummary, output_dir: str = None, time_filter_days: int = 365):
    """
    导出扫描结果到文件

    生成3个文件:
    1. resonance_codes.txt — 共振股票代码列表(每行一个)，可直接导入同花顺
    2. resonance_detail.csv — 共振股票详情(代码/名称/维度/信号区间/卦象)
    3. all_signals.csv — 全部信号详情

    Args:
        summary: 扫描汇总
        output_dir: 输出目录，默认为项目目录下 output/
        time_filter_days: 时间窗口标注
    """
    import csv
    import os

    if output_dir is None:
        output_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
    os.makedirs(output_dir, exist_ok=True)

    classified = classify_signals(summary.signals)

    # --- 1. 共振股票代码列表 (同花顺导入用) ---
    codes_path = os.path.join(output_dir, "resonance_codes.txt")
    with open(codes_path, "w", encoding="utf-8") as f:
        for code, name, sigs in classified["resonance"]:
            f.write(f"{code}\n")
    print(f"  共振代码列表: {codes_path} ({len(classified['resonance'])}只)")

    # --- 2. 共振股票详情 CSV (按最新信号结束时间倒序) ---
    detail_path = os.path.join(output_dir, "resonance_detail.csv")
    with open(detail_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["股票代码", "股票名称", "日线信号数", "周线信号数", "月线信号数",
                         "日线区间", "周线区间", "月线区间", "最新收盘价", "MA60周线"])
        # 按最新信号结束时间倒序排列
        sorted_resonance = sorted(
            classified["resonance"],
            key=lambda x: max(s.end_date for s in x[2]),
            reverse=True,
        )
        for code, name, sigs in sorted_resonance:
            d_sigs = sorted([s for s in sigs if s.period == "daily"],
                            key=lambda s: s.end_date, reverse=True)
            w_sigs = sorted([s for s in sigs if s.period == "weekly"],
                            key=lambda s: s.end_date, reverse=True)
            m_sigs = sorted([s for s in sigs if s.period == "monthly"],
                            key=lambda s: s.end_date, reverse=True)
            d_range = "; ".join(f"{s.start_date}~{s.end_date}" for s in d_sigs) or "-"
            w_range = "; ".join(f"{s.start_date}~{s.end_date}" for s in w_sigs) or "-"
            m_range = "; ".join(f"{s.start_date}~{s.end_date}" for s in m_sigs) or "-"
            close = sigs[0].close
            ma60 = sigs[0].ma60
            writer.writerow([code, name, len(d_sigs), len(w_sigs), len(m_sigs),
                             d_range, w_range, m_range, close, ma60])
    print(f"  共振详情CSV: {detail_path}")

    # --- 3. 全部信号 CSV (按维度分块，每块内按结束时间倒序) ---
    all_path = os.path.join(output_dir, "all_signals.csv")
    with open(all_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["股票代码", "股票名称", "维度", "信号起始日", "信号结束日",
                         "卦象序列", "收盘价", "MA60周线", "分层归类"])
        # 预计算每只股票的归类
        stock_category = {}
        for cat_name, items in [("仅日线", classified["daily_only"]),
                                 ("仅周线月线", classified["wm_only"]),
                                 ("日线+周线月线共振", classified["resonance"])]:
            for code, name, sigs in items:
                stock_category[code] = cat_name
        for cat_name, items in [("三重共振", classified["triple"])]:
            for code, name, sigs in items:
                stock_category[code] = cat_name

        # 按维度分块，每块内按结束时间倒序
        period_order = [("daily", "日线"), ("weekly", "周线"), ("monthly", "月线")]
        for period_key, period_label in period_order:
            period_signals = [s for s in summary.signals if s.period == period_key]
            period_signals.sort(key=lambda s: s.end_date, reverse=True)
            for s in period_signals:
                seq_str = " → ".join(s.hexagram_names)
                cat = stock_category.get(s.code, "")
                writer.writerow([s.code, s.name, s.period_label, s.start_date, s.end_date,
                                 seq_str, s.close, s.ma60, cat])
    print(f"  全部信号CSV: {all_path} ({len(summary.signals)}条)")

    # --- 汇总 ---
    print(f"\n  📂 导出完成:")
    print(f"  {'─' * 50}")
    print(f"  共振股票(日线+周线/月线): {len(classified['resonance'])}只")
    print(f"    其中三重共振: {len(classified['triple'])}只")
    print(f"  仅日线(噪音): {len(classified['daily_only'])}只")
    print(f"  仅周线/月线(待买): {len(classified['wm_only'])}只")
    print(f"  信号股票总数: {len(classified['resonance']) + len(classified['daily_only']) + len(classified['wm_only'])}只")


def print_scan_report(summary: ScanSummary):
    """打印扫描报告"""
    print(f"\n{'=' * 70}")
    print(f"  扫描报告")
    print(f"{'=' * 70}")

    # 汇总
    print(f"\n  📊 汇总")
    print(f"  {'─' * 40}")
    print(f"  全市场A股:      {summary.total_stocks} 只")
    print(f"  MA60周线上:     {summary.ma60_passed} 只 ({summary.ma60_passed/summary.total_stocks*100:.1f}%)")
    print(f"  MA60周线下:     {summary.ma60_failed} 只")
    print(f"  MA60数据异常:   {summary.ma60_errors} 只")
    print(f"  扫描信号(原始): {summary.signal_count_before_filter} 次")
    print(f"  时间过滤:       去除{summary.signal_count_before_filter - summary.signal_count}次(最近{summary.time_filter_days}天外)")
    print(f"  保留信号:       {summary.signal_count} 次 (涉及{summary.signal_stocks}只)")
    print(f"  总耗时:         {summary.elapsed_seconds}s")

    if not summary.signals:
        print(f"\n  本次扫描无卦象序列信号命中。")
        return

    # 按维度分组
    by_period = {}
    for s in summary.signals:
        by_period.setdefault(s.period_label, []).append(s)

    for period_label, sigs in by_period.items():
        print(f"\n  📈 {period_label}维度 — 命中{len(sigs)}次")
        print(f"  {'─' * 60}")
        print(f"  {'股票':<12} {'名称':<8} {'信号区间':<28} {'卦象序列'}")
        print(f"  {'─' * 60}")
        for s in sigs:
            seq_str = " → ".join(s.hexagram_names)
            date_range = f"{s.start_date} ~ {s.end_date}"
            print(f"  {s.code:<12} {s.name:<8} {date_range:<28} {seq_str}")

    # 按股票分组（每只股票跨维度汇总）
    by_stock = {}
    for s in summary.signals:
        by_stock.setdefault(s.code, []).append(s)

    if len(by_stock) > 1:
        print(f"\n  🔍 股票信号汇总 (多维度命中)")
        print(f"  {'─' * 50}")
        print(f"  {'代码':<10} {'名称':<8} {'日线':>6} {'周线':>6} {'月线':>6} {'合计':>6}")
        print(f"  {'─' * 50}")
        for code, sigs in sorted(by_stock.items(), key=lambda x: -len(x[1])):
            name = sigs[0].name
            d = len([s for s in sigs if s.period == "daily"])
            w = len([s for s in sigs if s.period == "weekly"])
            m = len([s for s in sigs if s.period == "monthly"])
            print(f"  {code:<10} {name:<8} {d:>6} {w:>6} {m:>6} {len(sigs):>6}")
