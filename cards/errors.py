# -*- coding: utf-8 -*-
"""errors.py —— 公共错题本读写模块（只管 公共/错题本.md，别的不碰）

格式（白妈定的四层）：
    ## 一、错题大类
    ### E-0001  细分问题
    - **发生时间**：…
    - **报错现象**：…
    - **根本原因**：…
    - **解决方法**：…（多行列表）
    - **解决日志**：…（多行列表）
    - **适用对象**：…

对外三个函数：
    add(title, symptom, cause, category="未分类", time="", solution=None,
        solution_log=None, reuse="") -> "E-xxxx"
    find(keyword) -> [dict]     # 关键词匹配标题/现象/根因/解法/大类/编号
    solve(entry_id, solution, solution_log, reuse="") -> bool
        # 编号精确匹配 E-xxxx，把状态改成已解决并补方法/日志/适用对象

兼容说明：老格式（## E-xxx 平铺一行）的条目会被解析读出，
solve 也能改，但写回时归到「未分类」大类。
"""

import os
import re
from datetime import datetime

BOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "错题本.md")
if not os.path.exists(BOOK):
    BOOK = "错题本.md"

DEFAULT_CATEGORY = "未分类"


def _read():
    if not os.path.exists(BOOK):
        return ""
    with open(BOOK, "r", encoding="utf-8") as f:
        return f.read()


def _write(text):
    """落盘前统一收边：空行不抖、小节标题前后各一个空行、末尾只留一个换行。

    错题本是给人看的，排版抖一下没事，抖一百下就成了乱码。
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.lstrip("\ufeff")
    # 三个以上连续空行 → 一个
    text = re.sub(r"\n{3,}", "\n\n", text)
    # 「## 大类」「### 细分」前面恰好一个空行
    text = re.sub(r"[ \t]*\n*\n(?=## )", "\n\n", text)
    text = re.sub(r"[ \t]*\n*\n(?=### )", "\n\n", text)
    # 「## 大类」前面恰好一个空行；「### 细分」标题下字段直接贴，不留空行（照白妈样板）
    # 注意：标题行尾要显式吃掉空行，别让 \n* 回溯把标题最后一个字（如"类"）挤到下一行（砚砚 17:13 修的坑）
    text = re.sub(r"(\n## [^\n]*[^\n#])[ \t]*\n+(?=[^\n#])", r"\1\n\n", text)
    text = re.sub(r"(\n### [^\n]*[^\n#])[ \t]*\n+(?=[^\n#])", r"\1\n", text)
    # 行尾多余空格
    text = "\n".join(l.rstrip() for l in text.split("\n"))
    return text.strip("\n") + "\n"


def _save(text):
    with open(BOOK, "w", encoding="utf-8", newline="\n") as f:
        f.write(_write(text))


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


# ---------- 编号 ----------

def _all_ids(text):
    return re.findall(r"### (E-\d+)", text)


def _next_id(text):
    num = 1
    for eid in _all_ids(text):
        m = re.match(r"E-(\d+)", eid)
        if m:
            num = max(num, int(m.group(1)) + 1)
    return "E-%04d" % num   # 四位编号


# ---------- 解析 ----------

def _parse_field(block, name, until):
    """取 - **name**： 直到 until 字段开始（含下一个 - ** 之前的所有行）"""
    m = re.search(r"- \*\*%s\*\*：\s*\n?(.*?)(?=\n- \*\*%s\*\*：|\Z)" % (name, until),
                  block, re.S)
    if not m:
        return ""
    return m.group(1).strip()


def parse_entry(block):
    """把一个 ### E-xxxx 块解析成 dict（新老格式都吃）"""
    m = re.match(r"###\s+(E-\d+)\s+(.*)", block.strip())
    if not m:
        return None
    d = {"id": m.group(1), "title": m.group(2).strip()}
    d["time"] = _parse_field(block, "发生时间", "报错现象")
    d["symptom"] = _parse_field(block, "报错现象", "根本原因")
    d["cause"] = _parse_field(block, "根本原因", "解决方法")
    d["solution"] = _parse_field(block, "解决方法", "解决日志")
    d["solution_log"] = _parse_field(block, "解决日志", "适用对象")
    d["reuse"] = _parse_field(block, "适用对象", "NO_SUCH_FIELD")
    # 老格式兜底：现象/根因挤在一行的
    if not d["symptom"]:
        m2 = re.search(r"现象：(.+?) 根因：(.+?)(?:\n|$)", block)
        if m2:
            d["symptom"], d["cause"] = m2.group(1).strip(), m2.group(2).strip()
    d["status"] = "已解决" if d["solution"] else "未解决"
    # 占位符不算解法：「（还没解决）」是 add 时写的默认字样，不是真解决了
    if d["solution"].strip() in ("（还没解决）", "（暂无）", "（无）"):
        d["solution"] = ""
        d["status"] = "未解决"
    return d


def _iter_entries(text):
    """按大类分块遍历，返回 [(大类, 块文本, dict|None)]；大类从 ## X、名称 取"""
    sections = re.split(r"(?=^## )", text, flags=re.M)
    for sec in sections:
        cat_m = re.match(r"##\s+(?:[一二三四五六七八九十\d]+、)?\s*(.+)", sec)
        if not cat_m or cat_m.group(1).startswith("E-"):
            continue   # 文档头（# 开头）或条目本身跳过
        cat = cat_m.group(1).strip()
        for bm in re.finditer(r"(### E-\d+.*?)(?=### E-\d+|\Z)", sec, re.S):
            yield cat, bm.group(1), parse_entry(bm.group(1))


# ---------- 格式化 ----------

def _fmt_list(v, prefix="  "):
    """字符串或字符串列表 -> 缩进列表文本"""
    if not v:
        return ""
    if isinstance(v, str):
        items = [v]
    else:
        items = [str(x) for x in v]
    return "\n".join("%s%d. %s" % (prefix, i + 1, x) for i, x in enumerate(items))


def _fmt_log(v, prefix="  "):
    if not v:
        return ""
    if isinstance(v, str):
        items = [v]
    else:
        items = [str(x) for x in v]
    return "\n".join("%s- %s" % (prefix, x) for x in items)


def _format_entry(d):
    """dict -> ### E-xxxx 块（字段分行）"""
    L = ["### %s  %s" % (d["id"], d["title"])]
    L.append("- **发生时间**：%s" % d.get("time", ""))
    L.append("- **报错现象**：%s" % (_fmt_list(d["symptom"], "") if isinstance(d.get("symptom"), list) else d.get("symptom", "")))
    L.append("- **根本原因**：%s" % (_fmt_list(d["cause"], "") if isinstance(d.get("cause"), list) else d.get("cause", "")))
    if d.get("solution"):
        L.append("- **解决方法**：")
        L.append(_fmt_list(d["solution"]))
    else:
        L.append("- **解决方法**：（还没解决）")
    if d.get("solution_log"):
        L.append("- **解决日志**：")
        L.append(_fmt_log(d["solution_log"]))
    else:
        L.append("- **解决日志**：（暂无）")
    L.append("- **适用对象**：%s" % d.get("reuse", ""))
    return "\n".join(L).rstrip()


# ---------- 对外 ----------

def add(title, symptom, cause, category=DEFAULT_CATEGORY, time="",
        solution=None, solution_log=None, reuse=""):
    """追加新条目。category 不传归「未分类」，新大类自动建节。返回四位编号。"""
    text = _read()
    eid = _next_id(text)
    entry = {
        "id": eid, "title": title,
        "time": time or _now(),
        "symptom": symptom, "cause": cause,
        "solution": solution, "solution_log": solution_log,
        "reuse": reuse,
    }
    block = _format_entry(entry)

    # 找到大类节（## X、名称 或 ## 名称），条目插到该节末尾
    cat_re = re.compile(
        r"##\s+(?:[一二三四五六七八九十\d]+、)?\s*%s\s*\n" % re.escape(category))
    sec_m = None
    for m in re.finditer(r"^## .*$", text, flags=re.M):
        mm = cat_re.match(m.group(0) + "\n")
        if mm:
            sec_m = m
            break

    # 找到下一个 ## 或文末，作为节的结束
    if sec_m:
        next_m = re.search(r"^## ", text[sec_m.end():], flags=re.M)
        insert_at = sec_m.end() + (next_m.start() if next_m else
                                   len(text) - sec_m.end())
        insert_at = text.rfind("\n", 0, insert_at + 1)
        text = text[:insert_at] + "\n\n" + block + "\n" + text[insert_at:]
        if insert_at >= len(text) - len(block) - 4:  # 插在文末
            text = text.rstrip() + "\n"
    else:
        # 新大类：建节（中文序号往后排）
        nums = "一二三四五六七八九十"
        top_secs = re.findall(r"^##\s+([一二三四五六七八九十\d]+)、", text, flags=re.M)
        idx = 0
        for t in top_secs:
            if t in nums:
                idx = max(idx, nums.index(t) + 1)
        heading = "## %s、%s" % (nums[idx] if idx < len(nums) else str(idx + 1),
                                 category)
        text = text.rstrip() + "\n\n" + heading + "\n\n" + block + "\n"
    _save(text)
    return eid


def find(keyword):
    """按关键词搜（编号/标题/大类/现象/根因/解法/日志/适用对象），返回 dict 列表"""
    if not keyword:
        return []   # 空关键词会命中所有条目，等于没搜（砚砚 002 审出）
    if not isinstance(keyword, str):
        return []
    text = _read()
    out = []
    for cat, _, d in _iter_entries(text):
        if not d:
            continue
        d["category"] = cat
        hay = " ".join(str(v) for v in d.values())
        if keyword in hay:
            out.append(d)
    return out


def solve(entry_id, solution, solution_log, reuse=""):
    """按四位编号精确匹配，补解决方法/日志/适用对象，状态改已解决。"""
    if not isinstance(entry_id, str) or not re.match(r"^E-\d{4}$", entry_id):
        return False   # None/非字符串不炸（砚砚 002 审出）
    text = _read()
    # 定位条目块
    m = re.search(r"(### %s\s.*?)(?=### E-\d+|^## |\Z)" % re.escape(entry_id),
                  text, re.S | re.M)
    if not m:
        return False
    d = parse_entry(m.group(1))
    d["solution"] = solution
    d["solution_log"] = solution_log
    if reuse:
        d["reuse"] = reuse
    new_block = _format_entry(d)
    text = text[:m.start()] + new_block + "\n" + text[m.end():]
    _save(text)
    return True


# ---------- 自测 ----------

if __name__ == "__main__":
    print("== 造两条假数据（一条带新大类，一条归未分类） ==")
    e1 = add("测试：壳被字符串炸了", "stderr 甩 traceback", "没判断 isinstance",
             category="代码与接口类",
             solution="补 isinstance 守卫", solution_log=["16:58 复测干净"],
             reuse="砚砚")
    e2 = add("测试：不传大类", "随便写的现象", "随便写的根因")
    print("新增：", e1, e2)

    print("\n== find('isinstance') ==")
    for d in find("isinstance"):
        print({k: d[k] for k in ("id", "category", "title", "status")})

    print("\n== solve('E-0002') 核编号匹配 ==")
    print("solve E-0002:", solve("E-0002", "补上权限",
                                 ["16:59 发布后正常"], reuse="所有 bot"))
    print("solve E-2   :", solve("E-2", "x", "y"), "（三位编号应挡住）")
    print("solve E-9999:", solve("E-9999", "x", "y"), "（不存在的应挡住）")

    print("\n== find('E-0002') ==")
    for d in find("E-0002"):
        print({k: d[k] for k in ("id", "category", "title", "status")})

    print("\n== 文件现在的样子 ==")
    print(_read())
