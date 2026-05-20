#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
股票K线形态选股系统
策略一：在最近N个交易日内，筛选出收盘时符合指定K线形态的个股
"""

import subprocess
import sys
import json
import os
import re
import time
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

# westock-data 脚本路径（自动检测或通过环境变量 WESTOCK_SCRIPT 指定）
def _find_westock_script():
    """查找 westock-data 脚本路径"""
    env_path = os.environ.get("WESTOCK_SCRIPT")
    if env_path and os.path.isfile(env_path):
        return env_path
    # 常见安装路径
    candidates = [
        os.path.join(os.path.expanduser("~"), ".workbuddy", "plugins", "marketplaces",
                     "cb_teams_marketplace", "plugins", "finance-data", "skills",
                     "westock-data", "scripts", "index.js"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return "westock-data"  # 回退：假设在 PATH 中

WESTOCK_SCRIPT = _find_westock_script()


def run_westock(args_list, timeout=30):
    """执行 westock-data 命令并返回输出"""
    cmd = ["node", WESTOCK_SCRIPT] + args_list
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, encoding="utf-8"
        )
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        return ""
    except Exception as e:
        return ""


def parse_kline_table(text):
    """解析 westock-data kline 返回的 Markdown 表格"""
    lines = [l.strip() for l in text.strip().split("\n") if l.strip()]
    # 找到表头行
    header_idx = -1
    for i, line in enumerate(lines):
        if "| date |" in line.lower() or "|date|" in line.lower():
            header_idx = i
            break
    if header_idx == -1:
        return []
    
    rows = []
    for line in lines[header_idx + 2:]:  # 跳过表头和分隔行
        if not line.startswith("|"):
            break
        cols = [c.strip() for c in line.split("|")[1:-1]]
        if len(cols) >= 4:
            try:
                date = cols[0]
                open_price = float(cols[1])
                close_price = float(cols[2])
                rows.append({
                    "date": date,
                    "open": open_price,
                    "close": close_price
                })
            except (ValueError, IndexError):
                continue
    return rows


def parse_pattern(pattern_str):
    """
    解析形态字符串
    '阳' -> True (阳线)
    '阴' -> False (阴线)
    返回布尔列表
    """
    result = []
    for ch in pattern_str:
        if ch == "阳":
            result.append(True)
        elif ch == "阴":
            result.append(False)
        # 其他字符忽略
    if not result:
        raise ValueError(f"无效的形态字符串: {pattern_str}，只能包含'阳'或'阴'")
    return result


def is_yang(candle):
    """判断是否为阳线（收盘 >= 开盘）"""
    return candle["close"] >= candle["open"]


def check_pattern_in_klines(klines, pattern, days):
    """
    在最近 days 根K线中，检查是否存在连续符合 pattern 形态的K线段
    klines: 按日期降序排列（最新在前）
    pattern: 布尔列表，True=阳线，False=阴线
    days: 限定的交易日范围
    
    返回：(matched: bool, match_info: dict)
    """
    if not klines:
        return False, {}
    
    # 取最近 days 根
    recent = klines[:days]
    # 转为升序（时间从早到晚）
    recent = list(reversed(recent))
    
    n = len(recent)
    pat_len = len(pattern)
    
    if n < pat_len:
        return False, {}
    
    # 在最近的 days 天内，找最后出现的匹配（最新的连续形态）
    # 从最新的位置往前找
    best_match = None
    for start in range(n - pat_len, -1, -1):
        segment = recent[start: start + pat_len]
        matched = True
        for i, expected_yang in enumerate(pattern):
            if is_yang(segment[i]) != expected_yang:
                matched = False
                break
        if matched:
            best_match = {
                "start_date": segment[0]["date"],
                "end_date": segment[-1]["date"],
                "candles": segment
            }
            break  # 找最新的一段即可
    
    if best_match:
        return True, best_match
    return False, {}


def get_stock_list_by_index(index_code="sh000300", limit=500):
    """从指数成份股获取股票列表"""
    print(f"  正在获取指数 {index_code} 成份股...")
    output = run_westock(["index", index_code])
    lines = [l.strip() for l in output.split("\n") if l.strip()]
    
    stocks = []
    for line in lines:
        if not line.startswith("|"):
            continue
        cols = [c.strip() for c in line.split("|")[1:-1]]
        if len(cols) >= 2:
            code = cols[0].strip()
            name = cols[1].strip() if len(cols) > 1 else ""
            # 过滤表头和分隔行
            if code and code not in ["code", "---", "股票代码"] and not code.startswith("---"):
                stocks.append({"code": code, "name": name})
    
    return stocks[:limit]


def get_kline_for_stock(stock, days, limit_days):
    """获取单只股票的K线数据"""
    code = stock["code"]
    name = stock.get("name", code)
    
    # 多取一些数据以确保有足够的交易日
    fetch_limit = limit_days + 10
    output = run_westock(["kline", code, "--period", "day", "--limit", str(fetch_limit), "--fq", "qfq"])
    
    if not output:
        return code, name, []
    
    klines = parse_kline_table(output)
    return code, name, klines


def screen_stocks(days, pattern_str, stock_list, max_workers=10, progress_callback=None):
    """
    主选股函数
    days: 最近N个交易日
    pattern_str: 形态字符串，如 '阳阳阳'
    stock_list: 股票列表 [{"code": ..., "name": ...}]
    """
    pattern = parse_pattern(pattern_str)
    pat_len = len(pattern)
    
    results = []
    total = len(stock_list)
    completed = 0
    
    print(f"\n开始扫描 {total} 只股票，形态: {pattern_str}（{pat_len}根K线），范围: 最近{days}个交易日\n")
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_stock = {
            executor.submit(get_kline_for_stock, stock, days, days): stock
            for stock in stock_list
        }
        
        for future in as_completed(future_to_stock):
            code, name, klines = future.result()
            completed += 1
            
            if klines:
                matched, match_info = check_pattern_in_klines(klines, pattern, days)
                if matched:
                    results.append({
                        "code": code,
                        "name": name,
                        "match_end_date": match_info.get("end_date", ""),
                        "match_start_date": match_info.get("start_date", ""),
                        "candles": match_info.get("candles", [])
                    })
                    print(f"  ✅ [{completed}/{total}] {name}({code}) - 匹配！结束日期: {match_info.get('end_date', '')}")
                else:
                    if completed % 20 == 0:
                        print(f"  ⏳ [{completed}/{total}] 扫描中...")
            else:
                if completed % 20 == 0:
                    print(f"  ⏳ [{completed}/{total}] 扫描中...")
            
            if progress_callback:
                progress_callback(completed, total, results)
    
    # 按匹配结束日期降序排序（最新的排前面）
    results.sort(key=lambda x: x["match_end_date"], reverse=True)
    return results


def format_results(results, days, pattern_str):
    """格式化输出结果"""
    pat_desc = "".join(
        "阳线" if p else "阴线"
        for p in parse_pattern(pattern_str)
    )
    
    print(f"\n{'='*60}")
    print(f"📊 选股结果汇总")
    print(f"{'='*60}")
    print(f"  策略：最近 {days} 个交易日内出现 [{pattern_str}] 形态")
    print(f"  形态说明：{pat_desc}")
    print(f"  筛选结果：共 {len(results)} 只股票符合条件")
    print(f"{'='*60}\n")
    
    if not results:
        print("  暂无符合条件的股票")
        return
    
    print(f"{'代码':<12} {'名称':<15} {'形态结束日期':<15} {'形态开始日期':<15}")
    print("-" * 60)
    for r in results:
        print(f"{r['code']:<12} {r['name']:<15} {r['match_end_date']:<15} {r['match_start_date']:<15}")
    
    return results


def save_results_json(results, days, pattern_str, output_file):
    """将结果保存为 JSON 供前端使用"""
    data = {
        "timestamp": datetime.now().isoformat(),
        "params": {
            "days": days,
            "pattern": pattern_str,
            "pattern_len": len(parse_pattern(pattern_str))
        },
        "count": len(results),
        "results": results
    }
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"\n✅ 结果已保存到: {output_file}")


def main():
    parser = argparse.ArgumentParser(description="K线形态选股系统")
    parser.add_argument("days", type=int, help="最近N个交易日（如60）")
    parser.add_argument("pattern", type=str, help="K线形态（如：阳阳阳 / 阳阴阳阳）")
    parser.add_argument("--index", type=str, default="sh000300", help="使用的指数（默认沪深300: sh000300）")
    parser.add_argument("--workers", type=int, default=8, help="并发线程数（默认8）")
    parser.add_argument("--output", type=str, default="screen_result.json", help="结果输出文件")
    
    args = parser.parse_args()
    
    # 参数验证
    try:
        pattern = parse_pattern(args.pattern)
    except ValueError as e:
        print(f"❌ 参数错误: {e}")
        sys.exit(1)
    
    if args.days < len(pattern):
        print(f"❌ 参数错误: 交易日数({args.days})不能少于形态长度({len(pattern)})")
        sys.exit(1)
    
    print(f"\n🚀 K线形态选股系统启动")
    print(f"   📅 时间范围: 最近 {args.days} 个交易日")
    print(f"   🕯️  K线形态: {args.pattern}（{len(pattern)}根）")
    print(f"   📈 股票池: {args.index}")
    
    # 获取股票列表
    stock_list = get_stock_list_by_index(args.index)
    if not stock_list:
        print("❌ 获取股票列表失败")
        sys.exit(1)
    
    print(f"   📋 股票池大小: {len(stock_list)} 只\n")
    
    # 执行选股
    start_time = time.time()
    results = screen_stocks(
        days=args.days,
        pattern_str=args.pattern,
        stock_list=stock_list,
        max_workers=args.workers
    )
    elapsed = time.time() - start_time
    
    # 格式化输出
    format_results(results, args.days, args.pattern)
    print(f"\n⏱️  耗时: {elapsed:.1f}秒")
    
    # 保存结果
    output_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), args.output)
    save_results_json(results, args.days, args.pattern, output_path)
    
    return results


if __name__ == "__main__":
    main()
