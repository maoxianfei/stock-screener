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
# 格式：python stock_screener.py <交易日数> <形态> [--index 指数代码]
python stock_screener.py 60 阳阳阳                    # 默认沪深300
python stock_screener.py 30 阳阴阳阳 --index sh000016  # 上证50
python stock_screener.py 20 阴阴阳阳阳 --index sz399905 # 中证500
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
| 交易日数 | 在最近多少个交易日内查找形态 | `60` |
| 形态字符串 | 用"阳"/"阴"描述连续 K 线 | `阳阳阳`、`阳阴阳阳`、`阴阴阳阳阳` |

### 形态示例

| 形态 | 含义 |
|------|------|
| `阳阳阳` | 连续 3 天收盘价 ≥ 开盘价（3 连阳） |
| `阳阴阳阳` | 阳、阴、阳、阳 按顺序出现 |
| `阴阴阳` | 两阴夹一阳的形态 |
| `阳阴阴阳阳` | 5 根 K 线按顺序符合该形态 |

> **判断规则**：收盘价 ≥ 开盘价 → 阳线；收盘价 < 开盘价 → 阴线

### 股票池

| 指数 | 代码 | 状态 |
|------|------|------|
| 沪深300 | `sh000300` | ✅ 可用 |
| 上证50 | `sh000016` | ✅ 可用 |
| 中证500 | `sz399905` | ✅ 可用 |
| 创业板指 | `sz399006` | ❌ 数据源暂不可用 |
| 中证1000 | `sh000852` | ❌ 数据源暂不可用 |

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
| `GET /api/screen?days=N&pattern=XX&index=YY` | 启动选股任务 |

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
- **快捷形态标签**：一键切换常用形态
- **实时进度条**：扫描进度、命中数量、耗时
- **结果搜索/过滤**：按代码或名称模糊搜索
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
