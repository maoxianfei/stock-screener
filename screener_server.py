#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
K线形态选股系统 - HTTP服务器版本
运行：python screener_server.py
访问：http://localhost:8888
"""

import http.server
import json
import os
import subprocess
import threading
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

# westock-data 脚本路径（自动检测或通过环境变量 WESTOCK_SCRIPT 指定）
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
PORT = 8888

# 交易所→过滤策略
# 因为上证综指/sh000001和深证综指/sz399106不支持成份股查询，
# 改用覆盖面大的指数获取股票列表后，再按代码前缀过滤
EXCHANGE_CONFIG = {
    "sh": {"index": "sh000300", "prefix": "sh6", "label": "上交所"},    # 沪深300 → 过滤sh6
    "sz": {"index": "sz399905", "prefix": "sz",  "label": "深交所"},    # 中证500 → 过滤sz
}

# 交易所中文名
EXCHANGE_NAMES = {
    "sh": "上交所",
    "sz": "深交所",
}

# 全局任务状态
task_state = {
    "running": False,
    "total": 0,
    "scanned": 0,
    "matched": 0,
    "results": [],
    "logs": [],
    "done": False,
    "error": ""
}


def run_westock(args_list, timeout=25):
    cmd = ["node", WESTOCK_SCRIPT] + args_list
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, encoding="utf-8")
        return r.stdout.strip()
    except Exception:
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
            except Exception:
                pass
    return rows


def parse_pattern(s):
    return [ch == "阳" for ch in s if ch in ("阳", "阴")]


def check_pattern(klines, pattern, days):
    recent = list(reversed(klines[:days]))
    n, pl = len(recent), len(pattern)
    for start in range(n - pl, -1, -1):
        seg = recent[start:start + pl]
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
            stocks.append({"code": cols[0], "name": cols[1] if len(cols) > 1 else cols[0]})
    return stocks


def get_stocks_for_exchange(exchange_key):
    """按交易所获取股票列表（大指数 + 代码前缀过滤）"""
    cfg = EXCHANGE_CONFIG[exchange_key]
    index_code = cfg["index"]
    prefix = cfg["prefix"]
    all_stocks = get_index_stocks(index_code)
    # 按代码前缀过滤
    filtered = [s for s in all_stocks if s["code"].startswith(prefix)]
    return filtered


def resolve_index_from_params(exchange=None, index=None):
    """根据 exchange 或 index 参数，解析出实际指数代码和显示名"""
    if exchange and exchange in EXCHANGE_CONFIG:
        return exchange, EXCHANGE_NAMES[exchange]
    if index:
        return index, index
    return "sh", "上交所"  # 默认


def screen_one(stock, pattern, days):
    code, name = stock["code"], stock["name"]
    # 多取一些数据以确保能获取到结束日后5个交易日
    out = run_westock(["kline", code, "--period", "day", "--limit", str(days + 15), "--fq", "qfq"])
    klines = parse_kline_table(out)
    if not klines:
        return None
    matched, s, e = check_pattern(klines, pattern, days)
    if matched:
        # 找到匹配结束日在klines中的索引位置
        # 注意：westock-data返回的klines是从新到旧排列的（[0]=最新日期）
        end_idx = None
        for i, k in enumerate(klines):
            if k["date"] == e:
                end_idx = i
                break
        # 提取结束后5个交易日的K线数据
        # klines中end_idx之前（索引更小）的条目才是结束日之后的日期
        post_klines = []
        if end_idx is not None:
            post_klines = list(reversed(klines[max(0, end_idx - 5):end_idx]))
        return {
            "code": code,
            "name": name,
            "match_start_date": s,
            "match_end_date": e,
            "post_klines": post_klines
        }
    return None


def run_screening(days, pattern_str, index_code_or_exchange, workers=10, exchange=None):
    global task_state
    task_state = {
        "running": True,
        "total": 0,
        "scanned": 0,
        "matched": 0,
        "results": [],
        "logs": [],
        "done": False,
        "error": ""
    }

    def log(msg):
        task_state["logs"].append(msg)

    try:
        # 按交易所过滤 or 直接按指数
        if exchange and exchange in EXCHANGE_CONFIG:
            cfg = EXCHANGE_CONFIG[exchange]
            log("正在获取%s股票列表...（指数: %s，按 %s 前缀过滤）" % (cfg["label"], cfg["index"], cfg["prefix"]))
            raw_stocks = get_index_stocks(cfg["index"])
            stocks = [s for s in raw_stocks if s["code"].startswith(cfg["prefix"])]
            log("从 %d 只成份股中筛选出 %d 只%s股票" % (len(raw_stocks), len(stocks), cfg["label"]))
        else:
            log("正在获取 %s 成份股..." % index_code_or_exchange)
            stocks = get_index_stocks(index_code_or_exchange)

        if not stocks:
            task_state["error"] = "获取成份股失败"
            task_state["done"] = True
            return

        task_state["total"] = len(stocks)
        pattern = parse_pattern(pattern_str)
        log("共 %d 只股票，开始扫描形态 [%s]，最近 %d 日" % (len(stocks), pattern_str, days))

        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(screen_one, s, pattern, days): s for s in stocks}
            for f in as_completed(futures):
                task_state["scanned"] += 1
                r = f.result()
                if r:
                    task_state["matched"] += 1
                    task_state["results"].append(r)
                    log("命中: %s(%s)  %s ~ %s" % (r["name"], r["code"], r["match_start_date"], r["match_end_date"]))

        task_state["results"].sort(key=lambda x: x["match_end_date"], reverse=True)
        log("扫描完成！共命中 %d 只股票" % len(task_state["results"]))

    except Exception as e:
        task_state["error"] = str(e)

    task_state["running"] = False
    task_state["done"] = True


class ScreenerHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # 静默日志

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if path == "/" or path == "/index.html":
            self.serve_file("stock_screener.html", "text/html; charset=utf-8")

        elif path == "/api/status":
            self.json_response(task_state)

        elif path == "/api/screen":
            try:
                days = int(query.get("days", [60])[0])
                pattern = query.get("pattern", ["阳阳阳"])[0]
                exchange = query.get("exchange", [None])[0]
                index = query.get("index", [None])[0]
                workers = int(query.get("workers", [10])[0])
            except Exception:
                self.json_response({"error": "参数格式错误"})
                return

            if task_state["running"]:
                self.json_response({"error": "正在运行中，请等待完成"})
                return

            index_code, index_label = resolve_index_from_params(exchange=exchange, index=index)

            t = threading.Thread(target=run_screening, args=(days, pattern, index_label, workers), kwargs={"exchange": exchange}, daemon=True)
            t.start()
            self.json_response({"status": "started", "days": days, "pattern": pattern, "index": index_label, "exchange": exchange})

        else:
            self.send_response(404)
            self.end_headers()

    def serve_file(self, filename, content_type):
        filepath = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
        try:
            with open(filepath, "rb") as f:
                content = f.read()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", len(content))
            self.end_headers()
            self.wfile.write(content)
        except FileNotFoundError:
            self.send_response(404)
            self.end_headers()

    def json_response(self, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    server = http.server.HTTPServer(("0.0.0.0", PORT), ScreenerHandler)
    print("=" * 50)
    print("K线形态选股系统已启动")
    print("访问地址: http://localhost:%d" % PORT)
    print("按 Ctrl+C 停止服务")
    print("=" * 50)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")
