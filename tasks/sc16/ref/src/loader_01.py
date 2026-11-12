"""loader_01：从远端拉取 metrics。"""
import time

ATTEMPTS = 3


def fetch_all(source, ids):
    """把 ids 逐个从 source 取回来，按顺序返回。

    契约：单个 id 遇到 TimeoutError 时最多尝试 3 次；3 次都失败就把最后一个
    TimeoutError 抛出。非超时异常（如 ValueError）不重试，直接抛。
    """
    out = []
    for i in ids:
        last = None
        for attempt in range(ATTEMPTS):
            try:
                out.append(source.get(i))
                break
            except TimeoutError as e:
                last = e
                if attempt < ATTEMPTS - 1:
                    time.sleep(0)
        else:
            raise last
    return out
