# K 线形态选股系统

基于日 K 线形态特征，在指定时间窗口内筛选符合条件的个股。

---

## 文件结构

```
stock_screener.py      # 命令行选股主程序
screener_server.py     # HTTP 服务器（驱动前端界面）
stock_screener.html    # 可视化前端界面
quick_test.py          # 快速测试脚本
test_screener.py       # 单元测试脚本
```

---

## 快速开始

### 方式一：命令行运行

```bash
# 格式：python stock_screener.py <交易日数> <形态> [--exchange sh/sz]
python stock_screener.py 10 阴阳阴阴阴阳 --exchange sh   # 上交所
python stock_screener.py 10 阳阴阳阴阴阴阳 --exchange sz # 深交所

# 也可直接指定指数代码（--index 与 --exchange 二选一）
python stock_screener.py 30 阳阳阳 --index sh000300     # 沪深300
```

### 方式二：Web 界面

```bash
cd <项目目录>
python screener_server.py
```

然后浏览器访问：**http://localhost:8888**

---

## 策略一：K 线形态选股

### 参数说明

| 参数 | 说明 | 示例 |
|------|------|------|
| 交易日数 | 在最近多少个交易日内查找形态 | `10` |
| 形态字符串 | 用"阳"/"阴"描述连续 K 线 | `阴阳阴阴阴阳`、`阳阴阳阴阴阴阳` |
| `--exchange` | 按交易所筛选（sh=上交所, sz=深交所） | `--exchange sh` |
| `--index` | 直接指定指数代码（与 --exchange 二选一） | `--index sh000300` |

### 内置形态

| 形态 | 长度 | 说明 |
|------|------|------|
| `阴阳阴阴阴阳` | 6 根 | 水雷屯 — 大底部信号 |
| `阳阴阳阴阴阴阳` | 7 根 | 火地晋 — 交易拐点信号 |
| `阴阳阴阴阴阴阳` | 7 根 | 自用形态 |

> **判断规则**：收盘价 ≥ 开盘价 → 阳线；收盘价 < 开盘价 → 阴线

### 股票池

系统按交易所维度组织股票池，内部使用大指数成份股 + 代码前缀过滤：

| 交易所 | 选项值 | 数据源指数 | 过滤策略 |
|--------|--------|-----------|----------|
| 上交所 | `sh` | 沪深300 (sh000300) | 代码前缀 `sh6` |
| 深交所 | `sz` | 中证500 (sz399905) | 代码前缀 `sz` |

---

## 技术架构

```
stock_screener.html  (前端)
       ↓  fetch /api/screen
screener_server.py   (HTTP 服务器，端口 8888)
       ↓  subprocess + ThreadPoolExecutor
westock-data CLI     (数据查询)
       ↓  HTTP
腾讯自选股数据接口
```

### 核心模块

#### screener_server.py

| 接口 | 说明 |
|------|------|
| `GET /` | 返回前端 HTML 页面 |
| `GET /api/status` | 返回当前扫描状态 |
| `GET /api/screen?days=N&pattern=XX&exchange=YY` | 启动选股任务 |

**状态对象结构：**
```json
{
  "running": false,
  "total": 300,
  "scanned": 150,
  "matched": 12,
  "results": [{"code":"sh600519","name":"贵州茅台","match_start_date":"...","match_end_date":"..."}],
  "logs": ["命中: 贵州茅台(sh600519) ..."],
  "done": true,
  "error": ""
}
```

#### stock_screener.py

| 函数 | 说明 |
|------|------|
| `run_westock()` | 调用 westock-data CLI 获取数据 |
| `parse_kline_table()` | 解析 K 线表格文本 |
| `parse_pattern()` | 将"阳阴"字符串转为布尔数组 |
| `check_pattern()` | 在 K 线序列中查找形态匹配 |
| `screen_one()` | 单只股票扫描（供多线程调用） |
| `get_index_stocks()` | 获取指数成份股列表 |

---

## K 线形态匹配算法

```python
def check_pattern(klines, pattern, days):
    """
    1. 取最近 days 根 K 线
    2. 从最近一天向前滑动窗口
    3. 匹配 pattern 中的"阳/阴"顺序
    """
    recent = list(reversed(klines[:days]))
    n, pl = len(recent), len(pattern)
    for start in range(n - pl, -1, -1):
        seg = recent[start:start + pl]
        if all((seg[i]["close"] >= seg[i]["open"]) == pattern[i] for i in range(pl)):
            return True, seg[0]["date"], seg[-1]["date"]
    return False, None, None
```

---

## 前端界面功能

- **形态可视化预览**：输入阳/阴字符即时渲染蜡烛图
- **快捷形态标签**：一键切换常用形态（含易卦含义提示）
- **交易所维度筛选**：上交所/深交所下拉切换
- **实时进度条**：扫描进度、命中数量、耗时
- **结果搜索/过滤**：按代码或名称模糊搜索
- **匹配后走势预览**：形态结束后5日K线迷你图
- **一键导出 CSV**：结果直接下载
- **配色说明**：阳线（涨）→ 红色，阴线（跌）→ 绿色（A 股标准）

---

## 数据依赖

- **westock-data**：需安装于 `~/.workbuddy/plugins/.../westock-data/scripts/index.js`
- **Node.js**：westock-data 通过 Node 调用
- **Python 3.8+**：运行后端服务器

---

## 故障排查

| 问题 | 原因 | 解决 |
|------|------|------|
| `service error` | 数据接口暂时不可用 | 等待数据源恢复或换用其他指数 |
| 前端无法连接后端 | 服务器未启动 | 运行 `python screener_server.py` |
| 所有股票均命中 | 时间窗口太长 | 缩短交易日数 |
| 无股票命中 | 时间窗口太短 | 扩大交易日数或换形态 |
