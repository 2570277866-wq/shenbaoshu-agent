# -*- coding: utf-8 -*-
"""
记忆文件读写核心 —— 与 HTTP 层解耦，便于单测。

设计契约见 docs/ARCHITECTURE.md 9.2。三条硬约束：

  1. **白名单**：只碰 memory/ 下四个文件。文件名按精确匹配放行，
     不做路径拼接 —— `../`、`a/b`、绝对路径、符号链接全部在门口死掉。
  2. **写前备份**：每次改动落一份 .backups/，误写可回滚。
  3. **不留半成品**：临时文件 + os.replace 原子替换。读者永远看不到写一半的文件。

再加一条运维向的：每次写操作追加一行 memory/.audit.jsonl，远程监看和事后追责都靠它。

为什么读不加锁、写才加：替换是原子的，读者要么看到旧版要么看到新版，不会看到中间态。
只有「读—改—写」的复合操作才需要锁，否则两个并发写会互相覆盖。
"""

import difflib
import json
import os
import re
import shutil
import threading
import uuid
from datetime import datetime
from pathlib import Path

try:
    import fcntl
except ImportError:  # 非 POSIX（Windows）降级为仅线程锁
    fcntl = None

# ---------------------------------------------------------------- 常量

#: 允许读写的文件。**精确匹配**，多一个字少一个字都不行。
ALLOWED_FILES = ("CONTEXT.md", "AGENTS.md", "decisions.md", "lessons.md")

#: 日志型文件（可追加条目）的默认插入方式。CONTEXT/AGENTS 是规则文件，
#: 结构固定，不接受无锚点插入 —— 见 insert()。
DEFAULT_INSERT = {
    "decisions.md": ("before_first_heading", None),
    "lessons.md": ("after_anchor", "## 已记录"),
}

BACKUP_DIRNAME = ".backups"
LOCK_DIRNAME = ".locks"      # 单独放，别把 0 字节锁文件和记忆文件混在一层
AUDIT_NAME = ".audit.jsonl"
BACKUP_KEEP = 20            # 每个文件保留最近 N 份备份
MAX_CONTENT_BYTES = 512 * 1024   # 单次写入上限，防跑飞的循环撑爆磁盘
MAX_FILE_BYTES = 4 * 1024 * 1024

_L1_HEADING = re.compile(r"^#\s")
_LOCKS = {name: threading.Lock() for name in ALLOWED_FILES}


# ---------------------------------------------------------------- 异常


class MemoryServiceError(Exception):
    """带错误码与 HTTP 状态的异常。HTTP 层只做转发，不自己编错误。"""

    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


# ---------------------------------------------------------------- 路径


def memory_root():
    """记忆目录。每次调用读环境变量，测试与运行时可换。"""
    return Path(os.environ.get("MEMORY_DIR") or (Path(__file__).resolve().parent.parent / "memory"))


def resolve(name):
    """
    文件名 → 绝对路径。**白名单精确匹配，不做任何路径规范化**。

    这是整个服务唯一的入口校验。不在这里放行，后面就没有第二次机会。
    """
    if not isinstance(name, str) or name not in ALLOWED_FILES:
        raise MemoryServiceError(
            "unknown_file",
            "文件 %r 不在白名单内。允许：%s" % (name, "、".join(ALLOWED_FILES)),
            400,
        )
    return memory_root() / name


def memory_path(name, must_exist=True):
    path = resolve(name)
    if must_exist and not path.is_file():
        raise MemoryServiceError("file_not_found", "记忆文件 %s 不存在" % name, 404)
    return path


# ---------------------------------------------------------------- 锁


class _locked:
    """线程锁 + 文件锁。进程内防并发线程，进程间防多 worker / 手工脚本同时改。"""

    def __init__(self, name):
        self.name = name

    def __enter__(self):
        self._tlock = _LOCKS[self.name]
        self._tlock.acquire()
        self._fd = None
        if fcntl is not None:
            lock_dir = memory_root() / LOCK_DIRNAME
            lock_dir.mkdir(parents=True, exist_ok=True)
            self._fd = os.open(str(lock_dir / (self.name + ".lock")), os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(self._fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        try:
            if self._fd is not None:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
                os.close(self._fd)
        finally:
            self._tlock.release()
        return False


# ---------------------------------------------------------------- 底层读写


def _read_text(path):
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _atomic_write(path, text):
    """临时文件 + os.replace。同一目录下的 replace 在 POSIX 上是原子的。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp." + uuid.uuid4().hex[:8])
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp), str(path))
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _backup(path):
    """改动前存一份。返回备份文件名（相对 memory/），失败不阻断写入但会记进返回体。"""
    if not path.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
    backup_dir = path.parent / BACKUP_DIRNAME
    backup_dir.mkdir(parents=True, exist_ok=True)
    dest = backup_dir / ("%s.%s.bak" % (path.name, stamp))
    shutil.copy2(str(path), str(dest))
    _prune_backups(path.name)
    return "%s/%s" % (BACKUP_DIRNAME, dest.name)


def _prune_backups(name):
    backup_dir = memory_root() / BACKUP_DIRNAME
    if not backup_dir.is_dir():
        return
    olds = sorted(backup_dir.glob(name + ".*.bak"))
    for stale in olds[:-BACKUP_KEEP]:
        try:
            stale.unlink()
        except OSError:
            pass


def _audit(entry):
    path = memory_root() / AUDIT_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = dict(entry)
    entry["ts"] = datetime.now().isoformat(timespec="seconds")
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass  # 审计写不进去不该让业务失败


def _check_size(content):
    n = len(content.encode("utf-8"))
    if n > MAX_CONTENT_BYTES:
        raise MemoryServiceError(
            "content_too_large",
            "写入内容 %d 字节，超过上限 %d 字节" % (n, MAX_CONTENT_BYTES),
            413,
        )
    return n


def _diff(old, new, limit=40):
    """给人工审看用的摘要 diff。截断，只做提示不做归档。"""
    lines = list(difflib.unified_diff(
        old.splitlines(), new.splitlines(),
        fromfile="before", tofile="after", lineterm="", n=1,
    ))
    if len(lines) > limit:
        lines = lines[:limit] + ["…（共 %d 行差异，已截断）" % len(lines)]
    return "\n".join(lines)


def _guard_content(content, field="content"):
    if not isinstance(content, str):
        raise MemoryServiceError("bad_request", "%s 必须是字符串" % field, 400)
    return content


# ---------------------------------------------------------------- 五个工具


def read(name):
    path = memory_path(name)
    content = _read_text(path)
    st = path.stat()
    return {
        "ok": True,
        "file": name,
        "content": content,
        "bytes": st.st_size,
        "lines": content.count("\n") + (1 if content else 0),
        "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
    }


def write(name, content, mode="overwrite", actor=None, dry_run=False):
    """
    覆盖或追加写入。mode='overwrite' | 'append'。

    dry_run=True 只回报将要发生什么，不落盘、不备份、不记审计。
    """
    content = _guard_content(content)
    if mode not in ("overwrite", "append"):
        raise MemoryServiceError("bad_request", "mode 只能是 overwrite 或 append", 400)

    path = resolve(name)
    if mode == "append":
        content = content.rstrip("\n") + "\n"
    _check_size(content)

    with _locked(name):
        old = _read_text(path)
        new = (old + content) if mode == "append" else content
        if len(new.encode("utf-8")) > MAX_FILE_BYTES:
            raise MemoryServiceError(
                "file_too_large", "%s 将达 %d 字节，超过单文件上限" % (name, len(new.encode("utf-8"))), 413)

        if dry_run:
            return {"ok": True, "dry_run": True, "file": name, "mode": mode,
                    "bytes_before": len(old.encode("utf-8")),
                    "bytes_after": len(new.encode("utf-8")),
                    "diff": _diff(old, new)}

        backup = _backup(path)
        _atomic_write(path, new)
        _audit({"op": "write", "file": name, "mode": mode, "actor": actor,
                "bytes_before": len(old.encode("utf-8")), "bytes_after": len(new.encode("utf-8")),
                "backup": backup})
        return {"ok": True, "file": name, "mode": mode,
                "bytes_before": len(old.encode("utf-8")),
                "bytes_after": len(new.encode("utf-8")),
                "backup": backup}


def replace(name, old, new, allow_multiple=False, actor=None, dry_run=False):
    """
    替换片段。**找不到就报错，找到多处也报错** —— 静默替换零处或全替换
    都会产出「看起来改了其实没改」的结果，这类错误人读文档时看不出来。
    """
    old = _guard_content(old, "old")
    new = _guard_content(new, "new")
    if not old:
        raise MemoryServiceError("bad_request", "old 不能为空", 400)

    path = memory_path(name)
    with _locked(name):
        content = _read_text(path)
        occ = content.count(old)
        if occ == 0:
            raise MemoryServiceError(
                "not_found", "在 %s 中未找到待替换片段：%s" % (name, _snippet(old)), 409)
        if occ > 1 and not allow_multiple:
            where = [str(i + 1) for i, line in enumerate(content.split("\n")) if old in line][:10]
            raise MemoryServiceError(
                "ambiguous",
                "%s 中匹配到 %d 处（行 %s）。确认后传 allow_multiple=true 全部替换，"
                "或把 old 写长一点只匹配目标那处。" % (name, occ, ", ".join(where)), 409)

        updated = content.replace(old, new) if allow_multiple else content.replace(old, new, 1)
        replaced = occ if allow_multiple else 1

        if dry_run:
            return {"ok": True, "dry_run": True, "file": name, "replaced": replaced,
                    "diff": _diff(content, updated)}

        backup = _backup(path)
        _atomic_write(path, updated)
        _audit({"op": "replace", "file": name, "actor": actor, "replaced": replaced,
                "bytes_before": len(content.encode("utf-8")),
                "bytes_after": len(updated.encode("utf-8")), "backup": backup})
        return {"ok": True, "file": name, "replaced": replaced,
                "bytes_before": len(content.encode("utf-8")),
                "bytes_after": len(updated.encode("utf-8")), "backup": backup}


def insert(name, content, anchor=None, position=None, actor=None, dry_run=False):
    """
    插入条目。position：after_anchor / before_anchor / before_first_heading / bottom。

    不传 position 时按文件取默认：decisions.md 插在第一条之前（最新在上），
    lessons.md 插在「## 已记录」之后。CONTEXT.md / AGENTS.md 是规则文件，
    必须显式给 anchor —— 它们不是日志，结构由人维护。
    """
    content = _guard_content(content)
    if not content.strip():
        raise MemoryServiceError("bad_request", "content 不能为空", 400)
    if any(_L1_HEADING.match(line) for line in content.split("\n")):
        raise MemoryServiceError(
            "bad_request", "content 里有一级标题（# 开头），会破坏文件结构。记忆条目用 ## 开头", 400)

    if position is None:
        preset = DEFAULT_INSERT.get(name)
        if preset is None:
            raise MemoryServiceError(
                "need_anchor", "%s 是规则文件，插入必须显式指定 anchor 与 position" % name, 400)
        position, preset_anchor = preset
        anchor = anchor or preset_anchor

    if position not in ("after_anchor", "before_anchor", "before_first_heading", "bottom"):
        raise MemoryServiceError("bad_request", "position 取值非法：%s" % position, 400)
    if position in ("after_anchor", "before_anchor") and not anchor:
        raise MemoryServiceError("bad_request", "%s 需要 anchor 参数" % position, 400)
    if anchor and position in ("before_first_heading", "bottom"):
        raise MemoryServiceError("bad_request", "%s 不接受 anchor 参数" % position, 400)

    path = memory_path(name)
    with _locked(name):
        text = _read_text(path)
        lines = text.split("\n")
        body = content.strip().split("\n")
        _check_size("\n".join(body))

        if position in ("after_anchor", "before_anchor"):
            idx = _find_anchor(lines, anchor, name)
            if position == "after_anchor":
                at = idx + 1
                while at < len(lines) and not lines[at].strip():
                    at += 1          # 吃掉紧随的空行，插入后统一补
                block = [""] + body + [""]
            else:
                at = idx
                block = body + [""]
                if at > 0 and lines[at - 1].strip():
                    block = [""] + block
        elif position == "before_first_heading":
            at = next((i for i, line in enumerate(lines) if line.startswith("## ")), len(lines))
            block = body + [""]
            if at > 0 and lines[at - 1].strip():
                block = [""] + block
        else:  # bottom
            at = len(lines)
            while at > 0 and not lines[at - 1].strip():
                at -= 1
            block = [""] + body + [""]

        updated_lines = lines[:at] + block + lines[at:]
        updated = "\n".join(updated_lines)
        updated = re.sub(r"\n{3,}", "\n\n", updated)
        if not updated.endswith("\n"):
            updated += "\n"

        if dry_run:
            return {"ok": True, "dry_run": True, "file": name, "position": position,
                    "anchor": anchor, "inserted_at_line": at + 1,
                    "diff": _diff(text, updated)}

        backup = _backup(path)
        _atomic_write(path, updated)
        _audit({"op": "insert", "file": name, "actor": actor, "position": position,
                "anchor": anchor, "line": at + 1,
                "bytes_before": len(text.encode("utf-8")),
                "bytes_after": len(updated.encode("utf-8")), "backup": backup})
        return {"ok": True, "file": name, "position": position, "anchor": anchor,
                "inserted_at_line": at + 1,
                "bytes_before": len(text.encode("utf-8")),
                "bytes_after": len(updated.encode("utf-8")), "backup": backup}


def list_files():
    root = memory_root()
    items = []
    for name in ALLOWED_FILES:
        path = root / name
        if path.is_file():
            st = path.stat()
            items.append({
                "file": name,
                "bytes": st.st_size,
                "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
                "exists": True,
            })
        else:
            items.append({"file": name, "bytes": 0, "mtime": None, "exists": False})
    backups = root / BACKUP_DIRNAME
    return {
        "ok": True,
        "dir": str(root),
        "files": items,
        "backups": len(list(backups.glob("*.bak"))) if backups.is_dir() else 0,
    }


def inject():
    """
    拼 CONTEXT + AGENTS，供系统提示词注入。缺文件不报错 —— 启动注入失败
    就不该让整个 Agent 起不来，缺哪个在 files 里标出来。
    """
    parts, files = [], []
    for name in ("CONTEXT.md", "AGENTS.md"):
        path = memory_root() / name
        text = _read_text(path) if path.is_file() else ""
        files.append({"file": name, "found": bool(text)})
        if text.strip():
            parts.append(text.strip())
    return {"ok": True, "text": "\n\n---\n\n".join(parts), "files": files}


# ---------------------------------------------------------------- 工具函数


def _find_anchor(lines, anchor, name):
    target = anchor.strip()
    for i, line in enumerate(lines):
        if line.strip() == target:
            return i
    raise MemoryServiceError(
        "anchor_not_found", "在 %s 中未找到锚点行：%s" % (name, target), 409)


def _snippet(text, limit=60):
    one = " ".join(text.split())
    return one if len(one) <= limit else one[:limit] + "…"


#: 备份名：<文件>.<YYYYmmdd>-<HHMMSS>-<4位hex>.bak。整串匹配，不给路径穿越留缝。
_BACKUP_NAME = re.compile(r"^(?:\.backups/)?(.+\.\d{8}-\d{6}-[0-9a-f]{4}\.bak)$")


def rollback(name, backup):
    """
    从备份恢复。backup 传写操作返回的 backup 字段（形如 .backups/lessons.md.<时间戳>.bak）。

    用整串正则匹配而非「含 / 就拒」—— 后者会把自己返回的格式也拒掉。
    """
    m = _BACKUP_NAME.match(backup) if isinstance(backup, str) else None
    if not m or not m.group(1).startswith(name + "."):
        raise MemoryServiceError(
            "bad_request", "backup 必须是 %s 的备份文件名（形如 %s/%s.<时间戳>.bak）"
                           % (name, BACKUP_DIRNAME, name), 400)
    src = memory_root() / BACKUP_DIRNAME / m.group(1)
    if not src.is_file():
        raise MemoryServiceError("file_not_found", "备份不存在：%s" % backup, 404)
    path = memory_path(name)
    with _locked(name):
        old = _read_text(path)
        _backup(path)
        _atomic_write(path, src.read_text(encoding="utf-8"))
        _audit({"op": "rollback", "file": name, "from": backup})
    return {"ok": True, "file": name, "restored_from": backup,
            "bytes_before": len(old.encode("utf-8"))}
