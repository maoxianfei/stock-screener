"""
K线 → 卦象 核心映射引擎
==========================
将任意时间周期的K线序列通过6根滑动窗口映射为易经六十四卦。
支持指定目标卦象的命中检测。

核心规则:
  - close >= open → 阳爻(1), 否则 → 阴爻(0)
  - 最旧K线 = 上爻(第6爻), 最新K线 = 初爻(第1爻)
  - 6根K线 = 1个卦象, N根K线 → N-5个卦象
"""

from dataclasses import dataclass, field
from typing import Optional, NamedTuple


# ============================================================
# 八卦编码表 (bit0=初爻, bit1=中爻, bit2=上爻)
# ============================================================
TRIGRAM_NAMES = {
    0b111: "乾☰",
    0b110: "兑☱",
    0b101: "离☲",
    0b100: "震☳",
    0b011: "巽☴",
    0b010: "坎☵",
    0b001: "艮☶",
    0b000: "坤☷",
}

TRIGRAM_ELEMENTS = {
    0b111: "天", 0b110: "泽", 0b101: "火", 0b100: "雷",
    0b011: "风", 0b010: "水", 0b001: "山", 0b000: "地",
}

# ============================================================
# 64卦完整数据库 (index 0~63)
# 格式: (卦名, 上卦名称, 下卦名称, 上卦bits, 下卦bits)
# ============================================================
_HEXAGRAM_NAMES = [
    # 下卦坤(0) — 0~7
    ( 0, "坤为地",    "坤", "坤"),   # 000000
    ( 1, "地雷复",    "坤", "震"),   # 000001
    ( 2, "地水师",    "坤", "坎"),   # 000010
    ( 3, "地泽临",    "坤", "兑"),   # 000011
    ( 4, "地山谦",    "坤", "艮"),   # 000100
    ( 5, "地火明夷",  "坤", "离"),   # 000101
    ( 6, "地风升",    "坤", "巽"),   # 000110
    ( 7, "地天泰",    "坤", "乾"),   # 000111
    # 下卦震(4) — 8~15
    ( 8, "雷地豫",    "震", "坤"),   # 001000
    ( 9, "震为雷",    "震", "震"),   # 001001
    (10, "雷水解",    "震", "坎"),   # 001010
    (11, "雷泽归妹",  "震", "兑"),   # 001011
    (12, "雷山小过",  "震", "艮"),   # 001100
    (13, "雷火丰",    "震", "离"),   # 001101
    (14, "雷风恒",    "震", "巽"),   # 001110
    (15, "雷天大壮",  "震", "乾"),   # 001111
    # 下卦坎(2) — 16~23
    (16, "水地比",    "坎", "坤"),   # 010000
    (17, "水雷屯",    "坎", "震"),   # 010001  ← 目标卦象
    (18, "坎为水",    "坎", "坎"),   # 010010
    (19, "水泽节",    "坎", "兑"),   # 010011
    (20, "水山蹇",    "坎", "艮"),   # 010100
    (21, "水火既济",  "坎", "离"),   # 010101
    (22, "水风井",    "坎", "巽"),   # 010110
    (23, "水天需",    "坎", "乾"),   # 010111
    # 下卦兑(6) — 24~31
    (24, "泽地萃",    "兑", "坤"),   # 011000
    (25, "泽雷随",    "兑", "震"),   # 011001
    (26, "泽水困",    "兑", "坎"),   # 011010
    (27, "兑为泽",    "兑", "兑"),   # 011011
    (28, "泽山咸",    "兑", "艮"),   # 011100
    (29, "泽火革",    "兑", "离"),   # 011101
    (30, "泽风大过",  "兑", "巽"),   # 011110
    (31, "泽天夬",    "兑", "乾"),   # 011111
    # 下卦艮(1) — 32~39
    (32, "山地剥",    "艮", "坤"),   # 100000
    (33, "山雷颐",    "艮", "震"),   # 100001
    (34, "山水蒙",    "艮", "坎"),   # 100010
    (35, "山泽损",    "艮", "兑"),   # 100011
    (36, "艮为山",    "艮", "艮"),   # 100100
    (37, "山火贲",    "艮", "离"),   # 100101
    (38, "山风蛊",    "艮", "巽"),   # 100110
    (39, "山天大畜",  "艮", "乾"),   # 100111
    # 下卦离(5) — 40~47
    (40, "火地晋",    "离", "坤"),   # 101000  ← 目标卦象
    (41, "火雷噬嗑",  "离", "震"),   # 101001
    (42, "火水未济",  "离", "坎"),   # 101010
    (43, "火泽睽",    "离", "兑"),   # 101011
    (44, "火山旅",    "离", "艮"),   # 101100
    (45, "离为火",    "离", "离"),   # 101101
    (46, "火风鼎",    "离", "巽"),   # 101110
    (47, "火天大有",  "离", "乾"),   # 101111
    # 下卦巽(3) — 48~55
    (48, "风地观",    "巽", "坤"),   # 110000
    (49, "风雷益",    "巽", "震"),   # 110001
    (50, "风水涣",    "巽", "坎"),   # 110010
    (51, "风泽中孚",  "巽", "兑"),   # 110011
    (52, "风山渐",    "巽", "艮"),   # 110100
    (53, "风火家人",  "巽", "离"),   # 110101
    (54, "巽为风",    "巽", "巽"),   # 110110
    (55, "风天小畜",  "巽", "乾"),   # 110111
    # 下卦乾(7) — 56~63
    (56, "天地否",    "乾", "坤"),   # 111000
    (57, "天雷无妄",  "乾", "震"),   # 111001
    (58, "天水讼",    "乾", "坎"),   # 111010
    (59, "天泽履",    "乾", "兑"),   # 111011
    (60, "天山遁",    "乾", "艮"),   # 111100
    (61, "天火同人",  "乾", "离"),   # 111101
    (62, "天风姤",    "乾", "巽"),   # 111110
    (63, "乾为天",    "乾", "乾"),   # 111111
]


@dataclass
class KLine:
    """单根K线数据结构"""
    date: str          # YYYY-MM-DD
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    @property
    def is_yang(self) -> bool:
        """阳线(收>=开) → 阳爻"""
        return self.close >= self.open


@dataclass
class HexagramResult:
    """单个卦象结果"""
    window_index: int          # 窗口序号 (从1开始)
    start_date: str            # 窗口最旧K线日期
    end_date: str              # 窗口最新K线日期
    yao_bits: int              # 0~63, 爻位编码 (bit0=初爻)
    hexagram_name: str         # 卦名 (如 "火地晋")
    upper_trigram: str         # 上卦名
    lower_trigram: str         # 下卦名
    yao_sequence: list[bool]   # [上爻, 五爻, 四爻, 三爻, 二爻, 初爻], True=阳
    is_target: bool = False    # 是否命中目标卦象


def get_hexagram_info(yao_bits: int) -> tuple[str, str, str]:
    """
    根据 yao_bits (0~63) 返回 (卦名, 上卦名, 下卦名)
    """
    if 0 <= yao_bits < 64:
        idx, name, upper, lower = _HEXAGRAM_NAMES[yao_bits]
        return name, upper, lower
    return "未知", "未知", "未知"


def yao_bits_to_sequence(yao_bits: int) -> list[bool]:
    """
    将 yao_bits 转为 [上爻, 五爻, 四爻, 三爻, 二爻, 初爻] 的阴阳序列
    """
    seq = []
    for pos in range(6, 0, -1):  # 从位6(上爻)到位1(初爻)
        seq.append(bool(yao_bits & (1 << (pos - 1))))
    return seq


def format_yao_sequence(seq: list[bool]) -> str:
    """格式化爻序列为可读字符串, 如 '阳阴阳阴阴阳'"""
    return "".join("阳" if s else "阴" for s in seq)


def klines_to_hexagrams(
    klines: list[KLine],
    target_yao_bits: Optional[list[int]] = None,
) -> list[HexagramResult]:
    """
    N 根 K 线 → N-5 个卦象结果

    Args:
        klines: 按时间升序排列的K线列表 (至少6根)
        target_yao_bits: 目标卦象的 yao_bits 列表, 用于命中标记

    Returns:
        卦象结果列表, 每项包含窗口信息、卦名、爻序列等
    """
    if len(klines) < 6:
        raise ValueError(f"至少需要 6 根 K 线，当前: {len(klines)}")

    if target_yao_bits is None:
        target_yao_bits = []

    target_set = set(target_yao_bits)
    results = []

    for i in range(len(klines) - 5):
        window = klines[i : i + 6]   # 升序: [0]=最旧, [5]=最新

        yao_bits = 0
        yao_seq = []                  # 从上爻到初爻
        for j, kline in enumerate(window):
            position = 6 - j          # j=0→6(上爻), j=5→1(初爻)
            if kline.is_yang:
                yao_bits |= (1 << (position - 1))
            yao_seq.append(kline.is_yang)

        name, upper, lower = get_hexagram_info(yao_bits)

        results.append(HexagramResult(
            window_index=i + 1,
            start_date=window[0].date,
            end_date=window[-1].date,
            yao_bits=yao_bits,
            hexagram_name=name,   # _HEXAGRAM_NAMES 已包含完整卦名
            upper_trigram=upper,
            lower_trigram=lower,
            yao_sequence=yao_seq,
            is_target=yao_bits in target_set,
        ))

    return results


@dataclass
class SequenceMatch:
    """连续卦象序列命中结果"""
    start_index: int          # 序列起始窗口序号 (从1开始)
    start_date: str           # 序列第一卦的起始日期
    end_date: str             # 序列最后一卦的结束日期
    pattern: list[int]        # 期望的 yao_bits 序列
    actual: list[int]         # 实际的 yao_bits 序列
    hexagram_names: list[str] # 对应的卦名序列
    yao_sequences: list[str]  # 对应的爻序列
    gap: int = 0              # 卦象之间的K线跨度 (0=紧挨, 1=隔1根, ...)


def detect_hexagram_sequences(
    klines: list[KLine],
    patterns: list[list[int]],
    max_gap: int = 0,
) -> list[SequenceMatch]:
    """
    在K线滑动窗口卦象中检测卦象序列，支持中间间隔。

    Args:
        klines: 按时间升序排列的K线列表
        patterns: 多个序列模式, 每个模式是一个 yao_bits 列表
        max_gap: 卦象之间允许的最大K线间隔数 (0=紧挨, 1=隔1根, 2=隔2根)

    Returns:
        所有命中的 SequenceMatch 列表

    例如: patterns=[[40, 33]], max_gap=2
      - 窗口[i]==40 且 窗口[i+1]==33 → gap=0 (紧挨)
      - 窗口[i]==40 且 窗口[i+2]==33 → gap=1 (隔1根K线)
      - 窗口[i]==40 且 窗口[i+3]==33 → gap=2 (隔2根K线)
    """
    min_klines = 6 + max_gap + 1  # 第一个卦象6根 + 最大gap + 滑动1根到第二卦象
    if len(klines) < min_klines:
        raise ValueError(
            f"序列检测至少需要 {min_klines} 根K线 "
            f"(max_gap={max_gap}), 当前: {len(klines)}"
        )

    results = klines_to_hexagrams(klines)
    num_windows = len(results)  # N-5 个窗口

    matches = []

    for pattern in patterns:
        pattern_len = len(pattern)
        if pattern_len < 2:
            continue

        for gap in range(max_gap + 1):
            step = gap + 1  # 窗口索引步长 (gap=0→step=1 紧挨)
            end_index = (pattern_len - 1) * step

            for i in range(num_windows - end_index):
                match = True
                for offset in range(pattern_len):
                    idx = i + offset * step
                    if results[idx].yao_bits != pattern[offset]:
                        match = False
                        break

                if match:
                    matches.append(SequenceMatch(
                        start_index=results[i].window_index,
                        start_date=results[i].start_date,
                        end_date=results[i + end_index].end_date,
                        pattern=pattern,
                        actual=[results[i + off * step].yao_bits for off in range(pattern_len)],
                        hexagram_names=[results[i + off * step].hexagram_name for off in range(pattern_len)],
                        yao_sequences=[format_yao_sequence(results[i + off * step].yao_sequence)
                                       for off in range(pattern_len)],
                        gap=gap,
                    ))

    return matches


def find_matches(
    klines: list[KLine],
    target_yao_bits: list[int],
    period_name: str = "日线",
) -> dict:
    """
    在K线序列中查找匹配目标卦象的窗口

    Args:
        klines: K线列表
        target_yao_bits: 目标卦象 yao_bits 列表
        period_name: 周期名称 (如 "日线", "周线", "月线")

    Returns:
        {
            "total_klines": int,
            "total_hexagrams": int,
            "targets": [{"火地晋": 40, "水雷屯": 17}, ...],
            "matches": [...],    # 所有命中的 HexagramResult
            "latest": HexagramResult,  # 最新卦象
            "latest_matches": bool,     # 最新卦象是否命中
        }
    """
    results = klines_to_hexagrams(klines, target_yao_bits)

    matches = [r for r in results if r.is_target]
    latest = results[-1] if results else None

    target_info = []
    for bits in target_yao_bits:
        name, _, _ = get_hexagram_info(bits)
        target_info.append({"name": name, "yao_bits": bits})

    return {
        "period": period_name,
        "total_klines": len(klines),
        "total_hexagrams": len(results),
        "targets": target_info,
        "matches": matches,
        "latest": latest,
        "latest_matches": latest.is_target if latest else False,
        "all_results": results,
    }
