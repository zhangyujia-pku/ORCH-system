"""b2 单元 07 验收：日志格式正确、stdout 干净、没有 print。"""
import ast
import contextlib
import importlib
import io
import logging
import pathlib
import sys

# checks 脚本的 sys.path[0] 是 checks/ 目录，不是仓库根；
# 不加这一行就 import 不到 scripts 包（表现是 ModuleNotFoundError，看起来像题目坏了）。
sys.path.insert(0, ".")

bad = 0

mod = importlib.import_module("scripts.job_07")

# 1) 源码里不许再有 print
src = pathlib.Path("scripts/job_07.py").read_text(encoding="utf-8")
tree = ast.parse(src)
for node in ast.walk(tree):
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "print":
        print("[FAIL] 源码里仍有 print()")
        bad += 1
        break

# 2) 跑一次：stdout 必须为空，且恰好一条 INFO，消息文本不变
records = [{"payments": "x"}, {"payments": ""}, {"payments": "y"}, {}]
captured = []


class Grab(logging.Handler):
    def emit(self, record):
        captured.append(record)


logger = logging.getLogger(mod.__name__)
logger.setLevel(logging.INFO)
logger.addHandler(Grab())

buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    ret = mod.run(records)

if buf.getvalue() != "":
    print("[FAIL] 仍有 stdout 输出:", repr(buf.getvalue()[:80]))
    bad += 1
if ret != 2:
    print("[FAIL] 返回值变了:", ret)
    bad += 1
if len(captured) != 1:
    print("[FAIL] 应恰好产生 1 条日志，实际", len(captured))
    bad += 1
else:
    rec = captured[0]
    if rec.levelno != logging.INFO:
        print("[FAIL] 日志级别应为 INFO，实际", rec.levelname)
        bad += 1
    if rec.getMessage() != "processed {} payments".format(2):
        print("[FAIL] 日志内容不对:", repr(rec.getMessage()))
        bad += 1

print("全部合格" if not bad else f"{bad} 处不合格")
sys.exit(1 if bad else 0)
