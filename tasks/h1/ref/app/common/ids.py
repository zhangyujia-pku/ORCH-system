"""ID：前缀 + 单调计数，便于测试断言形状。"""
import itertools

_seq = itertools.count(1)


def new_id(prefix):
    """形如 ord-000001。"""
    return f"{prefix}-{next(_seq):06d}"
