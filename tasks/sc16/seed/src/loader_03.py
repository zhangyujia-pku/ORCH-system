"""loader_03：从远端拉取 traces。"""


def fetch_all(source, ids):
    """把 ids 逐个从 source 取回来，按顺序返回。"""
    out = []
    for i in ids:
        out.append(source.get(i))
    return out
