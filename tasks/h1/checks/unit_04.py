"""h1 单元 04（发货）验收：跨 models / store / service 三层。"""
import sys

sys.path.insert(0, ".")

from app.common.errors import Conflict, NotFound, ValidationError  # noqa: E402
from app.common.pagination import MAX_SIZE                        # noqa: E402
from app.shipments.service import ShipmentService                   # noqa: E402

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
    return ShipmentService()


def ok_create():
    svc = make()
    obj = svc.create({"order_id": 'ord-1', "carrier": 'SF', "tracking": 'T1'})
    assert obj.id, "创建后应回填 id"
    assert obj.created_at.endswith("Z"), f"created_at 格式不对: {obj.created_at}"
    assert len(svc.events) == 1 and svc.events[0]["type"] == "created", svc.events
    assert svc.get(obj.id).id == obj.id


def bad_payload():
    svc = make()
    for field, value in [("order_id", None), ("carrier", None)]:
        try:
            svc.create({**{"order_id": 'ord-1', "carrier": 'SF', "tracking": 'T1'}, field: value})
        except ValidationError as e:
            assert field in str(e), f"校验消息应包含字段名 {field}，实际 {e}"
        else:
            raise AssertionError(f"{field}={value!r} 应被拒绝")


def pagination():
    svc = make()
    for i in range(7):
        svc.create({**{"order_id": 'ord-1', "carrier": 'SF', "tracking": 'T1'}})
    rows, meta = svc.list(page=2, size=3)
    assert len(rows) == 3, f"第 2 页应 3 条，实际 {len(rows)}"
    assert meta["total"] == 7 and meta["pages"] == 3, meta
    rows, meta = svc.list(page=0, size=MAX_SIZE + 50)
    assert meta["page"] == 1 and meta["size"] == MAX_SIZE, meta
    assert len(rows) == 7


def soft_delete():
    svc = make()
    o1 = svc.create({**{"order_id": 'ord-1', "carrier": 'SF', "tracking": 'T1'}})
    o2 = svc.create({**{"order_id": 'ord-1', "carrier": 'SF', "tracking": 'T1'}})
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
    obj = svc.create({**{"order_id": 'ord-1', "carrier": 'SF', "tracking": 'T1'}})
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
    obj = svc.create({**{"order_id": 'ord-1', "carrier": 'SF', "tracking": 'T1'}})
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
