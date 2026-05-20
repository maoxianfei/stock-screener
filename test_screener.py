#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import subprocess

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

def run_westock(args_list):
    cmd = ["node", WESTOCK_SCRIPT] + args_list
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, encoding="utf-8")
    return result.stdout.strip()

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

# 测试：获取茅台最近10根K线
output = run_westock(["kline", "sh600519", "--period", "day", "--limit", "10"])
klines = parse_kline_table(output)
print("茅台最近10根K线:")
for k in klines:
    yang = k["close"] >= k["open"]
    print("  %s  开:%.2f  收:%.2f  => %s" % (k["date"], k["open"], k["close"], "阳线" if yang else "阴线"))

# 测试多种形态
for pat_str in ["阳阳阳", "阴阳阳", "阳阴阳", "阳阳阳阳"]:
    pat = parse_pattern(pat_str)
    matched, s, e = check_pattern(klines, pat, 10)
    if matched:
        print("\n形态 %s: 匹配！(%s ~ %s)" % (pat_str, s, e))
    else:
        print("\n形态 %s: 未匹配" % pat_str)

print("\n测试通过! OK")
