# -*- coding: utf-8 -*-
"""
本地调用 Dify 工作流 —— 调试用。不上 Dify 界面也能跑一遍全链路。

用法：
    export DIFY_BASE_URL="http://127.0.0.1/v1"
    export DIFY_API_KEY="app-xxxxxxxx"          # 从 Dify 应用「访问 API」页取
    python3 call_dify.py inputs.json            # 或 echo '{"...": ...}' | python3 call_dify.py

inputs.json 就是开始节点那 10 个变量（docs/ARCHITECTURE.md 三）。

⚠️ 两条边界：
    1. 只连配置的内网地址。密钥走环境变量，不写死、不入库（CLAUDE.md 第八节）。
    2. 这是**调试工具**，不是运行时组件。交付后企业走 Dify 自己的界面或 Webhook。

为什么要有它：脚本能在本地跑单测，但「脚本 + 提示词 + 知识库 + 编排」只有
整条跑一遍才知道通不通。没有这个工具，每次改提示词都得在界面上手点十几次。
"""

import json
import os
import sys
import urllib.error
import urllib.request

TIMEOUT = 600  # 单次生成可能几十分钟，别用默认值


def run_workflow(inputs, base_url=None, api_key=None, user="local-debug"):
    base_url = (base_url or os.environ.get("DIFY_BASE_URL") or "").rstrip("/")
    api_key = api_key or os.environ.get("DIFY_API_KEY")
    if not base_url or not api_key:
        raise SystemExit("缺 DIFY_BASE_URL 或 DIFY_API_KEY —— 从环境变量给，别写进脚本")

    payload = json.dumps({
        "inputs": inputs,
        "response_mode": "blocking",
        "user": user,
    }).encode("utf-8")

    req = urllib.request.Request(
        base_url + "/workflows/run",
        data=payload,
        headers={
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        raise SystemExit("Dify 返回 HTTP %s：\n%s" % (e.code, body[:2000]))
    except urllib.error.URLError as e:
        raise SystemExit("连不上 Dify（%s）：%s" % (base_url, e.reason))


def main():
    raw = sys.stdin.read() if len(sys.argv) < 2 else open(sys.argv[1], encoding="utf-8").read()
    inputs = json.loads(raw)
    result = run_workflow(inputs)

    data = result.get("data") or {}
    print("状态：", data.get("status"))
    if data.get("error"):
        print("错误：", data["error"])
    if data.get("elapsed_time") is not None:
        print("耗时：%.1f 秒" % data["elapsed_time"])

    outputs = data.get("outputs") or {}
    for key, val in outputs.items():
        shown = str(val)
        print("\n--- %s ---\n%s" % (key, shown if len(shown) < 4000 else shown[:4000] + "…"))

    # 审查结果单独展开 —— 回退与否看它
    report = outputs.get("chk_report")
    if isinstance(report, str):
        try:
            report = json.loads(report)
        except ValueError:
            report = None
    if isinstance(report, dict):
        print("\n=== 一致性审查 ===")
        print("pass：", report.get("pass"))
        for issue in report.get("issues") or []:
            print("  [%s] %s · %s: %s" % (issue.get("severity", "?"), issue.get("type"),
                                          issue.get("section"), issue.get("detail")))

    return 0 if data.get("status") == "succeeded" else 1


if __name__ == "__main__":
    sys.exit(main())
