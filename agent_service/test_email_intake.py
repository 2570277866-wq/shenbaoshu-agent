# -*- coding: utf-8 -*-
"""
email_intake 单测 —— 假 IMAP / 假 SMTP / 假解析器 / 假 submit，不碰网络。

对齐 test_service.py 约定：模块级可替换点直接 swap
（email_intake.IMAP4_SSL / SMTP / PARSER），poll_once 同步直调不碰线程。
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from email.message import EmailMessage
from email.utils import formataddr

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import email_intake  # noqa: E402

# ---------------------------------------------------------------- 假件


class FakeIMAP:
    """按 uid 剧本回放邮件。uid 为字符串键。"""

    def __init__(self, host, port):
        self.host, self.port = host, port
        self.uids = {}          # uid(str) -> raw bytes
        self.uidvalidity = 100
        self.fail_on_uid = {}   # uid -> Exception（模拟单封坏邮件）
        self.login_calls = []

    def login(self, user, password):
        self.login_calls.append((user, password))
        return ("OK", [b""])

    def select(self, mailbox):
        return ("OK", [b"1"])

    def uid(self, cmd, *args):
        if cmd == "status":
            return ("OK", [b"INBOX (UIDVALIDITY %d UIDNEXT 99)" % self.uidvalidity])
        if cmd == "search":
            uids = " ".join(sorted(self.uids, key=int)) if self.uids else ""
            return ("OK", [uids.encode()])
        if cmd == "fetch":
            uid = args[0]
            if uid in self.fail_on_uid:
                raise self.fail_on_uid[uid]
            raw = self.uids.get(uid)
            if raw is None:
                return ("OK", [None])
            head = b"%s (UID %s BODY[] {%d}" % (uid.encode(), uid.encode(), len(raw))
            return ("OK", [(head, raw), b")"])
        raise AssertionError("FakeIMAP 不认识的命令：%r %r" % (cmd, args))

    def logout(self):
        return ("OK", [b""])


class FakeSMTP:
    sent = []  # 每测试重置

    def __init__(self, host, port):
        self.host, self.port = host, port

    def login(self, user, password):
        return None

    def send_message(self, msg):
        self.sent.append(msg)

    def quit(self):
        pass


def msg_plain(msg):
    body = msg.get_body(preferencelist=("plain",))
    return body.get_content() if body else ""


def msg_attachments(msg):
    return [(a.get_filename(), a.get_payload(decode=True) or b"")
            for a in msg.iter_attachments()]


# ---------------------------------------------------------------- 工具

FULL_FIELDS = {
    "project_name": "智慧园区能耗监测平台",
    "declaration_type": "科技型中小企业",
    "tech_direction": "物联网与边缘计算",
    "project_leader": "张三",
    "team_size": 12,
    "budget_range": "180万-220万",
    "expected_outcome": "形成软件 1 套、专利 2 项。",
    "project_highlights": "模型体积小于 50MB，检测精度 95%。",
    "special_requirements": "",
    "material_tech": "检测精度 95.2%",
    "material_ip": "发明专利 1 项",
    "material_finance": "上年度研发投入 200 万",
    "style_input": "",
}

CUSTOMER = "customer@example.com"
ORDER_ADDR = "order@example.com"


def make_cfg(**over):
    cfg = {
        "imap_host": "imap.test", "imap_port": 993, "imap_ssl": True,
        "imap_user": ORDER_ADDR, "imap_password": "x", "mailbox": "INBOX",
        "smtp_host": "smtp.test", "smtp_port": 465, "smtp_ssl": True,
        "smtp_user": ORDER_ADDR, "smtp_password": "x",
        "reviewer": "reviewer@example.com", "poll_interval": 120,
        "ack_prefix": "【申报书接单】",
        "max_attachment_mb": 5, "max_attachments": 3,
        "max_attach_chars": 6000, "max_parse_chars": 6000,
    }
    cfg.update(over)
    return cfg


def make_email(subject, body, sender=CUSTOMER, attachments=(), headers=None,
               html=None):
    msg = EmailMessage()
    msg["From"] = formataddr(("客户", sender))
    msg["To"] = ORDER_ADDR
    msg["Subject"] = subject
    for key, value in (headers or {}).items():
        msg[key] = value
    if html is not None:
        msg.set_content(html, subtype="html", charset="utf-8")
    else:
        msg.set_content(body, subtype="plain", charset="utf-8")
    for fname, data, main, sub in attachments:
        msg.add_attachment(data, maintype=main, subtype=sub, filename=fname)
    return msg.as_bytes()


def make_docx_bytes(paragraphs):
    from docx import Document
    doc = Document()
    for text in paragraphs:
        doc.add_paragraph(text)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def make_pdf_bytes(text):
    """手搓最小合法 PDF（含 xref），pypdf 能抽出 text。"""
    content = ("BT /F1 12 Tf 72 720 Td (%s) Tj ET" % text).encode()
    bodies = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
         b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>"),
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = [0]
    for i, body in enumerate(bodies, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (i, body)
    xref = len(out)
    out += b"xref\n0 6\n0000000000 65535 f \n"
    for off in offsets[1:]:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF" % xref
    return out


# ---------------------------------------------------------------- 测试


class TestEmailIntake(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="email_intake_test_")
        self.runs = self.tmp.name
        self.submitted = []
        self.cfg = make_cfg()
        os.environ.pop("EMAIL_ENABLED", None)   # 防 shell 环境污染「默认关闭」测试
        # swap 模块级可替换点，tearDown 还原
        self._old_imap, self._old_smtp, self._old_parser = (
            email_intake.IMAP4_SSL, email_intake.SMTP, email_intake.PARSER)
        email_intake.IMAP4_SSL = self.fake_imap([])
        email_intake.SMTP = FakeSMTP
        FakeSMTP.sent = []

    def tearDown(self):
        email_intake.IMAP4_SSL = self._old_imap
        email_intake.SMTP = self._old_smtp
        email_intake.PARSER = self._old_parser
        self.tmp.cleanup()

    # ---- 工具

    def fake_imap(self, messages, **kw):
        """返回工厂：poll_once 每次轮询新起连接时按剧本造 FakeIMAP。"""
        def factory(host, port):
            imap = FakeIMAP(host, port)
            for uid, raw in enumerate(messages, 1):
                imap.uids[str(uid)] = raw
            for key, value in kw.items():
                setattr(imap, key, value)
            return imap
        return factory

    def fake_submit(self, fields, profile="enterprise"):
        self.submitted.append((fields, profile))
        return {"id": "run_test1"}

    def fake_parser(self, fields=None, error=None, parse_error=None,
                    record=None):
        def parser(text, profile="enterprise"):
            if record is not None:
                record.append((text, profile))
            if error:
                empty = {v[0]: "" for v in email_intake.build_workflow.START_VARS}
                return {"fields": empty, "missing": [], "raw": text,
                        "error": error, "parse_error": parse_error}
            result = {"fields": dict(FULL_FIELDS), "missing": [], "raw": text}
            if fields is not None:
                result["fields"].update(fields)
            return result
        return parser

    def load_journal(self):
        path = os.path.join(self.runs, email_intake.JOURNAL_NAME)
        return email_intake.load_journal(path)

    # ---- 启用/关闭

    def test_disabled_without_env(self):
        """EMAIL_ENABLED 没设 → start 返回 False，不开线程。"""
        self.assertFalse(email_intake.start(self.fake_submit, self.runs))
        self.assertIsNone(email_intake._THREAD)

    def test_start_missing_creds_returns_false(self):
        """EMAIL_ENABLED=1 但缺必填配置 → config None、不开线程。"""
        os.environ["EMAIL_ENABLED"] = "1"
        try:
            self.assertIsNone(email_intake.config())
            self.assertFalse(email_intake.start(self.fake_submit, self.runs))
            self.assertIsNone(email_intake._THREAD)
        finally:
            os.environ.pop("EMAIL_ENABLED", None)

    # ---- 收信主流程

    def test_happy_path_submits_and_acks(self):
        raw = make_email("申报需求", "项目名称：智慧园区能耗监测平台\n团队 12 人，预算 200 万")
        email_intake.PARSER = self.fake_parser()
        email_intake.IMAP4_SSL = self.fake_imap([raw])

        self.assertEqual(email_intake.poll_once(self.cfg, self.fake_submit, self.runs), 1)

        self.assertEqual(len(self.submitted), 1)
        self.assertEqual(set(self.submitted[0][0]), set(FULL_FIELDS))  # 13 键全
        self.assertEqual(self.submitted[0][1], "enterprise")  # 邮件接单固定企业单
        sent = FakeSMTP.sent
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["To"], CUSTOMER)
        self.assertIn("已接单", msg_plain(sent[0]))
        self.assertIn("run_test1", msg_plain(sent[0]))
        self.assertIn("识别到的关键信息", msg_plain(sent[0]))
        entry = self.load_journal()["entries"]["1"]
        self.assertEqual(entry["state"], "submitted")
        self.assertEqual(entry["run_id"], "run_test1")

    def test_dedup_same_uid_twice(self):
        raw = make_email("申报需求", "项目名称：测试项目")
        email_intake.PARSER = self.fake_parser()
        email_intake.IMAP4_SSL = self.fake_imap([raw])

        email_intake.poll_once(self.cfg, self.fake_submit, self.runs)
        email_intake.poll_once(self.cfg, self.fake_submit, self.runs)

        self.assertEqual(len(self.submitted), 1)   # journal 去重

    def test_missing_required_replies_no_submit(self):
        raw = make_email("申报需求", "只提了项目名称，别的都没说")
        email_intake.PARSER = self.fake_parser(fields={"budget_range": "",
                                                        "team_size": 12})
        email_intake.IMAP4_SSL = self.fake_imap([raw])

        email_intake.poll_once(self.cfg, self.fake_submit, self.runs)

        self.assertEqual(self.submitted, [])
        sent = FakeSMTP.sent
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["To"], CUSTOMER)
        self.assertIn("预算规模", msg_plain(sent[0]))   # 缺失项中文标签
        self.assertIn("暂时无法开始撰写", msg_plain(sent[0]))
        self.assertEqual(self.load_journal()["entries"]["1"]["state"],
                         "replied_missing")

    def test_parse_error_replies_no_submit(self):
        raw = make_email("申报需求", "正文随意")
        email_intake.PARSER = self.fake_parser(
            error="parse_failed", parse_error="ValueError: 输出里没有 JSON")
        email_intake.IMAP4_SSL = self.fake_imap([raw])

        email_intake.poll_once(self.cfg, self.fake_submit, self.runs)

        self.assertEqual(self.submitted, [])
        self.assertEqual(len(FakeSMTP.sent), 1)
        self.assertIn("解析失败", msg_plain(FakeSMTP.sent[0]))

    def test_team_size_zero_counts_missing(self):
        raw = make_email("申报需求", "正文")
        email_intake.PARSER = self.fake_parser(fields={"team_size": 0})
        email_intake.IMAP4_SSL = self.fake_imap([raw])

        email_intake.poll_once(self.cfg, self.fake_submit, self.runs)

        self.assertEqual(self.submitted, [])
        self.assertIn("投入人数", msg_plain(FakeSMTP.sent[0]))

    def test_overlong_field_truncated_and_noted(self):
        raw = make_email("申报需求", "正文")
        email_intake.PARSER = self.fake_parser(
            fields={"project_name": "长" * 300})
        email_intake.IMAP4_SSL = self.fake_imap([raw])

        email_intake.poll_once(self.cfg, self.fake_submit, self.runs)

        self.assertEqual(len(self.submitted), 1)
        self.assertLessEqual(len(self.submitted[0][0]["project_name"]), 200 + len("…（截断）"))
        self.assertIn("项目名称", msg_plain(FakeSMTP.sent[0]))
        self.assertIn("已截断", msg_plain(FakeSMTP.sent[0]))

    # ---- 附件

    def test_attachment_txt_merged_into_parse(self):
        recorded = []
        raw = make_email(
            "申报需求", "正文：项目名称 XX",
            attachments=[("素材.txt", "检测精度 95.2%\n发明专利 1 项".encode("utf-8"),
                          "text", "plain")])
        email_intake.PARSER = self.fake_parser(record=recorded)
        email_intake.IMAP4_SSL = self.fake_imap([raw])

        email_intake.poll_once(self.cfg, self.fake_submit, self.runs)

        text = recorded[0][0]
        self.assertIn("检测精度 95.2%", text)
        self.assertIn("【邮件附件内容】附件1（素材.txt）", text)

    def test_attachment_docx_extracted(self):
        recorded = []
        raw = make_email(
            "申报需求", "正文：项目名称 XX",
            attachments=[("素材.docx",
                          make_docx_bytes(["技术参数：检测精度 95.2%", "发明专利 1 项"]),
                          "application",
                          "vnd.openxmlformats-officedocument.wordprocessingml.document")])
        email_intake.PARSER = self.fake_parser(record=recorded)
        email_intake.IMAP4_SSL = self.fake_imap([raw])

        email_intake.poll_once(self.cfg, self.fake_submit, self.runs)

        text = recorded[0][0]
        self.assertIn("检测精度 95.2%", text)
        self.assertIn("发明专利 1 项", text)

    def test_attachment_pdf_extracted(self):
        recorded = []
        raw = make_email(
            "申报需求", "正文：项目名称 XX",
            attachments=[("素材.pdf", make_pdf_bytes("MATERIAL_ABC"), "application", "pdf")])
        email_intake.PARSER = self.fake_parser(record=recorded)
        email_intake.IMAP4_SSL = self.fake_imap([raw])

        email_intake.poll_once(self.cfg, self.fake_submit, self.runs)

        self.assertIn("MATERIAL_ABC", recorded[0][0])

    def test_attachment_limits(self):
        raw = make_email(
            "申报需求", "正文",
            attachments=[("a.txt", b"1", "text", "plain"),
                         ("b.txt", b"2", "text", "plain"),
                         ("c.txt", b"3", "text", "plain"),
                         ("d.txt", b"4", "text", "plain")])
        email_intake.PARSER = self.fake_parser()
        email_intake.IMAP4_SSL = self.fake_imap([raw])

        email_intake.poll_once(self.cfg, self.fake_submit, self.runs)

        ack = msg_plain(FakeSMTP.sent[0])
        self.assertIn("超出 3 个上限", ack)          # 第 4 个被忽略并注明
        self.assertIn("d.txt", ack)
        self.assertNotIn("（d.txt）：", ack)          # 没进正文合并块

    def test_attachment_oversize_skipped(self):
        raw = make_email(
            "申报需求", "正文",
            attachments=[("big.txt", b"x" * (6 * 1024 * 1024), "text", "plain")])
        email_intake.PARSER = self.fake_parser()
        email_intake.IMAP4_SSL = self.fake_imap([raw])

        email_intake.poll_once(self.cfg, self.fake_submit, self.runs)

        self.assertIn("超过 5MB", msg_plain(FakeSMTP.sent[0]))

    # ---- 正文提取与跳过规则

    def test_html_only_body_stripped(self):
        recorded = []
        html = ("<html><head><style>body{}</style></head><body>"
                "<p>项目名称：智慧园区</p><p>预算 200 万</p>"
                "<script>alert(1)</script></body></html>")
        raw = make_email("申报需求", "", html=html)
        email_intake.PARSER = self.fake_parser(record=recorded)
        email_intake.IMAP4_SSL = self.fake_imap([raw])

        email_intake.poll_once(self.cfg, self.fake_submit, self.runs)

        text = recorded[0][0]
        self.assertIn("项目名称：智慧园区", text)
        self.assertNotIn("alert", text)      # script 剥掉
        self.assertNotIn("body{}", text)     # style 剥掉

    def test_auto_submitted_skipped(self):
        raw = make_email("自动回复", "收到", headers={"Auto-Submitted": "auto-replied"})
        email_intake.PARSER = self.fake_parser()
        email_intake.IMAP4_SSL = self.fake_imap([raw])

        email_intake.poll_once(self.cfg, self.fake_submit, self.runs)

        self.assertEqual(self.submitted, [])
        self.assertEqual(FakeSMTP.sent, [])
        self.assertEqual(self.load_journal()["entries"]["1"]["state"], "skipped")

    def test_bounce_skipped(self):
        raw = make_email("退信", "", headers={"Return-Path": "<>"})
        email_intake.PARSER = self.fake_parser()
        email_intake.IMAP4_SSL = self.fake_imap([raw])

        email_intake.poll_once(self.cfg, self.fake_submit, self.runs)

        self.assertEqual(self.submitted, [])
        self.assertEqual(FakeSMTP.sent, [])
        self.assertEqual(self.load_journal()["entries"]["1"]["state"], "skipped")

    def test_bad_message_survives(self):
        good = make_email("正常单", "项目名称：正常项目")
        email_intake.PARSER = self.fake_parser()
        email_intake.IMAP4_SSL = self.fake_imap(
            [good, good], fail_on_uid={"1": RuntimeError("boom")})

        email_intake.poll_once(self.cfg, self.fake_submit, self.runs)

        entries = self.load_journal()["entries"]
        self.assertEqual(entries["1"]["state"], "error")       # 坏邮件不中断
        self.assertEqual(entries["2"]["state"], "submitted")   # 下一封照常
        self.assertEqual(len(self.submitted), 1)

    # ---- 完成巡检

    def _seed_journal_and_run(self, state, document="# 测试项目\n\n正文", **state_kw):
        run_id = "run_test1"
        run_dir = os.path.join(self.runs, run_id)
        os.makedirs(run_dir, exist_ok=True)
        state_json = {"id": run_id, "state": state,
                      "project_name": "智慧园区能耗监测平台",
                      "error": None, "pass": True, "blocking": 0}
        state_json.update(state_kw)
        with open(os.path.join(run_dir, "state.json"), "w", encoding="utf-8") as fh:
            json.dump(state_json, fh, ensure_ascii=False)
        if document is not None:
            with open(os.path.join(run_dir, "document.md"), "w", encoding="utf-8") as fh:
                fh.write(document)
        journal = {"uidvalidity": 100,
                   "entries": {"1": {"state": "submitted", "run_id": run_id,
                                     "from": CUSTOMER, "subject": "申报需求"}}}
        email_intake.save_journal(
            os.path.join(self.runs, email_intake.JOURNAL_NAME), journal)

    def test_completion_done_notifies_reviewer_with_docx(self):
        self._seed_journal_and_run("done")

        self.assertEqual(email_intake.check_completions(self.cfg, self.runs), 1)

        sent = FakeSMTP.sent
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["To"], "reviewer@example.com")
        self.assertIn("【待审核】", sent[0]["Subject"])
        body = msg_plain(sent[0])
        self.assertIn("run_test1", body)
        self.assertIn(CUSTOMER, body)
        self.assertIn("通过", body)
        atts = msg_attachments(sent[0])
        self.assertEqual(len(atts), 1)
        fname, data = atts[0]
        self.assertIn("智慧园区能耗监测平台", fname)
        self.assertTrue(data.startswith(b"PK"))   # docx zip 头
        self.assertEqual(
            self.load_journal()["entries"]["1"]["state"], "done_notified")
        # 再巡检一轮不重复发
        FakeSMTP.sent = []
        email_intake.check_completions(self.cfg, self.runs)
        self.assertEqual(FakeSMTP.sent, [])

    def test_completion_failed_notifies_reviewer(self):
        self._seed_journal_and_run("failed", document=None,
                                   error="ValueError: 引擎崩了")

        self.assertEqual(email_intake.check_completions(self.cfg, self.runs), 1)

        sent = FakeSMTP.sent
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["To"], "reviewer@example.com")
        self.assertIn("任务失败", msg_plain(sent[0]))
        self.assertIn("ValueError: 引擎崩了", msg_plain(sent[0]))
        self.assertEqual(msg_attachments(sent[0]), [])     # 失败无 docx
        self.assertEqual(
            self.load_journal()["entries"]["1"]["state"], "failed_notified")

    def test_completion_interrupted_notifies_reviewer(self):
        self._seed_journal_and_run("interrupted", document=None,
                                   error="服务重启，任务中断")

        email_intake.check_completions(self.cfg, self.runs)

        self.assertEqual(len(FakeSMTP.sent), 1)
        self.assertIn("服务重启", msg_plain(FakeSMTP.sent[0]))

    def test_completion_waiting_for_queued(self):
        self._seed_journal_and_run("queued", document=None)

        self.assertEqual(email_intake.check_completions(self.cfg, self.runs), 0)
        self.assertEqual(FakeSMTP.sent, [])

    # ---- journal 持久性与 UIDVALIDITY

    def test_journal_persists_across_restart(self):
        raw = make_email("申报需求", "项目名称：测试项目")
        email_intake.PARSER = self.fake_parser()
        email_intake.IMAP4_SSL = self.fake_imap([raw])

        email_intake.poll_once(self.cfg, self.fake_submit, self.runs)
        # 模拟重启：新的 IMAP 实例，同一 runs_dir 下的 journal 还在
        email_intake.IMAP4_SSL = self.fake_imap([raw])
        email_intake.poll_once(self.cfg, self.fake_submit, self.runs)

        self.assertEqual(len(self.submitted), 1)

    def test_uidvalidity_change_resets(self):
        raw = make_email("申报需求", "项目名称：测试项目")
        email_intake.PARSER = self.fake_parser()
        email_intake.IMAP4_SSL = self.fake_imap([raw])

        email_intake.poll_once(self.cfg, self.fake_submit, self.runs)
        # 邮箱重建：UIDVALIDITY 变了 → 去重记录清空，旧邮件重新处理
        email_intake.IMAP4_SSL = self.fake_imap([raw], uidvalidity=200)
        email_intake.poll_once(self.cfg, self.fake_submit, self.runs)

        self.assertEqual(len(self.submitted), 2)
        self.assertEqual(self.load_journal()["uidvalidity"], 200)

    # ---- dry-run CLI

    def test_dry_run_cli(self):
        raw = make_email("申报需求", "项目名称：智慧园区能耗监测平台")
        path = os.path.join(self.runs, "sample.eml")
        with open(path, "wb") as fh:
            fh.write(raw)
        email_intake.PARSER = self.fake_parser()

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = email_intake.main(["--dry-run", path])

        out = buf.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("run_dryrun", out)
        self.assertIn("智慧园区能耗监测平台", out)
        self.assertIn("邮件草稿（不发）", out)   # 不发真信
        self.assertEqual(FakeSMTP.sent, [])      # 网络零接触


if __name__ == "__main__":
    unittest.main()
