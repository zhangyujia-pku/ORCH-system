"""发票 的内存存储。"""
from app.common.clock import now_iso
from app.common.errors import NotFound
from app.common.pagination import paginate


class InvoiceStore:
    def __init__(self):
        self._rows = {}

    def create(self, obj):
        self._rows[obj.id] = obj
        return obj

    def get(self, obj_id):
        obj = self._rows.get(obj_id)
        if obj is None or getattr(obj, "deleted_at", ""):
            raise NotFound(f"id: 未找到 {obj_id}")
        return obj

    def list(self, page=1, size=20):
        rows = [o for o in self._rows.values() if not getattr(o, "deleted_at", "")]
        rows.sort(key=lambda o: (o.created_at, o.id))
        return paginate(rows, page, size)

    def update(self, obj_id, **changes):
        obj = self.get(obj_id)
        for k, v in changes.items():
            setattr(obj, k, v)
        return obj

    def delete(self, obj_id):
        obj = self.get(obj_id)
        obj.deleted_at = now_iso()
        return obj
