"""生成 v2 的"重单元"批量题（h1）。

为什么重出一版：b3 的单元只有 3 行改动，A 一次会话就能装下整批（22.8 万 token / 8 单元
≈ 2.85 万/单元），而一个 worker 的固定开销约 55.9 万——差 20 倍，编排必亏。
这一版把每个单元做成**跨 3 个文件、约 200 行**的改动，并让"约定"只写在文档里，
逼着执行者去读、去找，从而把 A 的单会话成本抬上来。

结构（每个领域一套，共 5 套）：
    app/common/{errors,pagination,clock,ids}.py     共享层（已实现，不改）
    docs/CONVENTIONS.md                             分层约定（唯一出处）
    app/<domain>/models.py   数据类 + 校验            ← 种子缺校验
    app/<domain>/store.py    内存存储 + 分页          ← 种子缺分页/软删
    app/<domain>/service.py  编排：校验→存储→事件     ← 种子是 NotImplementedError
    checks/unit_XX.py        每单元一条验收

自检：seed 必败、ref 必过。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from string import Template

ROOT = Path(__file__).resolve().parent.parent
TASKS = ROOT / "tasks"

DOMAINS = [
    ("orders", "order", "订单", ["sku", "qty", "amount"]),
    ("invoices", "invoice", "发票", ["number", "amount", "currency"]),
    ("refunds", "refund", "退款", ["order_id", "amount", "reason"]),
    ("shipments", "shipment", "发货", ["order_id", "carrier", "tracking"]),
    ("tickets", "ticket", "工单", ["subject", "priority", "assignee"]),
]

COMMON_ERRORS = '''"""领域错误：约定层次里唯一允许抛出的异常族。"""


class AppError(Exception):
    """所有领域异常的基类。"""


class ValidationError(AppError):
    """入参不合法。消息里必须带字段名。"""


class NotFound(AppError):
    """目标不存在。"""


class Conflict(AppError):
    """状态冲突（如重复创建、已删除再改）。"""
'''

COMMON_PAGINATION = '''"""分页：约定 page 从 1 开始，size 上限 100，越界时夹紧而不是报错。"""

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
'''

COMMON_CLOCK = '''"""时间：所有落库时间必须走这里，格式固定为 ISO8601（秒级，UTC）。"""
import datetime as _dt


def now_iso():
    """当前时间，形如 2026-11-11T00:00:00Z。"""
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text):
    """解析 now_iso() 的输出。"""
    return _dt.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ")
'''

COMMON_IDS = '''"""ID：前缀 + 单调计数，便于测试断言形状。"""
import itertools

_seq = itertools.count(1)


def new_id(prefix):
    """形如 ord-000001。"""
    return f"{prefix}-{next(_seq):06d}"
'''

CONVENTIONS = '''# 分层约定（唯一出处）

写任何领域代码之前先读这里，其他地方不再重复。

1. **models** 只负责结构与校验：
   - 提供 `to_dict()` / `from_dict(d)`；
   - 提供 `validate()`，非法时抛 `common.errors.ValidationError`，**消息必须带字段名**；
   - 不在 models 里访问存储。

2. **store** 只负责持久化（内存）：
   - 提供 `create/get/list/update/delete`；
   - `list` 必须用 `common.pagination.paginate`，返回 `(rows, meta)`；
   - `delete` 是**软删除**：置 `deleted_at`，之后 `get` 抛 `NotFound`，`list` 不再返回它。

3. **service** 只负责编排：
   - 顺序固定：校验 → 存储 → 事件；
   - 事件写到 `self.events`（列表），形如 `{"type": "...", "id": "...", "at": now_iso()}`；
   - 时间一律用 `common.clock.now_iso()`，不要用 datetime.now()。

4. **异常**一律用 `common.errors` 里的类型，不要抛裸 `ValueError`。
'''

MODELS_SEED = Template('''"""$NAME 的领域模型。"""
from dataclasses import dataclass, field, asdict

from app.common.clock import now_iso
from app.common.errors import ValidationError


@dataclass
class $CLASS:
    id: str = ""
    $FIELDS
    state: str = "created"
    state: str = "created"
    created_at: str = field(default_factory=now_iso)
    deleted_at: str = ""

    def to_dict(self):
        return asdict(self)

    @staticmethod
    def from_dict(d):
        return $CLASS(**{k: v for k, v in d.items() if k in $CLASS.__annotations__})

    def validate(self):
        """校验必填与取值范围。非法时抛 ValidationError，消息里带字段名。"""
        return True
''')

MODELS_REF = Template('''"""$NAME 的领域模型。"""
from dataclasses import dataclass, field, asdict

from app.common.clock import now_iso
from app.common.errors import ValidationError


@dataclass
class $CLASS:
    id: str = ""
    $FIELDS
    state: str = "created"
    state: str = "created"
    created_at: str = field(default_factory=now_iso)
    deleted_at: str = ""

    def to_dict(self):
        return asdict(self)

    @staticmethod
    def from_dict(d):
        return $CLASS(**{k: v for k, v in d.items() if k in $CLASS.__annotations__})

    def validate(self):
        """校验必填与取值范围。非法时抛 ValidationError，消息里带字段名。"""
        $CHECKS
        return True
''')

STORE_SEED = Template('''"""$NAME 的内存存储。"""
from app.common.errors import NotFound


class ${CLASS}Store:
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
''')

STORE_REF = Template('''"""$NAME 的内存存储。"""
from app.common.clock import now_iso
from app.common.errors import NotFound
from app.common.pagination import paginate


class ${CLASS}Store:
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
''')

SERVICE_SEED = Template('''"""$NAME 的服务层：校验 → 存储 → 事件。"""
from app.common.errors import ValidationError
from app.common.ids import new_id
from app.$DOMAIN.models import $CLASS
from app.$DOMAIN.rules import next_state
from app.$DOMAIN.store import ${CLASS}Store


class ${CLASS}Service:
    def __init__(self):
        self.store = ${CLASS}Store()
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
''')

SERVICE_REF = Template('''"""$NAME 的服务层：校验 → 存储 → 事件。"""
from app.common.clock import now_iso
from app.common.errors import ValidationError
from app.common.ids import new_id
from app.$DOMAIN.models import $CLASS
from app.$DOMAIN.rules import next_state
from app.$DOMAIN.store import ${CLASS}Store


class ${CLASS}Service:
    def __init__(self):
        self.store = ${CLASS}Store()
        self.events = []

    def _emit(self, kind, obj_id):
        self.events.append({"type": kind, "id": obj_id, "at": now_iso()})

    def create(self, payload):
        """创建：校验通过后落库，并记一条 created 事件。"""
        obj = $CLASS.from_dict(payload)
        obj.validate()
        if not obj.id:
            obj.id = new_id("$PREFIX")
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
''')

RULES_SEED = Template('''"""$NAME 的状态机。状态与动作的合法组合见下方注释。"""

# 合法迁移：created -> processing -> done；任何状态都可以 cancelled
STATES = ("created", "processing", "done", "cancelled")
ACTIONS = ("start", "finish", "cancel")

TRANSITIONS = {}


def next_state(current, action):
    """返回迁移后的状态；非法组合抛 Conflict，消息里带当前状态与动作。"""
    raise NotImplementedError("next_state")
''')

RULES_REF = Template('''"""$NAME 的状态机。状态与动作的合法组合见下方注释。"""
from app.common.errors import Conflict

# 合法迁移：created -> processing -> done；任何状态都可以 cancelled
STATES = ("created", "processing", "done", "cancelled")
ACTIONS = ("start", "finish", "cancel")

TRANSITIONS = {
    ("created", "start"): "processing",
    ("processing", "finish"): "done",
    ("created", "cancel"): "cancelled",
    ("processing", "cancel"): "cancelled",
}


def next_state(current, action):
    """返回迁移后的状态；非法组合抛 Conflict，消息里带当前状态与动作。"""
    if action == "cancel" and current in ("created", "processing"):
        return "cancelled"
    key = (current, action)
    if key not in TRANSITIONS:
        raise Conflict(f"state: 不允许 {current} --{action}-->")
    return TRANSITIONS[key]
''')

RULES_SEED = Template('''"""$NAME 的状态机。状态与动作的合法组合见下方注释。"""

# 合法迁移：created -> processing -> done；任何状态都可以 cancelled
STATES = ("created", "processing", "done", "cancelled")
ACTIONS = ("start", "finish", "cancel")

TRANSITIONS = {}


def next_state(current, action):
    """返回迁移后的状态；非法组合抛 Conflict，消息里带当前状态与动作。"""
    raise NotImplementedError("next_state")
''')

RULES_REF = Template('''"""$NAME 的状态机。状态与动作的合法组合见下方注释。"""
from app.common.errors import Conflict

# 合法迁移：created -> processing -> done；任何状态都可以 cancelled
STATES = ("created", "processing", "done", "cancelled")
ACTIONS = ("start", "finish", "cancel")

TRANSITIONS = {
    ("created", "start"): "processing",
    ("processing", "finish"): "done",
    ("created", "cancel"): "cancelled",
    ("processing", "cancel"): "cancelled",
}


def next_state(current, action):
    """返回迁移后的状态；非法组合抛 Conflict，消息里带当前状态与动作。"""
    if action == "cancel" and current in ("created", "processing"):
        return "cancelled"
    key = (current, action)
    if key not in TRANSITIONS:
        raise Conflict(f"state: 不允许 {current} --{action}-->")
    return TRANSITIONS[key]
''')

CHECK = Template('''"""h1 单元 $NN（$NAME）验收：跨 models / store / service 三层。"""
import sys

sys.path.insert(0, ".")

from app.common.errors import Conflict, NotFound, ValidationError  # noqa: E402
from app.common.pagination import MAX_SIZE                        # noqa: E402
from app.$DOMAIN.service import ${CLASS}Service                   # noqa: E402

bad = 0


def check(name, fn):
    global bad
    try:
        fn()
    except AssertionError as e:
        print(f"[FAIL] {name}: {e}")
        bad += 1
    except Exception as e:                                        # noqa: BLE001
        print(f"[FAIL] {name}: 抛了 {type(e).__name__}: {e}")
        bad += 1


def make():
    return ${CLASS}Service()


def ok_create():
    svc = make()
    obj = svc.create($PAYLOAD)
    assert obj.id, "创建后应回填 id"
    assert obj.created_at.endswith("Z"), f"created_at 格式不对: {obj.created_at}"
    assert len(svc.events) == 1 and svc.events[0]["type"] == "created", svc.events
    assert svc.get(obj.id).id == obj.id


def bad_payload():
    svc = make()
    for field, value in $BAD_CASES:
        try:
            svc.create({**$PAYLOAD, field: value})
        except ValidationError as e:
            assert field in str(e), f"校验消息应包含字段名 {field}，实际 {e}"
        else:
            raise AssertionError(f"{field}={value!r} 应被拒绝")


def pagination():
    svc = make()
    for i in range(7):
        svc.create({**$PAYLOAD})
    rows, meta = svc.list(page=2, size=3)
    assert len(rows) == 3, f"第 2 页应 3 条，实际 {len(rows)}"
    assert meta["total"] == 7 and meta["pages"] == 3, meta
    rows, meta = svc.list(page=0, size=MAX_SIZE + 50)
    assert meta["page"] == 1 and meta["size"] == MAX_SIZE, meta
    assert len(rows) == 7


def soft_delete():
    svc = make()
    o1 = svc.create({**$PAYLOAD})
    o2 = svc.create({**$PAYLOAD})
    svc.delete(o1.id)
    assert len(svc.events) == 3 and svc.events[-1]["type"] == "deleted", svc.events
    try:
        svc.get(o1.id)
    except NotFound:
        pass
    else:
        raise AssertionError("软删后 get 应抛 NotFound")
    rows, meta = svc.list()
    assert meta["total"] == 1 and rows[0].id == o2.id, meta


def state_flow():
    svc = make()
    obj = svc.create({**$PAYLOAD})
    svc.update(obj.id, "start")
    assert svc.get(obj.id).state == "processing", svc.get(obj.id).state
    svc.update(obj.id, "finish")
    assert svc.get(obj.id).state == "done", svc.get(obj.id).state
    try:
        svc.update(obj.id, "start")
    except Conflict:
        pass
    else:
        raise AssertionError("done 状态不应允许 start")
    assert [e["type"] for e in svc.events] == ["created", "moved", "moved"], svc.events


def state_flow():
    svc = make()
    obj = svc.create({**$PAYLOAD})
    svc.update(obj.id, "start")
    assert svc.get(obj.id).state == "processing", svc.get(obj.id).state
    svc.update(obj.id, "finish")
    assert svc.get(obj.id).state == "done", svc.get(obj.id).state
    try:
        svc.update(obj.id, "start")
    except Conflict:
        pass
    else:
        raise AssertionError("done 状态不应允许 start")
    assert [e["type"] for e in svc.events] == ["created", "moved", "moved"], svc.events


check("创建与事件", ok_create)
check("非法入参", bad_payload)
check("分页边界", pagination)
check("状态机", state_flow)
check("状态机", state_flow)
check("软删除", soft_delete)

print("全部合格" if not bad else f"{bad} 处不合格")
sys.exit(1 if bad else 0)
''')


def w(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def build(root: Path) -> dict:
    tid = "h1"
    dest = root / tid
    seed, checks, ref = dest / "seed", dest / "checks", dest / "ref"
    for d in (seed, checks, ref):
        if d.exists():
            shutil.rmtree(d)

    # 共享层（三层都用，且不允许改）
    for name, body in (("errors", COMMON_ERRORS), ("pagination", COMMON_PAGINATION),
                       ("clock", COMMON_CLOCK), ("ids", COMMON_IDS)):
        w(seed / "app" / "common" / f"{name}.py", body)
        w(ref / "app" / "common" / f"{name}.py", body)
    w(seed / "app" / "common" / "__init__.py", "")
    w(ref / "app" / "common" / "__init__.py", "")
    w(seed / "app" / "__init__.py", "")
    w(ref / "app" / "__init__.py", "")
    w(seed / "docs" / "CONVENTIONS.md", CONVENTIONS)
    w(ref / "docs" / "CONVENTIONS.md", CONVENTIONS)
    w(seed / ".gitignore", "__pycache__/\n*.pyc\n.pytest_cache/\n")
    w(ref / ".gitignore", "__pycache__/\n*.pyc\n.pytest_cache/\n")

    accept = []
    for i, (domain, prefix, name, fields) in enumerate(DOMAINS, 1):
        cls = domain[:-1].capitalize() if domain.endswith("s") else domain.capitalize()
        cls = {"orders": "Order", "invoices": "Invoice", "refunds": "Refund",
               "shipments": "Shipment", "tickets": "Ticket"}[domain]
        field_lines = "\n    ".join(f"{f}: object = None" for f in fields)
        payload = "{" + ", ".join(f'"{f}": {repr(_sample(f))}' for f in fields) + "}"
        bad_cases = "[" + ", ".join(f'("{f}", None)' for f in fields[:2]) + "]"
        checks_body = {
            "orders": ['if not self.sku: raise ValidationError("sku: 不能为空")',
                       'if not isinstance(self.qty, int) or self.qty <= 0: raise ValidationError("qty: 必须是正整数")',
                       'if not isinstance(self.amount, (int, float)) or self.amount < 0: raise ValidationError("amount: 不能为负")'],
            "invoices": ['if not self.number: raise ValidationError("number: 不能为空")',
                         'if not isinstance(self.amount, (int, float)) or self.amount <= 0: raise ValidationError("amount: 必须为正")',
                         'if not self.currency: raise ValidationError("currency: 不能为空")'],
            "refunds": ['if not self.order_id: raise ValidationError("order_id: 不能为空")',
                        'if not isinstance(self.amount, (int, float)) or self.amount <= 0: raise ValidationError("amount: 必须为正")',
                        'if not self.reason: raise ValidationError("reason: 不能为空")'],
            "shipments": ['if not self.order_id: raise ValidationError("order_id: 不能为空")',
                          'if not self.carrier: raise ValidationError("carrier: 不能为空")',
                          'if not self.tracking: raise ValidationError("tracking: 不能为空")'],
            "tickets": ['if not self.subject: raise ValidationError("subject: 不能为空")',
                        'if self.priority not in ("low", "normal", "high"): raise ValidationError("priority: 取值非法")',
                        'if not self.assignee: raise ValidationError("assignee: 不能为空")'],
        }[domain]
        sub = {"NN": f"{i:02d}", "DOMAIN": domain, "NAME": name, "CLASS": cls,
               "PREFIX": prefix, "FIELDS": field_lines, "PAYLOAD": payload,
               "BAD_CASES": bad_cases, "CHECKS": "\n        ".join(checks_body)}

        for d in (seed, ref):
            w(d / "app" / domain / "__init__.py", "")
        w(seed / "app" / domain / "models.py", MODELS_SEED.substitute(sub))
        w(ref / "app" / domain / "models.py", MODELS_REF.substitute(sub))
        w(seed / "app" / domain / "rules.py", RULES_SEED.substitute(sub))
        w(ref / "app" / domain / "rules.py", RULES_REF.substitute(sub))
        w(seed / "app" / domain / "rules.py", RULES_SEED.substitute(sub))
        w(ref / "app" / domain / "rules.py", RULES_REF.substitute(sub))
        w(seed / "app" / domain / "store.py", STORE_SEED.substitute(sub))
        w(ref / "app" / domain / "store.py", STORE_REF.substitute(sub))
        w(seed / "app" / domain / "service.py", SERVICE_SEED.substitute(sub))
        w(ref / "app" / domain / "service.py", SERVICE_REF.substitute(sub))

        body = CHECK.substitute(sub)
        w(checks / f"unit_{i:02d}.py", body)
        w(seed / "checks" / f"unit_{i:02d}.py", body)
        accept.append(f"python checks/unit_{i:02d}.py")

    n = len(DOMAINS)
    goal = (
        f"这个仓库是一个小型领域服务框架，{n} 个领域（{'、'.join(d for d, *_ in DOMAINS)}）"
        "都只写了一半。请按 docs/CONVENTIONS.md 的分层约定把它们补完整：\n"
        "· models 里补 validate()（非法入参抛 ValidationError，消息带字段名）；\n"
        "· store 里补软删除与分页（用 app.common.pagination.paginate）；\n"
        "· rules 里实现状态机 next_state（非法迁移抛 Conflict）；\n"
        "· service 里实现 create / list / update / delete，并按约定写事件。\n"
        "app/common/ 下的共享层是稳定的，不要改。每个领域互不依赖，可以并行。"
        f"验收：依次跑 {' '.join(f'checks/unit_{i:02d}.py' for i in range(1, n + 1))}，全部退出码 0。"
    )
    w(dest / "meta.json", json.dumps({
        "id": tid, "tier": "H", "goal": goal, "timeout_s": 3600,
        "accept": accept, "batch_size": n, "kind": "重单元（跨三层）",
    }, ensure_ascii=False, indent=2))
    return {"id": tid, "n": n, "accept": accept}


def _sample(field: str):
    return {"sku": "SKU-1", "qty": 2, "amount": 10.0, "number": "INV-1",
            "currency": "CNY", "order_id": "ord-1", "reason": "damaged",
            "carrier": "SF", "tracking": "T1", "subject": "broken",
            "priority": "high", "assignee": "amy"}.get(field)


def verify(tid: str) -> tuple[int, int]:
    dest = TASKS / tid
    meta = json.loads((dest / "meta.json").read_text(encoding="utf-8"))

    def run(ref: bool) -> int:
        tmp = Path(tempfile.mkdtemp(prefix="chk-h1-"))
        try:
            shutil.copytree(dest / "seed", tmp / "repo")
            if ref:
                shutil.copytree(dest / "ref", tmp / "repo", dirs_exist_ok=True)
            ok = 0
            for cmd in meta["accept"]:
                r = subprocess.run(cmd, cwd=str(tmp / "repo"), shell=True, capture_output=True)
                ok += r.returncode == 0
                if r.returncode != 0 and not ref:
                    pass
            return ok
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    return run(False), run(True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--verify", action="store_true")
    args = ap.parse_args()
    dest = TASKS / "h1"
    if dest.exists() and not args.force:
        print("[skip] tasks/h1 已存在（--force 覆盖）")
        return 0
    info = build(TASKS)
    print(f"[ok] {info['id']}：{info['n']} 个单元 · {len(info['accept'])} 条验收")
    if args.verify:
        seed_ok, ref_ok = verify("h1")
        good = seed_ok == 0 and ref_ok == info["n"]
        print(f"[verify] seed 通过 {seed_ok}/{info['n']}，ref 通过 {ref_ok}/{info['n']}"
              f" → {'合格' if good else '不合格'}")
        return 0 if good else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
