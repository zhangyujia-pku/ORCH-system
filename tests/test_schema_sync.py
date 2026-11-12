"""防止 schema 和代码漂移：dataclass 的字段必须和 schema 的属性一致。

没有这个测试，三个月后你会有一份"文档说能写 model、代码说不认识"的契约，
然后花一晚上 debug 一个根本没生效的字段。
"""

from __future__ import annotations

import json
import unittest
from dataclasses import fields
from pathlib import Path

from orch import FIELDS, Task

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = ROOT / "schema" / "tasks.schema.json"


class TestSchemaSync(unittest.TestCase):
    def setUp(self):
        self.schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        self.task_schema = self.schema["$defs"]["task"]
        self.props = set(self.task_schema["properties"])
        self.code_fields = {f.name for f in fields(Task)}

    def test_properties_match_dataclass(self):
        self.assertEqual(self.props, self.code_fields)
        self.assertEqual(self.props, set(FIELDS))

    def test_schema_is_closed(self):
        self.assertFalse(self.task_schema["additionalProperties"])
        self.assertFalse(self.schema["additionalProperties"])

    def test_required_is_sane(self):
        req = set(self.task_schema["required"])
        self.assertTrue({"id", "goal", "writes", "acceptance"} <= req)
        self.assertTrue(req <= self.props, msg=f"required 里有不存在的字段：{req - self.props}")

    def test_min_items_matches_validator(self):
        # 校验器里 writes/acceptance 为空即报 E011/E012，schema 必须同样收紧
        self.assertEqual(self.task_schema["properties"]["writes"]["minItems"], 1)
        self.assertEqual(self.task_schema["properties"]["acceptance"]["minItems"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
