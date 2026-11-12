"""发票 的领域模型。"""
from dataclasses import dataclass, field, asdict

from app.common.clock import now_iso
from app.common.errors import ValidationError


@dataclass
class Invoice:
    id: str = ""
    number: object = None
    amount: object = None
    currency: object = None
    state: str = "created"
    state: str = "created"
    created_at: str = field(default_factory=now_iso)
    deleted_at: str = ""

    def to_dict(self):
        return asdict(self)

    @staticmethod
    def from_dict(d):
        return Invoice(**{k: v for k, v in d.items() if k in Invoice.__annotations__})

    def validate(self):
        """校验必填与取值范围。非法时抛 ValidationError，消息里带字段名。"""
        if not self.number: raise ValidationError("number: 不能为空")
        if not isinstance(self.amount, (int, float)) or self.amount <= 0: raise ValidationError("amount: 必须为正")
        if not self.currency: raise ValidationError("currency: 不能为空")
        return True
