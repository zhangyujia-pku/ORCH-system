"""分页：约定 page 从 1 开始，size 上限 100，越界时夹紧而不是报错。"""

MAX_SIZE = 100


def clamp(page, size):
    """把 page/size 夹到合法区间。非法类型按默认值处理。"""
    try:
        page = int(page)
    except (TypeError, ValueError):
        page = 1
    try:
        size = int(size)
    except (TypeError, ValueError):
        size = 20
    page = max(1, page)
    size = max(1, min(MAX_SIZE, size))
    return page, size


def paginate(items, page=1, size=20):
    """返回 (当页元素, 元信息)。元信息字段固定为 page/size/total/pages。"""
    page, size = clamp(page, size)
    total = len(items)
    pages = (total + size - 1) // size if total else 0
    start = (page - 1) * size
    return items[start:start + size], {"page": page, "size": size,
                                       "total": total, "pages": pages}
