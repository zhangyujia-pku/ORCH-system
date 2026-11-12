"""订单 的服务层：校验 → 存储 → 事件。"""
from app.common.clock import now_iso
from app.common.errors import ValidationError
from app.common.ids import new_id
from app.orders.models import Order
from app.orders.rules import next_state
from app.orders.store import OrderStore


class OrderService:
    def __init__(self):
        self.store = OrderStore()
        self.events = []

    def _emit(self, kind, obj_id):
        self.events.append({"type": kind, "id": obj_id, "at": now_iso()})

    def create(self, payload):
        """创建：校验通过后落库，并记一条 created 事件。"""
        obj = Order.from_dict(payload)
        obj.validate()
        if not obj.id:
            obj.id = new_id("order")
        self.store.create(obj)
        self._emit("created", obj.id)
        return obj

    def get(self, obj_id):
        return self.store.get(obj_id)

    def list(self, page=1, size=20):
        """列表：分页由 store 负责，这里只透传。"""
        return self.store.list(page, size)

    def update(self, obj_id, action):
        """状态迁移：按 rules.next_state 校验，记一条 moved 事件。"""
        obj = self.store.get(obj_id)
        current = getattr(obj, "state", "created") or "created"
        new_state = next_state(current, action)
        obj.state = new_state
        self._emit("moved", obj.id)
        return obj

    def update(self, obj_id, action):
        """状态迁移：按 rules.next_state 校验，记一条 moved 事件。"""
        obj = self.store.get(obj_id)
        current = getattr(obj, "state", "created") or "created"
        new_state = next_state(current, action)
        obj.state = new_state
        self._emit("moved", obj.id)
        return obj

    def delete(self, obj_id):
        """删除：软删，并记一条 deleted 事件。"""
        obj = self.store.delete(obj_id)
        self._emit("deleted", obj.id)
        return obj
