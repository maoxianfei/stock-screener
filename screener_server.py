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


def screen_one(stock, pattern, days):
    code, name = stock["code"], stock["name"]
    out = run_westock(["kline", code, "--period", "day", "--limit", str(days + 5), "--fq", "qfq"])
    klines = parse_kline_table(out)
    if not klines:
        return None
    matched, s, e = check_pattern(klines, pattern, days)
    if matched:
        return {"code": code, "name": name, "match_start_date": s, "match_end_date": e}
    return None


def run_screening(days, pattern_str, index_code, workers=10):
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
        log("正在获取 %s 成份股..." % index_code)
        stocks = get_index_stocks(index_code)
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
                index = query.get("index", ["sh000300"])[0]
                workers = int(query.get("workers", [10])[0])
            except Exception:
                self.json_response({"error": "参数格式错误"})
                return

            if task_state["running"]:
                self.json_response({"error": "正在运行中，请等待完成"})
                return

            t = threading.Thread(target=run_screening, args=(days, pattern, index, workers), daemon=True)
            t.start()
            self.json_response({"status": "started", "days": days, "pattern": pattern, "index": index})

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
