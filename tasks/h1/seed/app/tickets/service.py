"""工单 的服务层：校验 → 存储 → 事件。"""
from app.common.errors import ValidationError
from app.common.ids import new_id
from app.tickets.models import Ticket
from app.tickets.rules import next_state
from app.tickets.store import TicketStore


class TicketService:
    def __init__(self):
        self.store = TicketStore()
        self.events = []

    def create(self, payload):
        """创建：校验通过后落库，并记一条 created 事件。"""
        raise NotImplementedError("create")

    def get(self, obj_id):
        return self.store.get(obj_id)

    def list(self, page=1, size=20):
        """列表：分页由 store 负责，这里只透传。"""
        raise NotImplementedError("list")

    def update(self, obj_id, action):
        """状态迁移：按 rules.next_state 校验，记一条 moved 事件。"""
        raise NotImplementedError("update")

    def update(self, obj_id, action):
        """状态迁移：按 rules.next_state 校验，记一条 moved 事件。"""
        raise NotImplementedError("update")

    def delete(self, obj_id):
        """删除：软删，并记一条 deleted 事件。"""
        raise NotImplementedError("delete")
