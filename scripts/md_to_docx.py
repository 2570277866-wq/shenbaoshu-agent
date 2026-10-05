# -*- coding: utf-8 -*-
"""
Markdown → docx —— 申报书最终交付格式。本地工具。

⚠ 一条约定例外：依赖第三方包 python-docx。scripts/ 的「仅标准库」约束是给
节点脚本的；本脚本与 build_workflow / run_pipeline 一样是本地工具。
未装 python-docx 时 import 不报错、调用时给清晰错误 —— 引擎（run_pipeline）
不依赖本脚本，装不装都不影响生成链路。

支持（申报书实际用到的子集，不追求通用 Markdown）：
    # 文档标题 → Title；## / ### → Heading 1/2
    **加粗**、`行内代码`；| 表格 | → Word 表格；- 与 1. 列表
    `【待补充：××】` 原样保留 —— 待补槽位进 docx 也要看得见

中文字体：正文宋体小四（12pt），标题黑体（Heading 样式自带的加粗保留）。

用法：
    python3 md_to_docx.py document.md output.docx
"""

import argparse
import re
import sys

try:
    from docx import Document as _Document
    from docx.oxml.ns import qn
    from docx.shared import Pt
    _HAS_DOCX = True
except ImportError:  # 报错推迟到调用时 —— import 本脚本不该因缺包失败
    _Document = None
    _HAS_DOCX = False

BODY_FONT = "宋体"
HEADING_FONT = "黑体"
BODY_SIZE = 12  # 小四

# 行内格式：**加粗** 与 `行内代码`。括号形式捕获内容，切段后按组处理。
INLINE = re.compile(r"(\*\*[^*\n]+\*\*|`[^`\n]+`)")
TABLE_LINE = re.compile(r"^\s*\|.*\|\s*$")
TABLE_SEP = re.compile(r"^\s*\|[\s:|-]+\|\s*$")
LIST_BULLET = re.compile(r"^\s*[-*]\s+(.*)$")
LIST_NUMBER = re.compile(r"^\s*\d+[.)]\s+(.*)$")


def _require_docx():
    if _Document is None:
        raise RuntimeError(
            "需要 python-docx —— pip install python-docx。"
            "引擎（run_pipeline）不依赖本脚本，装不装不影响生成链路。")


def _set_run_font(run, east, size_pt, bold=False):
    """中西文字体分开设：font.name 只管西文，中文必须走 w:eastAsia。"""
    run.font.name = "Times New Roman"
    run.font.size = Pt(size_pt)
    run.font.bold = bold
    run._element.rPr.rFonts.set(qn("w:eastAsia"), east)


def _add_inline(paragraph, text, size_pt=BODY_SIZE):
    """按 ** / ` 切段，加粗与行内代码各自成 run。"""
    for part in INLINE.split(text):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**"):
            run = paragraph.add_run(part[2:-2])
            _set_run_font(run, BODY_FONT, size_pt, bold=True)
        elif part.startswith("`") and part.endswith("`"):
            run = paragraph.add_run(part[1:-1])
            _set_run_font(run, BODY_FONT, size_pt)
        else:
            run = paragraph.add_run(part)
            _set_run_font(run, BODY_FONT, size_pt)


def _add_table(doc, rows):
    """连续 | 行合成一张表。第二行 |---| 是分隔行，不进表格。"""
    rows = [r for r in rows if not TABLE_SEP.match(r)]
    cells = [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows]
    if not cells or not cells[0]:
        return
    width = len(cells[0])
    table = doc.add_table(rows=len(cells), cols=width)
    table.style = "Table Grid"
    for i, row in enumerate(cells):
        for j in range(width):
            cell = table.cell(i, j)
            cell.text = ""
            para = cell.paragraphs[0]
            _add_inline(para, row[j] if j < len(row) else "")
            if i == 0:
                for run in para.runs:
                    run.font.bold = True


def convert(text, title=None):
    """Markdown 文本 → docx Document。title 用于覆盖文档首行（CLI 已剥标题时用）。"""
    _require_docx()
    doc = _Document()
    if title:
        heading = doc.add_heading(title, level=0)
        for run in heading.runs:
            _set_run_font(run, HEADING_FONT, 16)

    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    table_rows = []

    def flush_table():
        if table_rows:
            _add_table(doc, table_rows)
            table_rows.clear()

    for line in lines:
        if TABLE_LINE.match(line):
            table_rows.append(line)
            continue
        flush_table()

        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("# "):
            heading = doc.add_heading(stripped[2:], level=0)
        elif stripped.startswith("## "):
            heading = doc.add_heading(stripped[3:], level=1)
        elif stripped.startswith("### "):
            heading = doc.add_heading(stripped[4:], level=2)
        else:
            heading = None
        if heading is not None:
            for run in heading.runs:
                _set_run_font(run, HEADING_FONT,
                              16 if heading.style.name == "Title" else 14)
            continue

        m = LIST_BULLET.match(line)
        if m:
            para = doc.add_paragraph(style="List Bullet")
            _add_inline(para, m.group(1))
            continue
        m = LIST_NUMBER.match(line)
        if m:
            para = doc.add_paragraph(style="List Number")
            _add_inline(para, m.group(1))
            continue

        para = doc.add_paragraph()
        _add_inline(para, stripped)

    flush_table()
    return doc


def save(doc, path):
    _require_docx()
    doc.save(path)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Markdown → docx（申报书交付格式）")
    parser.add_argument("markdown", help="Markdown 文件路径")
    parser.add_argument("output", help="docx 输出路径")
    args = parser.parse_args(argv)

    with open(args.markdown, encoding="utf-8") as fh:
        text = fh.read()
    save(convert(text), args.output)
    print("已写入 %s" % args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
