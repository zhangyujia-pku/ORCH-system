import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import check_all, check_one, fail, files

errs = check_all()
for p in files():
    errs += [f"{p.name}: {e}" for e in check_one(p)]
sys.exit(fail(errs, "全局一致性"))
