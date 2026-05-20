#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
快速测试：用上证50 验证选股逻辑
"""
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed

def _find_westock_script():
    env_path = os.environ.get("WESTOCK_SCRIPT")
    if env_path and os.path.isfile(env_path):
        return env_path
    candidates = [
        os.path.join(os.path.expanduser("~"), ".workbuddy", "plugins", "marketplaces",
                     "cb_teams_marketplace", "plugins", "finance-data", "skills",
                     "westock-data", "scripts", "index.js"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return "westock-data"

WESTOCK_SCRIPT = _find_westock_script()

def run_westock(args_list, timeout=20):
    cmd = ["node", WESTOCK_SCRIPT] + args_list
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, encoding="utf-8")
        return r.stdout.strip()
    except:
        return ""

def parse_kline_table(text):
    lines = [l.strip() for l in text.strip().split("\n") if l.strip()]
    header_idx = -1
    for i, line in enumerate(lines):
        if "| date |" in line.lower():
            header_idx = i
            break
    if header_idx == -1:
        return []
    rows = []
    for line in lines[header_idx + 2:]:
        if not line.startswith("|"):
            break
        cols = [c.strip() for c in line.split("|")[1:-1]]
        if len(cols) >= 3:
            try:
                rows.append({"date": cols[0], "open": float(cols[1]), "close": float(cols[2])})
            except:
                pass
    return rows

def parse_pattern(s):
    return [ch == "阳" for ch in s if ch in ("阳", "阴")]

def check_pattern(klines, pattern, days):
    recent = list(reversed(klines[:days]))
    n, pl = len(recent), len(pattern)
    for start in range(n - pl, -1, -1):
        seg = recent[start:start+pl]
        if all((seg[i]["close"] >= seg[i]["open"]) == pattern[i] for i in range(pl)):
            return True, seg[0]["date"], seg[-1]["date"]
    return False, None, None

def get_index_stocks(index_code):
    out = run_westock(["index", index_code])
    stocks = []
    for line in out.split("\n"):
        if not line.strip().startswith("|"):
            continue
        cols = [c.strip() for c in line.split("|")[1:-1]]
        if len(cols) >= 2 and cols[0] not in ["code", "---"] and not cols[0].startswith("---"):
            stocks.append({"code": cols[0], "name": cols[1] if len(cols) > 1 else ""})
    return stocks

def screen_one(stock, pattern, days):
    code, name = stock["code"], stock["name"]
    out = run_westock(["kline", code, "--period", "day", "--limit", str(days + 5), "--fq", "qfq"])
    klines = parse_kline_table(out)
    if not klines:
        return None
    matched, s, e = check_pattern(klines, pattern, days)
    if matched:
        return {"code": code, "name": name, "start": s, "end": e}
    return None

# ---- 主程序 ----
DAYS = 30
PATTERN_STR = "阴阳阳"
INDEX = "sh000016"

print("获取 %s 成份股..." % INDEX)
stocks = get_index_stocks(INDEX)
print("共 %d 只股票，开始扫描形态 [%s]，最近 %d 日..." % (len(stocks), PATTERN_STR, DAYS))

pattern = parse_pattern(PATTERN_STR)
results = []
done = 0

with ThreadPoolExecutor(max_workers=10) as ex:
    futures = {ex.submit(screen_one, s, pattern, DAYS): s for s in stocks}
    for f in as_completed(futures):
        done += 1
        r = f.result()
        if r:
            results.append(r)
            print("  [%d/%d] 命中: %s(%s)  %s ~ %s" % (done, len(stocks), r["name"], r["code"], r["start"], r["end"]))
        elif done % 10 == 0:
            print("  [%d/%d] 扫描中..." % (done, len(stocks)))

print("\n==== 结果汇总 ====")
print("形态: %s | 时间范围: 最近%d日 | 命中: %d只" % (PATTERN_STR, DAYS, len(results)))
for i, r in enumerate(sorted(results, key=lambda x: x["end"], reverse=True), 1):
    print("  %2d. %-12s %-12s  %s ~ %s" % (i, r["code"], r["name"], r["start"], r["end"]))
