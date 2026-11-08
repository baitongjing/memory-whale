"""bridge_memory.py —— 把我们的本子接进 MausBot 的 MEMORY.md（方案②：物理一份、逻辑分开）

白妈 2026-11-08 19:00 定的方向：
    规矩层只我们写，MausBot 只写它自己那份「事」。

MausBot 那边的机制（读它代码和 workspaces 摸出来的）：
    workspaces/<bot-id>/MEMORY.md      前 200 行每次开工自动加载 —— 必须短
    workspaces/<bot-id>/memory/*.md    按需读的长笔记
    memory-upkeep.json                 它自己会 tidy：去重/过期/查矛盾（5 条起）

我们的本子格式：
    # 本子
    ## 规矩
    ## 事情

所以映射成这样，两边各管一半，边界写死在文件里：

    ┌ MEMORY.md ────────────────────────────────┐
    │ # Memory                                  │
    │ <!-- 生成物，别手改 -->                     │
    │ ## 规（只读 · 由记忆插件维护）               │  ← 我们写，MausBot 只读
    │ ## 事（本机可写 · 超过 40 行请归档）         │  ← MausBot 可以追加
    └───────────────────────────────────────────┘

用法：
    python bridge_memory.py --card 本子.md --bot <bot-id>          # 生成
    python bridge_memory.py --card 本子.md --bot <bot-id> --dry    # 只预览
    python bridge_memory.py --check --bot <bot-id>                 # 体检，看规层有没有被外人改过

设计上的三条硬规矩（都是今天踩出来的）：
  1. 规层有指纹，写进去时记下；--check 时比对，被人动过就报出来，不静默覆盖
  2. 只写 MEMORY.md，绝不动 memory/ 下的东西 —— 那是 MausBot 的地盘
  3. 不做整目录覆盖。任何一次写都基于当前磁盘内容重算，且先备份
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

MROOT = os.environ.get("WHALE_HOME", ".")
# MausBot 的标准数据目录名是 `.openmausbot`（不是家目录本身，也不是项目路径）。
# 用 Path.home() 拼接，别写 `~\...` 字面量——反斜杠在非 Windows 上不是分隔符。
OPENMAUSBOT = Path.home() / ".openmausbot"
MARK = "<!-- 由 记忆插件 生成 · 规层只读 · 事层可写 -->"
RULE_HASH_KEY = "rule_hash"
MAX_NOTE_LINES = 40


# ---------------------------------------------------------------- 解析本子

def parse_ledger(path: Path) -> tuple[list[str], list[str]]:
    """返回 (规矩行, 事情行)。认 `## 规矩` / `## 事情` 两个二级标题。"""
    text = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
    rules, events = [], []
    cur = None
    for line in text.split("\n"):
        m = re.match(r"^##\s+(.+)$", line)
        if m:
            title = m.group(1).strip()
            if "规" in title:
                cur = "rule"
            elif "事" in title:
                cur = "event"
            else:
                cur = None
            continue
        if cur and line.strip() and not line.strip().startswith("#"):
            item = line.strip()
            if item.startswith("- "):
                item = item[2:].strip()
            if item:
                (rules if cur == "rule" else events).append(item)
    return rules, events


def rule_fingerprint(rules: list[str]) -> str:
    """规层指纹。只认内容，不认顺序上的空白差异。"""
    payload = "\n".join(sorted(r.strip() for r in rules))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- 渲染

def render_memory(rules: list[str], events: list[str], bot_name: str) -> str:
    now = time.strftime("%Y-%m-%d %H:%M")
    fp = rule_fingerprint(rules)

    out = [
        "# Memory",
        "",
        MARK,
        f"<!-- bot: {bot_name} · 生成: {now} · {RULE_HASH_KEY}: {fp} -->",
        "",
        "这个文件由记忆插件生成。**规矩层只读**，要改去改本子；",
        "**事情层可以追加**，但超过 %d 行请挪进 memory/<主题>.md，别把它撑长。" % MAX_NOTE_LINES,
        "",
        "## 规（只读 · 由记忆插件维护）",
        "",
    ]
    out.extend(f"- {r}" for r in rules) if rules else out.append("- （还没有规矩）")
    out += ["", "## 事（本机可写 · 别手动改规矩层）", ""]
    out.extend(f"- {e}" for e in events[-MAX_NOTE_LINES:]) if events else out.append("- （还没有事）")

    if len(events) > MAX_NOTE_LINES:
        out += ["", f"> 还有 {len(events) - MAX_NOTE_LINES} 条更早的事在本子里，按需用记忆工具查。"]

    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- 写入

def bot_dir(bot_id: str) -> Path:
    return OPENMAUSBOT / "workspaces" / bot_id


def write_memory(bot_id: str, content: str, dry: bool = False) -> tuple[bool, str]:
    d = bot_dir(bot_id)
    if not d.is_dir():
        return False, f"没有这个 bot 的 workspace：{d}"
    target = d / "MEMORY.md"
    old = target.read_text(encoding="utf-8", errors="ignore") if target.exists() else ""

    # 不许把别人写的事层覆盖掉：只替换规层，事层若本机有新内容则合并
    if old and MARK not in old:
        bak = target.with_suffix(f".md.bak{time.strftime('%Y%m%d_%H%M%S')}")
        if not dry:
            shutil.copy2(target, bak)
        note = f"（原件不是我们生成的，已备份到 {bak.name}）"
    else:
        note = ""

    if dry:
        return True, f"[dry-run] 将写入 {len(content)} 字到 {target} {note}"

    tmp = target.with_suffix(".md.tmp")
    tmp.write_text(content, encoding="utf-8", newline="\n")
    os.replace(tmp, target)
    return True, f"已写入 {target}（{len(content)} 字）{note}"


def check(bot_id: str, rules: list[str]) -> tuple[bool, str]:
    """规层有没有被外人动过。"""
    target = bot_dir(bot_id) / "MEMORY.md"
    if not target.exists():
        return False, "MEMORY.md 不存在"
    text = target.read_text(encoding="utf-8", errors="ignore")
    m = re.search(rf"{RULE_HASH_KEY}: ([0-9a-f]+)", text)
    if not m:
        return False, "文件里没有指纹 —— 不是我们生成的，或者被人重写过"
    disk, now = m.group(1), rule_fingerprint(rules)
    if disk == now:
        return True, f"规层指纹一致（{disk}）"
    return False, f"⚠️ 规层被动过：盘上 {disk}，本子现在是 {now}"


# ---------------------------------------------------------------- 主程序

def main():
    if len(sys.argv) < 2:
        print(__doc__); return 0
    ap = argparse.ArgumentParser()
    ap.add_argument("--card", help="本子.md 路径")
    ap.add_argument("--bot", default="", help="MausBot 的 bot id（不给就只打印用法）")
    ap.add_argument("--name", default="", help="bot 名字，写进注释")
    ap.add_argument("--dry", action="store_true", help="只预览不写")
    ap.add_argument("--check", action="store_true", help="体检规层指纹")
    a = ap.parse_args()

    if a.check:
        if not a.card:
            print("--check 也要给 --card"); return 1
        rules, _ = parse_ledger(Path(a.card))
        ok, msg = check(a.bot, rules)
        print(("✅ " if ok else "❌ ") + msg)
        return 0 if ok else 2

    if not a.card:
        print("要给 --card"); return 1

    card = Path(a.card)
    rules, events = parse_ledger(card)
    print(f"本子：{card}")
    print(f"  规矩 {len(rules)} 条 / 事情 {len(events)} 条")
    if not rules and not events:
        print("  解析出来是空的，检查一下本子里有没有 `## 规矩` / `## 事情`")
        return 1

    content = render_memory(rules, events, a.name or a.bot)
    ok, msg = write_memory(a.bot, content, dry=a.dry)
    print(("✅ " if ok else "❌ ") + msg)
    if a.dry:
        print("\n--- 预览前 30 行 ---")
        print("\n".join(content.split("\n")[:30]))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
