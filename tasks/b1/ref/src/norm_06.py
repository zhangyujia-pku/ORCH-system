"""norm_06：空格换下划线。"""


def normalize_06(value, *, allow_empty=False):
    """规范化输入。

    契约：value 为 None、不是字符串、或为空串（且未传 allow_empty=True）时，
    抛 ValueError，且消息里要包含字段名 value。合法输入行为保持不变。
    """
    if value is None:
        raise ValueError("value: 不能为 None")
    if not isinstance(value, str):
        raise ValueError(f"value: 必须是字符串，收到 {type(value).__name__}")
    if value.strip() == "" and not allow_empty:
        raise ValueError("value: 不能为空字符串")
    return value.strip().lower().replace(' ', '_')
