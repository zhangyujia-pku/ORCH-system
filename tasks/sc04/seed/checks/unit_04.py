"""b3 单元 04 验收：3 次重试、超时抛出、非超时异常不重试。"""
import sys

sys.path.insert(0, "src")
from loader_04 import fetch_all  # noqa: E402

bad = 0


class Flaky:
    """前 n 次抛超时，之后成功。"""

    def __init__(self, fails):
        self.fails = fails
        self.calls = 0

    def get(self, key):
        self.calls += 1
        if self.calls <= self.fails:
            raise TimeoutError("boom")
        return f"ok:{key}"


# 1) 失败两次后成功：应重试到第 3 次
src = Flaky(2)
got = fetch_all(src, ["a"])
if got != ["ok:a"]:
    print("[FAIL] 重试后返回值不对:", got)
    bad += 1
if src.calls != 3:
    print("[FAIL] 应尝试 3 次，实际", src.calls)
    bad += 1

# 2) 一直失败：应抛 TimeoutError，且只尝试 3 次
src = Flaky(99)
try:
    fetch_all(src, ["a"])
except TimeoutError:
    pass
except Exception as e:                       # noqa: BLE001
    print("[FAIL] 抛了", type(e).__name__, "而不是 TimeoutError")
    bad += 1
else:
    print("[FAIL] 一直超时却没有抛异常")
    bad += 1
if src.calls != 3:
    print("[FAIL] 应有上限 3 次，实际", src.calls)
    bad += 1

# 3) 非超时异常不重试
class Broken:
    calls = 0

    def get(self, key):
        Broken.calls += 1
        raise ValueError("bad key")


try:
    fetch_all(Broken(), ["a"])
except ValueError:
    pass
else:
    print("[FAIL] ValueError 应直接抛出")
    bad += 1
if Broken.calls != 1:
    print("[FAIL] ValueError 不应重试，实际调用", Broken.calls, "次")
    bad += 1

print("全部合格" if not bad else f"{bad} 处不合格")
sys.exit(1 if bad else 0)
