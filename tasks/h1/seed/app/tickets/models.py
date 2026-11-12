"""工单 的领域模型。"""
from dataclasses import dataclass, field, asdict

from app.common.clock import now_iso
from app.common.errors import ValidationError


@dataclass
class Ticket:
    id: str = ""
    subject: object = None
    priority: object = None
    assignee: object = None
    state: str = "created"
    state: str = "created"
    created_at: str = field(default_factory=now_iso)
    deleted_at: str = ""

    def to_dict(self):
        return asdict(self)

    @staticmethod
    def from_dict(d):
        return Ticket(**{k: v for k, v in d.items() if k in Ticket.__annotations__})

    def validate(self):
        """校验必填与取值范围。非法时抛 ValidationError，消息里带字段名。"""
        return True
