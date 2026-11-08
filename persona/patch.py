# -*- coding: utf-8 -*-
"""patch.py —— 人格卡按块改写器（T-0006 工作条 002，白彤锦 2026-11-08）

规矩（白妈定的，写死在代码里）：
  1. 只许按块改（## 身份 / ## 关系 / ## 三、性格 ……），不许整张覆盖。
     —— 今天砚砚改卡把「## 心跳」标题顶掉过，整张重写就会漏。
  2. 每次改完在卡尾追加一行：时间 + 谁 + 改了哪块 + 为什么。
  3. 改完自动调 validate（砚砚的校验器）。没写好就传 validator=None 跳过；
     没传也找不到 validate 模块时跳过，不炸。

对外：
    list_blocks(path) -> [块名, ...]
    patch_block(path, block, new_content, who, why, validator="__auto__") -> dict
        # validator="__auto__"（默认）：尝试 import validate 自动校验
        # validator=None：跳过校验
        # 返回 {"ok": bool, "data": {...}, "error": str}，data 里带 validate 结果
"""

import os
import re
from datetime import datetime

BLOCK_RE = re.compile(r"^## +(.+?)\s*$", re.M)


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _read(path):
    with open(path, "r", encoding="utf-8-sig") as f:
        return f.read().replace("\r\n", "\n")


def _write(path, text):
    tmp = path + ".tmp_patch"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)   # 原子替换，写一半不至于毁卡


def list_blocks(path):
    """列出这张卡现有的块名（## 开头的行）"""
    return BLOCK_RE.findall(_read(path))


def _norm_block_name(s):
    """把块名规整成可比的样子：去掉中文序号、去掉尾部括号说明。"""
    s = (s or "").strip()
    s = re.sub(r"^[一二三四五六七八九十百\d]+[、.．]\s*", "", s)
    s = re.sub(r"[（(][^）)]*[）)]\s*$", "", s)
    return s.strip()


def _find_block(text, block):
    """返回 (块头行, 块起点, 块终点)。块终点 = 下一个 ## 前或文末。

    块名匹配规则（2026-11-08 抹腻子补）：
      卡上写的是 `## 五、喜好与审美`，可人喊「喜好」。
      whoami 那边认短名，patch 这边不认 —— 同一张卡两套规矩，合并当天就露馅了。
      现在按三步走：精确 → 规整后比 → 别名表，**唯一命中才放行**，多个对上就报歧义，绝不猜。
    """
    want = block.strip()

    def _hit(m):
        nxt = BLOCK_RE.search(text, m.end())
        return (m, m.start(), nxt.start() if nxt else len(text))

    # 1) 精确
    exact = [m for m in BLOCK_RE.finditer(text) if m.group(1).strip() == want]
    if len(exact) == 1:
        return _hit(exact[0])
    if len(exact) > 1:
        return None, -1, "歧义：「%s」对上 %d 个块" % (want, len(exact))

    # 2) 规整后比（去序号、去括号）
    want_n = _norm_block_name(want)
    norm_hits = [m for m in BLOCK_RE.finditer(text) if _norm_block_name(m.group(1)) == want_n]
    if len(norm_hits) == 1:
        return _hit(norm_hits[0])
    if len(norm_hits) > 1:
        return None, -1, "歧义：「%s」对上 %d 个块，请说清楚" % (want, len(norm_hits))

    # 3) 别名（借 whoami 的表，两边别再各写一套）
    try:
        import whoami as _who
        alias = _who.ALIASES.get(want, [])
    except Exception:
        alias = []
    if alias:
        alias_hits = [m for m in BLOCK_RE.finditer(text)
                      if _norm_block_name(m.group(1)) in alias]
        if len(alias_hits) == 1:
            return _hit(alias_hits[0])
        if len(alias_hits) > 1:
            return None, -1, "歧义：「%s」经别名对上 %d 个块" % (want, len(alias_hits))

    return None, -1, ""




def _run_validate(path, validator):
    """跑校验器。返回 dict：{"ran": bool, "result": ..., "note": str}"""
    mod = None
    if validator == "__auto__":
        try:
            import validate as mod   # 砚砚那份，同目录
        except Exception:
            return {"ran": False, "result": None,
                    "note": "validate 模块还没就位，跳过校验（接口留着）"}
        fn = getattr(mod, "validate", None) or getattr(mod, "validate_card", None)
    elif validator is None:
        return {"ran": False, "result": None, "note": "validator=None，按约定跳过"}
    else:
        fn = validator
    if fn is None:
        return {"ran": False, "result": None, "note": "校验器没提供入口，跳过"}
    try:
        return {"ran": True, "result": fn(path), "note": ""}
    except Exception as e:
        return {"ran": False, "result": None,
                "note": "校验器自己炸了（不影响落盘）：%s: %s" % (type(e).__name__, e)}


def patch_block(path, block, new_content, who, why, validator="__auto__"):
    """把某个 ## 块的正文换成 new_content（块标题行保留）。

    - block 不存在 → 拒绝（新建块要明说，见 add_block）
    - why 不写 → 拒绝（留痕是硬规矩）
    - 整张覆盖 → 没这个入口，想都别想
    """
    if not why or not str(why).strip():
        return {"ok": False, "data": None, "error": "为什么改得写清楚（留痕规矩）"}
    if not who or not str(who).strip():
        return {"ok": False, "data": None, "error": "谁改的得写（留痕规矩）"}
    if not os.path.exists(path):
        return {"ok": False, "data": None, "error": "卡不存在：%s" % path}

    text = _read(path)
    m, start, end = _find_block(text, block)
    if m is None:
        # _find_block 对不上时第三位可能是报错说明（如「歧义：对上 2 个块」），优先透传
        detail = end if isinstance(end, str) and end else ""
        if detail:
            return {"ok": False, "data": None, "error": detail}
        return {"ok": False, "data": None,
                "error": "没有「%s」这个块。现有的：%s" % (block, "、".join(list_blocks(path)))}

    # 块头保留，只换正文
    new_content = str(new_content).replace("\r\n", "\n").strip("\n")
    new_text = text[:m.end()] + "\n" + new_content + "\n\n" + text[end:].lstrip("\n")
    # 卡尾留痕
    stamp = "- %s %s：改了〔%s〕——%s\n" % (_now(), who.strip(), block.strip(),
                                           str(why).strip())
    if not new_text.endswith("\n"):
        new_text += "\n"
    new_text += "\n## 改卡记录\n\n" if "## 改卡记录" not in new_text else "\n"
    new_text = new_text.rstrip("\n") + "\n" + stamp
    _write(path, new_text)

    v = _run_validate(path, validator)
    return {"ok": True,
            "data": {"block": block.strip(), "log_line": stamp.strip(),
                     "validate": v},
            "error": ""}


def add_block(path, block, new_content, who, why, validator="__auto__"):
    """新建一个块（只在块确实不存在时用）。"""
    if not why or not str(why).strip() or not who or not str(who).strip():
        return {"ok": False, "data": None, "error": "谁改的、为什么改，都得写（留痕规矩）"}
    if not os.path.exists(path):
        return {"ok": False, "data": None, "error": "卡不存在：%s" % path}
    text = _read(path)
    m, _, _ = _find_block(text, block)
    if m is not None:
        return {"ok": False, "data": None,
                "error": "「%s」已经有了，要改内容用 patch_block，别重复建" % block}
    stamp = "- %s %s：新建〔%s〕——%s" % (_now(), who.strip(), block.strip(),
                                        str(why).strip())
    new_text = text.rstrip("\n") + "\n\n## %s\n\n%s\n\n## 改卡记录\n\n%s\n" % (
        block.strip(), str(new_content).strip("\n"), stamp) \
        if "## 改卡记录" not in text else \
        text.rstrip("\n") + "\n\n## %s\n\n%s\n\n%s\n" % (
        block.strip(), str(new_content).strip("\n"), stamp)
    _write(path, new_text)
    v = _run_validate(path, validator)
    return {"ok": True, "data": {"block": block.strip(), "log_line": stamp,
                                 "validate": v}, "error": ""}


# ---------- 自测 ----------

if __name__ == "__main__":
    import shutil
    import tempfile

    here = os.path.dirname(os.path.abspath(__file__))
    # 自测自己造一张临时卡，不依赖仓库里有没有示例卡
    import tempfile
    _tmp = tempfile.mkdtemp()
    test_card = os.path.join(_tmp, "SelfTest.md")
    open(test_card, "w", encoding="utf-8").write(
        "# 我是自测\n\n## 一、身份\n- **名字**：自测\n\n## 二、关系\n| 谁 | 怎么叫 |\n|---|---|\n\n"
        "## 三、性格\n- 做出来就凑过去，说你看你看\n\n## 四、怎么说话\n- 一句一句说\n")
    print("试验田：%s（真卡没动）\n" % test_card)

    print("== 现有块 ==")
    print(list_blocks(test_card))

    print("\n== 1) 正常改一块（性格） ==")
    full = [b for b in list_blocks(test_card) if "性格" in b][0]
    r = patch_block(test_card, full, "1. **手上永远在弄点东西**——改测试用的正文",
                    "白彤锦", "试验：验证按块改不留疤")
    print("ok =", r["ok"], "| validate:", r["data"]["validate"]["ran"],
          r["data"]["validate"]["note"][:40])

    print("\n== 2) 不写为什么 → 拒 ==")
    r = patch_block(test_card, "身份", "x", "白彤锦", "")
    print("ok =", r["ok"], "| error:", r["error"])

    print("\n== 3) 不存在的块 → 拒，且列出有的 ==")
    r = patch_block(test_card, "不存在的块", "x", "白彤锦", "试试")
    print("ok =", r["ok"], "| error:", r["error"][:80])

    print("\n== 4) 新建块 ==")
    r = add_block(test_card, "附·测试块", "测试内容", "白彤锦", "试验新建")
    print("ok =", r["ok"])

    print("\n== 5) 改完以后：别的块原封不动？ ==")
    after = _read(test_card)
    print("块列表:", list_blocks(test_card))
    print("「## 关系」还在:", "## 二、关系" in after or "## 关系" in after)
    print("「怎么说话」还在:", "怎么说话" in after)
    print("心跳/自查没被顶掉:", "自查" in after)

    print("\n== 6) 卡尾留痕 ==")
    log_i = after.find("## 改卡记录")
    print(after[log_i:log_i + 220] if log_i >= 0 else "（没找到改卡记录！）")

    print("\n== 改完的卡长这样（头 30 行） ==")
    print("\n".join(after.splitlines()[:30]))
