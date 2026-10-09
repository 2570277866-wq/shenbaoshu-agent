# -*- coding: utf-8 -*-
"""
邮件自动接单 —— 客户发邮件到专用接单邮箱，本模块收信 → 解析 → 提交管线
→ 出稿后通知内部审核员（附 docx）。人工终审后由人发给客户，AI 只出初稿。

流程：
    守护线程每 EMAIL_POLL_INTERVAL 秒一轮：
    ① IMAP 收新邮件（BODY.PEEK 不置已读）→ 正文+附件拼成自由文本
       → parse_inputs 抽 13 字段 → 8 个必填齐全才 submit → ack 客户
       （缺必填 / 解析失败 → 回邮件列缺失项，不发单）
    ② 巡检 journal 里 state=submitted 的任务 → done 转 docx 通知审核人
       failed/interrupted 通知错误摘要

启用方式：EMAIL_ENABLED=1 且配置齐全（见 config()）。未启用时本模块静默，
Web 服务照常启动 —— 接单是旁路，不能因邮箱配错拖垮主业。

去重：UID 日志落 RUNS_DIR/email_journal.json（已被 .gitignore 覆盖）。
不依赖 IMAP \\Seen 标记 —— 客户邮箱里的原邮件保持未读。

已知边界（V1）：
    - QQ/163 的 SMTP/IMAP 密码是「授权码」，不是登录密码；且要在邮箱设置里
      手动开启 IMAP/SMTP 服务
    - 客户回复 ack 会被当成新邮件处理（大概率缺字段 → 收到「资料不全」回复）；
      无 References 追踪，后续版本可加
    - HTML-only 邮件剥标签后可能丢表格结构，解析质量下降（缺失回复兜底）

dry-run 单跑（不碰网络、不需要起服务）：
    .venv/bin/python email_intake.py --dry-run 样例.eml
"""

import argparse
import html
import io
import json
import os
import re
import sys
import threading
import time
import zipfile
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import formataddr, parseaddr
from html.parser import HTMLParser

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")
sys.path.insert(0, SCRIPTS)

import imaplib  # noqa: E402
import smtplib  # noqa: E402

import build_workflow  # noqa: E402
import md_to_docx  # noqa: E402
import parse_inputs  # noqa: E402

try:
    from pypdf import PdfReader
    _HAS_PYPDF = True
except ImportError:
    _HAS_PYPDF = False

# ---------------------------------------------------------------- 测试可替换点
# 与 main.ENGINE / main.PARSER 同构：测试时换成假实现。

IMAP4_SSL = imaplib.IMAP4_SSL
IMAP4 = imaplib.IMAP4
SMTP = smtplib.SMTP_SSL
SMTP_PLAIN = smtplib.SMTP
PARSER = parse_inputs.parse_inputs

REQUIRED_VARS = [v[0] for v in build_workflow.START_VARS if v[4]]  # 企业别名，保留兼容


def required_vars(profile="enterprise"):
    return [v[0] for v in build_workflow.get_profile(profile)["start_vars"] if v[4]]

_THREAD = None          # start() 起的守护线程；测试断言「默认不开线程」用

JOURNAL_NAME = "email_journal.json"

ACK_PREFIX = "【申报书接单】"
REVIEW_SUBJECT_PREFIX = "【待审核】申报书初稿"
DOCX_FILENAME = "申报书-%s.docx"


def _log(msg):
    print("[email-intake %s] %s" % (time.strftime("%H:%M:%S"), msg), file=sys.stderr)


# ---------------------------------------------------------------- 配置


def config(env=None):
    """读环境变量。未启用（EMAIL_ENABLED != "1"）或缺必填项 → None。

    None 不报错 —— start() 只打一条日志，Web 服务照常启动。
    """
    env = env or os.environ
    if env.get("EMAIL_ENABLED") != "1":
        return None

    def get(key, default=None):
        value = env.get(key)
        return value if value not in (None, "") else default

    cfg = {
        "imap_host": get("EMAIL_IMAP_HOST"),
        "imap_port": int(get("EMAIL_IMAP_PORT", 993)),
        "imap_ssl": get("EMAIL_IMAP_SSL", "1") != "0",
        "imap_user": get("EMAIL_IMAP_USER"),
        "imap_password": get("EMAIL_IMAP_PASSWORD"),
        "mailbox": get("EMAIL_MAILBOX", "INBOX"),
        "smtp_host": get("EMAIL_SMTP_HOST"),
        "smtp_port": int(get("EMAIL_SMTP_PORT", 465)),
        "smtp_ssl": get("EMAIL_SMTP_SSL", "1") != "0",
        "smtp_user": get("EMAIL_SMTP_USER") or get("EMAIL_IMAP_USER"),
        "smtp_password": get("EMAIL_SMTP_PASSWORD") or get("EMAIL_IMAP_PASSWORD"),
        "reviewer": get("EMAIL_REVIEWER"),
        "poll_interval": int(get("EMAIL_POLL_INTERVAL", 120)),
        "ack_prefix": get("EMAIL_ACK_SUBJECT_PREFIX", ACK_PREFIX),
        "max_attachment_mb": int(get("EMAIL_MAX_ATTACHMENT_MB", 5)),
        "max_attachments": int(get("EMAIL_MAX_ATTACHMENTS", 3)),
        "max_attach_chars": int(get("EMAIL_MAX_ATTACH_CHARS", 6000)),
        "max_parse_chars": int(get("EMAIL_MAX_PARSE_CHARS", 6000)),
    }
    missing = [k for k in ("imap_host", "imap_user", "imap_password",
                           "smtp_host", "reviewer") if not cfg[k]]
    if missing:
        _log("邮件接单配置不全，缺：%s —— 接单功能关闭" % ", ".join(missing))
        return None
    return cfg


def _dry_cfg():
    """--dry-run 用的兜底配置（不读邮箱、不发信，只有各种上限生效）。"""
    return {
        "imap_host": "", "imap_port": 993, "imap_ssl": True,
        "imap_user": "order@example.com", "imap_password": "",
        "mailbox": "INBOX",
        "smtp_host": "", "smtp_port": 465, "smtp_ssl": True,
        "smtp_user": "order@example.com", "smtp_password": "",
        "reviewer": "reviewer@example.com", "poll_interval": 120,
        "ack_prefix": ACK_PREFIX,
        "max_attachment_mb": 5, "max_attachments": 3,
        "max_attach_chars": 6000, "max_parse_chars": 6000,
    }


# ---------------------------------------------------------------- 启动与轮询


def start(submit, runs_dir):
    """配置齐全则起守护线程。返回是否启用。main.py 导入本模块时调用。"""
    global _THREAD
    cfg = config()
    if not cfg:
        _log("邮件接单未启用（EMAIL_ENABLED != 1 或配置不全），跳过")
        return False
    _THREAD = threading.Thread(
        target=_poll_loop, args=(cfg, submit, runs_dir),
        daemon=True, name="email-poll-watch")
    _THREAD.start()
    _log("邮件接单已启用：%s 每 %ds 一轮，审核人 %s"
         % (cfg["imap_user"], cfg["poll_interval"], cfg["reviewer"]))
    return True


def _poll_loop(cfg, submit, runs_dir):
    while True:
        try:
            poll_once(cfg, submit, runs_dir)
            check_completions(cfg, runs_dir)
        except Exception as exc:  # 轮询线程永不退出：邮箱挂了只记日志等下轮
            _log("本轮异常，下轮重试：%s: %s" % (type(exc).__name__, exc))
        time.sleep(cfg["poll_interval"])


def _journal_path(runs_dir):
    return os.path.join(runs_dir, JOURNAL_NAME)


def poll_once(cfg, submit, runs_dir):
    """一轮收信：IMAP 取新 UID → 逐封处理。返回处理数。单封异常不中断。"""
    path = _journal_path(runs_dir)
    journal = load_journal(path)
    cls = IMAP4_SSL if cfg["imap_ssl"] else IMAP4
    imap = cls(cfg["imap_host"], cfg["imap_port"])
    try:
        imap.login(cfg["imap_user"], cfg["imap_password"])
        uv = _uidvalidity(imap, cfg["mailbox"])
        if uv is not None and journal["uidvalidity"] != uv:
            if journal["uidvalidity"] is not None:
                _log("邮箱 UIDVALIDITY 变化（%s → %s）—— 去重记录清空重来"
                     % (journal["uidvalidity"], uv))
            journal["uidvalidity"] = uv
            journal["entries"] = {}
        _typ, data = imap.select(cfg["mailbox"])
        _typ, data = imap.uid("search", None, "ALL")
        uids = [u for u in data[0].split() if u] if data and data[0] else []
        new = [u for u in uids if u.decode() not in journal["entries"]]
        if new:
            _log("新邮件 %d 封" % len(new))
        for uid in new:
            handle_uid(imap, uid.decode(), cfg, submit, journal)
        save_journal(path, journal)
        return len(new)
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def _uidvalidity(imap, mailbox):
    _typ, data = imap.uid("status", mailbox, "(UIDVALIDITY)")
    if not data or not data[0]:
        return None
    m = re.search(rb"UIDVALIDITY\s+(\d+)", data[0])
    return int(m.group(1)) if m else None


def handle_uid(imap, uid, cfg, submit, journal):
    """取一封邮件 → 解析处理 → 写 journal。任何异常都吞掉、单封不中断。"""
    entry = {"uid": uid, "state": "error"}
    try:
        _typ, data = imap.uid("fetch", uid, "(BODY.PEEK[])")
        raw = _fetch_bytes(data)
        if raw is None:
            _log("UID %s fetch 结果解析不了，跳过" % uid)
            entry["error"] = "fetch 结果解析不了"
            journal["entries"][uid] = entry
            return
        entry = process_message(raw, cfg, submit)
    except Exception as exc:
        entry["error"] = "%s: %s" % (type(exc).__name__, exc)
        _log("UID %s 处理异常（继续下一封）：%s" % (uid, entry["error"]))
    journal["entries"][uid] = entry
    return entry


def _fetch_bytes(data):
    """imaplib uid fetch 返回结构不统一：兼容 (头标注, 正文, …) 元组与裸 bytes。"""
    if not data:
        return None
    item = data[0]
    if isinstance(item, tuple):
        for part in item:
            # 头标注以 "(" 开头且含 BODY[；正文是 email 原文，不可能是这个形态
            if isinstance(part, bytes) and not part.startswith(b"(") \
                    and b"BODY[" not in part[:20]:
                return part
        return None
    return item if isinstance(item, bytes) else None


# ---------------------------------------------------------------- 单封处理


def process_message(raw, cfg, submit, send=None):
    """一封邮件 bytes → {state, ...}。不抛异常（handle_uid 兜底）。

    send 参数：dry-run 与测试注入不发信的实现；默认用模块级 send_mail。
    """
    if send is None:
        send = send_mail
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    from_addr, from_name = _sender(msg)
    subject = str(msg.get("Subject") or "(无主题)").strip()

    entry = {"from": from_addr, "from_name": from_name, "subject": subject,
             "received_at": time.strftime("%Y-%m-%d %H:%M:%S")}

    auto = msg.get("Auto-Submitted")
    if auto and str(auto).strip().lower() != "no":
        entry["state"] = "skipped"
        entry["reason"] = "自动回复邮件，跳过（Auto-Submitted: %s）" % auto
        return entry
    return_path = msg.get("Return-Path")
    if return_path is not None and str(return_path).strip() in ("", "<>"):
        entry["state"] = "skipped"
        entry["reason"] = "退信（Return-Path 为空），跳过"
        return entry

    body = extract_body(msg)
    attachments, attach_notes = extract_attachments(msg, cfg)
    merged = _merge_text(body, attachments, cfg["max_parse_chars"])

    result = PARSER(merged, profile="enterprise")  # 邮件接单只接企业单
    fields = result.get("fields") or {}
    ok, missing_labels, overlong = _validate(fields)
    notes = list(attach_notes) + [
        "字段「%s」超过长度上限，已截断" % label for label in overlong]

    if not ok or result.get("error"):
        entry["state"] = "replied_missing"
        entry["missing"] = missing_labels
        reply = build_missing_reply(cfg, subject, missing_labels,
                                    result.get("parse_error"), from_addr)
        send(cfg, reply)
        return entry

    job = submit(fields, profile="enterprise")  # 邮件接单只接企业单
    entry["state"] = "submitted"
    entry["run_id"] = job["id"]
    entry["project_name"] = fields.get("project_name") or ""
    send(cfg, build_ack(cfg, subject, job["id"], fields, notes, from_addr))
    return entry


def _sender(msg):
    from_header = msg.get("From")
    addresses = getattr(from_header, "addresses", None)
    if addresses:
        first = addresses[0]
        return ((getattr(first, "addr_spec", None) or "").strip(),
                (getattr(first, "display_name", None) or "").strip())
    text = str(from_header or "")
    name, addr = parseaddr(text)
    return addr.strip(), name.strip()


def _merge_text(body, attachments, max_chars):
    parts = [body] if body else []
    for i, (name, text) in enumerate(attachments, 1):
        parts.append("【邮件附件内容】附件%d（%s）：\n%s" % (i, name, text))
    merged = "\n\n".join(p for p in parts if p).strip()
    if len(merged) > max_chars:
        merged = merged[:max_chars] + "…（内容过长已截断）"
    return merged


# ---------------------------------------------------------------- 正文与附件提取


def extract_body(msg):
    """text/plain 优先；没有就 text/html 剥标签。"""
    plain_parts, html_parts = [], []
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype == "text/plain":
                plain_parts.append(part)
            elif ctype == "text/html":
                html_parts.append(part)
    else:
        part = msg
        (plain_parts if msg.get_content_type() == "text/plain"
         else html_parts).append(part)

    if plain_parts:
        texts = [_decode_part(p) for p in plain_parts]
        return "\n".join(t for t in texts if t).strip()
    if html_parts:
        texts = [html_to_text(_decode_part(p)) for p in html_parts]
        return "\n".join(t for t in texts if t).strip()
    return ""


def _decode_part(part):
    data = part.get_payload(decode=True) or b""
    charset = part.get_content_charset() or "utf-8"
    try:
        return data.decode(charset)
    except (LookupError, UnicodeDecodeError):
        return data.decode("utf-8", errors="replace")


class _HTMLText(HTMLParser):
    """HTML → 纯文本：跳过 script/style/head，块级标签换行。"""

    _BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
              "table", "section", "article"}

    def __init__(self):
        super().__init__()
        self.parts = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head"):
            self._skip += 1
        elif tag in self._BLOCK and self.parts and not self.parts[-1].endswith("\n"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head") and self._skip:
            self._skip -= 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)

    def text(self):
        return re.sub(r"\n{3,}", "\n\n", "".join(self.parts)).strip()


def html_to_text(html_text):
    parser = _HTMLText()
    parser.feed(html_text)
    return parser.text()


def extract_attachments(msg, cfg):
    """附件 → [(文件名, 文本)] + 说明列表。超限/不支持的在说明里交代。"""
    notes, found = [], []
    if not msg.is_multipart():
        return found, notes
    for part in msg.walk():
        filename = part.get_filename()
        if not filename:
            continue
        ext = os.path.splitext(filename)[1].lower()
        if ext not in (".txt", ".docx", ".pdf"):
            notes.append("附件「%s」类型不支持（仅 .txt/.docx/.pdf），已忽略" % filename)
            continue
        if len(found) >= cfg["max_attachments"]:
            notes.append("附件「%s」超出 %d 个上限，已忽略"
                         % (filename, cfg["max_attachments"]))
            continue
        data = part.get_payload(decode=True) or b""
        limit = cfg["max_attachment_mb"] * 1024 * 1024
        if len(data) > limit:
            notes.append("附件「%s」超过 %dMB，已忽略"
                         % (filename, cfg["max_attachment_mb"]))
            continue
        text = text_from_attachment(data, ext)
        if not text and ext == ".pdf" and not _HAS_PYPDF:
            notes.append("附件「%s」是 PDF，但服务端没装 pypdf，未解析" % filename)
            continue
        if len(text) > cfg["max_attach_chars"]:
            text = text[:cfg["max_attach_chars"]] + "…（附件内容过长已截断）"
        found.append((filename, text))
    return found, notes


def text_from_attachment(data, ext):
    if ext == ".txt":
        return text_from_txt(data)
    if ext == ".docx":
        return text_from_docx(data)
    if ext == ".pdf":
        return text_from_pdf(data)
    return ""


def text_from_txt(data):
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            return data.decode(enc).strip()
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace").strip()


def text_from_docx(data):
    """zipfile 直读 document.xml 抽 <w:t> 文本 —— 表格里的也抽得到，零依赖。"""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            xml = zf.read("word/document.xml").decode("utf-8", errors="replace")
    except (zipfile.BadZipFile, KeyError):
        return ""
    xml = re.sub(r"</w:p>", "\n", xml)          # 段落边界换行
    text = re.sub(r"<[^>]+>", "", xml)           # 剥全部标签
    return html.unescape(text).strip()


def text_from_pdf(data):
    if not _HAS_PYPDF:
        return ""
    try:
        reader = PdfReader(io.BytesIO(data))
        pages = [(page.extract_text() or "") for page in reader.pages]
        return "\n".join(pages).strip()
    except Exception:
        return ""


# ---------------------------------------------------------------- 字段校验


def _validate(fields, profile="enterprise"):
    """对照 profile 的 start_vars 单一真相源：必填缺失 → 中文标签清单；超长 → 就地截断。"""
    labels = build_workflow.var_labels(profile)
    start_vars = build_workflow.get_profile(profile)["start_vars"]
    missing = [labels[v] for v in required_vars(profile)
               if fields.get(v) in ("", 0, None)]
    overlong = []
    for var, _label, _ftype, max_len, _req, _opts in start_vars:
        value = fields.get(var)
        if max_len and isinstance(value, str) and len(value) > max_len:
            fields[var] = value[:max_len] + "…（截断）"
            overlong.append(labels[var])
    return (not missing), missing, overlong


# ---------------------------------------------------------------- 邮件构建与发送


def _reply_subject(prefix, original):
    return "%s Re: %s" % (prefix, original if original.startswith("Re:") else original)


def _base_message(cfg, to_addr, subject):
    msg = EmailMessage()
    msg["From"] = formataddr(("申报书接单服务", cfg["smtp_user"]))
    msg["To"] = to_addr
    msg["Subject"] = subject
    return msg


def build_ack(cfg, original_subject, run_id, fields, notes, customer, profile="enterprise"):
    subject = _reply_subject(cfg["ack_prefix"], original_subject)
    body = [
        "您好，已接单。系统正在自动撰写初稿（任务号 %s）。" % run_id,
        "预计 30–60 分钟内完成初稿（任务排队时可能更久）。",
        "出稿后由人工审核，审核完成后与您联系。",
        "",
        "系统识别到的关键信息（如有误请直接回复本邮件纠正）：",
    ]
    labels = build_workflow.var_labels(profile)
    for var in required_vars(profile):
        label = labels[var]
        value = fields.get(var)
        body.append("- %s：%s" % (label, value if value not in ("", 0, None) else "（未识别）"))
    if notes:
        body.append("")
        body.append("附件说明：")
        body.extend("- %s" % n for n in notes)
    body.append("")
    body.append("（本邮件为系统自动发送）")
    msg = _base_message(cfg, customer, subject)
    msg.set_content("\n".join(body), subtype="plain", charset="utf-8")
    return msg


def build_missing_reply(cfg, original_subject, missing_labels, parse_error, customer):
    subject = _reply_subject(cfg["ack_prefix"], original_subject)
    body = [
        "您好，您的申报需求邮件已收到，但以下信息缺失或无法识别，暂时无法开始撰写：",
    ]
    body.extend("- %s" % label for label in missing_labels)
    if parse_error:
        body.append("")
        body.append("（解析失败：%s，请把信息直接写在邮件正文里再发一次）" % parse_error)
    body.append("")
    body.append("请回复本邮件补充以上信息，收到后自动开始撰写。")
    body.append("（本邮件为系统自动发送）")
    msg = _base_message(cfg, customer, subject)
    msg.set_content("\n".join(body), subtype="plain", charset="utf-8")
    return msg


def build_reviewer_done(cfg, run_id, project_name, customer, original_subject,
                        state, docx_bytes):
    subject = "%s：《%s》（%s）" % (REVIEW_SUBJECT_PREFIX, project_name or "未命名", run_id)
    body = [
        "邮件接单的申报书初稿已完成，请人工审核后交付客户。",
        "",
        "- 任务号：%s" % run_id,
        "- 项目名称：%s" % (project_name or "（未识别）"),
        "- 客户邮箱：%s" % customer,
        "- 客户原邮件主题：%s" % original_subject,
        "- 一致性审查：%s（blocking %d）" % (
            "通过" if state.get("pass") else "有 warning",
            state.get("blocking") or 0),
        "- 结果页：/runs/%s/result" % run_id,
        "",
        "docx 初稿见附件。",
    ]
    msg = _base_message(cfg, cfg["reviewer"], subject)
    msg.set_content("\n".join(body), subtype="plain", charset="utf-8")
    msg.add_attachment(
        docx_bytes,
        maintype="application",
        subtype="vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=DOCX_FILENAME % (project_name or run_id))
    return msg


def build_reviewer_failed(cfg, run_id, project_name, customer, original_subject, state):
    subject = "【申报书接单】任务失败：《%s》（%s）" % (project_name or "未命名", run_id)
    body = [
        "邮件接单的任务失败了，需要人工介入：",
        "",
        "- 任务号：%s" % run_id,
        "- 项目名称：%s" % (project_name or "（未识别）"),
        "- 客户邮箱：%s" % customer,
        "- 客户原邮件主题：%s" % original_subject,
        "- 失败原因：%s" % (state.get("error") or "未知"),
        "",
        "客户还没有收到失败通知，请联系客户说明并重新安排。",
    ]
    msg = _base_message(cfg, cfg["reviewer"], subject)
    msg.set_content("\n".join(body), subtype="plain", charset="utf-8")
    return msg


def build_docx_bytes(document_md, title):
    """Markdown → docx 内存 bytes。与 main.py /api/runs/{id}/docx 同一条路径。"""
    doc = md_to_docx.convert(document_md, title=title or "项目申报书")
    buf = io.BytesIO()
    md_to_docx.save(doc, buf)
    return buf.getvalue()


def send_mail(cfg, msg):
    """发一封邮件。失败抛异常 —— 调用方决定重试还是记日志。"""
    cls = SMTP if cfg["smtp_ssl"] else SMTP_PLAIN
    smtp = cls(cfg["smtp_host"], cfg["smtp_port"])
    try:
        smtp.login(cfg["smtp_user"], cfg["smtp_password"])
        smtp.send_message(msg)
    finally:
        try:
            smtp.quit()
        except Exception:
            pass


# ---------------------------------------------------------------- 完成巡检


def check_completions(cfg, runs_dir):
    """巡检 journal 里 state=submitted 的任务，终态 → 通知审核人。返回通知数。"""
    path = _journal_path(runs_dir)
    journal = load_journal(path)
    notified = 0
    for uid, entry in list(journal["entries"].items()):
        if entry.get("state") != "submitted":
            continue
        run_id = entry.get("run_id")
        state_path = os.path.join(runs_dir, run_id, "state.json")
        if not os.path.exists(state_path):
            continue  # 任务目录还没建好，下轮再看
        try:
            with open(state_path, encoding="utf-8") as fh:
                state = json.load(fh)
        except (ValueError, OSError):
            continue

        try:
            if state.get("state") == "done":
                doc_path = os.path.join(runs_dir, run_id, "document.md")
                if not os.path.exists(doc_path):
                    _log("%s done 但 document.md 缺失，下轮重试" % run_id)
                    continue
                with open(doc_path, encoding="utf-8") as fh:
                    text = fh.read()
                docx = build_docx_bytes(text, state.get("project_name") or "项目申报书")
                msg = build_reviewer_done(
                    cfg, run_id, state.get("project_name"), entry.get("from", ""),
                    entry.get("subject", ""), state, docx)
                send_mail(cfg, msg)
                entry["state"] = "done_notified"
            elif state.get("state") in ("failed", "interrupted"):
                msg = build_reviewer_failed(
                    cfg, run_id, state.get("project_name"), entry.get("from", ""),
                    entry.get("subject", ""), state)
                send_mail(cfg, msg)
                entry["state"] = "failed_notified"
            else:
                continue  # queued/running，继续等
            entry["notified_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            notified += 1
        except Exception as exc:
            # 通知失败不标 notified —— 下轮重试
            _log("%s 通知审核人失败，下轮重试：%s: %s"
                 % (run_id, type(exc).__name__, exc))
    if notified:
        save_journal(path, journal)
    return notified


# ---------------------------------------------------------------- journal


def load_journal(path):
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            return {"uidvalidity": data.get("uidvalidity"),
                    "entries": data.get("entries") or {}}
        except (ValueError, OSError):
            _log("journal 读不出来，重建：%s" % path)
    return {"uidvalidity": None, "entries": {}}


def save_journal(path, journal):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(journal, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ---------------------------------------------------------------- dry-run CLI


def main(argv=None):
    parser = argparse.ArgumentParser(description="邮件接单 dry-run：解析 .eml 不碰网络")
    parser.add_argument("--dry-run", metavar="EML", required=True,
                        help="本地 .eml 文件路径")
    args = parser.parse_args(argv)
    with open(args.dry_run, "rb") as fh:
        raw = fh.read()
    cfg = config() or _dry_cfg()
    if cfg.get("imap_host") == "":
        print("（未配置 EMAIL_*，用 dry-run 默认上限；解析会尝试连本机 Ollama）")
    submitted = []

    def fake_submit(fields, profile="enterprise"):
        submitted.append(fields)
        return {"id": "run_dryrun"}

    def dry_send(_cfg, msg):
        print("\n[dry-run] 邮件草稿（不发）：To=%s 主题=%s"
              % (msg["To"], msg["Subject"]))
        body = msg.get_body(preferencelist=("plain",))
        if body:
            print(body.get_content())

    entry = process_message(raw, cfg, fake_submit, send=dry_send)
    print(json.dumps(entry, ensure_ascii=False, indent=2))
    if submitted:
        print("\n[dry-run] 将提交的字段：")
        print(json.dumps(submitted[0], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
