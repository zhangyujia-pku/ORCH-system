import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import check_one, fail

p = Path(__file__).resolve().parents[1] / "docs" / "api_spec.md"
if not p.exists():
    print("[FAIL] 缺少文档 api_spec.md")
    sys.exit(1)
sys.exit(fail(check_one(p), "api_spec.md"))
