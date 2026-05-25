"""
周线维度 — 火地晋→水雷屯 形态匹配统计
==========================================
从全市场A股拉取21根周线K线数据，
筛选符合「火地晋(40) → 水雷屯(17)」连续卦象的标的，
标识每次形态触发的结束日期，输出完整统计CSV。

用法:
  python weekly_match_stats.py
  python weekly_match_stats.py --single 600519
  python weekly_match_stats.py --workers 10
"""

import sys
import os
import csv
import time
import argparse
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

# 确保项目根目录在 path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from data_fetcher import fetch_klines
from hexagram_engine import detect_hexagram_sequences, KLine

# ============================================================
# 信号配置
# ============================================================
SIGNAL_YJIN = 40   # 火地晋
SIGNAL_SLT = 17    # 水雷屯
PATTERNS = [[SIGNAL_YJIN, SIGNAL_SLT]]

WEEKLY_COUNT = 21   # 周线拉取数量

# ============================================================
# 数据结构
# ============================================================

@dataclass
class StockMatchResult:
    """单只股票的形态匹配结果"""
    code: str
    name: str
    total_matches: int          # 总命中次数
    match_dates: list[str]      # 形态结束日期列表
    match_details: list[str]    # 日期区间详情 "start~end"
    latest_close: float         # 最新收盘价
    error: str = ""


# ============================================================
# 单股扫描
# ============================================================

def scan_single_weekly(code: str, name: str = "") -> StockMatchResult:
    """
    对单只股票执行周线21周期的火地晋→水雷屯形态检测

    Returns:
        StockMatchResult 包含命中次数和每次的结束日期
    """
    try:
        klines = fetch_klines(code, market="a", period="weekly", count=WEEKLY_COUNT)
        if not klines or len(klines) < 7:
            return StockMatchResult(
                code=code, name=name, total_matches=0,
                match_dates=[], match_details=[],
                latest_close=klines[-1].close if klines else 0,
                error=f"周线不足({len(klines) if klines else 0}根)"
            )

        latest_close = klines[-1].close
        seq_matches = detect_hexagram_sequences(klines, PATTERNS)

        match_dates = []
        match_details = []
        for sm in seq_matches:
            match_dates.append(sm.end_date)
            match_details.append(f"{sm.start_date}~{sm.end_date}")

        return StockMatchResult(
            code=code, name=name,
            total_matches=len(seq_matches),
            match_dates=match_dates,
            match_details=match_details,
            latest_close=latest_close,
        )
    except Exception as e:
        return StockMatchResult(
            code=code, name=name, total_matches=0,
            match_dates=[], match_details=[],
            latest_close=0, error=str(e)[:100]
        )


# ============================================================
# 全市场股票列表
# ============================================================

def get_a_share_list() -> list[tuple[str, str]]:
    """
    获取全部A股股票列表 (沪深+北交所)

    Returns:
        [(code, name), ...]
    """
    from mootdx.quotes import Quotes
    client = Quotes.factory(market='std')

    results = []

    # 沪市
    sh_all = client.stocks(market=1)
    if sh_all is not None and len(sh_all) > 0:
        mask = sh_all['code'].str.match(r'^60\d{4}$') | \
               sh_all['code'].str.match(r'^688\d{3}$')
        sh_a = sh_all[mask]
        for _, row in sh_a.iterrows():
            results.append((row['code'], row['name']))

    # 深市 + 北交所
    sz_all = client.stocks(market=0)
    if sz_all is not None and len(sz_all) > 0:
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
            results.append((row['code'], row['name']))

    results.sort(key=lambda x: x[0])
    return results


# ============================================================
# 批量扫描 + 统计导出
# ============================================================

def run_weekly_scan(
    stock_list: list[tuple[str, str]],
    max_workers: int = 8,
) -> tuple[list[StockMatchResult], float]:
    """
    并发扫描全市场A股周线形态

    Args:
        stock_list: [(code, name), ...]
        max_workers: 并发线程数

    Returns:
        (命中结果列表, 总耗时秒数)
    """
    results_all: list[StockMatchResult] = []
    total = len(stock_list)
    done = 0
    t0 = time.time()

    def scan(item):
        code, name = item
        return scan_single_weekly(code, name)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(scan, item): item for item in stock_list}
        for future in as_completed(futures):
            result = future.result()
            results_all.append(result)
            done += 1
            if done % 100 == 0:
                elapsed = time.time() - t0
                speed = done / elapsed if elapsed > 0 else 0
                hits_so_far = sum(1 for r in results_all if r.total_matches > 0)
                print(f"  进度: {done}/{total} ({done/total*100:.1f}%) | "
                      f"已命中: {hits_so_far}只 | 速度: {speed:.0f}只/秒", end="\r")

    elapsed = time.time() - t0
    print(f"\n  扫描完成: {done}/{total} | 耗时 {elapsed:.1f}s")

    return results_all, elapsed


def export_weekly_stats(
    results: list[StockMatchResult],
    output_dir: str = None,
) -> str:
    """
    导出周线形态统计CSV

    输出字段:
      股票代码, 股票名称, 命中次数, 最新收盘价,
      所有结束日期(分号分隔), 所有信号区间(分号分隔), 备注

    Returns:
        导出文件路径
    """
    if output_dir is None:
        output_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
    os.makedirs(output_dir, exist_ok=True)

    # 分类
    hits = sorted(
        [r for r in results if r.total_matches > 0],
        key=lambda r: (-r.total_matches, r.code)
    )
    no_hits = sorted(
        [r for r in results if r.total_matches == 0 and not r.error],
        key=lambda r: r.code
    )
    errors = sorted(
        [r for r in results if r.error],
        key=lambda r: r.code
    )

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = os.path.join(output_dir, f"weekly_yjslt_stats_{timestamp}.csv")

    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "股票代码", "股票名称", "命中次数",
            "形态结束日期(所有)", "信号区间(所有)",
            "最新收盘价", "备注"
        ])

        for r in hits:
            writer.writerow([
                r.code, r.name, r.total_matches,
                "; ".join(r.match_dates),
                "; ".join(r.match_details),
                round(r.latest_close, 2),
                "",
            ])

        # 有error的放在命中区后面
        for r in errors:
            writer.writerow([
                r.code, r.name, 0, "", "",
                round(r.latest_close, 2), f"数据异常: {r.error}"
            ])

        # 无命中的不写入详细行，仅统计

    print(f"\n{'='*70}")
    print(f"  周线火地晋→水雷屯 统计报告")
    print(f"{'='*70}")
    print(f"  全市场A股:    {len(results)} 只")
    print(f"  形态命中:     {len(hits)} 只 ({len(hits)/max(len(results),1)*100:.2f}%)")
    print(f"  无命中:       {len(no_hits)} 只")
    print(f"  数据异常:     {len(errors)} 只")

    total_signals = sum(r.total_matches for r in hits)
    print(f"  总信号次数:   {total_signals}")
    print(f"  均命中次数:   {total_signals/max(len(hits),1):.1f} 次/股")

    # 命中次数分布
    dist = {}
    for r in hits:
        dist[r.total_matches] = dist.get(r.total_matches, 0) + 1
    print(f"\n  命中次数分布:")
    for k in sorted(dist.keys(), reverse=True):
        bar = "█" * dist[k]
        print(f"    {k}次: {dist[k]:>4}只 {bar}")

    # 多命中股票 Top 10
    if hits:
        print(f"\n  Top 10 多命中股票:")
        print(f"  {'代码':<10} {'名称':<10} {'命中':<6} {'信号结束日期'}")
        print(f"  {'─'*50}")
        for r in hits[:10]:
            print(f"  {r.code:<10} {r.name:<10} {r.total_matches:<6} {'; '.join(r.match_dates)}")

    print(f"\n  CSV已导出: {csv_path}")

    return csv_path


# ============================================================
# 入口
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="周线维度 — 火地晋→水雷屯 K线形态匹配统计"
    )
    parser.add_argument(
        "--single", type=str, default=None,
        help="单股扫描模式, 如 --single 600519"
    )
    parser.add_argument(
        "--workers", type=int, default=8,
        help="并发线程数 (默认8)"
    )
    parser.add_argument(
        "--output-dir", type=str, default=None,
        help="输出目录, 默认项目下output/"
    )

    args = parser.parse_args()

    print("=" * 70)
    print("  周线维度 — 火地晋→水雷屯 K线形态匹配统计")
    print(f"  拉取周线: {WEEKLY_COUNT} 根 | 并发: {args.workers} 线程")
    print("=" * 70)

    if args.single:
        print(f"\n  单股扫描: {args.single}")
        result = scan_single_weekly(args.single)
        if result.error:
            print(f"  错误: {result.error}")
            return 1
        print(f"  最新收盘价: {result.latest_close}")
        print(f"  入场信号: {result.total_matches} 次")
        if result.total_matches > 0:
            for i, (d, detail) in enumerate(zip(result.match_dates, result.match_details), 1):
                print(f"    #{i} 形态结束日期: {d}  ({detail})")
        else:
            print(f"  无火地晋→水雷屯信号")
        return 0

    # 全市场扫描
    print("\n[Step 1] 获取全市场A股列表...")
    stock_list = get_a_share_list()
    print(f"  全市场A股: {len(stock_list)} 只")

    print(f"\n[Step 2] 周线形态批量扫描 (每只{WEEKLY_COUNT}根周线)...")
    results, elapsed = run_weekly_scan(stock_list, max_workers=args.workers)

    print(f"\n[Step 3] 导出统计报告...")
    export_weekly_stats(results, output_dir=args.output_dir)

    return 0


if __name__ == "__main__":
    sys.exit(main())
