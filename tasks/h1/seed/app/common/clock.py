"""时间：所有落库时间必须走这里，格式固定为 ISO8601（秒级，UTC）。"""
import datetime as _dt


def now_iso():
    """当前时间，形如 2026-11-11T00:00:00Z。"""
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text):
    """解析 now_iso() 的输出。"""
    return _dt.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ")
