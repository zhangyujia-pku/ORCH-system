"""b1 单元 08 验收：参数校验三态 + 行为不变。"""
import sys

sys.path.insert(0, "src")
from norm_08 import normalize_08  # noqa: E402

bad = 0


def expect_raise(value, kwargs):
    global bad
    try:
        normalize_08(value, **kwargs)
    except ValueError as e:
        if "value" not in str(e):
            print("[FAIL] 错误消息未包含字段名 value:", repr(str(e)))
            bad += 1
    except Exception as e:                       # noqa: BLE001
        print("[FAIL] 抛了", type(e).__name__, "而不是 ValueError")
        bad += 1
    else:
        print("[FAIL] 非法输入未报错:", repr(value), kwargs)
        bad += 1


expect_raise(None, {})
expect_raise(123, {})
expect_raise("", {})
expect_raise("   ", {})          # 全空白也算空

if normalize_08("", allow_empty=True) != "":
    print("[FAIL] allow_empty=True 时应接受空串并原样返回")
    bad += 1

if normalize_08("  AbC  ") != 'AbC':
    print("[FAIL] 合法输入被改变:", repr(normalize_08("  AbC  ")))
    bad += 1

print("全部合格" if not bad else f"{bad} 处不合格")
sys.exit(1 if bad else 0)
