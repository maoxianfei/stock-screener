"""
卦象筛选系统 — 入口程序
=========================
三步漏斗: 全市场A股 → MA60周线过滤 → 卦象序列扫描 → 最近一年信号过滤
支持 日线/周线/月线 三维度，筛选符合指定卦象序列的个股。

用法:
  python main.py              # 全市场扫描 (MA60周线 + 卦象 + 时间过滤)
  python main.py --single 600021   # 单股三维度扫描
"""

import sys
import os
import argparse

# 添加项目根目录到 path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from hexagram_engine import (
    klines_to_hexagrams,
    find_matches,
    detect_hexagram_sequences,
    get_hexagram_info,
    format_yao_sequence,
)
from data_fetcher import fetch_klines
from screener import (
    run_full_scan,
    print_scan_report,
    export_to_file,
    ScanSummary,
)


# ============================================================
# 信号配置：火地晋 → 水雷屯 连续序列
# ============================================================
SIGNAL_YJIN = 40   # 火地晋
SIGNAL_SLT = 17    # 水雷屯

# 序列模式列表 (可扩展多个模式)
SEQUENCE_PATTERNS = [
    [SIGNAL_YJIN, SIGNAL_SLT],   # 火地晋 → 水雷屯
]


def scan_single(code: str, market: str = "a"):
    """
    单只个股三维度卦象序列扫描
    仅输出序列信号命中，不展示独立卦象
    """
    print("=" * 60)
    print(f"  卦象筛选系统 — 单股扫描")
    print(f"  信号模式: 火地晋(40) → 水雷屯(17)")
    print(f"  标的: {code}")
    print("=" * 60)

    periods = [
        ("daily", "日线", 200),
        ("weekly", "周线", 120),
        ("monthly", "月线", 60),
    ]

    results = []

    for period, label, count in periods:
        try:
            klines = fetch_klines(code, market=market, period=period, count=count)
            if not klines or len(klines) < 7:
                print(f"  {label}: K线不足 ({len(klines) if klines else 0}根)")
                continue

            seq_matches = detect_hexagram_sequences(klines, SEQUENCE_PATTERNS)

            if seq_matches:
                print(f"  {label}: ★ {len(seq_matches)}次信号")
                for idx, sm in enumerate(seq_matches, 1):
                    seq_str = " → ".join(sm.hexagram_names)
                    print(f"    #{idx} {sm.start_date} ~ {sm.end_date} | {seq_str}")
            else:
                print(f"  {label}: 0次信号")

            results.append({
                "period": label,
                "klines": len(klines),
                "date_range": f"{klines[0].date} ~ {klines[-1].date}",
                "seq_count": len(seq_matches),
                "seq_matches": seq_matches,
            })
        except Exception as e:
            print(f"  {label}: [错误] {e}")
            results.append({"period": label, "seq_count": 0, "error": str(e)})

    # 汇总
    total_signals = sum(r.get("seq_count", 0) for r in results)
    print(f"\n{'─' * 60}")
    print(f"  汇总: {code} 序列信号共 {total_signals} 次")
    for r in results:
        print(f"    {r['period']}: {r.get('seq_count', 0)}次")

    return results


def scan_market(time_filter_days: int = 365, export: bool = False, output_dir: str = None):
    """
    全市场批量扫描: MA60预筛选 + 卦象序列扫描

    Args:
        time_filter_days: 时间过滤窗口(天)
        export: 是否导出文件
        output_dir: 导出目录
    """
    summary = run_full_scan(
        sequence_patterns=SEQUENCE_PATTERNS,
        max_workers_ma=10,
        max_workers_hex=5,
        kline_count=150,
        time_filter_days=time_filter_days,
    )

    print_scan_report(summary)

    if export:
        print(f"\n{'=' * 70}")
        print(f"  导出数据文件")
        print(f"{'=' * 70}")
        export_to_file(summary, output_dir=output_dir, time_filter_days=time_filter_days)

    return summary


def main():
    parser = argparse.ArgumentParser(description="卦象筛选系统")
    parser.add_argument("--single", type=str, default=None,
                        help="单股扫描模式，指定股票代码 (如 600021)")
    parser.add_argument("--market", type=str, default="a",
                        help="市场 (a=A股, hk=港股, us=美股)，默认 a")
    parser.add_argument("--days", type=int, default=365,
                        help="时间过滤窗口(天)，默认365")
    parser.add_argument("--export", action="store_true",
                        help="导出数据文件(CSV+代码列表)")
    parser.add_argument("--output-dir", type=str, default=None,
                        help="导出目录，默认项目下output/")

    args = parser.parse_args()

    if args.single:
        scan_single(args.single, args.market)
    else:
        scan_market(
            time_filter_days=args.days,
            export=args.export,
            output_dir=args.output_dir,
        )


if __name__ == "__main__":
    main()
