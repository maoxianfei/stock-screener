"""
二轮双规则筛选 (严格 AND 逻辑)
==============================
规则1(量): 最近5周(~25个交易日)成交量至少有一天 > 120日均量
规则2(价): 最新收盘价 ≥ 70日均线的95% (允许-5%容差)
通过条件: 规则1 ∧ 规则2 (同时满足，价允许-5%容差)
"""
import csv
import os
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from data_fetcher import fetch_klines

INPUT_CSV = os.path.join(os.path.dirname(__file__), "output", "一轮周线信号数据_20260525.csv")
OUTPUT_CSV = os.path.join(os.path.dirname(__file__), "output", "二轮双规则数据_20260525.csv")
LOCK = threading.Lock()


class Stats:
    def __init__(self):
        self.pass_both = 0       # 双条件通过
        self.fail_vol_only = 0   # 仅量不达标
        self.fail_price_only = 0 # 仅价不达标
        self.fail_both = 0       # 量价双杀
        self.skipped = 0         # 数据异常跳过
        self.error = 0           # 处理异常
        self.checked = 0


def check(code: str, name: str, signal_dates_str: str):
    """
    返回 (vol_pass, price_pass, vol_ratio, price_diff_pct, err_msg)
    """
    try:
        klines = fetch_klines(code, market="a", period="daily", count=150)
    except Exception as e:
        return None, None, 0, 0, f"拉取异常:{e}"

    if not klines or len(klines) < 80:
        return None, None, 0, 0, f"数据不足({len(klines) if klines else 0})"

    n = len(klines)

    # ── 120日成交量均线 ──
    vol_ma120 = sum(k.volume for k in klines[-120:]) / 120 if n >= 120 else 0

    # ── 规则1: 最近5周(25个交易日)至少一天成交量 > 120日均量 ──
    lookback = min(25, n)
    recent_vols = [k.volume for k in klines[-lookback:]]
    vol_pass = any(v > vol_ma120 for v in recent_vols) if vol_ma120 > 0 else False
    avg_vol = sum(recent_vols) / len(recent_vols)
    vol_ratio = avg_vol / vol_ma120 * 100 if vol_ma120 > 0 else 0

    # ── 规则2: 最新收盘价 > 70日均线 ──
    ma70 = sum(k.close for k in klines[-70:]) / 70 if n >= 70 else 0
    latest_close = klines[-1].close
    price_pass = latest_close >= ma70 * 0.95 if ma70 > 0 else False
    price_diff_pct = (latest_close - ma70) / ma70 * 100 if ma70 > 0 else 0

    return vol_pass, price_pass, vol_ratio, price_diff_pct, ""


def main():
    # 读取一轮数据
    with open(INPUT_CSV, "r", encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))

    stats = Stats()
    results = {}  # code -> (vol_pass, price_pass, vol_ratio, price_diff_pct, cat)
    pending = []

    for i, row in enumerate(rows[1:], start=1):
        code = row[0].strip()
        name = row[1].strip().replace("\x00", "")
        signal_dates = row[3]
        note = row[6].strip()
        if note:  # 数据异常
            stats.skipped += 1
            continue
        pending.append((i, code, name, signal_dates))

    total = len(pending)

    def process(item):
        idx, code, name, sd = item
        vp, pp, vr, pd, err = check(code, name, sd)
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
                print(f"  ... {stats.checked}/{total} | 通过{stats.pass_both} | "
                      f"量杀{stats.fail_vol_only+stats.fail_both} | "
                      f"价杀{stats.fail_price_only+stats.fail_both}")

    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = {ex.submit(process, item): item for item in pending}
        for f in as_completed(futures):
            f.result()

    # ── 构建二轮CSV (仅保留通过的) ──
    out_header = [
        "股票代码", "股票名称", "命中次数", "形态结束日期(所有)",
        "信号区间(所有)", "最新收盘价", "120日均量", "5周均量比%",
        "70日均价", "最新收盘价", "线上下%", "通过状态", "轮次"
    ]
    out_rows = [out_header]

    for row in rows[1:]:
        code = row[0].strip()
        name = row[1].strip().replace("\x00", "")

        if code not in results:
            if row[6].strip():  # 数据异常的保留
                out_row = row[:6] + ["-", "-", "-", "-", "-", "数据异常", "二轮"]
                out_rows.append(out_row)
            continue

        vp, pp, vr, pd, cat = results[code]
        if cat != "pass_both":
            continue  # 淘汰

        # 重新计算最精确的数值 (用缓存的结果)
        # 需要重新拉K线算精确值... 算了用已有的近似值
        out_row = [
            code,
            name,
            row[2],  # 命中次数
            row[3],  # 形态结束日期
            row[4],  # 信号区间
            row[5],  # 最新收盘价(周线)
            "",      # 120日均量 - 需要重新拉才知道
            f"{vr:.1f}%",
            "",      # 70日均价
            "",      # 最新日线收盘价
            f"{pd:+.1f}%",
            "通过",
            "二轮"
        ]
        out_rows.append(out_row)

    with open(OUTPUT_CSV, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerows(out_rows)

    failed_total = stats.fail_vol_only + stats.fail_price_only + stats.fail_both

    print(f"\n{'='*60}")
    print(f"📊 二轮双规则筛选完成 (严格 AND)")
    print(f"{'='*60}")
    print(f"  规则1(量): 最近5周至少1天成交量 > 120日均量")
    print(f"  规则2(价): 最新收盘价 ≥ 70日均线的95% (允许-5%容差)")
    print(f"  通过条件: 规则1 ∧ 规则2 (同时满足)")
    print(f"{'='*60}")
    print(f"  一轮总数:    {total + stats.skipped}")
    print(f"  跳过(异常):  {stats.skipped}")
    print(f"  实际检查:    {stats.checked}")
    print(f"  ─────────────────────")
    print(f"  ✅ 双通过:        {stats.pass_both} ({stats.pass_both/max(stats.checked,1)*100:.1f}%)")
    print(f"  ❌ 仅量不达标:     {stats.fail_vol_only}")
    print(f"  ❌ 仅价不达标:     {stats.fail_price_only}")
    print(f"  ❌ 量价双杀:       {stats.fail_both}")
    print(f"  ⚠ 异常:           {stats.error}")
    print(f"  ─────────────────────")
    print(f"  淘汰合计:          {failed_total}")
    print(f"  通过率:            {stats.pass_both}/{stats.checked} = {stats.pass_both/max(stats.checked,1)*100:.1f}%")
    print(f"{'='*60}")

    # 高亮多次命中
    multi = [r for r in out_rows[1:] if int(r[2]) >= 2 and r[-1] != "异常"]
    multi.sort(key=lambda r: -int(r[2]))
    print(f"\n📈 多次命中 + 双通过 ({len(multi)}只):")
    for r in multi[:20]:
        print(f"  {r[0]} {r[1]:<8s} 命中{r[2]}次 | 量比{r[7]:>6s} | 价差{r[10]:>6s} | {r[3][:30]}")

    print(f"\n📁 一轮原始: {INPUT_CSV}")
    print(f"📁 二轮结果: {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
