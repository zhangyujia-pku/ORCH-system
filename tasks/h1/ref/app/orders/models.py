"""订单 的领域模型。"""
from dataclasses import dataclass, field, asdict

from app.common.clock import now_iso
from app.common.errors import ValidationError


@dataclass
class Order:
    id: str = ""
    sku: object = None
    qty: object = None
    amount: object = None
    state: str = "created"
    state: str = "created"
    created_at: str = field(default_factory=now_iso)
    deleted_at: str = ""

    def to_dict(self):
        return asdict(self)

    @staticmethod
    def from_dict(d):
        return Order(**{k: v for k, v in d.items() if k in Order.__annotations__})

    def validate(self):
        """校验必填与取值范围。非法时抛 ValidationError，消息里带字段名。"""
        if not self.sku: raise ValidationError("sku: 不能为空")
        if not isinstance(self.qty, int) or self.qty <= 0: raise ValidationError("qty: 必须是正整数")
        if not isinstance(self.amount, (int, float)) or self.amount < 0: raise ValidationError("amount: 不能为负")
        return True
