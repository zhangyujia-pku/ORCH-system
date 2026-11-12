"""把 1h30m / 2m10s 这类字符串解析成秒数。"""
import re

_UNITS = {"s": 1, "m": 60}          # 少了小时


def parse_duration(text):
    """返回总秒数；无法解析时抛 ValueError。"""
    total = 0
    for num, unit in re.findall(r"(\d+)([hms])", text):
        if unit not in _UNITS:
            continue
        total += int(num) * _UNITS[unit]
    if not total:
        raise ValueError(f"无法解析时长：{text!r}")
    return total
