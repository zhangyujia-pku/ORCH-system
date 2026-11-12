import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import check_one, fail

p = Path(__file__).resolve().parents[1] / "docs" / "auth_spec.md"
if not p.exists():
    print("[FAIL] 缺少文档 auth_spec.md")
    sys.exit(1)
sys.exit(fail(check_one(p), "auth_spec.md"))
