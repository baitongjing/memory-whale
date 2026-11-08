"""桥：把 mcp_shell 的六个工具接到 store.py 内核上。

壳里只需一行：
    import kernel_bridge
    _kernel = kernel_bridge.handle

约定（见 接口约定.md）：任何调用一律返回 {"ok": bool, "data": ..., "error": str}。
绝不抛异常出去 —— 壳会把异常甩成 traceback，报给模型就是一堆天书。
"""

from __future__ import annotations

import traceback
from typing import Any

import store as _store

STORE = _store.CardStore()


def _ok(data: Any = None) -> dict:
    return {"ok": True, "data": data, "error": ""}


def _err(msg: str, data: Any = None) -> dict:
    return {"ok": False, "data": data, "error": msg}


# ------------------------------------------------------------------ 工具实现

def t_task_new(a: dict) -> dict:
    title = (a.get("title") or "").strip()
    chief = (a.get("chief") or "").strip()
    if not title or not chief:
        return _err("title 和 chief 都得给（总参是谁要写清楚）")
    items = a.get("items") or []
    if isinstance(items, str):
        items = [x.strip() for x in items.split(";") if x.strip()]
    if not items:
        items = ["001 待补工作条"]
    ok, data, err = STORE.new_card(title=title, chief=chief, items=items,
                                   note=(a.get("note") or "").strip(),
                                   card_id=(a.get("task_id") or None))
    return _ok(data) if ok else _err(err, data)


def t_task_list(a: dict) -> dict:
    status = (a.get("status") or "").strip() or None
    assignee = (a.get("assignee") or "").strip()
    cards = STORE.list_cards(status=status)
    if assignee:
        # 别用 split(":")[-1] 取人名 —— 人名里带冒号就被切碎了，改用从右数第一次出现的位置切
        keep = []
        for c in cards:
            for x in c["taken_items"]:
                no, sep, who = x.partition(":")
                if sep and who.strip() == assignee:
                    keep.append(c)
                    break
        cards = keep
    return _ok({"count": len(cards), "cards": cards})


def t_task_claim(a: dict) -> dict:
    for k in ("task_id", "worker", "item_no"):
        if not (a.get(k) or "").strip():
            return _err(f"缺参数 {k}")
    ok, data, err = STORE.claim(a["task_id"].strip(), a["item_no"].strip(), a["worker"].strip())
    return _ok(data) if ok else _err(err, data)


def t_task_done(a: dict) -> dict:
    for k in ("task_id", "item_no"):
        if not (a.get(k) or "").strip():
            return _err(f"缺参数 {k}")
    worker = (a.get("worker") or a.get("author") or "—").strip()
    ok, data, err = STORE.done(a["task_id"].strip(), a["item_no"].strip(), worker,
                               (a.get("result") or "").strip())
    return _ok(data) if ok else _err(err, data)


def t_task_note(a: dict) -> dict:
    if not (a.get("task_id") or "").strip():
        return _err("缺参数 task_id")
    author = (a.get("author") or "—").strip()
    text = (a.get("text") or "").strip()
    if not text:
        return _err("往心跳区写东西，text 不能空")
    ok, data, err = STORE.heartbeat(a["task_id"].strip(), author, "记录", "", text)
    return _ok(data) if ok else _err(err, data)


def t_task_handoff(a: dict) -> dict:
    """转交：把「为什么干不了 + 试过什么」钉在卡上，别让下一棒重踩。"""
    if not (a.get("task_id") or "").strip():
        return _err("缺参数 task_id")
    reason = (a.get("reason") or "").strip()
    tried = (a.get("tried") or "").strip()
    if not reason:
        return _err("要说清为什么干不了（没权限／接口不通／想不明白／等回话）")
    author = (a.get("worker") or a.get("author") or "—").strip()
    item = (a.get("item_no") or "").strip()
    ok, data, err = STORE.heartbeat(a["task_id"].strip(), author, f"转交({reason})", item,
                                    f"试过：{tried or '没写'}")
    if not ok:
        return _err(err, data)
    # 在工作条上留一句，接手的人翻开就看见。
    # 写失败不许装成功 —— 心跳进了、进度没进，那就是半截交接，下一棒会看漏。
    try:
        card = STORE.load(a["task_id"].strip())
        card["sections"].setdefault("进度", []).append(
            f"- {_store._now()} {author}：工作条 {item or '—'} 转出 —— {reason}；试过：{tried or '没写'}")
        ok2, d2, e2 = STORE.save(card, expected_rev=card["rev"])
        if not ok2:
            return _err(f"心跳已写、进度未写：{e2}", data)
    except Exception as ex:
        return _err(f"心跳已写、进度未写：{type(ex).__name__}: {ex}", data)
    return _ok(data)


HANDLERS = {
    "task_new": t_task_new,
    "task_list": t_task_list,
    "task_claim": t_task_claim,
    "task_handoff": t_task_handoff,
    "task_note": t_task_note,
    "task_done": t_task_done,
}


def handle(tool: str, args: dict) -> dict:
    """壳调这里。永不抛。"""
    try:
        fn = HANDLERS.get((tool or "").strip())
        if fn is None:
            return _err(f"不认识这个工具：{tool}（有这六个：{', '.join(HANDLERS)}）")
        if not isinstance(args, dict):
            return _err(f"参数得是对象，收到的是 {type(args).__name__}")
        return fn(args)
    except FileNotFoundError as e:
        return _err(f"卡找不到：{e}")
    except Exception as e:
        return _err(f"内核炸了：{type(e).__name__}: {e}\n{traceback.format_exc(limit=2)}")


if __name__ == "__main__":
    # 自测：拿一个临时库跑一遍六工具
    import tempfile, json
    from pathlib import Path
    tmp = Path(tempfile.mkdtemp())
    STORE.root = tmp
    STORE.tasks = tmp / "任务"
    STORE.archive = STORE.tasks / "归档"
    STORE.tasks.mkdir(parents=True); STORE.archive.mkdir(parents=True)

    def show(tag, r):
        print(f"\n[{tag}] ok={r['ok']} err={r['error'][:60]}")
        print("   data:", json.dumps(r["data"], ensure_ascii=False)[:220])

    show("new", handle("task_new", {"title": "桥自测", "chief": "白彤鲸",
                                    "items": ["写壳", "验内核"], "note": "桥接测试"}))
    show("list", handle("task_list", {}))
    show("claim", handle("task_claim", {"task_id": "T-0001", "worker": "白彤砚", "item_no": "001"}))
    show("claim-again", handle("task_claim", {"task_id": "T-0001", "worker": "白彤锦", "item_no": "001"}))
    show("handoff", handle("task_handoff", {"task_id": "T-0001", "item_no": "002",
                                            "reason": "接口不通", "tried": "试过 MCP 拉消息，拿不到",
                                            "worker": "白彤砚"}))
    show("done", handle("task_done", {"task_id": "T-0001", "item_no": "001",
                                      "result": "壳跑通", "worker": "白彤砚"}))
    show("bad-tool", handle("task_fly", {}))
    show("bad-args", handle("task_new", "not-a-dict"))
    print("\n--- 卡面 ---")
    print((STORE.tasks / "T-0001.md").read_text(encoding="utf-8"))
