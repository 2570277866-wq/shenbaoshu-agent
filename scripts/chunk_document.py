# -*- coding: utf-8 -*-
"""
节点 ② 长文档分块 —— 控制上下文长度，供迭代节点串行生成。

契约见 docs/ARCHITECTURE.md 第五节「节点 ②」。

输入：
    document    完整 Markdown（或任一超长章节）
    max_chars   单块上限，默认 6000

输出：
    chunks      [{"index", "chapter", "title", "content", "chars"}]
    issues      分块阶段的问题，交节点③ 汇总

分块原则（写在文档里的，实现照办）：
    1. 按语义边界切 —— 章节 > 子标题 > 段落，绝不按固定字数硬切
    2. 表格、代码块不跨块切分
    3. 切不动就整块留下并报 issue —— 硬切出来的碎片检索到也没法用

为什么硬切不行：硬切产生「上半句说 A、下半句说 B」的碎片，
喂给 LLM 会生成前后不接的内容，且分块本身看不出来。
"""

import re

DEFAULT_MAX_CHARS = 6000

HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
ORDER_PREFIX = re.compile(r"^[一二三四五六七八九十]+、\s*")


def _normalize_inputs(args, kwargs):
    if kwargs:
        return kwargs
    if len(args) == 1 and isinstance(args[0], dict):
        return args[0]
    if not args:
        return {}
    raise TypeError("main() 只接受一个 dict 参数，或与输入变量同名的一组关键字参数")


def _blocks(text):
    """
    切成最小完整块。块之间用空行或标题分隔，表格与代码块各自成块。

    返回 [{"text", "level", "title"}]；level 为标题级别，正文块为 0。
    """
    blocks = []
    cur = []
    in_code = False

    def flush():
        if cur:
            body = "\n".join(cur).strip("\n")
            if body:
                blocks.append({"text": body, "level": 0, "title": None})
        del cur[:]

    for line in text.split("\n"):
        # 代码围栏：内部一律不切
        if line.strip().startswith("```"):
            in_code = not in_code
            cur.append(line)
            if not in_code:
                flush()
            continue
        if in_code:
            cur.append(line)
            continue

        m = HEADING.match(line)
        if m:
            flush()
            blocks.append({
                "text": line.rstrip(),
                "level": len(m.group(1)),
                "title": ORDER_PREFIX.sub("", m.group(2).strip()),
            })
            continue

        if not line.strip():
            flush()
            continue

        cur.append(line)

    flush()
    return blocks


def _chapter(title, level):
    """章节名归一，与一致性审查的 section 字段对齐。"""
    return ORDER_PREFIX.sub("", title) if title else "（前言）"


def main(*args, **kwargs):
    inputs = _normalize_inputs(args, kwargs)
    text = inputs.get("document") or ""
    try:
        max_chars = int(inputs.get("max_chars") or DEFAULT_MAX_CHARS)
    except (TypeError, ValueError):
        max_chars = DEFAULT_MAX_CHARS

    issues = []
    if not text.strip():
        return {
            "chunks": [],
            "issues": [{"type": "empty_document", "section": "-",
                        "detail": "document 为空，无内容可分块"}],
            "stats": {"chunk_count": 0, "max_chunk_chars": 0},
        }

    blocks = _blocks(text)

    chunks = []
    cur_blocks = []
    cur_len = 0
    chapter = "（前言）"
    subtitle = None

    def emit():
        if not cur_blocks:
            return
        content = "\n\n".join(b["text"] for b in cur_blocks)
        chunks.append({
            "index": len(chunks),
            "chapter": chapter,
            "title": subtitle or chapter,
            "content": content,
            "chars": len(content),
        })
        del cur_blocks[:]

    for i, b in enumerate(blocks):
        # 章节边界优先于长度限制 —— 宁可块小，不可跨章
        if b["level"] == 2:
            emit()
            chapter = _chapter(b["title"], b["level"])
            subtitle = None
            cur_blocks.append(b)          # 标题本身留在正文里，不丢结构
            cur_len = len(b["text"]) + 2
            continue

        if b["level"] == 3:
            subtitle = _chapter(b["title"], b["level"])

        blen = len(b["text"])

        if blen > max_chars:
            # 单块超预算：整块留下，不硬切
            emit()
            cur_len = 0
            issues.append({
                "type": "oversized_block",
                "section": chapter,
                "detail": "单块 %d 字 > 上限 %d，整块保留未切分（%s）"
                          % (blen, max_chars, b["text"][:30].replace("\n", " ")),
            })
            cur_blocks.append(b)
            emit()
            cur_len = 0
            continue

        if cur_len and cur_len + blen + 2 > max_chars:
            emit()
            cur_len = 0

        cur_blocks.append(b)
        cur_len += blen + 2

    emit()

    # 章节被拆成多块时提示 —— 迭代节点需要知道哪些章要串行
    split_chapters = {}
    for c in chunks:
        split_chapters[c["chapter"]] = split_chapters.get(c["chapter"], 0) + 1
    for name, n in split_chapters.items():
        if n > 1:
            issues.append({
                "type": "chapter_split",
                "section": name,
                "detail": "章节「%s」超出上下文预算，已拆为 %d 块，需串行生成后拼接" % (name, n),
            })

    return {
        "chunks": chunks,
        "issues": issues,
        "stats": {
            "chunk_count": len(chunks),
            "max_chunk_chars": max((c["chars"] for c in chunks), default=0),
            "total_chars": len(text),
        },
    }


if __name__ == "__main__":
    import json
    import sys

    payload = json.loads(sys.stdin.read() or "{}")
    result = main(payload)
    print(json.dumps(
        {"chunks": [dict(c, content=c["content"][:60] + "…") for c in result["chunks"]],
         "issues": result["issues"], "stats": result["stats"]},
        ensure_ascii=False, indent=2))
