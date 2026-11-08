"""trash_filter.py —— 把本子里的「垃圾话」挑出来，打包成一份清单，不删。

白妈 2026-11-08 19:58 的要求：
    「这种垃圾话有没有办法过滤出来，然后在每天十点的时候自动打包成文件挂出去」

所以这个脚本只做一件事：**挑出来，列清单，等人点头。**
它不删任何东西 —— 删错了捞不回来，列错了最多是白看一张单子。

什么算「垃圾话」（四条判据，命中一条进候选）：

    T1 纯确认    「白妈说可以」「白妈确认重载成功」「好」
                 —— 主体 + 确认词，没有对象、没有结论、没有后续

    T2 流水账    「鲸宝重启了」「砚砚交活了」
                 —— 有动作但没说结果是什么、值不值钱

    T3 重复     同一天里，同一件事被记了 N 遍（措辞可以不同）
                 —— 按「日期 + 主干 2-gram」聚类

    T4 断头话   「鲸宝问壳是否」「锦锦在群里发」
                 —— 半句话，动词后面没有宾语，读的人补不出来

每条候选都写明「命中哪条判据」「为什么」，让人一眼能反驳。
误判是允许的，看不清就标「拿不准」，宁可让人多看一眼，不可让机器自作主张。

用法：
    python trash_filter.py                 # 扫描 + 出清单
    python trash_filter.py --book 某本子.md
    python trash_filter.py --json          # 机器可读
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

DEFAULT_BOOK = os.environ.get("WHALE_LEDGER", "ledger.md")
OUT_DIR = os.environ.get("WHALE_EXPORT", os.path.join(os.environ.get("WHALE_HOME", "."), "export"))

NAMES = ("白彤鲸", "白彤芽", "白彤锦", "白彤砚", "鲸宝", "苗苗", "锦锦", "砚砚", "白妈", "彤妈", "_b1363")

# ---------------- 判据 ----------------

CONFIRM_TAIL = re.compile(r"(说可以|说行|说好|说行吧|点头|同意了|确认了|确认成功|重载成功|重启成功|说没问题|没问题了|好了|成了|对的|可以)$")
CONFIRM_ONLY = re.compile(r"^(白妈|彤妈|鲸宝|苗苗|锦锦|砚砚|他|她|白妈$)?\s*(说可以|说行|好|可以|嗯|对|同意了|点头)$")
FLOW_TAIL = re.compile(r"(交活了|交活了|交了|出交接单|验收全过|全绿|跑通了|重启了|修好了|改完了|写好了|装好了)$")
DANGLING_TAIL = re.compile(r"(是否|是不是|能不能|要不要|有没有|怎么|什么|为啥)$")


def body_of(s: str) -> str:
    return re.sub(r"^20\d\d-\d\d-\d\d\s*[·•]\s*", "", s).strip()


def has_info(body: str) -> bool:
    """有信息 = 有数字/路径/文件名/引号里的话/超过一定长度且有多个名词。"""
    if re.search(r"\d{2,}", body):
        return True
    if re.search(r"[A-Za-z_]{3,}", body):
        return True
    if re.search(r"[「『\"“].+[」』\"”]", body):
        return True
    if len(body) >= 45:
        return True
    return False


TRUNC_TAIL = re.compile(r"(让|问|希望|要|说|把|给|叫|和|跟|对|在|从|为|就|还|也|都|又|再|先|再|了|的|是)$")

RESULT_WORDS = re.compile(r"(全绿|全过|通过|三项|五项|七项|零报错|零依赖|全验收|已签|签字|行数)")

def has_result(body: str) -> bool:
    """有没有"结果"而不只是"动作"。"""
    return bool(RESULT_WORDS.search(body))


def judge(s: str) -> tuple[str | None, str]:
    """返回 (判据代号 or None, 理由)。"""
    body = body_of(s)
    n = len(body)

    # 【先判腰斩】短、且结尾停在虚词/动词上 —— 这不是垃圾话，是压缩把人话切断了。
    # 这两件事必须分开：垃圾话可以扔，腰斩的话得想办法补回来。
    if n <= 14 and TRUNC_TAIL.search(body):
        return "T5", f"只有 {n} 个字且断在虚词上——像是被压缩腰斩了，不是垃圾话"
    if n <= 8:
        return "T1", f"只有 {n} 个字，没内容"
    if CONFIRM_TAIL.search(body) and n <= 24 and not has_info(body):
        return "T1", "确认/结论类，没有对象和后续"
    if CONFIRM_ONLY.match(body):
        return "T1", "纯应答，零信息"
    # 「七项全绿」「1070 行」这种是结果，不是流水账
    if FLOW_TAIL.search(body) and n <= 30 and not has_info(body) and not has_result(body):
        return "T2", "只说了做过，没说做成什么样"
    # 「彤妈问火山引擎在模型提供商里叫什么」是完整的问题，不是半句
    if DANGLING_TAIL.search(body) and n > 18 and not body.endswith(("叫什么", "是什么", "怎么样")):
        return "T4", "半句话，后面没跟上结论"
    return None, ""


def main_gram(body: str) -> set[str]:
    x = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", body)
    for nm in NAMES:
        x = x.replace(nm, "")
    return {x[i:i + 2] for i in range(len(x) - 1)}


def jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if (a | b) else 0.0


def find_dups(items: list[tuple[str, str]], thresh: float = 0.55):
    """同日同主干的重复组。返回 [(keep_idx, [dup_idx...], 说明)]"""
    by_day = defaultdict(list)
    for i, (day, body) in enumerate(items):
        by_day[day].append(i)

    groups = []
    for day, idxs in by_day.items():
        grams = {i: main_gram(items[i][1]) for i in idxs}
        used = set()
        for a_pos, i in enumerate(idxs):
            if i in used:
                continue
            grp = [i]
            for j in idxs[a_pos + 1:]:
                if j in used:
                    continue
                if jaccard(grams[i], grams[j]) >= thresh:
                    grp.append(j)
            if len(grp) > 1:
                keep = max(grp, key=lambda k: len(items[k][1]))
                dups = [k for k in grp if k != keep]
                groups.append((keep, dups, f"同一天同一件事记了 {len(grp)} 遍"))
                used.update(grp)
    return groups


def scan(book: Path) -> dict:
    text = book.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
    m = re.search(r"^##\s*事情\s*$", text, re.M)
    if not m:
        return {"error": "本子里找不到 `## 事情` 这一节"}
    tail = text[m.end():]
    raw = [l[2:].strip() for l in tail.split("\n") if l.startswith("- ")]

    items = []
    for s in raw:
        day = s[:10] if re.match(r"^20\d\d-\d\d-\d\d", s) else "未知"
        items.append((day, body_of(s)))

    cands = []
    for i, s in enumerate(raw):
        code, why = judge(s)
        if code:
            cands.append({"idx": i, "text": s, "code": code, "why": why})

    dups = []
    for keep, ds, why in find_dups(items):
        for d in ds:
            dups.append({"idx": d, "text": raw[d], "code": "T3",
                         "why": f"{why}，建议保留第 {keep} 条"})

    # 合并去重（同一条可能命中多个判据）
    picked = {}
    for c in cands + dups:
        picked.setdefault(c["idx"], c)

    return {
        "book": str(book),
        "total": len(raw),
        "trash": sorted(picked.values(), key=lambda x: x["idx"]),
        "by_code": {c: sum(1 for x in picked.values() if x["code"] == c) for c in ("T1", "T2", "T3", "T4", "T5")},
    }


def render(r: dict) -> str:
    if "error" in r:
        return f"扫描失败：{r['error']}"
    now = time.strftime("%Y-%m-%d %H:%M")
    lines = [
        f"# 垃圾话清单 · {now}",
        "",
        f"本子：`{r['book']}`",
        f"事情共 **{r['total']}** 条，挑出 **{len(r['trash'])}** 条可疑。",
        "",
        "| 判据 | 意思 | 条数 |",
        "|---|---|---|",
        f"| T1 | 纯确认、零信息 | {r['by_code']['T1']} |",
        f"| T2 | 流水账，只说过做过 | {r['by_code']['T2']} |",
        f"| T3 | 同一天重复记 | {r['by_code']['T3']} |",
        f"| T4 | 半句话，没结论 | {r['by_code']['T4']} |",
        f"| T5 | **被压缩腰斩，该补不该删** | {r['by_code']['T5']} |",
        "",
        "> **这只是候选。** 看着不对的直接划掉，机器不删，等你点头。",
        "",
        "---",
        "",
    ]
    for x in r["trash"]:
        lines += [
            f"### {x['code']} · 第 {x['idx']} 条",
            f"- 原文：{x['text']}",
            f"- 为什么挑它：{x['why']}",
            "",
        ]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--book", default=DEFAULT_BOOK)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--out", default="")
    ap.add_argument("--no-write", action="store_true")
    a = ap.parse_args()

    book = Path(a.book)
    if not book.exists():
        # 第一次跑的人手里通常还没有本子 —— 这不是错误，是还没开始用
        print(f"没有找到本子：{book}")
        print("用法：python trash_filter.py --book <你的本子.md>")
        print("或者设环境变量 WHALE_LEDGER 指向它。")
        return 0

    r = scan(book)
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0

    report = render(r)
    if a.no_write:
        print(report)
        return 0

    os.makedirs(OUT_DIR, exist_ok=True)
    out = Path(a.out) if a.out else Path(OUT_DIR) / f"垃圾话_{time.strftime('%Y%m%d')}.md"
    out.write_text(report, encoding="utf-8", newline="\n")
    print(f"共 {r['total']} 条事，挑出 {len(r['trash'])} 条可疑")
    print(f"清单：{out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
