"""whoami —— 读人格卡，按块取，按需注入。

为什么要有这个文件：
  人格卡现在是一份 md，八块。但不同场合要的不是同一块——
  开聊该塞的是「性格 + 怎么说话 + 关系」；「能力边界」「记忆」不必每次都占上下文。

块名怎么认（今天现场踩出来的）：
  卡上写的是 `## 三、性格（每条都是一个动作）`，
  可人喊的是「性格」、patch 传来的可能也是「性格」。
  所以这里把每个标题拆成：主名 + 别名，喊哪个都认；
  **但只有唯一命中才放行**，两个块都能对上就报错，不猜。

对外：
    sections(path)            → 所有块（含主名、别名、行号区间）
    resolve(path, name)       → 按名字找块，唯一才返回
    summary(path, want=None)  → 取指定块拼成一段，默认取注入用的三块
    whoami(name, root=None)   → 给工具层用的便捷入口，返回 {ok, data, error}

约定：所有对外函数不抛异常，返回 (ok, data, error) 风格 —— 跟任务卡那边一个规矩。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

DEFAULT_ROOT = os.environ.get("WHALE_CARD_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "examples", "persona"))

# 开聊默认注入的三块 —— 少即是多，别把整张卡塞进上下文
INJECT_BLOCKS = ("性格", "怎么说话", "关系")

# 常见别名（卡上不一定用这几个字，但人这么喊）
ALIASES = {
    "性格": ["个性", "脾气"],
    "怎么说话": ["说话", "语气", "口吻", "说话方式"],
    "关系": ["人物", "家人", "称呼"],
    "喜好": ["喜好与审美", "审美", "喜欢"],
    "能力边界": ["能力", "边界", "会什么"],
    "干活规矩": ["规矩", "纪律"],
    "记忆": ["本子", "记忆工具"],
    "身份": ["基本信息"],
}

_HEAD_RE = re.compile(r"^(#{1,3})\s+(.+?)\s*$")


# --------------------------------------------------------------- 解析

def _clean_title(raw: str) -> tuple[str, list[str]]:
    """`三、性格（每条都是一个动作）` → ("性格", ["三、性格", "性格（每条都是一个动作）", "性格（每条都是一个动作）"...])"""
    t = raw.strip()
    aliases: list[str] = [t]

    # 去掉尾部的括号说明
    no_paren = re.sub(r"[（(][^）)]*[）)]\s*$", "", t).strip()
    if no_paren and no_paren != t:
        aliases.append(no_paren)

    # 去掉中文序号前缀
    no_num = re.sub(r"^[一二三四五六七八九十百\d]+[、.．]\s*", "", no_paren or t).strip()
    if no_num and no_num != t:
        aliases.append(no_num)

    main = no_num or no_paren or t
    # 主名再去掉「附·」这类前缀
    main = re.sub(r"^(附|附录)[·・.]\s*", "", main).strip()
    aliases.append(main)

    # 去重保序
    seen, out = set(), []
    for a in aliases:
        if a and a not in seen:
            seen.add(a)
            out.append(a)
    return main, out


def sections(path: str | os.PathLike) -> tuple[bool, list[dict[str, Any]], str]:
    p = Path(path)
    if not p.exists():
        return False, [], f"卡不存在：{p}"
    text = p.read_text(encoding="utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")

    # 跳过 frontmatter
    start = 0
    if lines and lines[0].strip() == "---":
        for i, l in enumerate(lines[1:], 1):
            if l.strip() == "---":
                start = i + 1
                break

    out: list[dict[str, Any]] = []
    cur = None
    for i in range(start, len(lines)):
        m = _HEAD_RE.match(lines[i])
        if m and len(m.group(1)) == 2:      # 只认 ## 二级标题当块
            if cur:
                cur["end"] = i
                cur["body"] = "\n".join(lines[cur["start"]:i]).strip("\n")
                out.append(cur)
            main, aliases = _clean_title(m.group(2))
            cur = {"name": main, "raw": m.group(2), "aliases": aliases,
                   "line": i + 1, "start": i + 1, "end": None, "body": ""}
    if cur:
        cur["end"] = len(lines)
        cur["body"] = "\n".join(lines[cur["start"]:]).strip("\n")
        out.append(cur)

    return True, out, ""


# --------------------------------------------------------------- 按名字找块

def resolve(path: str | os.PathLike, name: str) -> tuple[bool, dict | None, str]:
    """按名字找块。精确优先；不中再模糊；**唯一命中才放行**。"""
    ok, secs, err = sections(path)
    if not ok:
        return False, None, err
    want = (name or "").strip()
    if not want:
        return False, None, "块名不能为空"

    # 1) 精确
    exact = [s for s in secs if want in s["aliases"]]
    if len(exact) == 1:
        return True, exact[0], ""
    if len(exact) > 1:
        return False, None, f"「{want}」同时命中 {len(exact)} 个块（{', '.join(s['raw'] for s in exact)}），不猜，请说清楚"

    # 2) 别名表
    wanted_alias = ALIASES.get(want, [])
    via_alias = [s for s in secs if any(a in s["aliases"] for a in wanted_alias)]
    if len(via_alias) == 1:
        return True, via_alias[0], ""
    if len(via_alias) > 1:
        return False, None, f"「{want}」经别名同时命中 {len(via_alias)} 个块，请说清楚"

    # 3) 包含关系（唯一才放行）
    contains = [s for s in secs if want in s["raw"] or s["name"] in want]
    if len(contains) == 1:
        return True, contains[0], ""
    if len(contains) > 1:
        return False, None, f"「{want}」模糊命中 {len(contains)} 个块（{', '.join(s['raw'] for s in contains)}），请说清楚"

    return False, None, f"卡上没有「{want}」这块。现有：{', '.join(s['name'] for s in secs)}"


# --------------------------------------------------------------- 取摘要

def summary(path: str | os.PathLike, want: tuple[str, ...] | None = None) -> tuple[bool, str, str]:
    ok, secs, err = sections(path)
    if not ok:
        return False, "", err
    names = want or INJECT_BLOCKS
    parts: list[str] = []
    for n in names:
        ok2, sec, err2 = resolve(path, n)
        if ok2 and sec and sec["body"]:
            parts.append(f"### {sec['name']}\n{sec['body'].strip()}")
        elif not ok2:
            # 缺块不算错误，记一笔就行 —— 卡是长出来的，不是一次填满的
            continue
    if not parts:
        return False, "", "这三个块都没内容，卡可能是空的"
    p = Path(path)
    return True, f"# 我是 {(p.stem)}\n\n" + "\n\n".join(parts), ""


# --------------------------------------------------------------- 工具层入口

def whoami(name: str, root: str | os.PathLike = DEFAULT_ROOT,
           blocks: list[str] | None = None) -> dict[str, Any]:
    """给 LLM 工具层调。name 可以是「白彤锦」也可以带 .md。返回 {ok,data,error}。"""
    try:
        p = Path(root) / (name if str(name).endswith(".md") else f"{name}.md")
        if not p.exists():
            return {"ok": False, "data": None, "error": f"没有这张卡：{p}"}
        ok, text, err = summary(p, tuple(blocks) if blocks else None)
        if not ok:
            return {"ok": False, "data": None, "error": err}
        return {"ok": True, "data": {"card": p.stem, "text": text}, "error": ""}
    except Exception as e:
        return {"ok": False, "data": None, "error": f"{type(e).__name__}: {e}"}


# --------------------------------------------------------------- 自测

if __name__ == "__main__":
    cards = [p for p in Path(DEFAULT_ROOT).glob("*.md") if p.stem != "模板"]
    if not cards:
        print("没有卡可测，先看 DEFAULT_ROOT：", DEFAULT_ROOT)
        raise SystemExit(0)

    for p in cards:
        print("=" * 62)
        print(p.name)
        ok, secs, err = sections(p)
        if not ok:
            print("  解析失败:", err); continue
        print("  块:", [s["raw"] for s in secs])

        # 模拟用户只喊短名
        for shout in ("性格", "怎么说话", "关系", "记忆", "不存在的东西"):
            ok2, sec, err2 = resolve(p, shout)
            print(f"  喊「{shout}」→ " + (f"✅ {sec['raw']}" if ok2 else f"❌ {err2[:60]}"))

        ok3, text, err3 = summary(p)
        print(f"  注入摘要：{'✅ ' + str(len(text)) + ' 字' if ok3 else '❌ ' + err3}")
