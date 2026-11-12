"""退款 的内存存储。"""
from app.common.errors import NotFound


class RefundStore:
    def __init__(self):
        self._rows = {}

    def create(self, obj):
        self._rows[obj.id] = obj
        return obj

    def get(self, obj_id):
        obj = self._rows.get(obj_id)
        if obj is None:
            raise NotFound(f"id: 未找到 {obj_id}")
        return obj

    def list(self, page=1, size=20):
        rows = [o for o in self._rows.values()]
        return rows, {"page": page, "size": size,
                      "total": len(rows), "pages": 1 if rows else 0}

    def update(self, obj_id, **changes):
        obj = self.get(obj_id)
        for k, v in changes.items():
            setattr(obj, k, v)
        return obj

    def delete(self, obj_id):
        self.get(obj_id)
        del self._rows[obj_id]
