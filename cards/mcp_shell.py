# -*- coding: utf-8 -*-
"""MCP 服务壳（T-0003，砚砚 2026-11-08）

只做壳：六个工具的 JSON Schema + stdio JSON-RPC 最小收发骨架 +
内核调用签名。不实现内核，内核由白彤鲸的 store.py 提供。

约定（接口约定 v0.1 / T-0003 说明）：
- 参数名一律小写下划线
- 工具返回一律 {"ok": bool, "data": ..., "error": ...}
- stdio 每行一个 JSON-RPC 2.0 消息（MCP 最小实现，支持 initialize /
  tools/list / tools/call，notifications 只收不回）

跑法：python mcp_shell.py [--dry-run]
  --dry-run 时不接真内核，内核调用走 _kernel_stub，方便单测壳本身。
"""

import json
import sys

# ---------------------------------------------------------------------------
# 内核接口签名（只签名，不实现 —— 实现在白彤鲸的 store.py / 内核）
# ---------------------------------------------------------------------------

def _kernel_stub(tool: str, args: dict) -> dict:
    """dry-run 用假内核：原样回显，验证壳的管道通不通。"""
    return {"echo_tool": tool, "echo_args": args}


# 真内核接线处：kernel_bridge（包 store.py）。
# 导入失败不许静默回退 stub（P1）：正常模式必须真内核，起不来就退出并说人话；
# 只有 --dry-run 才允许用 _kernel_stub。main() 里按 dry_run 兜底处理。
try:
    from kernel_bridge import handle as _kernel
except Exception as _e:  # 先记住原因，main() 里决定炸不炸
    _kernel = None
    _kernel_import_error = "%s: %s" % (type(_e).__name__, _e)

# 内核需要的六个函数（签名约定，由内核实现，壳只按下表转发）：
#
#   task_new(title: str, chief: str, note: str = "") -> dict
#       建新卡，返回 {"ok": True, "data": {"id": "T-xxxx", "path": ...}}
#   task_list(status: str = "", assignee: str = "") -> dict
#       列任务卡；两个过滤参数可空
#   task_claim(task_id: str, worker: str, item_no: str) -> dict
#       认领某卡某工作条（rev 锁在内核里判）
#   task_handoff(task_id: str, item_no: str, reason: str, tried: str) -> dict
#       转交：写为什么干不了、试过什么，卡状态转「转交中」
#   task_note(task_id: str, text: str, author: str) -> dict
#       心跳区追加一行
#   task_done(task_id: str, item_no: str, result: str = "") -> dict
#       勾掉工作条；整卡完成时内核决定是否转「已完成」并计下沉

# ---------------------------------------------------------------------------
# 六个工具的 JSON Schema（inputSchema 按 MCP 规范）
# ---------------------------------------------------------------------------

def _schema(properties: dict, required: list) -> dict:
    return {"type": "object", "properties": properties, "required": required}


def _s_str(desc: str) -> dict:
    return {"type": "string", "description": desc}


TOOLS = [
    {
        "name": "task_new",
        "description": "新建一张公共任务卡（总参用）",
        "inputSchema": _schema({
            "title": _s_str("任务标题"),
            "chief": _s_str("总参名字"),
            "note": _s_str("说明，可空"),
        }, ["title", "chief"]),
    },
    {
        "name": "task_list",
        "description": "列公共任务卡，可按状态/认领人过滤",
        "inputSchema": _schema({
            "status": _s_str("派发中/进行中/卡住/已完成/已归档，空=全部"),
            "assignee": _s_str("按认领人过滤，空=不过滤"),
        }, []),
    },
    {
        "name": "task_claim",
        "description": "认领某张卡的某个工作条（rev 锁防抢）",
        "inputSchema": _schema({
            "task_id": _s_str("卡号，如 T-0003"),
            "worker": _s_str("认领人名字"),
            "item_no": _s_str("工作条号，如 001"),
        }, ["task_id", "worker", "item_no"]),
    },
    {
        "name": "task_handoff",
        "description": "转交工作条：写清为什么干不了、试过什么",
        "inputSchema": _schema({
            "task_id": _s_str("卡号"),
            "item_no": _s_str("工作条号"),
            "worker": _s_str("谁在转交"),
            "reason": _s_str("为什么干不了（没权限/接口不通/想不明白/等回话）"),
            "tried": _s_str("已经试过什么，别让下一棒重踩"),
        }, ["task_id", "item_no", "worker", "reason", "tried"]),
    },
    {
        "name": "task_note",
        "description": "往卡的心跳区追加一行",
        "inputSchema": _schema({
            "task_id": _s_str("卡号"),
            "text": _s_str("心跳内容，一行"),
            "author": _s_str("谁写的"),
        }, ["task_id", "text", "author"]),
    },
    {
        "name": "task_done",
        "description": "勾掉一个工作条，附验证结果",
        "inputSchema": _schema({
            "task_id": _s_str("卡号"),
            "item_no": _s_str("工作条号"),
            "result": _s_str("验证结果，可空"),
        }, ["task_id", "item_no"]),
    },
]


def _ok(data) -> dict:
    return {"ok": True, "data": data, "error": None}


def _err(msg: str) -> dict:
    return {"ok": False, "data": None, "error": msg}


def call_tool(name: str, args: dict) -> dict:
    """壳的统一入口：查表转发给内核，异常兜成 {"ok": False, ...}，不甩 traceback。"""
    known = {t["name"] for t in TOOLS}
    if name not in known:
        return _err(f"unknown tool: {name}")
    try:
        ret = _kernel(name, args or {})
    except Exception as e:  # 人话原因，不是 traceback
        return _err(f"{type(e).__name__}: {e}")
    if not isinstance(ret, dict) or "ok" not in ret:  # P2: 内核不守约就包一层
        return _err("内核返回格式不守约（缺 ok 字段）")
    return ret


# ---------------------------------------------------------------------------
# stdio JSON-RPC 2.0 最小收发骨架
# ---------------------------------------------------------------------------

def _reply(req_id, result=None, error=None) -> str:
    msg = {"jsonrpc": "2.0", "id": req_id}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    return json.dumps(msg, ensure_ascii=False)


def handle(msg: dict) -> "str | None":
    """处理一条请求；notification（无 id）只收不回，返回 None。"""
    method = msg.get("method", "")
    req_id = msg.get("id")

    if method == "initialize":
        result = {
            "protocolVersion": msg.get("params", {}).get(
                "protocolVersion", "2024-11-05"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "mausbot-task-cards", "version": "0.1.0"},
        }
        return _reply(req_id, result=result)

    if method == "notifications/initialized":
        return None

    if method == "tools/list":
        return _reply(req_id, result={"tools": TOOLS})

    if method == "tools/call":
        params = msg.get("params", {})
        ret = call_tool(params.get("name", ""), params.get("arguments") or {})
        return _reply(req_id, result={
            "content": [{"type": "text",
                         "text": json.dumps(ret, ensure_ascii=False)}],
            "isError": not ret.get("ok", False),
        })

    if req_id is not None:
        return _reply(req_id, error={"code": -32601,
                                     "message": f"unknown method: {method}"})
    return None


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    if dry_run:
        globals()["_kernel"] = _kernel_stub
    elif _kernel is None:
        # 正常模式内核没接上：绝不带 stub 悄悄活着（P1）
        sys.stderr.write("内核没接上，拒绝启动：%s\n" % _kernel_import_error)
        return 1
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            print(_reply(None, error={"code": -32700,
                                      "message": "parse error"}),
                  flush=True)
            continue
        if not isinstance(msg, dict):  # JSON 合法但不是对象，也当 parse error
            print(_reply(None, error={"code": -32600,
                                      "message": "invalid request: not an object"}),
                  flush=True)
            continue
        out = handle(msg)
        if out is not None:
            print(out, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
