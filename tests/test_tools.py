"""确定性工具：docx 内容断言 + 任务分类。

为什么这两件事放一起测：它们共同回答一个问题——
**文档任务到底能不能进可验收范围**。
分类负责"识别出来"，断言负责"判得了"，缺一个都不成立。
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
import tempfile
import zipfile
from pathlib import Path

import unittest
import unittest.mock

from orch import docxcheck, docxgen, eta, kinds, ooxml, pptxcheck, pptxgen, tools, xlsxcheck, xlsxgen

NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def make_docx(path: Path, paragraphs: list[tuple[str, str]]) -> Path:
    """写一个最小可读的 docx。(文本, 样式)；样式给 'Heading1' 等。"""
    body = []
    for text, style in paragraphs:
        ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
        body.append(f"<w:p>{ppr}<w:r><w:t>{text}</w:t></w:r></w:p>")
    doc = (f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{NS}">'
           f"<w:body>{''.join(body)}</w:body></w:document>")
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        z.writestr("word/document.xml", doc)
    return path


class DocxCheckTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.dir = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def test_reads_text_and_counts_chinese_chars(self):
        p = make_docx(self.dir / "a.docx", [("标题", "Heading1"), ("正文一二三。", "")])
        info = docxcheck.read_docx(p)
        self.assertEqual(info.chars, 7)          # 「标题」2 + 「正文一二三」5，标点不算
        self.assertEqual(info.paragraphs, 2)
        self.assertEqual(info.headings, ["标题"])

    def test_char_bounds(self):
        p = make_docx(self.dir / "b.docx", [("短", "")])
        self.assertTrue(docxcheck.check(p, min_chars=10))
        self.assertEqual(docxcheck.check(p, min_chars=1), [])

    def test_banned_words(self):
        p = make_docx(self.dir / "c.docx", [("我觉得这样很好", "")])
        problems = docxcheck.check(p, ban=["我觉得"])
        self.assertTrue(any("禁用词" in x for x in problems))

    def test_heading_required(self):
        p = make_docx(self.dir / "d.docx", [("只有正文", "")])
        self.assertTrue(docxcheck.check(p, require_heading=True))

    def test_missing_and_broken_files_are_reported(self):
        self.assertTrue(docxcheck.check(self.dir / "nope.docx"))
        bad = self.dir / "bad.docx"
        bad.write_bytes(b"not a zip")
        self.assertTrue(any("docx" in x for x in docxcheck.check(bad)))

    def test_cli_exit_codes(self):
        ok = make_docx(self.dir / "ok.docx", [("标题", "Heading1"), ("正文足够长一点点", "")])
        r = subprocess.run([sys.executable, "-m", "orch.docxcheck", str(ok),
                            "--min-chars", "3", "--require-heading"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        r2 = subprocess.run([sys.executable, "-m", "orch.docxcheck", str(ok),
                             "--min-chars", "9999"], capture_output=True, text=True)
        self.assertEqual(r2.returncode, 1)


class KindsTest(unittest.TestCase):
    def test_code_and_doc_detected(self):
        self.assertEqual(kinds.task_kind(["src/a.py"]), kinds.CODE)
        self.assertEqual(kinds.task_kind(["docs/说明.md"]), kinds.DOC)
        self.assertEqual(kinds.task_kind(["报表.xlsx"]), kinds.SHEET)
        self.assertEqual(kinds.task_kind(["汇报.pptx"]), kinds.SLIDE)

    def test_mixed_takes_the_most_specialised(self):
        # 同一任务既有代码又有表格：表格更需要专门工具，应优先被识别
        self.assertEqual(kinds.task_kind(["src/a.py", "报表.xlsx"]), kinds.SHEET)

    def test_wildcards_are_unknown(self):
        self.assertEqual(kinds.task_kind(["src/*.py"]), kinds.OTHER)

    def test_office_detected_even_when_kind_is_doc(self):
        self.assertTrue(kinds.is_office(["报告.docx"]))
        self.assertFalse(kinds.is_office(["报告.md"]))

    def test_summary_counts(self):
        class T:
            def __init__(self, writes):
                self.writes = writes

        s = kinds.summary([T(["a.py"]), T(["b.py"]), T(["x.docx"])])
        self.assertEqual(s["total"], 3)
        self.assertEqual(s["office_tasks"], 1)
        self.assertEqual(s["counts"][kinds.CODE], 2)


class ToolsRegistryTest(unittest.TestCase):
    def test_docx_check_is_verified(self):
        t = tools.for_file("报告.docx", tools.CHECK)
        self.assertIsNotNone(t)
        self.assertEqual(t.name, "docx-content-check")

    def test_unverified_tools_are_never_picked(self):
        # 模板脚本没验过，不能被自动选中——否则又会把"跑不起来"记成模型的问题
        self.assertIsNone(tools.for_file("报告.docx", tools.CONVERT))

    def test_non_docx_has_no_tool(self):
        self.assertIsNone(tools.for_file("a.py", tools.CHECK))

    def test_render_marks_verified(self):
        text = tools.render(tools.default_tools())
        self.assertIn("✓ docx-content-check", text)


class DocxGenTest(unittest.TestCase):
    """生成端：md → docx → 用断言工具验回来。两端都确定性。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.dir = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    MD = "# 引言\n\n第一段，带**加粗**的句子。\n\n## 小节\n\n- 甲\n- 乙\n"

    def test_roundtrip_passes_check(self):
        src = self.dir / "a.md"
        src.write_text(self.MD, encoding="utf-8")
        dst = self.dir / "a.docx"
        stats = docxgen.convert(src, dst)
        self.assertTrue(dst.exists())
        self.assertEqual(stats["headings"], 2)
        self.assertEqual(stats["bullets"], 2)
        info = docxcheck.read_docx(dst)
        self.assertEqual(len(info.headings), 2)
        self.assertEqual(docxcheck.check(dst, min_chars=5, require_heading=True), [])

    def test_deterministic_output(self):
        src = self.dir / "b.md"
        src.write_text(self.MD, encoding="utf-8")
        a, b = self.dir / "b1.docx", self.dir / "b2.docx"
        docxgen.convert(src, a)
        docxgen.convert(src, b)
        self.assertEqual(a.read_bytes(), b.read_bytes(), "同输入必须同输出")

    def test_rejects_non_markdown(self):
        src = self.dir / "c.exe"
        src.write_text("x", encoding="utf-8")
        with self.assertRaises(ValueError):
            docxgen.convert(src, self.dir / "c.docx")

    def test_missing_source_raises(self):
        with self.assertRaises(FileNotFoundError):
            docxgen.convert(self.dir / "nope.md", self.dir / "x.docx")


class EtaTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def _run(self, name: str, seconds: int, layers: int) -> None:
        d = self.root / name
        d.mkdir()
        t0 = datetime(2026, 11, 9, 12, 0, 0, tzinfo=timezone(timedelta(hours=8)))
        lines = [json.dumps({"event": "run.start",
                             "ts": t0.isoformat(timespec="seconds")})]
        for i in range(layers):
            lines.append(json.dumps({"event": "layer.start",
                                     "ts": (t0 + timedelta(seconds=i)).isoformat(timespec="seconds")}))
        lines.append(json.dumps({"event": "run.end",
                                 "ts": (t0 + timedelta(seconds=seconds)).isoformat(timespec="seconds")}))
        (d / "trace.jsonl").write_text("\n".join(lines), encoding="utf-8")

    def test_estimate_uses_history(self):
        self._run("20261109-120000-aaaa", 120, 2)      # → 60s/层
        est = eta.estimate(self.root, n_tasks=6, max_agents=3)
        self.assertAlmostEqual(est["per_layer"], 60.0, places=1)
        self.assertEqual(est["waves"], 2)
        self.assertIn("history", est["basis"])

    def test_estimate_falls_back_without_history(self):
        est = eta.estimate(self.root, n_tasks=2)
        self.assertEqual(est["per_layer"], eta.DEFAULT_LAYER_SECONDS)
        self.assertIn("default", est["basis"])

    def test_short_batch_blocks(self):
        est = {"seconds": 120, "minutes": 2.0}
        mode, _ = eta.choose(est, host_running=False)
        self.assertEqual(mode, "foreground")

    def test_long_batch_with_host_delivers(self):
        est = {"seconds": 1200, "minutes": 20.0}
        mode, why = eta.choose(est, host_running=True)
        self.assertEqual(mode, "serve")
        self.assertIn("投回", why)

    def test_long_batch_without_host_warns(self):
        est = {"seconds": 1200, "minutes": 20.0}
        mode, why = eta.choose(est, host_running=False)
        self.assertEqual(mode, "foreground")   # 没宿主只能当场跑
        self.assertIn("掐断", why)


class XlsxTest(unittest.TestCase):
    """表格：CSV/Markdown 生成 → 断言回来。两端都是确定性的。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.dir = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    CSV = "姓名,分数\n张三,92\n李四,88\n"

    def test_csv_roundtrip(self):
        src = self.dir / "a.csv"
        src.write_text(self.CSV, encoding="utf-8")
        dst = self.dir / "a.xlsx"
        stats = xlsxgen.convert(src, dst)
        self.assertEqual(stats["sheets"], 1)
        self.assertEqual(xlsxcheck.check(dst, min_rows=3, min_cells=6,
                                        must_cells=("A1=姓名", "B2=92")), [])
        self.assertEqual(ooxml.validate_package(dst), [])

    def test_markdown_tables_become_sheets(self):
        src = self.dir / "b.md"
        src.write_text("# 数据\n\n## 营收\n\n| 季度 | 金额 |\n|---|---|\n| Q1 | 120 |\n\n"
                       "## 成本\n\n| 季度 | 金额 |\n|---|---|\n| Q1 | 60 |\n", encoding="utf-8")
        dst = self.dir / "b.xlsx"
        xlsxgen.convert(src, dst)
        wb = xlsxcheck.read_xlsx(dst)
        self.assertEqual([s.name for s in wb.sheets], ["营收", "成本"])
        self.assertEqual(xlsxcheck.check(dst, require_sheets=("营收", "成本")), [])

    def test_cell_mismatch_is_reported(self):
        src = self.dir / "c.csv"
        src.write_text(self.CSV, encoding="utf-8")
        dst = self.dir / "c.xlsx"
        xlsxgen.convert(src, dst)
        problems = xlsxcheck.check(dst, must_cells=("B2=100",))
        self.assertEqual(len(problems), 1)
        self.assertIn("92", problems[0])

    def test_missing_sheet_is_reported(self):
        src = self.dir / "d.csv"
        src.write_text(self.CSV, encoding="utf-8")
        dst = self.dir / "d.xlsx"
        xlsxgen.convert(src, dst)
        self.assertTrue(any("利润" in p for p in xlsxcheck.check(dst, require_sheets=("利润",))))

    def test_deterministic_output(self):
        src = self.dir / "e.csv"
        src.write_text(self.CSV, encoding="utf-8")
        a, b = self.dir / "e1.xlsx", self.dir / "e2.xlsx"
        xlsxgen.convert(src, a)
        xlsxgen.convert(src, b)
        self.assertEqual(a.read_bytes(), b.read_bytes())

    def test_rejects_unknown_source(self):
        src = self.dir / "f.json"
        src.write_text("{}", encoding="utf-8")
        with self.assertRaises(ValueError):
            xlsxgen.convert(src, self.dir / "f.xlsx")


class PptxTest(unittest.TestCase):
    """幻灯片：Markdown 大纲生成 → 断言回来。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.dir = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    MD = ("# 复盘\n\n2026 年 11 月\n\n## 结论\n\n- 一\n- 二\n\n"
          "## 下一步\n\n- 三\n")

    def test_outline_roundtrip(self):
        src = self.dir / "a.md"
        src.write_text(self.MD, encoding="utf-8")
        dst = self.dir / "a.pptx"
        stats = pptxgen.convert(src, dst)
        self.assertEqual(stats["slides"], 3)
        self.assertEqual(stats["covers"], 1)
        deck = pptxcheck.read_pptx(dst)
        self.assertEqual([s.title for s in deck.slides], ["复盘", "结论", "下一步"])
        self.assertEqual(pptxcheck.check(dst, min_slides=3, min_chars=6, require_title=True), [])
        self.assertEqual(ooxml.validate_package(dst), [])

    def test_missing_slides_is_reported(self):
        src = self.dir / "b.md"
        src.write_text(self.MD, encoding="utf-8")
        dst = self.dir / "b.pptx"
        pptxgen.convert(src, dst)
        self.assertTrue(any("页数" in p for p in pptxcheck.check(dst, min_slides=5)))

    def test_ban_word_is_reported(self):
        src = self.dir / "c.md"
        src.write_text("## 页\n\n- 这个词不该出现：待定\n", encoding="utf-8")
        dst = self.dir / "c.pptx"
        pptxgen.convert(src, dst)
        self.assertTrue(any("待定" in p for p in pptxcheck.check(dst, ban=("待定",))))

    def test_empty_outline_raises(self):
        src = self.dir / "d.md"
        src.write_text("", encoding="utf-8")
        with self.assertRaises(ValueError):
            pptxgen.convert(src, self.dir / "d.pptx")

    def test_deterministic_output(self):
        src = self.dir / "e.md"
        src.write_text(self.MD, encoding="utf-8")
        a, b = self.dir / "e1.pptx", self.dir / "e2.pptx"
        pptxgen.convert(src, a)
        pptxgen.convert(src, b)
        self.assertEqual(a.read_bytes(), b.read_bytes())


class OoxmlValidateTest(unittest.TestCase):
    """结构自检必须能抓出悬空引用——这是"能生成"和"能打开"的分界。"""

    def test_missing_part_is_caught(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "bad.docx"
            ooxml.write_package(p, {
                "[Content_Types].xml":
                    '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/'
                    'package/2006/content-types"><Default Extension="xml" ContentType="application/xml"/>'
                    '<Override PartName="/word/document.xml" ContentType="x"/></Types>',
                "_rels/.rels":
                    '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/'
                    'package/2006/relationships"><Relationship Id="rId1" Type="t" '
                    'Target="word/document.xml"/></Relationships>',
            })
            problems = ooxml.validate_package(p)
            self.assertTrue(any("word/document.xml" in x for x in problems), problems)

    def test_dangling_rid_is_caught(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "bad.pptx"
            ooxml.write_package(p, {
                "[Content_Types].xml":
                    '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/'
                    'package/2006/content-types"><Default Extension="xml" ContentType="application/xml"/>'
                    '</Types>',
                "ppt/presentation.xml":
                    '<p:presentation xmlns:p="p"><p:sldIdLst><p:sldId id="256" r:id="rId9" '
                    'xmlns:r="r"/></p:sldIdLst></p:presentation>',
            })
            problems = ooxml.validate_package(p)
            self.assertTrue(any("rId9" in x for x in problems), problems)


try:                       # 可选依赖：装了就拿来做第二意见，没装就跳过
    import openpyxl as _openpyxl
except Exception:          # noqa: BLE001
    _openpyxl = None
try:
    import pptx as _pptx
except Exception:          # noqa: BLE001
    _pptx = None


class ThirdPartyOpenTest(unittest.TestCase):
    """用别人的实现再打开一遍。

    我们自己的解析器认可，不等于 Excel / PowerPoint 认可——
    手写 OOXML 最容易在"约定"上翻车（缺默认样式、fills 数量不对）。
    装了第三方库就跑这一层，没装就跳过，不让它变成运行时的硬依赖。
    """

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.dir = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    @unittest.skipUnless(_openpyxl, "未安装 openpyxl")
    def test_xlsx_opens_with_openpyxl(self):
        src = self.dir / "a.csv"
        src.write_text("项目,金额\n收入,1200\n", encoding="utf-8")
        dst = self.dir / "a.xlsx"
        xlsxgen.convert(src, dst)
        with unittest.mock.patch("warnings.warn") as warn:
            wb = _openpyxl.load_workbook(dst)
        self.assertEqual([[c.value for c in r] for r in wb.active.iter_rows()],
                         [["项目", "金额"], ["收入", 1200]])
        self.assertEqual(warn.call_count, 0, "不应触发样式类警告")

    @unittest.skipUnless(_pptx, "未安装 python-pptx")
    def test_pptx_opens_with_python_pptx(self):
        src = self.dir / "b.md"
        src.write_text("# 复盘\n\n## 结论\n\n- 一\n- 二\n", encoding="utf-8")
        dst = self.dir / "b.pptx"
        pptxgen.convert(src, dst)
        prs = _pptx.Presentation(str(dst))
        self.assertEqual(len(prs.slides), 2)
        texts = "".join(sh.text_frame.text for sh in prs.slides[1].shapes if sh.has_text_frame)
        self.assertIn("结论", texts)


if __name__ == "__main__":
    unittest.main(verbosity=2)
