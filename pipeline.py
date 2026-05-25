"""
火地晋爆点 — 周线信号筛选 Pipeline
=====================================
一轮：全市场周线扫描，检测「火地晋→水雷屯」周K线形态
二轮：日K线双规则过滤，量价共振确认

用法:
  python pipeline.py                    # 跑全流程
  python pipeline.py --step 1           # 只跑一轮
  python pipeline.py --step 2           # 只跑二轮(需指定input)
  python pipeline.py --single 600021    # 单股全流程
  python pipeline.py --workers 10       # 自定义并发

输出:
  output/一轮周线信号数据_火地晋水雷屯_YYYYMMDD.csv
  output/二轮双规则数据_火地晋水雷屯_YYYYMMDD.csv
"""

import sys
import os
import csv
import time
import argparse
import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

# 项目根目录
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_DIR)

from data_fetcher import fetch_klines
from hexagram_engine import detect_hexagram_sequences

OUTPUT_DIR = os.path.join(PROJECT_DIR, "output")
LOCK = threading.Lock()

# ════════════════════════════════════════════════════════
# 统一配置
# ════════════════════════════════════════════════════════

CONFIG = {
    # ── 卦象信号 ──
    "signal_hexagrams": [40, 17],        # 火地晋 → 水雷屯
    "sequence_patterns": [[40, 17]],
    "max_gap": 0,                        # 紧挨（火地晋→水雷屯在全市场天然高频）

    # ── 一轮：周线扫描 ──
    "round1": {
        "weekly_count": 21,              # 拉取周线数量
    },

    # ── 二轮：日线双规则过滤 ──
    "round2": {
        "daily_count": 150,              # 拉取日线数量
        "vol_window": 120,               # 成交量均线周期
        "price_ma": 70,                  # 价格均线周期
        "vol_lookback_days": 25,         # 量规则回看天数（~5周）
        "price_tolerance": 0.95,         # 价规则容差（0.95 = 允许-5%）
    },
}


# ════════════════════════════════════════════════════════
# 数据结构
# ════════════════════════════════════════════════════════

@dataclass
class Round1Result:
    """一轮扫描结果"""
    code: str
    name: str
    total_matches: int
    match_dates: list[str]
    match_intervals: list[str]
    match_gaps: list[int]         # 每次命中的gap值
    latest_close: float
    error: str = ""


# ════════════════════════════════════════════════════════
# 一轮：周线形态扫描
# ════════════════════════════════════════════════════════

def get_stock_list() -> list[tuple[str, str]]:
    """获取全市场A股列表"""
    from mootdx.quotes import Quotes
    client = Quotes.factory(market='std')
    results = []

    # 沪市
    sh_all = client.stocks(market=1)
    if sh_all is not None and len(sh_all) > 0:
        mask = sh_all['code'].str.match(r'^60\d{4}$') | \
               sh_all['code'].str.match(r'^688\d{3}$')
        for _, row in sh_all[mask].iterrows():
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
        for _, row in sz_all[mask].iterrows():
            results.append((row['code'], row['name']))

    results.sort(key=lambda x: x[0])
    return results


def scan_one_weekly(code: str, name: str = "") -> Round1Result:
    """单股周线火地晋→水雷屯形态检测"""
    wc = CONFIG["round1"]["weekly_count"]
    try:
        klines = fetch_klines(code, market="a", period="weekly", count=wc)
        if not klines or len(klines) < 7:
            return Round1Result(
                code=code, name=name, total_matches=0,
                match_dates=[], match_intervals=[], match_gaps=[],
                latest_close=klines[-1].close if klines else 0,
                error=f"周线不足({len(klines) if klines else 0}根)"
            )

        latest_close = klines[-1].close
        seq = detect_hexagram_sequences(klines, CONFIG["sequence_patterns"],
                                        max_gap=CONFIG["max_gap"])

        dates = [s.end_date for s in seq]
        intervals = [f"{s.start_date}~{s.end_date}" for s in seq]
        gaps = [s.gap for s in seq]

        return Round1Result(
            code=code, name=name,
            total_matches=len(seq),
            match_dates=dates,
            match_intervals=intervals,
            match_gaps=gaps,
            latest_close=latest_close,
        )
    except Exception as e:
        return Round1Result(
            code=code, name=name, total_matches=0,
            match_dates=[], match_intervals=[], match_gaps=[],
            latest_close=0, error=str(e)[:100]
        )


def run_round1(stock_list: list[tuple[str, str]], max_workers: int = 8):
    """一轮：全市场周线扫描"""
    results: list[Round1Result] = []
    total = len(stock_list)
    done = 0
    t0 = time.time()

    def _scan(item):
        code, name = item
        return scan_one_weekly(code, name)

    print(f"  [一轮] 并发 {max_workers} 线程, 每只 {CONFIG['round1']['weekly_count']} 根周线")
    print(f"  [一轮] 扫描 {total} 只股票...")

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_scan, item): item for item in stock_list}
        for f in as_completed(futures):
            results.append(f.result())
            done += 1
            if done % 200 == 0:
                elapsed = time.time() - t0
                speed = done / elapsed if elapsed > 0 else 0
                hits_so_far = sum(1 for r in results if r.total_matches > 0)
                print(f"    [{done}/{total}] {done/total*100:.0f}% | "
                      f"命中{hits_so_far}只 | {speed:.0f}只/秒", end="\r")

    elapsed = time.time() - t0
    print(f"\n  [一轮] 完成: {done}/{total} | 耗时 {elapsed:.1f}s")
    return results, elapsed


def export_round1(results: list[Round1Result]) -> str:
    """导出一轮CSV"""
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    hits = sorted(
        [r for r in results if r.total_matches > 0],
        key=lambda r: (-r.total_matches, r.code)
    )
    no_hits = [r for r in results if r.total_matches == 0 and not r.error]
    errors = [r for r in results if r.error]

    timestamp = datetime.now().strftime("%Y%m%d")
    path = os.path.join(OUTPUT_DIR, f"一轮周线信号数据_火地晋水雷屯_{timestamp}.csv")

    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "股票代码", "股票名称", "命中次数",
            "形态结束日期(所有)", "信号区间(所有)", "间隔(根)",
            "最新收盘价", "备注", "轮次"
        ])

        for r in hits:
            writer.writerow([
                r.code, r.name, r.total_matches,
                "; ".join(r.match_dates),
                "; ".join(r.match_intervals),
                "; ".join(str(g) for g in r.match_gaps),
                round(r.latest_close, 2), "", "一轮",
            ])

        for r in errors:
            writer.writerow([
                r.code, r.name, 0, "", "", "",
                round(r.latest_close, 2), f"数据异常: {r.error}", "一轮",
            ])

    total_signals = sum(r.total_matches for r in hits)
    dist = {}
    for r in hits:
        dist[r.total_matches] = dist.get(r.total_matches, 0) + 1

    print(f"\n  {'─'*55}")
    print(f"  📊 一轮周线信号统计")
    print(f"  {'─'*55}")
    print(f"  全市场:    {len(results)} 只")
    print(f"  形态命中:  {len(hits)} 只 ({len(hits)/max(len(results),1)*100:.1f}%)")
    print(f"  无命中:    {len(no_hits)} 只")
    print(f"  数据异常:  {len(errors)} 只")
    print(f"  总信号:    {total_signals} 次")

    print(f"\n  命中分布:")
    for k in sorted(dist.keys(), reverse=True):
        bar = "█" * min(dist[k], 60)
        print(f"    {k}次: {dist[k]:>4}只 {bar}")

    print(f"\n  📁 一轮CSV: {path}")
    return path


# ════════════════════════════════════════════════════════
# 二轮：日线双规则筛选
# ════════════════════════════════════════════════════════

class Round2Stats:
    def __init__(self):
        self.pass_both = 0
        self.fail_vol_only = 0
        self.fail_price_only = 0
        self.fail_both = 0
        self.error = 0
        self.skipped = 0
        self.checked = 0


def check_round2(code: str):
    """
    对单股执行二轮双规则检查

    Returns:
        (vol_pass, price_pass, vol_ratio, price_diff, err_msg)
        vol_pass 为 None 表示数据异常
    """
    r2 = CONFIG["round2"]
    try:
        klines = fetch_klines(code, market="a", period="daily", count=r2["daily_count"])
    except Exception as e:
        return None, None, 0, 0, f"拉取异常:{e}"

    if not klines or len(klines) < 80:
        return None, None, 0, 0, f"数据不足({len(klines) if klines else 0})"

    n = len(klines)

    # 规则1：最近N天至少1天成交量 > 120日均量
    vw = r2["vol_window"]
    vol_ma120 = sum(k.volume for k in klines[-vw:]) / vw if n >= vw else 0
    lookback = min(r2["vol_lookback_days"], n)
    recent_vols = [k.volume for k in klines[-lookback:]]
    vol_pass = any(v > vol_ma120 for v in recent_vols) if vol_ma120 > 0 else False
    avg_vol = sum(recent_vols) / len(recent_vols)
    vol_ratio = avg_vol / vol_ma120 * 100 if vol_ma120 > 0 else 0

    # 规则2：最新收盘价 >= 70日均线 × tolerance
    pm = r2["price_ma"]
    ma_price = sum(k.close for k in klines[-pm:]) / pm if n >= pm else 0
    latest_close = klines[-1].close
    price_pass = latest_close >= ma_price * r2["price_tolerance"] if ma_price > 0 else False
    price_diff = (latest_close - ma_price) / ma_price * 100 if ma_price > 0 else 0

    return vol_pass, price_pass, vol_ratio, price_diff, ""


def run_round2(input_csv: str, max_workers: int = 10) -> str:
    """二轮：读取一轮CSV，执行日线双规则筛选"""
    with open(input_csv, "r", encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))

    stats = Round2Stats()
    results = {}
    pending = []

    for row in rows[1:]:
        code = row[0].strip()
        name = row[1].strip().replace("\x00", "")
        note = row[7].strip()  # 备注列（间隔列插入后移到第8列）
        if note:
            stats.skipped += 1
            continue
        pending.append((code, name, row[3]))  # 形态结束日期

    total = len(pending)
    r2 = CONFIG["round2"]

    print(f"  [二轮] 读取一轮数据: {len(rows)-1} 条记录")
    print(f"  [二轮] 规则1(量): 最近{r2['vol_lookback_days']}天至少1天量>{r2['vol_window']}日均量")
    print(f"  [二轮] 规则2(价): 收盘 ≥ {r2['vol_window']}日均价 × {r2['price_tolerance']}")
    print(f"  [二轮] 通过: 量 ∧ 价 | 并发 {max_workers} 线程")
    print(f"  [二轮] 开始日K线数据拉取+检查 ({total} 只)...")

    def _process(item):
        code, name, sd = item
        vp, pp, vr, pd, err = check_round2(code)
        with LOCK:
            stats.checked += 1
            if vp is None:
                stats.error += 1
                cat = "error"
            elif vp and pp:
                stats.pass_both += 1
                cat = "pass_both"
            elif vp and not pp:
                stats.fail_price_only += 1
                cat = "fail_price_only"
            elif not vp and pp:
                stats.fail_vol_only += 1
                cat = "fail_vol_only"
            else:
                stats.fail_both += 1
                cat = "fail_both"
            results[code] = (vp, pp, vr, pd, cat)

            if stats.checked % 100 == 0:
                print(f"    [{stats.checked}/{total}] "
                      f"通过{stats.pass_both} | 量杀{stats.fail_vol_only+stats.fail_both} | "
                      f"价杀{stats.fail_price_only+stats.fail_both}", end="\r")

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(_process, item): item for item in pending}
        for f in as_completed(futures):
            f.result()

    print()  # 换行

    # 构建二轮CSV
    timestamp = datetime.now().strftime("%Y%m%d")
    out_path = os.path.join(OUTPUT_DIR, f"二轮双规则数据_火地晋水雷屯_{timestamp}.csv")

    out_header = [
        "股票代码", "股票名称", "命中次数", "形态结束日期(所有)",
        "信号区间(所有)", "最新收盘价(周线)", "120日均量", "5周均量比%",
        "70日均价", "最新收盘价(日线)", "线上下%", "通过状态", "轮次"
    ]
    out_rows = [out_header]

    for row in rows[1:]:
        code = row[0].strip()
        name = row[1].strip().replace("\x00", "")

        if code not in results:
            if row[7].strip():  # 备注
                out_rows.append(row[:6] + [row[6]] + ["-", "-", "-", "-", "-", "数据异常", "二轮"])
            continue

        vp, pp, vr, pd, cat = results[code]
        if cat != "pass_both":
            continue

        out_rows.append([
            code, name, row[2], row[3], row[4], row[6],  # row[6]=最新收盘价(周线)
            "", f"{vr:.1f}%", "", "", f"{pd:+.1f}%", "通过", "二轮"
        ])

    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerows(out_rows)

    failed = stats.fail_vol_only + stats.fail_price_only + stats.fail_both

    print(f"\n  {'─'*55}")
    print(f"  📊 二轮双规则筛选")
    print(f"  {'─'*55}")
    print(f"  一轮命中:  {total + stats.skipped}")
    print(f"  跳过异常:  {stats.skipped}")
    print(f"  实际检查:  {stats.checked}")
    print(f"  {'─'*30}")
    print(f"  ✅ 双通过:     {stats.pass_both} ({stats.pass_both/max(stats.checked,1)*100:.1f}%)")
    print(f"  ❌ 仅量不达标:  {stats.fail_vol_only}")
    print(f"  ❌ 仅价不达标:  {stats.fail_price_only}")
    print(f"  ❌ 量价双杀:    {stats.fail_both}")
    print(f"  ⚠ 异常:        {stats.error}")
    print(f"  {'─'*30}")
    print(f"  淘汰合计:      {failed}")
    print(f"  通过率:        {stats.pass_both}/{stats.checked} = {stats.pass_both/max(stats.checked,1)*100:.1f}%")

    # 多次命中
    multi = [r for r in out_rows[1:] if int(r[2]) >= 2 and r[-2] == "通过"]
    multi.sort(key=lambda r: -int(r[2]))
    if multi:
        print(f"\n  📈 多次命中 ({len(multi)}只):")
        print(f"  {'代码':<8} {'名称':<8} {'命中':<4} {'量比':>6} {'价差':>7}  {'信号区间'}")
        print(f"  {'─'*60}")
        for r in multi:
            print(f"  {r[0]:<8} {r[1]:<8} {r[2]:<4} {r[7]:>6} {r[10]:>7}  {r[4][:40]}")

    print(f"\n  📁 二轮CSV: {out_path}")
    return out_path


# ════════════════════════════════════════════════════════
# 单股全流程
# ════════════════════════════════════════════════════════

def run_single(code: str):
    """单股一轮+二轮全流程"""
    print(f"\n{'='*60}")
    print(f"  🔍 单股全流程: {code}")
    print(f"{'='*60}")

    # 一轮
    r1 = scan_one_weekly(code)
    if r1.error:
        print(f"  ❌ 一轮异常: {r1.error}")
        return

    print(f"\n  [一轮] {r1.name or code} | 周线收盘: {r1.latest_close}")
    print(f"  [一轮] 命中: {r1.total_matches} 次")
    for i, (d, iv, gap) in enumerate(zip(r1.match_dates, r1.match_intervals, r1.match_gaps), 1):
        print(f"    #{i} 结束日: {d}  区间: {iv}")

    if r1.total_matches == 0:
        print(f"  ⚠ 无信号，跳过二轮")
        return

    # 二轮
    vp, pp, vr, pd, err = check_round2(code)
    if vp is None:
        print(f"\n  [二轮] ❌ 异常: {err}")
        return

    cat = "✅ 通过" if (vp and pp) else "❌ 淘汰"
    details = []
    if not vp:
        details.append(f"量不达标(均量比{vr:.1f}%)")
    if not pp:
        details.append(f"价不达标(距70日线{pd:+.1f}%)")

    print(f"\n  [二轮] {cat}")
    print(f"  [二轮] 规则1(量): {'✅' if vp else '❌'} 均量比 {vr:.1f}%")
    print(f"  [二轮] 规则2(价)(容差{CONFIG['round2']['price_tolerance']}): {'✅' if pp else '❌'} 距70日线 {pd:+.1f}%")
    if details:
        print(f"  [二轮] 原因: {'; '.join(details)}")


# ════════════════════════════════════════════════════════
# 主入口
# ════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="火地晋爆点 — 周线信号筛选 Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python pipeline.py                    跑全流程（一轮+二轮）
  python pipeline.py --step 1           只跑一轮扫描
  python pipeline.py --step 2 --input output/一轮xxx.csv  只跑二轮筛选
  python pipeline.py --single 600021    单股全流程
  python pipeline.py --workers 15       自定义并发数
        """
    )
    parser.add_argument("--step", type=str, default="all",
                        choices=["1", "2", "all"],
                        help="执行步骤: 1=一轮, 2=二轮, all=全流程 (默认all)")
    parser.add_argument("--single", type=str, default=None,
                        help="单股全流程, 如 --single 600021")
    parser.add_argument("--workers", type=int, default=10,
                        help="并发线程数 (默认10)")
    parser.add_argument("--input", type=str, default=None,
                        help="二轮输入CSV路径 (仅 --step 2 时使用, 默认自动找最新一轮文件)")
    parser.add_argument("--tolerance", type=float, default=None,
                        help=f"价格容差 (默认{CONFIG['round2']['price_tolerance']}, 0.95=允许-5%%)")

    args = parser.parse_args()

    if args.tolerance is not None:
        CONFIG["round2"]["price_tolerance"] = args.tolerance

    # ── 单股模式 ──
    if args.single:
        run_single(args.single)
        return 0

    # ── Pipeline 模式 ──
    print("=" * 60)
    print("  火地晋爆点 — 周线信号筛选 Pipeline")
    print(f"  信号: 火地晋→水雷屯 | 紧挨 | 并发: {args.workers} 线程")
    print(f"  步骤: {'一轮→二轮' if args.step == 'all' else f'仅{args.step}轮'}")
    print("=" * 60)

    r1_csv = None

    if args.step in ("1", "all"):
        # ====== 一轮 ======
        print("\n[Phase 1/2] 一轮：全市场周线形态扫描")
        print("-" * 40)

        print("  获取A股列表...")
        stock_list = get_stock_list()
        print(f"  全市场: {len(stock_list)} 只")

        results, _ = run_round1(stock_list, max_workers=args.workers)
        r1_csv = export_round1(results)

    if args.step in ("2", "all"):
        # ====== 二轮 ======
        print("\n[Phase 2/2] 二轮：日线双规则筛选")
        print("-" * 40)

        if args.input:
            input_csv = args.input
        elif r1_csv:
            input_csv = r1_csv
        else:
            # 自动找最新一轮文件
            os.makedirs(OUTPUT_DIR, exist_ok=True)
            r1_files = sorted([
                f for f in os.listdir(OUTPUT_DIR)
                if f.startswith("一轮周线信号数据_火地晋水雷屯_") and f.endswith(".csv")
            ], reverse=True)
            if not r1_files:
                print("  ❌ 未找到一轮CSV，请先执行 --step 1 或指定 --input")
                return 1
            input_csv = os.path.join(OUTPUT_DIR, r1_files[0])
            print(f"  自动选择: {r1_files[0]}")

        if not os.path.exists(input_csv):
            print(f"  ❌ 一轮文件不存在: {input_csv}")
            return 1

        run_round2(input_csv, max_workers=args.workers)

    return 0


if __name__ == "__main__":
    sys.exit(main())
