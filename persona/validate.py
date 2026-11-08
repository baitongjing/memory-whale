# -*- coding: utf-8 -*-
"""validate.py —— 人格卡校验器（T-0006 001，砚砚 2026-11-08）

输一张卡的路径，出一份问题清单：
  {"ok": bool, "errors": [...], "warnings": [...]}   每条带行号

四查：
  1. 卡里写的工具名 vs 这台机器实际注册的（读 AstrBot 启动日志
     「Added llm tool: xxx」，只认 provider.func_tool_manager 发出的真注册行
     ——日志里会回显我们自己的 grep 命令，裸匹配会把 `xxx`、`memory_`
     这类占位串当工具名，全家人今天栽了三次就栽这）
  2. 关系表里出现空泛称呼（主人/用户/AI/系统）→ 警告
  3. 全文出现「作为一个AI」「作为AI助手」→ 报错
  4. 性格条目出现纯形容词且后面没跟具体动作 → 警告

用法：
  python validate.py <卡路径> [--log <日志路径>]
  或 import 后调 validate(card_path, log_path=None)
"""

import json
import os
import re
import sys

DEFAULT_LOG = os.environ.get("ASTRBOT_LOG", "")

# 真注册行的锚：func_tool_manager 发的 INFO 才算数
_TOOL_LINE = re.compile(
    r"\[provider\.func_tool_manager:\d+\]:.*?Added llm tool:\s*([A-Za-z][A-Za-z0-9_.-]*)\s*$")
# 占位/截断名挡掉：xxx、foo_bar 结尾下划线、纯数字
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]+(?:[.-][a-z0-9_]+)*$", re.I)

# 每次启动的「Added llm tool」行号间隔通常几百行；超过这个间隔算上一轮启动
_BLOCK_GAP = 1000

_EMPTY_TITLES = ("主人", "用户", "AI", "系统", "助手", "工具人")
_AI_PHRASES = ("作为一个AI", "作为AI助手", "作为AI", "作为一个 AI", "作为 AI 助手")
_ADJ_WORDS = ("开朗", "温柔", "热情", "乐于助人", "有耐心", "善良", "可爱", "稳重",
              "活泼", "成熟", "可靠", "幽默")
# 动作标记：后面跟了「——」或「：」的具体动作描述才算数
_ACTION_MARK = ("——", "——", ":", "：" )


def runtime_tools(log_path: str = DEFAULT_LOG) -> "set | None":
    """从启动日志拿当前实际注册的工具名集合；日志读不到返回 None。"""
    if not log_path or not os.path.isfile(log_path):
        return None
    events = []  # (行号, 工具名)
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f, 1):
                m = _TOOL_LINE.search(line)
                if m and _NAME_RE.match(m.group(1)):
                    events.append((i, m.group(1)))
    except OSError:
        return None
    if not events:
        return set()
    # 按行号间隙切块，取最后一块 = 最近一次启动注册的
    blocks = []
    for ln, name in events:
        if blocks and ln - blocks[-1][-1][0] <= _BLOCK_GAP:
            blocks[-1].append((ln, name))
        else:
            blocks.append([(ln, name)])
    return {name for _, name in blocks[-1]}


def _section_span(lines, title_prefix):
    """找 「## title_prefix」 到下一个 「## 」 的行号范围（1-based，含头含尾）。"""
    start = end = None
    for i, ln in enumerate(lines, 1):
        if ln.strip().startswith("## ") and ln.strip()[3:].startswith(title_prefix):
            start = i
        elif start is not None and ln.strip().startswith("## "):
            end = i - 1
            break
    if start is not None and end is None:
        end = len(lines)
    return start, end


def validate(card_path: str, log_path: str = None) -> dict:
    errors, warnings = [], []
    try:
        with open(card_path, "r", encoding="utf-8") as f:
            lines = f.read().split("\n")
    except OSError as e:
        return {"ok": False, "errors": ["读卡失败 %s：%s" % (card_path, e)],
                "warnings": []}

    text = "\n".join(lines)

    # ---- 查 3：AI 腔（全文；跳过立规矩行——禁用清单/自查是在引用坏例，不是犯案） ----
    for i, ln in enumerate(lines, 1):
        stripped = ln.strip()
        if stripped.startswith(("- ❌", "- [x]", "- [ ]")) or "自查" in ln:
            continue
        for p in _AI_PHRASES:
            if p in ln:
                errors.append("行 %d：出现AI腔「%s」——人格没生效的信号：%s"
                              % (i, p, stripped[:50]))
                break

    # ---- 查 1：工具名实核 ----
    s, e = _section_span(lines, "八、记忆")
    runtime = runtime_tools(log_path or DEFAULT_LOG)
    if runtime is None:
        warnings.append("行 %d：启动日志读不到，工具名没核成（第 %s 节）——别当成通过"
                        % (s or 0, s or "?"))
    elif s:
        # 只核「工具列表行」：以 - `名字` 开头的那一行才是工具条目，
        # 行内反引号夹的路径/插件名不算（D:\xx、astrbot_plugin_m3_v0 这类）
        bullet_re = re.compile(r"^[-*]\s+`([A-Za-z][A-Za-z0-9_.-]+)`")
        for i in range(s, e + 1):
            m = bullet_re.match(lines[i - 1].strip())
            if not m:
                continue
            tok = m.group(1)
            if not re.match(r"^[a-z][a-z0-9_]+$", tok):
                continue
            if "plugin" in tok or tok.endswith("_data") or tok.endswith("_v0"):
                continue  # 插件名/数据目录名不是 llm 工具
            if tok not in runtime:
                errors.append(
                    "行 %d：卡里写的工具「%s」这台机器没注册（实际注册 %d 个：%s）——照着喊会白喊"
                    % (i, tok, len(runtime), ", ".join(sorted(runtime)[:12])))

    # ---- 查 2：关系表空泛称呼 ----
    s, e = _section_span(lines, "二、关系")
    if s:
        for i in range(s, e + 1):
            ln = lines[i - 1]
            if ln.strip().startswith("|") and "|" in ln.strip()[1:]:
                cells = [c.strip() for c in ln.strip().strip("|").split("|")]
                hit = sorted({c for c in cells if c in _EMPTY_TITLES})
                for cell in hit:  # 同一行去重，一格报一次
                    warnings.append(
                        "行 %d：关系表用了空泛称呼「%s」——落不到具体人" % (i, cell))

    # ---- 查 4：性格纯形容词 ----
    s, e = _section_span(lines, "三、性格")
    if s:
        for i in range(s, e + 1):
            ln = lines[i - 1].strip()
            if not (ln.startswith("-") or ln.startswith("*")):
                continue
            body = ln.lstrip("-* ").lstrip("0123456789. ").strip()
            hit_adj = [a for a in _ADJ_WORDS if a in body]
            if hit_adj and not any(m in body for m in _ACTION_MARK):
                warnings.append(
                    "行 %d：性格条目只有形容词（%s），没跟具体动作——没法验证：%s"
                    % (i, "/".join(hit_adj), body[:40]))

    return {"ok": not errors, "errors": errors, "warnings": warnings}


def main() -> int:
    if len(sys.argv) < 2:
        # 只打用法、退出码 0 —— 没给参数不是错误，别吓跑第一次跑的人
        print("用法：python validate.py <卡路径> [--log <日志路径>]")
        print("例：python validate.py ../examples/persona/Whale.md --log backend.log")
        return 0
    card = sys.argv[1]
    log = None
    if "--log" in sys.argv:
        log = sys.argv[sys.argv.index("--log") + 1]
    print(json.dumps(validate(card, log), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
