"""t02 验收：修好了，且没弄坏原来的。"""
import sys

sys.path.insert(0, ".")
from duration import parse_duration

cases = [
    ("1h30m", 5400), ("2h", 7200), ("45s", 45), ("2m10s", 130),
    ("1h2m3s", 3723), ("90m", 5400),
]
bad = 0
for text, want in cases:
    try:
        got = parse_duration(text)
    except Exception as e:
        print(f"[FAIL] {text} 抛出 {type(e).__name__}"); bad += 1; continue
    if got != want:
        print(f"[FAIL] {text} = {got}，期望 {want}"); bad += 1

try:
    parse_duration("abc")
except ValueError:
    pass
else:
    print("[FAIL] 非法输入应抛 ValueError"); bad += 1

print(f"\n{'全部合格' if not bad else f'{bad} 处不合格'}")
sys.exit(1 if bad else 0)
