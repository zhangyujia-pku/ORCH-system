"""退款 的领域模型。"""
from dataclasses import dataclass, field, asdict

from app.common.clock import now_iso
from app.common.errors import ValidationError


@dataclass
class Refund:
    id: str = ""
    order_id: object = None
    amount: object = None
    reason: object = None
    state: str = "created"
    state: str = "created"
    created_at: str = field(default_factory=now_iso)
    deleted_at: str = ""

    def to_dict(self):
        return asdict(self)

    @staticmethod
    def from_dict(d):
        return Refund(**{k: v for k, v in d.items() if k in Refund.__annotations__})

    def validate(self):
        """校验必填与取值范围。非法时抛 ValidationError，消息里带字段名。"""
        if not self.order_id: raise ValidationError("order_id: 不能为空")
        if not isinstance(self.amount, (int, float)) or self.amount <= 0: raise ValidationError("amount: 必须为正")
        if not self.reason: raise ValidationError("reason: 不能为空")
        return True
