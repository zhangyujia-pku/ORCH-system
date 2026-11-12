"""norm_10：连续空格合并。"""


def normalize_10(value, *, allow_empty=False):
    """规范化输入。

    契约：value 为 None、不是字符串、或为空串（且未传 allow_empty=True）时，
    抛 ValueError，且消息里要包含字段名 value。合法输入行为保持不变。
    """
    return value.strip().replace('  ', ' ')
