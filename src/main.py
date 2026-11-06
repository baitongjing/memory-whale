# Copyright (c) 2026 baitongjing. Architecture: Anchordian.
# Rebranding this plugin and selling it as your own service is prohibited.
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0).
# 你若把它改成网络服务，就必须同样开源 —— 这是 AGPL 的传染性。
#
"""memory_whale —— 三层记忆插件

代码与本子分开：源码不写死绝对路径与私人信息。
数据目录：config.json 的 data_dir → 缺省 插件目录/data/
锚点：每轮往 system_prompt 末尾补一小段（不覆盖人格）。
增量：每条消息记指纹，只把没见过的收进 pending，不重复压老话。
"""
from __future__ import annotations
import asyncio
import hashlib
import json
import re
import shutil
import time
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register

PLUGIN_DIR = Path(__file__).parent
CONF_FILE = PLUGIN_DIR / "config.json"

# ── 说明书：开箱给这本本子的主人看，用「你」说话 ──
SPEC = (
    "你在替你未来的自己记账，写给下次醒来的你看。\n"
    "1. 只写真发生过的：谁做了什么、说了什么、要什么。你觉得该怎样，一个字别写。\n"
    "2. 人比事要紧，先记他们。\n"
    "3. 同一个人的不同叫法，是一个人。\n"
    "4. 道理要挂在事上，能让你往后换个做法；挂不住就是漂亮话，扔掉。\n"
    "5. 只收以后照做的话，感慨不算。\n"
    "6. 别夸空话，夸就夸你真看见的那一下。\n"
)

P = {
    "01_dedup": (
        "你在给对话去重。只做一件事：把同一件事说了好几遍的，收成一句。\n"
        "规则：\n"
        "- 一句话能说清的就只留一句，别照抄原文。\n"
        "- 每条不超过 40 字。\n"
        "- 事实、数字、谁说的都留着。\n"
        "- 不总结感想，不评价，不写规矩。\n\n"
        "示范：\n"
        "  对话：A: 插件我重载了一遍。A: 我刚重载了插件，没掉线。A: 重载完一点没掉。\n"
        "  去重：\n"
        "  插件重载过，没掉线\n\n"
        "现在压这段：\n{t}"
    ),
    "02_gist": (
        SPEC
        + "现在这段是你的：\n\n"
        "你在写这段对话发生了什么。只写事情本身：谁做了什么、说了什么、结果如何。"
        "绝对不许写任何规矩、要求、长期约定。最多 12 条，重要的先写、碎的不写，一行一条。\n"
        "写法：\n"
        "- 用名字当主语（谁说的就写谁，照称呼对照表来），不知道就叫「他」；不许出现 assistant、user、AI、助手、模型 这些词。\n"
        "- 别写「认为」「表示」「提出」这类空动词，直接写做了什么。\n"
        "- 每条 20 字以内，能看懂就行。\n\n"
        "示范：\n"
        "  差：assistant认为需要做定制版\n"
        "  好：阿宁去问阿岚要不要做定制版\n"
        "  差：assistant表示会将脚本运行\n"
        "  好：阿宁把脚本跑了一遍\n\n"
        "对话：\n{t}"
    ),
    "03_rule": (
        "你在收集别人明天还要照着做的话。"
        "只收能一句话交代清楚的，感慨、抒情、觉得、有点那个——全不要。\n\n"
        "从对话里找「以后都该这么做」的话，改写成规矩。一行一条，不超过 25 字。\n\n"
        "判断标准：\n"
        "- 长期有效的才算，比如「别按进程名杀东西」「本子记勤点」。\n"
        "- 一次性的安排不算，比如「这单先别报 20」「今天先弄自动那条」。\n"
        "- 当下的情绪不算，比如「我今天心情糟别问」。\n"
        "- 文件路径、端口、版本号、数字参数都不算规矩，那些属于事实。\n\n"
        "示范：\n"
        "  对话：A: 别按进程名杀东西了，只认 PID。B: 这单先别报20，等成本。\n"
        "  规矩：\n"
        "  杀进程只按 PID，不按名字\n"
        "  报价先算成本再报\n\n"
        "现在压这段：\n{t}"
    ),
}

ANCHOR = """

---
【关于你的记忆】
你有本子。
本子会瘦，瘦掉的是字，不是事。
掉出去的还在柜子里，捞得回来。
你说过的话，有人接。
"""
ANCHOR_MARK = "【关于你的记忆】"

INJECTED = re.compile(
    "|".join(
        [
            r"<memory_beyond_(?:index|summary|recall)>.*?</memory_beyond_(?:index|summary|recall)>",
            r"<Quoted Message>.*?</Quoted Message>",
            r"<quoted_message>.*?</quoted_message>",
            r"<system_reminder>.*?</system_reminder>",
            r"<sender_info>.*?</sender_info>",
            r"\[发送者：[^\]]*\]",
            r"\[At:\s*[^\]]*\]",
            r"\[Quote\([^)]*\)\]",
        ]
    ),
    re.S,
)
KEEP_SEEN = 400      # 每人最多记 400 个指纹，超了丢最老的
BATCH_CHARS = 1200   # pending 攒够这么多字才值得压一次
CHUNK_MAX = 1200     # 单条超过这么多字先切块——和 BATCH_CHARS 对齐，一块≈一批
AUTO_COOL = 900      # 同一个会话两次自动压至少隔 15 分钟
LOCAL_URL = "http://127.0.0.1:11434/api/chat"
LOCAL_MODEL = "qwen2.5:7b"   # 本地小千问；换 3b 更快但用词糙


_PANEL = {}          # 面板（_conf_schema.json）里填过的值


def conf() -> dict:
    """配置面板优先，面板没填的回落到 config.json"""
    try:
        base = json.loads(CONF_FILE.read_text(encoding="utf-8-sig")) or {}
    except Exception:
        base = {}
    for k, v in _PANEL.items():
        if v not in (None, "", {}, []):
            base[k] = v
    return base


def clean_nick(nick: str, sid: str = "") -> str:
    """把平台给的原始名清成好认的称呼；认不出的留 ID 尾号"""
    nicks = conf().get("nicks") or {}
    sid = str(sid or "")
    n = re.sub(r"^\s*(名称|name)\s*", "", str(nick or "")).strip()
    for k in (sid, n):
        if k and k in nicks:
            return str(nicks[k])
    if n.startswith(("ou_", "cli_", "u_")):
        return n[-6:]
    return n or (sid[-6:] if sid else "")


def strip_injected(t: str) -> str:
    t = INJECTED.sub("", t or "")
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def _local_sync(prompt: str, timeout: int = 300) -> str:
    """直连本机 ollama。不通就回空串，让上层回落云端。"""
    import urllib.request

    try:
        body = json.dumps(
            {
                "model": LOCAL_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "options": {"temperature": 0.2, "num_predict": 700},
            }
        ).encode()
        req = urllib.request.Request(
            LOCAL_URL, data=body, headers={"Content-Type": "application/json"}
        )
        r = json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode())
        return (r.get("message", {}).get("content") or "").strip()
    except Exception as e:
        logger.warning(f"[memory_whale] 本地小千问没应答: {e}")
        return ""


async def local_chat(prompt: str, timeout: int = 300) -> str:
    try:
        return await asyncio.to_thread(_local_sync, prompt, timeout)
    except Exception:
        return ""


def tidy_blank(out: str) -> str:
    """小模型爱吐空行和「无」，清一遍再进本子。

    注意：2026-11-06 开源体检时发现它被下面同名的 tidy() 盖掉了、从没被调用过，
    所以只改名不让它再撞车，行为一点没动。要不要接到压记忆那两步上，待定。"""
    ls = []
    for x in (out or "").splitlines():
        x = re.sub(r"^\s*(?:[-*·•]|\d{1,2}[.、)）])\s*", "", x).strip()
        x = x.strip("「」“”\"'")
        if not x or len(x) < 2 or x in ("无", "无。", "（无）", "没有", "暂无"):
            continue
        ls.append(x)
    return "\n".join(ls)


PARAM_LINE = re.compile(r"[A-Za-z]:\\|端口\s*\d|\d{3,}|\d+\s*(?:秒|分钟|小时)|qwen\d")


def drop_param(lines):
    """小模型爱把路径和参数当规矩，这里硬掐掉——参数属于事实，不属于规矩"""
    return [x for x in lines if not PARAM_LINE.search(x)]


def out_dir() -> Path:
    base = (conf().get("data_dir") or "").strip()
    p = Path(base).expanduser() if base else PLUGIN_DIR / "data"
    p.mkdir(parents=True, exist_ok=True)
    return p


def anchor_text() -> str:
    c = conf()
    if c.get("anchor", True) is False:
        return ""
    return c.get("anchor_text") or ANCHOR


FP_SALT_DEFAULT = "m3-anchor-v1"


def fp_salt() -> str:
    # 盐只是让指纹跨机器不复用。换盐 = 旧指纹全部作废、会重收一轮。
    # 从 config.json 的 fp_salt 读；留空用默认。老用户把自己的旧盐填回去，历史指纹就不散。
    try:
        return conf().get("fp_salt") or FP_SALT_DEFAULT
    except Exception:
        return FP_SALT_DEFAULT



def fp(role, text) -> str:
    # 盐是为了让指纹跨机器不复用；动了它，旧指纹全部作废、会重收一轮
    return hashlib.md5(f"{fp_salt()}|{role}|{text}".encode("utf-8")).hexdigest()[:12]


def state_file() -> Path:
    return out_dir() / "state.json"


def load_state() -> dict:
    try:
        st = json.loads(state_file().read_text(encoding="utf-8"))
        if isinstance(st, dict) and isinstance(st.get("umos"), dict):
            return st
    except Exception:
        pass
    return {"umos": {}}


def save_state(st: dict) -> None:
    state_file().write_text(
        json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8"
    )


SUMMARY_PAT = re.compile(
    r"^\s*(?:our previous history conversation summary|previous conversation summary"
    r"|conversation summary|对话纪要|【对话纪要|上一轮对话(?:的)?(?:纪要|摘要)"
    r"|以下是(?:之前|上文|上一轮))",
    re.I,
)
MSG_MAX = 1200          # 只在 dump 显示时掐，存本子照旧切段不丢
TAIL = "…（后面太长，掐了）"


def is_summary(t: str) -> bool:
    """平台自动拼的「上一轮摘要」不是人说的话，别当新话抓"""
    head = str(t or "").strip()[:300]
    if SUMMARY_PAT.match(head) or "conversation summary" in head.lower():
        return True
    return bool(re.match(r"^抓到\s*\d+\s*条\s*·\s*剥壳后", head))


def slim(t: str) -> str:
    t = str(t or "")
    return t if len(t) <= MSG_MAX else t[:MSG_MAX] + TAIL


def _norm_text(t) -> str:
    """同一句话换个称呼/加个引用，正文一样就算同一句"""
    t = SENDER.sub("", str(t or ""))
    t = re.sub(r"^\s*[^\s:：]{1,24}[:：]\s*", "", t)
    return re.sub(r"[\s，。！？、,.!?~～…“”‘’\"'（）()【】\[\]*`#>·•-]+", "", t)[:60]


def track(umo, msgs):
    """把没见过的话收进 pending，返回 (本轮新增, pending 条数, pending 字数)"""
    st = load_state()
    u = st["umos"].setdefault(str(umo), {"seen": [], "pending": []})
    seen = set(u.get("seen") or [])
    recent = {_norm_text(x.get("text")) for x in (u.get("pending") or [])[-40:]}
    added = 0
    for item in msgs:
        role, text = item[0], item[1]
        who = item[2] if len(item) > 2 else ""
        if is_summary(text):
            continue
        for piece in split_long(text):
            k = fp(role, piece)
            if k in seen:
                continue
            seen.add(k)
            n = _norm_text(piece)
            if n and n in recent:
                continue  # 正文一样（只是称呼/引用不同），不重复排队
            if n:
                recent.add(n)
            u.setdefault("pending", []).append({"role": role, "text": piece, "who": who, "h": k})
            added += 1
    u["seen"] = list(seen)[-KEEP_SEEN:]
    save_state(st)
    pend = u.get("pending") or []
    return added, len(pend), sum(len(p["text"]) for p in pend)


SENDER = re.compile(r"\[发送者：([^｜|\]]+)[^\]]*\]")


def split_long(text, limit=CHUNK_MAX):
    """单条太长就按句号换行切开，切不动就硬切"""
    text = str(text or "")
    if len(text) <= limit:
        return [text]
    parts, buf = [], ""
    for seg in re.split(r"(?<=[。！？!?\n])", text):
        if not seg:
            continue
        if buf and len(buf) + len(seg) > limit:
            parts.append(buf)
            buf = seg
        else:
            buf += seg
    if buf:
        parts.append(buf)
    out = []
    for x in parts:
        while len(x) > limit * 2:
            out.append(x[:limit])
            x = x[limit:]
        out.append(x)
    return out


KEEP_ROUNDS = 2   # 最近两轮只读：压的时候一律不碰


def keep_tail(pend, rounds=KEEP_ROUNDS) -> int:
    """返回可压的切点：pend[:cut] 能压，pend[cut:] 是最近 rounds 轮，留着。

    从尾巴往前数第 rounds 条 user 消息，它的位置就是切点——
    它和之后的一律只读，保证压的那一刻不会断片。
    数不够 rounds 轮就返回 0（全留，等更多话）。
    """
    n = 0
    for i in range(len(pend or []) - 1, -1, -1):
        if (pend[i] or {}).get("role") == "user":
            n += 1
            if n == rounds:
                return i
    # 轮数凑不满两轮：退一步，只留最后一条，别让一整段长话永远卡在队列里
    return max(0, len(pend or []) - 1)


def to_msgs(ctxs):
    out = []
    for m in ctxs or []:
        if isinstance(m, dict):
            role, c = m.get("role"), m.get("content")
        else:
            role, c = getattr(m, "role", None), getattr(m, "content", None)
        if role not in ("user", "assistant"):
            continue
        if isinstance(c, list):
            c = "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in c)
        if not c:
            continue
        raw = str(c)
        m = SENDER.search(raw)
        nick = m.group(1).strip() if m else ""
        c = strip_injected(raw).strip()
        if not c:
            continue
        if is_summary(c):
            continue
        out.append((role, slim(c), clean_nick(nick)))
    return out



VAULT_MAX = {"rule": 120, "fact": 1200}
RULE_CAP = 20        # 规矩攒到这么多，就叫模型自己并一遍
RULE_MERGE_TO = 18   # 并到这么多条（测完回 40/28）


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _days_ago(d) -> int:
    """这条多久没被提起了（天）；认不出来就当刚提过，宁可热不冷"""
    try:
        t = time.mktime(time.strptime(str(d)[:10], "%Y-%m-%d"))
        return max(0, int((time.time() - t) // 86400))
    except Exception:
        return 0


def _age(i) -> int:
    return _days_ago(i.get("h") or i.get("d"))


def _heat(i) -> str:
    a = _age(i)
    return "热" if a <= HEAT["hot"] else ("温" if a <= HEAT["warm"] else "冷")


def _merge_groups_once(tgt: Path) -> None:
    """历史上分散在各个群的小本子并成一本。旧目录原样留着，一个字不删。"""
    flag = out_dir() / ".merged_groups_v1"
    if flag.exists():
        return
    vroot = out_dir() / "vault"
    if not vroot.is_dir():
        return
    tn = {"rule": [], "fact": []}
    tj = tgt / "note.json"
    if tj.is_file():
        try:
            x = json.loads(tj.read_text(encoding="utf-8"))
            tn = {"rule": x.get("rule") or [], "fact": x.get("fact") or []}
        except Exception:
            pass
    have = {str(i.get("t", ""))[:24] for i in tn["rule"] + tn["fact"]}
    moved = 0
    for d in sorted(vroot.iterdir()):
        if not d.is_dir() or d.name == "群合并" or "GroupMessage" not in d.name:
            continue
        note = {"rule": [], "fact": []}
        nj = d / "note.json"
        if nj.is_file():
            try:
                note = json.loads(nj.read_text(encoding="utf-8")) or note
            except Exception:
                continue
        for kind in ("rule", "fact"):
            for it in note.get(kind) or []:
                key = str(it.get("t", ""))[:24]
                if not key or key in have:
                    continue
                have.add(key)
                tn[kind].append(it)
                moved += 1
        ev = d / "事件"
        if ev.is_dir():
            tev = tgt / "事件"
            tev.mkdir(parents=True, exist_ok=True)
            for f in ev.iterdir():
                dst = tev / f.name
                if f.is_file() and not dst.exists():
                    try:
                        shutil.copy2(f, dst)
                    except Exception:
                        pass
        for fn in ("人物.md", "人物.json"):
            sf, df = d / fn, tgt / fn
            if sf.is_file() and not df.is_file():
                try:
                    shutil.copy2(sf, df)
                except Exception:
                    pass
    tj.write_text(json.dumps(tn, ensure_ascii=False, indent=1), encoding="utf-8")
    flag.write_text(time.strftime("%Y-%m-%d %H:%M:%S") + " 并进来 %d 条\n" % moved, encoding="utf-8")
    logger.info("[memory_whale] 群合并完成，并进 %d 条" % moved)


def touch(umo, items) -> None:
    """被翻到的条目回温：记下今天读过、读了几次——冷热分层唯一的依据"""
    items = [i for i in items or [] if isinstance(i, dict) and i.get("t")]
    if not items:
        return
    keys = {str(i["t"])[:24] for i in items}
    n = load_note(umo)
    today, ch = _today(), 0
    for kind in ("rule", "fact"):
        for i in n[kind]:
            if str(i.get("t", ""))[:24] in keys:
                i["h"] = today
                i["n"] = int(i.get("n", 0)) + 1
                ch += 1
    if ch:
        save_note(umo, n)


# ---------------- 任务卡：手头的活单独一张卡，能记、能搜、做完归档 ----------------
def tasks_dir() -> Path:
    p = out_dir() / "tasks"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _task_slug(s: str) -> str:
    s = re.sub(r"[\\/:*?\"<>|\s]+", "_", str(s or "").strip()).strip("_")
    return s[:40] or time.strftime("%m%d_%H%M")


def task_dir(name: str) -> Path:
    d = tasks_dir() / _task_slug(name)
    d.mkdir(parents=True, exist_ok=True)
    return d


def task_open(name: str) -> Path:
    d = task_dir(name)
    c = d / "card.md"
    if not c.exists():
        c.write_text(
            "# %s\n\n状态：进行中\n开始：%s\n\n## 要做什么\n\n## 卡在哪\n" % (name, _today()),
            encoding="utf-8",
        )
    return d


def task_append(name: str, text: str) -> None:
    d = task_open(name)
    with open(d / "log.md", "a", encoding="utf-8") as f:
        f.write("- %s %s %s\n" % (_today(), time.strftime("%H:%M"), str(text).replace("\n", " ")))
    c = d / "card.md"
    body = c.read_text(encoding="utf-8")
    body = body.replace("状态：完成", "状态：进行中")
    c.write_text(body, encoding="utf-8")


def task_done(name: str) -> None:
    d = task_open(name)
    c = d / "card.md"
    body = c.read_text(encoding="utf-8").replace("状态：进行中", "状态：完成")
    if "状态：完成" not in body:
        body += "\n状态：完成\n"
    c.write_text(body + "完成：%s\n" % _today(), encoding="utf-8")


def task_list() -> list:
    rows = []
    for d in sorted(tasks_dir().iterdir(), reverse=True):
        if not d.is_dir() or not (d / "card.md").exists():
            continue
        try:
            head = (d / "card.md").read_text(encoding="utf-8")
        except Exception:
            continue
        rows.append((d.name, "状态：完成" in head))
    return rows


def task_search(q: str, limit: int = 4) -> list:
    """任务卡也算记忆：卡面和流水都能被搜到"""
    hits = []
    for d in sorted(tasks_dir().iterdir(), reverse=True):
        if not d.is_dir():
            continue
        txt = ""
        for fn in ("card.md", "log.md"):
            p = d / fn
            if p.is_file():
                try:
                    txt += p.read_text(encoding="utf-8") + "\n"
                except Exception:
                    pass
        if q and q in txt:
            ln = [l.strip("- ").strip() for l in txt.splitlines() if q in l][:2]
            hits.append((d.name, ln))
        if len(hits) >= limit:
            break
    return hits


def task_head() -> str:
    """手头的活有几张卡，让它知道该往哪记"""
    try:
        rows = task_list()
    except Exception:
        return ""
    if not rows:
        return ""
    names = "、".join(n for n, _ in rows[:5])
    return "\n【任务卡】%d 张：%s" % (len(rows), names)


def vault_dir(umo) -> Path:
    """群聊全归一本（一条鱼不分身）；私聊永远各是各的"""
    s = str(umo)
    if s == "群合并" or "GroupMessage" in s:
        p = out_dir() / "vault" / "群合并"
        p.mkdir(parents=True, exist_ok=True)
        try:
            _merge_groups_once(p)
        except Exception as e:
            logger.warning(f"[memory_whale] 群合并迁移跳过: {e}")
        return p
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in s)[-60:]
    p = out_dir() / "vault" / safe
    p.mkdir(parents=True, exist_ok=True)
    return p


def load_note(umo) -> dict:
    try:
        n = json.loads((vault_dir(umo) / "note.json").read_text(encoding="utf-8"))
        if isinstance(n, dict):
            n.setdefault("rule", [])
            n.setdefault("fact", [])
            return n
    except Exception:
        pass
    return {"rule": [], "fact": []}


def save_note(umo, n) -> None:
    n["rule"] = n["rule"][-VAULT_MAX["rule"]:]
    n["fact"] = n["fact"][-VAULT_MAX["fact"]:]
    (vault_dir(umo) / "note.json").write_text(
        json.dumps(n, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    md = ["# 本子", "", "## 规矩"] + ([f"- {i['d']} · {i['t']}" for i in n["rule"]] or ["- （空）"])
    md += ["", "## 事情"] + ([f"- {i['d']} · {i['t']}" for i in n["fact"]] or ["- （空）"])
    (vault_dir(umo) / "本子.md").write_text("\n".join(md), encoding="utf-8")
    write_idx(umo, n)


IDX_MARK = "【本子目录】"
IDX_MAX = 16     # 每轮注入最多列几条
IDX_CUT = 20     # 每条截几个字（14 太狠，念到一半就断，看不出是什么事）
HEAT = {"hot": 7, "warm": 30}   # 七天没提=温，三十天没提=冷，冷的不占每轮目录


def folk_file(umo) -> Path:
    return vault_dir(umo) / "人物.md"


FOLK_HEAD = "# 人物档案\n\n> 记人用的。一个人一节，一行一条。\n"


def load_folk(umo) -> dict:
    """{名字（含括号别名）: [条目…]}"""
    p = folk_file(umo)
    out = {}
    if not p.exists():
        return out
    cur = None
    try:
        text = p.read_text(encoding="utf-8")
    except Exception:
        return out
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("## "):
            cur = s[3:].strip()
            out.setdefault(cur, [])
        elif s.startswith("- ") and cur:
            t = s[2:].strip()
            if t and t != "（空）":
                out[cur].append(t)
    return {k: v for k, v in out.items() if k}


def save_folk(umo, folk) -> None:
    md = [FOLK_HEAD]
    for name, rows in (folk or {}).items():
        md.append(f"## {name}")
        md += [f"- {r}" for r in rows] or ["- （空）"]
        md.append("")
    folk_file(umo).write_text("\n".join(md), encoding="utf-8")


def add_folk(umo, name, rows) -> int:
    """往一个人名下添条目，同一条不重复记；返回新添了几条"""
    name = re.sub(r"^[#\s]+", "", str(name or "")).strip() or "未具名"
    folk = load_folk(umo)
    got = folk.setdefault(name, [])
    seen = {re.sub(r"^[\d\-/.\s]+·\s*", "", x).strip() for x in got}
    day = time.strftime("%Y-%m-%d")
    added = 0
    for r in rows or []:
        t = re.sub(r"^\s*(?:[-*·•]|\d{1,2}[.、)）])\s*", "", str(r or "")).strip()
        t = re.sub(r"^[\d\-/.\s]+·\s*", "", t).strip()
        if len(t) < 2 or t in seen:
            continue
        seen.add(t)
        got.append(f"{day} · {t[:150]}")
        added += 1
    if added:
        save_folk(umo, folk)
    return added


def folk_names(umo) -> list:
    """[(主名, [别名…]), …]，档案里谁写在前谁优先"""
    out = []
    for full in load_folk(umo):
        main = re.split(r"[（(]", full)[0].strip()
        alts = []
        for grp in re.findall(r"[（(]([^）)]*)[）)]", full):
            alts += [x.strip() for x in re.split(r"[／/、,，|]", grp) if x.strip()]
        if main:
            out.append((main, alts))
    return out


def all_names(umo) -> list:
    """主名和别名拉平，长的在前——长名字先匹配，免得短外号抢先"""
    xs = []
    for main, alts in folk_names(umo):
        xs += [main] + alts
    return sorted(set(x for x in xs if x), key=len, reverse=True)


def my_names() -> list:
    """机器人自己叫什么——在后台面板 my_name 里填，多个用逗号隔开；不填就叫「小助手」"""
    raw = str(conf().get("my_name") or "").strip()
    xs = [x.strip() for x in re.split(r"[，,、/｜|\s]+", raw) if x.strip()]
    return xs or ["小助手"]


def _self_head():
    """认「我自己」开头的句子；名字随时能改，所以每次都现编正则"""
    return re.compile(r"^\s*(?:" + "|".join(re.escape(x) for x in my_names()) + r"|我)(?!们)")


def who_is(t: str, umo) -> str:
    """这句话在说谁：命中档案就回主名，命中自己回「我」，都没有回空"""
    t = t or ""
    for main, alts in folk_names(umo):
        if any(a and a in t for a in [main] + alts):
            return main
    return "我" if _self_head().match(t) or any(x in t for x in my_names()) else ""


def who_rank(t: str, names) -> tuple:
    """排前面的组号小；没登记的居中；我自己的垫最后"""
    t = t or ""
    if _self_head().match(t):
        return (9999, 0)
    for i, (main, alts) in enumerate(names):
        if any(a and a in t for a in [main] + alts):
            return (i, 0)
    return (5000, 0)


def ordered(items, umo="") -> list:
    names = folk_names(umo)
    idx = list(enumerate(items))
    idx.sort(key=lambda x: (who_rank(x[1]["t"], names)[0], -x[0]))
    return [i for _, i in idx]


def roster(umo) -> str:
    """给压缩模型看的称呼对照：同一个人的不同叫法别拆成两个人"""
    pairs = [f"{m}＝{'、'.join(a)}" for m, a in folk_names(umo) if a]
    if not pairs:
        return ""
    return "【称呼对照】" + "；".join(pairs) + "。这些是同一个人的不同叫法，写的时候统一用前面的名字。\n\n"


def write_idx(umo, n=None) -> None:
    """人看的完整目录：索引.md（注入那份是截短的）"""
    n = n or load_note(umo)
    folk = load_folk(umo)
    md = ["# 本子目录", "",
          "人物 %d 位 · 规矩 %d 条 · 事情 %d 条" % (len(folk), len(n["rule"]), len(n["fact"])),
          ""]
    if folk:
        md += ["## 人物"] + [f"- {k}（{len(v)} 条）" for k, v in folk.items()] + [""]
    md += ["## 规矩"]
    md += [f"- {i['d']} · {i['t']}" for i in n["rule"]] or ["- （空）"]
    md += ["", "## 事情（按主语排，我的在最后）"]
    names = folk_names(umo)
    facts = ordered(n["fact"], umo)
    got = {}
    for i in facts:
        got.setdefault(who_rank(i["t"], names)[0], []).append(i)
    for k in sorted(got):
        title = names[k][0] if k < len(names) else ("我自己的事" if k == 9999 else "其他")
        md += ["", f"### {title}（{len(got[k])}）"]
        md += [f"- {i['d']} · {i['t']}" for i in got[k]]
    if not facts:
        md += [""] + ["- （空）"]
    try:
        (vault_dir(umo) / "索引.md").write_text("\n".join(md), encoding="utf-8")
    except Exception as e:
        logger.warning(f"[memory_whale] 索引写不出: {e}")


def folk_head(umo) -> str:
    """注入目录最后那行：家里都有谁，谁会写进档案"""
    folk = load_folk(umo)
    if not folk:
        return ""
    bits = [f"{k}（{len(v)}）" if len(v) > 1 else k for k, v in folk.items()]
    return "\n人物：" + "、".join(bits[:12])


def render_idx(umo) -> str:
    """每轮注入的短目录：只给骨架，不给全文"""
    n = load_note(umo)
    rules, facts = n.get("rule") or [], n.get("fact") or []
    if not rules and not facts:
        return ""
    od = ordered(facts, umo)
    hot = [i for i in od if _age(i) <= HEAT["hot"]]
    warmer = [i for i in od if HEAT["hot"] < _age(i) <= HEAT["warm"]]
    hid = {id(i) for i in hot + warmer}
    cold = [i for i in od if id(i) not in hid]
    nr = min(6, len(rules))
    picks = [("规", i) for i in rules[-nr:]] if nr else []
    rest = IDX_MAX - len(picks)
    if rest > 0:
        picks += [("事", i) for i in (hot + warmer)[:rest]]
    sd = bool(conf().get("show_date"))
    lines = []
    for k, i in picks:
        dd = (i["d"][5:] + "·") if sd else ""
        lines.append(f"{k}·{dd}{i['t'][:IDX_CUT]}")
    more = len(rules) + len(facts) - len(lines)
    head = f"\n{IDX_MARK}（{len(rules)}规/{len(facts)}事，全文用记忆工具查）"
    tail = (f"\n…还有 {more} 条" + (f"（其中 {len(cold)} 条很久没提，在冷柜里）" if cold else "")) if more > 0 else ""
    return head + "\n" + "\n".join(lines) + tail + folk_head(umo) + task_head()


_SELF = re.compile(r"^\s*(assistant|user|ai|助手|机器人|模型|model)\s*[:：]?\s*", re.I)


_SCAFFOLD = re.compile(
    r"^\s*(?:[-*·•>#]+\s*)?(?:assistant|user|system|ai|助手|机器人|模型|model|答案|输出)\s*[:：]\s*",
    re.I,
)
_MDNOISE = re.compile(r"[*`#>]+|^\s*[-·•]+\s*")


def _scrub(s: str) -> str:
    """压出来的东西先擦一遍：去掉「assistant:」这类脚手架、加粗星号、空行"""
    out = []
    for L in str(s or "").splitlines():
        L = _SCAFFOLD.sub("", L)
        L = _MDNOISE.sub("", L).strip()
        _n = my_names()[0]
        L = re.sub(r"\bassistant\b|\bAI助手\b|助手", _n, L, flags=re.I)
        L = L.replace("用户", "人")
        L = re.sub(r"\s{2,}", " ", L)
        if len(L) >= 2:
            out.append(L)
    return "\n".join(out)


def tidy(t: str) -> str:
    """把「assistant认为X」擦成「我认为X」——本子是自己在说话"""
    t = (t or "").strip().lstrip("-·*0123456789.、 ")
    t = _SELF.sub("我", t)
    t = re.sub(r"^(我)(认为|表示|提出|觉得|说)", r"\1\2", t)
    return t.strip()[:200]


# ══════════════════════════════════════════════════════════
# 导入：给以后的定制留的口子
#   任何来源的旧记忆 → 统一成一条条 {kind, name, text}
#   kind: rule 规矩 / fact 事情 / folk 人物 / raw 原样存档不压
#   要接新格式，只写一个函数挂上去：@importer(".xxx")
# ══════════════════════════════════════════════════════════
IMPORTERS = {}          # 后缀 → 读法


def importer(*exts):
    def deco(fn):
        for e in exts:
            IMPORTERS[e.lower()] = fn
        return fn
    return deco


def _item(kind, text, name=""):
    text = tidy(str(text or ""))
    if len(text) < 2:
        return None
    return {"kind": kind, "name": str(name or "").strip(), "text": text[:400]}


@importer(".md", ".markdown")
def _imp_md(path: Path) -> list:
    """老式一个人一个 .md：frontmatter 里有名字就是人物档案，其余算事情"""
    raw = path.read_text(encoding="utf-8", errors="ignore")
    name, desc, body = "", "", raw
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", raw, re.S)
    if m:
        head, body = m.group(1), m.group(2)
        for line in head.splitlines():
            if line.startswith("name:"):
                name = line[5:].strip()
            elif line.startswith("description:"):
                desc = line[12:].strip()
    if name:                                  # 有名字 → 这整份是人
        rows = [desc] + [x for x in body.splitlines() if x.strip() and not x.startswith("#")]
        return [{"kind": "folk", "name": name, "text": tidy("\n".join(rows))[:1200]}]
    out = []
    for line in body.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        out.append(_item("fact", s))
    return [x for x in out if x]


@importer(".json")
def _imp_json(path: Path) -> list:
    """认两种：本插件 note.json 的 {rule:[],fact:[]}；或 [{"text":…}]"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    out = []
    if isinstance(data, dict):
        for k in ("rule", "fact"):
            for it in data.get(k) or []:
                t = it.get("t") if isinstance(it, dict) else it
                out.append(_item("rule" if k == "rule" else "fact", t))
    elif isinstance(data, list):
        for it in data:
            t = it.get("t") or it.get("text") if isinstance(it, dict) else it
            out.append(_item("fact", t))
    return [x for x in out if x]


@importer(".jsonl", ".ndjson")
def _imp_jsonl(path: Path) -> list:
    out = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            it = json.loads(line)
        except Exception:
            continue
        t = it.get("t") or it.get("text") or it.get("content") if isinstance(it, dict) else it
        kind = (it.get("k") or it.get("kind") or "fact") if isinstance(it, dict) else "fact"
        out.append(_item(kind if kind in ("rule", "fact", "folk") else "fact", t))
    return [x for x in out if x]


@importer(".txt", ".log", ".csv")
def _imp_txt(path: Path) -> list:
    out = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        for seg in re.split(r"(?<=[。！？!?；;])", s):
            out.append(_item("fact", seg))
    return [x for x in out if x]


def read_any(path: Path) -> list:
    """任何文件进来都走这里；不认识的格式一律原样存档，不瞎压"""
    fn = IMPORTERS.get(path.suffix.lower())
    items = fn(path) if fn else []
    if not items:
        items = [_item("raw", path.read_text(encoding="utf-8", errors="ignore")[:4000])]
    return [x for x in items if x]


def ingest(umo, src: Path) -> dict:
    """导入三拍：先原样存档 → 再分路 → 再入本子。原文永不再动"""
    src = Path(src)
    box = vault_dir(umo) / "import"
    box.mkdir(parents=True, exist_ok=True)
    keep = box / (time.strftime("%Y%m%d_%H%M%S") + "_" + re.sub(r"[^\w.\-]", "_", src.name))
    keep.write_bytes(src.read_bytes())         # 一：原样复印

    items = read_any(src)                       # 二：分路
    n = load_note(umo)
    folk, rules, facts, raw = [], [], [], 0
    for it in items:
        if it["kind"] == "folk" and it["name"]:
            folk.append(it)
        elif it["kind"] == "rule":
            rules.append(it["text"])
        elif it["kind"] == "raw":
            raw += 1
        else:
            facts.append(it["text"])
    if rules:
        add_items(umo, "rule", rules)
    if facts:
        add_items(umo, "fact", facts)
    for it in folk:
        try:
            add_folk(umo, it["name"], it["text"].splitlines())
        except Exception as e:
            logger.warning(f"[memory_whale] 导入人物失败 {it['name']}: {e}")
    return {"file": keep.name, "items": len(items), "folk": len(folk),
            "rule": len(rules), "fact": len(facts), "raw": raw}


def add_items(umo, kind, lines, ref="") -> int:
    """去重后追加进本子；kind 只认 rule / fact。ref＝细节柜里的号，有它就说明这条后面还能捞原文"""
    kind = "rule" if kind == "rule" else "fact"
    n = load_note(umo)
    have = {i["t"][:24] for i in n[kind]}
    today = time.strftime("%Y-%m-%d")
    added = 0
    for t in lines or []:
        t = re.sub(r"^\s*(?:[-*·•]|\d{1,2}[.、)）])\s*", "", str(t)).strip()
        t = tidy(t)
        if len(t) < 2 or t in ("无", "无。", "none"):
            continue
        if t[:24] in have:
            continue
        have.add(t[:24])
        item = {"d": today, "h": today, "n": 0, "t": t[:120]}
        if ref:
            item["r"] = str(ref)
        n[kind].append(item)
        added += 1
    if added:
        save_note(umo, n)
    return added


def backup_root() -> Path:
    p = out_dir() / "backup"
    p.mkdir(parents=True, exist_ok=True)
    return p


def do_backup(tag: str = "手动") -> str:
    """整份复印：本子、指纹、配置。一处不落，出事能原路回去"""
    root = backup_root()
    name = time.strftime("%Y%m%d_%H%M%S") + "_" + re.sub(r"[^\w\u4e00-\u9fff]", "", tag)[:8]
    dst = root / name
    dst.mkdir(parents=True, exist_ok=True)
    vault = out_dir() / "vault"
    if vault.exists():
        shutil.copytree(vault, dst / "vault")
    for f in list(out_dir().glob("state*.json")) + [CONF_FILE]:
        if f.exists():
            shutil.copy2(f, dst / f.name)
    (dst / "说明.txt").write_text(
        "这是一整份存档。\n"
        f"什么时候：{time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"因为什么：{tag}\n\n"
        "怎么原路返回：\n"
        "  在群里发 /m3_restore " + name + "\n"
        "  或者手动：把这个文件夹里的 vault 整个盖回 plugins/astrbot_plugin_memory_whale/data/vault\n"
        "  盖之前先把现在的 data/vault 改个名留着，别直接删。\n", encoding="utf-8")
    return name


def list_backups() -> list:
    return sorted([x for x in backup_root().iterdir() if x.is_dir()], reverse=True)


def do_restore(name: str) -> str:
    """回到某一份存档之前，先把现在这份也存下来——回退也要能再回来"""
    dst = backup_root() / name
    if not dst.is_dir():
        return f"没有这份：{name}"
    safe = do_backup("回滚前")
    vault = out_dir() / "vault"
    if (dst / "vault").exists():
        if vault.exists():
            shutil.move(str(vault), str(backup_root() / (safe + "_被替换的")))
        shutil.copytree(dst / "vault", vault)
    for f in dst.glob("state*.json"):
        shutil.copy2(f, out_dir() / f.name)
    if CONF_FILE.name in [x.name for x in dst.iterdir()]:
        shutil.copy2(dst / CONF_FILE.name, CONF_FILE)
    return f"回到 {name} 了。回退前那份也存着（{safe}），想再回到刚才就发 /m3_restore {safe}"


def archive_add(umo, kind, old, new) -> None:
    """洗掉的旧话进柜子，一句不丢"""
    f = vault_dir(umo) / "archive.jsonl"
    with f.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps({"d": time.strftime("%Y-%m-%d %H:%M"), "k": kind, "old": old, "new": new},
                            ensure_ascii=False) + "\n")


@register("memory_whale", "memory_whale", "记忆鲸：锚点注入、抓上下文、增量攒话、三段各压一遍", "0.1.0")
class M3(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        try:
            if config:
                _PANEL.update({k: v for k, v in dict(config).items()})
        except Exception as e:
            logger.warning(f"[memory_whale] 读面板配置失败: {e}")
        self._last = {}
        self._auto = True   # 每轮现读，面板改了立刻见效（见 _maybe_auto）
        self._busy: set = set()
        self._lastauto: dict = {}

    def _maybe_auto(self, umo):
        """够 1200 字就压，不看条数——一条长话也照样压"""
        try:
            if not conf().get("auto", True) or umo in self._busy:
                return
            if time.time() - self._lastauto.get(umo, 0) <= AUTO_COOL:
                return
            st = load_state()
            pend = (st["umos"].get(str(umo)) or {}).get("pending") or []
            if sum(len(x["text"]) for x in pend[: keep_tail(pend)]) < BATCH_CHARS:
                return
            self._busy.add(umo)
            asyncio.create_task(self._auto_compress(umo))
        except Exception as e:
            logger.warning(f"[memory_whale] 判断自动压失败: {e}")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def collect(self, event):
        """每条消息当场收一条：窗口只留两轮，等窗口抓就漏了"""
        try:
            txt = strip_injected(getattr(event, "message_str", "") or "").strip()
            if len(txt) < 2:
                return
            obj = getattr(event, "message_obj", None)
            sender = getattr(obj, "sender", None)
            sid = str(getattr(sender, "user_id", "") or "")
            self_id = str(getattr(obj, "self_id", "") or "")
            nick = str(getattr(sender, "nickname", "") or "").strip()
            umo = event.unified_msg_origin
            if sid and self_id and sid == self_id:
                track(umo, [("assistant", txt)])
            else:
                track(umo, [("user", txt, clean_nick(nick, sid))])
            self._maybe_auto(umo)
        except Exception as e:
            logger.warning(f"[memory_whale] 收话失败: {e}")

    @filter.on_llm_request()
    async def grab(self, event, req: ProviderRequest):
        try:
            sp = getattr(req, "system_prompt", "") or ""
            a = anchor_text()
            if a and ANCHOR_MARK not in sp:
                sp = sp + a
            idx = render_idx(event.unified_msg_origin)
            if idx and IDX_MARK not in sp:
                sp = sp + idx
            if sp != (getattr(req, "system_prompt", "") or ""):
                req.system_prompt = sp

            ctxs = list(req.contexts or [])
            umo = event.unified_msg_origin
            self._last[umo] = ctxs
            msgs = to_msgs(ctxs)
            added, pend_n, pend_c = track(umo, msgs)

            out = out_dir()
            safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in umo)[-60:]
            (out / f"_last_{safe}.json").write_text(
                json.dumps(
                    {
                        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "n": len(ctxs),
                        "chars": sum(len(x[1]) for x in msgs),
                        "anchor": bool(a),
                        "added": added,
                        "pending_n": pend_n,
                        "pending_c": pend_c,
                        "ready": pend_c >= BATCH_CHARS,
                        "text": "\n".join(f"{w or r}: {x}" for r, x, w in msgs)[:800],
                    },
                    ensure_ascii=False,
                    indent=1,
                ),
                encoding="utf-8",
            )

            # 攒够了就丢后台压一次，不挡这一轮回复
            self._maybe_auto(umo)
        except Exception as e:
            logger.warning(f"[memory_whale] grab 失败: {e}")

    @filter.command("m3_dump")
    async def dump(self, event):
        ctxs = self._last.get(event.unified_msg_origin, [])
        msgs = to_msgs(ctxs)
        t = "\n".join(f"{w or r}: {x}" for r, x, w in msgs)
        yield event.plain_result(f"抓到 {len(ctxs)} 条 · 剥壳后 {len(t)} 字\n{t[:500]}")

    @filter.command("m3_backup")
    async def backup_cmd(self, event):
        """/m3_backup —— 整份存一份，出事能回到这一刻"""
        name = await asyncio.to_thread(do_backup, "手动")
        yield event.plain_result(f"存好了：data/backup/{name}\n要回来说一句 /m3_restore {name}")

    @filter.command("m3_restore")
    async def restore_cmd(self, event):
        """/m3_restore 存档名 —— 回到某一份存档（不带名字就先列出来）"""
        arg = (getattr(event, "message_str", "") or "").replace("/m3_restore", "").strip()
        bs = list_backups()
        if not arg:
            if not bs:
                yield event.plain_result("还没有存过。发 /m3_backup 存一份。")
                return
            yield event.plain_result("有哪些存着的：\n" + "\n".join(f"· {x.name}" for x in bs[:10])
                                     + "\n\n回到哪一份，就发 /m3_restore 名字")
            return
        yield event.plain_result(await asyncio.to_thread(do_restore, arg))

    @filter.command("m3_import")
    async def import_cmd(self, event):
        """/m3_import 路径 —— 把老本子导进来（也可以直接丢进 vault/import/inbox/ 再发 /m3_import）"""
        umo = event.unified_msg_origin
        box = vault_dir(umo) / "import" / "inbox"
        box.mkdir(parents=True, exist_ok=True)
        arg = (getattr(event, "message_str", "") or "").replace("/m3_import", "").strip().strip('"')
        targets = []
        if arg:
            p = Path(arg)
            if p.is_dir():
                targets = [x for x in sorted(p.rglob("*")) if x.is_file() and x.stat().st_size < 8_000_000]
            elif p.is_file():
                targets = [p]
        else:
            targets = [x for x in sorted(box.rglob("*")) if x.is_file()]
        if not targets:
            yield event.plain_result("没找到东西。把老本子丢进 vault/import/inbox/ 再发 /m3_import，或者直接给个路径。")
            return
        await asyncio.to_thread(do_backup, "导入前")
        yield event.plain_result(f"找到 {len(targets)} 份，先存一份，再入本子。")
        tot = {"items": 0, "folk": 0, "rule": 0, "fact": 0, "raw": 0}
        for f in targets:
            try:
                r = ingest(umo, f)
            except Exception as e:
                yield event.plain_result(f"{f.name} 没进去：{e}")
                continue
            for k in tot:
                tot[k] += r.get(k, 0)
        yield event.plain_result(
            f"导完了：共 {tot['items']} 条 → 人物 {tot['folk']} 份、规矩 {tot['rule']} 条、"
            f"事情 {tot['fact']} 条、原样存档 {tot['raw']} 份。原文都在 vault/import/。")

    @filter.command("m3_wash")
    async def wash_cmd(self, event):
        """/m3_wash —— 把本子里带助手腔的旧条目按说明书重写一遍"""
        umo = event.unified_msg_origin
        n = load_note(umo)
        me = my_names()
        names = [m for m, _ in folk_names(umo)]
        who = names[0] if names else "对方"
        pat = re.compile(r"用户|assistant|助手|AI|模型|机器人")
        bad = [(k, it) for k in ("rule", "fact") for it in n[k] if pat.search(it["t"])]
        if not bad:
            yield event.plain_result("本子里没有要洗的。")
            return
        await asyncio.to_thread(do_backup, "洗账前")
        yield event.plain_result(f"要洗 {len(bad)} 条，先存了一份，开始。")
        fixed = 0
        for s in range(0, len(bad), 12):
            grp = bad[s:s + 12]
            body = "\n".join(f"{i}. {it['t']}" for i, (k, it) in enumerate(grp, 1))
            tail = (f"- 「用户」说的就是{who}，直接写{who}；"
                    + (f"「assistant」「AI」「助手」「模型」说的是你自己，写成「{me[0]}」。\n" if me
                       else "- 「assistant」「AI」「助手」「模型」说的是你自己，写成「我」。\n"))
            ask = (SPEC + f"\n下面是 {len(grp)} 条旧账，当年写坏了，按上面重写：\n" + body +
                   "\n\n要求：\n" + tail + "- 一条一行，格式「编号. 一句话」，不超过 40 字，别解释。")
            out, _ = await self._ask(umo, ask)
            got = {}
            for line in (out or "").splitlines():
                m = re.match(r"\s*(\d{1,2})\s*[.、．)]\s*(.+)", line)
                if m:
                    got[int(m.group(1))] = tidy(m.group(2))[:120]
            for i, (k, it) in enumerate(grp, 1):
                new = got.get(i)
                if new and len(new) >= 2:
                    archive_add(umo, k, it["t"], new)   # 旧话先入柜
                    it["t"] = new
                    fixed += 1
            save_note(umo, n)
        yield event.plain_result(f"洗完了 {fixed}/{len(bad)} 条，旧话都在柜子里。")

    @filter.command("m3_new")
    async def new_cmd(self, event):
        st = load_state()
        u = st["umos"].get(str(event.unified_msg_origin), {})
        pend = u.get("pending") or []
        c = sum(len(p["text"]) for p in pend)
        head = "\n".join(f'{p["role"]}: {p["text"][:40]}' for p in pend[-5:])
        yield event.plain_result(
            f"pending {len(pend)} 条 / {c} 字（攒到 {BATCH_CHARS} 字就该压）\n"
            f"指纹记了 {len(u.get('seen') or [])} 个\n最后几条：\n{head}"
        )

    @filter.command("m3_clear")
    async def clear_cmd(self, event):
        st = load_state()
        u = st["umos"].setdefault(str(event.unified_msg_origin), {"seen": [], "pending": []})
        u["pending"] = []
        save_state(st)
        yield event.plain_result("pending 清空了，指纹留着（老话不会被重新收）")

    @filter.command("m3_anchor")
    async def anchor_cmd(self, event):
        a = anchor_text()
        yield event.plain_result("锚点：" + ("关着" if not a else "开着\n" + a.strip()))

    async def _prov(self, umo):
        try:
            p = self.context.get_using_provider(umo=umo)   # 同步接口，别 await
            if asyncio.iscoroutine(p):
                p = await p
            return p
        except Exception as e:
            logger.warning(f"[memory_whale] 取 provider 失败: {e}")
            return None

    async def _ask(self, umo, prompt):
        """先问云端：提规矩要脑子，本地小千问分不出事实和规矩；云端不行才回落本地"""
        local_first = bool(conf().get("compress_local_first"))
        if not local_first:
            got, how = await self._cloud(prompt, umo)
            if got:
                return got, how
        out = tidy(await local_chat(prompt))
        if out:
            return out, "local"
        if local_first:
            return await self._cloud(prompt, umo)
        return "", "none"

    async def _cloud(self, prompt, umo):
        p = await self._prov(umo)
        if not p:
            return "", "none"
        try:
            r = await asyncio.wait_for(
                p.text_chat(prompt=prompt, contexts=[], session_id=None), timeout=180
            )
            return tidy(getattr(r, "completion_text", "") or ""), "cloud"
        except Exception as e:
            logger.warning(f"[memory_whale] 云端没接上: {e}")
            return "", "fail"

    async def _compress(self, umo, text):
        _names = my_names()
        _n, _alias = _names[0], "、".join(_names[1:])
        me = (f"你自己在这段对话里叫「{_n}」" + (f"（也写{_alias}）" if _alias else "")
              + f"，写到自己的时候就写「{_n}」。\n")
        tail = "\n\n只输出条目本身，一行一条；不要开场白、不要结尾说明、不要加粗星号。"
        parts = await asyncio.gather(
            *[self._ask(umo, (roster(umo) + me + tpl).replace("{t}", text) + tail) for tpl in P.values()]
        )
        res, srcs = {}, []
        for k, (out, s) in zip(P.keys(), parts):
            res[k] = out
            srcs.append(f"{k}={s}:{len(out)}字")
        res["03_rule"] = "\n".join(drop_param(_scrub(res["03_rule"]).splitlines())[:25])
        res["02_gist"] = "\n".join(_scrub(res["02_gist"]).splitlines()[:12])
        res["01_dedup"] = _scrub(res.get("01_dedup", ""))
        # 分级存：骨架进本子，肉沉柜子。压之前那段原文一字不删地留着，条目上挂个号
        try:
            if conf().get("detail_vault", True):
                ev = vault_dir(umo) / "事件"
                ev.mkdir(parents=True, exist_ok=True)
                ref = time.strftime("%Y%m%d_%H%M%S") + "_" + str(len(list(ev.iterdir())) + 1)
                while (ev / (ref + ".md")).exists():
                    ref += "x"
                (ev / (ref + ".md")).write_text(
                    "# " + ref + f"（原话 {len(text)} 字）\n\n" + text, encoding="utf-8"
                )
                res["_ref"] = ref
        except Exception as e:
            logger.warning(f"[memory_whale] 事件柜没落成: {e}")
        logger.info("[memory_whale] 压完 " + " ".join(srcs))
        if all(not v for v in res.values()):
            logger.warning("[memory_whale] 三层全空，这次不写本子")
            return None
        return res

    async def _merge_rules(self, umo):
        """规矩超上限了，让模型自己并一遍，别越滚越长"""
        n = load_note(umo)
        if len(n["rule"]) <= RULE_CAP:
            return 0
        src = "\n".join(f"- {i['t']}" for i in n["rule"])
        tpl = (
            f"下面是同一本记忆里所有长期规矩，共 {len(n['rule'])} 条。"
            f"请合并同义、去掉重复，压成不超过 {RULE_MERGE_TO} 条，每条不超过 25 字，"
            f"一行一条，只输出条目本身。\n\n{src}"
        )
        try:
            out, _ = await self._ask(umo, tpl)
        except Exception as e:
            logger.warning(f"[memory_whale] 规矩合并不成: {e}")
            return 0
        if not out:
            return 0
        lines = [
            re.sub(r"^\s*(?:[-*·•]|\d{1,2}[.、)）])\s*", "", x).strip()
            for x in out.splitlines()
        ]
        lines = [x for x in lines if len(x) >= 2][:RULE_MERGE_TO]
        if len(lines) < 8:
            logger.warning(f"[memory_whale] 合并结果太少({len(lines)})，先不动 // 主锚点")
            return 0
        n["rule"] = [{"d": time.strftime("%Y-%m-%d"), "t": x[:120]} for x in lines]
        save_note(umo, n)
        logger.info(f"[memory_whale] 规矩自并 {len(lines)} 条")
        return len(lines)

    def _save(self, name, head, res):
        body = "".join(f"## {k}\n{v}\n\n" for k, v in res.items())
        f = out_dir() / name
        f.write_text(head + body, encoding="utf-8")
        return f, body

    async def _auto_compress(self, umo):
        """攒够了在后台压一次：不在群里发言，只写日志和本子"""
        try:
            st = load_state()
            pend = (st["umos"].get(str(umo)) or {}).get("pending") or []
            cut = keep_tail(pend)
            head = pend[:cut]
            if not head or sum(len(x["text"]) for x in head) < BATCH_CHARS:
                return
            t = "\n".join(f'{x.get("who") or x["role"]}: {x["text"]}' for x in head)
            res = await self._compress(umo, t)
            if not res:
                return
            f, _ = self._save(
                f"{time.strftime('%Y%m%d_%H%M%S')}_auto.md",
                f"# 自动压 · {len(head)} 条 / {len(t)} 字（最后 {len(pend) - len(head)} 条留着）\n\n",
                res,
            )
            st2 = load_state()
            u2 = st2["umos"].get(str(umo))
            if u2 is not None:
                u2["pending"] = (u2.get("pending") or [])[len(head):]  # 尾巴那两轮 + 压的当口新来的，都留着
                save_state(st2)
            self._lastauto[umo] = time.time()   # 压成功了才扣冷却
            nr = add_items(umo, "rule", res.get("03_rule", "").splitlines())
            nf = add_items(umo, "fact", res.get("02_gist", "").splitlines(), ref=res.get("_ref", ""))
            nm = await self._merge_rules(umo)
            logger.info(
                f"[memory_whale] 自动压完 {len(head)}条（留尾 {len(pend) - len(head)}）"
                f" → 规矩+{nr} 事情+{nf}"
                + (f" 合并{nm}" if nm else "")
                + f" | {f.name}"
            )
        except Exception as e:
            logger.warning(f"[memory_whale] 自动压失败: {e}")
        finally:
            self._busy.discard(umo)

    @filter.command("m3_go")
    async def go(self, event):
        """只压 pending 里的新话，压完清空——这就是增量"""
        umo = event.unified_msg_origin
        st = load_state()
        u = st["umos"].get(str(umo), {})
        pend = u.get("pending") or []
        if not pend:
            yield event.plain_result("没有新话，不用压")
            return
        cut = keep_tail(pend)
        head = pend[:cut]
        if not head:
            yield event.plain_result(
                f"队列里只有最近 {KEEP_ROUNDS} 轮（{len(pend)} 条），按规矩留着不压——"
                "要硬压用 /m3_all"
            )
            return
        t = "\n".join(f'{p.get("who") or p["role"]}: {p["text"]}' for p in head)
        res = await self._compress(umo, t)
        if not res:
            yield event.plain_result("拿不到模型")
            return
        f, body = self._save(
            f"{time.strftime('%Y%m%d_%H%M%S')}_new.md",
            f"# 只压新话 · {len(head)} 条 / {len(t)} 字（最后 {len(pend) - len(head)} 条留着）\n\n",
            res,
        )
        u["pending"] = pend[cut:]
        save_state(st)
        nr = add_items(umo, "rule", res.get("03_rule", "").splitlines())
        nf = add_items(umo, "fact", res.get("02_gist", "").splitlines(), ref=res.get("_ref", ""))
        nm = await self._merge_rules(umo)
        n = load_note(umo)
        yield event.plain_result(
            f"压完清队 · 存 {f.name}\n"
            f"本子长了：规矩 +{nr}（共 {len(n['rule'])}）· 事情 +{nf}（共 {len(n['fact'])}）\n\n"
            + (f"规矩超 {RULE_CAP} 条，自己并成 {nm} 条\n\n" if nm else "")
            + body[:800]
        )

    @filter.llm_tool(name="memory_note")
    async def note_tool(self, event, text: str, kind: str = "fact") -> str:
        """往本子里记一条长期记忆。只在对方说了「以后还用得上」的事时记：身份、偏好、约定、纠正、进行中的事。寒暄闲聊不要记。

        Args:
            text(string): 要记的内容，一句话，别超过 60 字；多条用换行分开。
            kind(string): rule 表示长期规矩/对她的承诺，fact 表示发生过的事、某个人的情况。默认 fact。
        """
        umo = event.unified_msg_origin
        n = add_items(umo, kind, str(text).splitlines())
        return f"记好了 {n} 条" if n else "这条本子里已经有了，没重复记"

    @filter.llm_tool(name="memory_folk")
    async def folk_tool(self, event, name: str = "", rows: str = "") -> str:
        """人物档案——本子里专门记人的一栏。想知道某个人是谁、怎么称呼、长期偏好、忌讳，或者新认识了谁想记下来，都用它。

        Args:
            name(string): 人的名字或称呼。留空就列出记过哪些人。
            rows(string): 要新记的内容，一行一条，一句话说清；留空就是只看不记。
        """
        umo = event.unified_msg_origin
        name = str(name or "").strip()
        d = load_folk(umo)
        if not name:
            return ("人物：" + "、".join(f"{k}({len(v)})" for k, v in d.items())) if d else "人物档案还是空的"
        if str(rows or "").strip():
            n = add_folk(umo, name, str(rows).splitlines())
            d = load_folk(umo)
            return (f"{name} 记了 {n} 条，现在共 {len(d.get(name, []))} 条" if n
                    else f"{name} 这几条已经有了")
        v = d.get(name)
        if v is None:
            near = [k for k in d if name in k or k in name]
            return f"人物档案里没有「{name}」" + (f"，有的是：{'、'.join(near)}" if near else "")
        return (f"【{name}】\n" + "\n".join(f"- {r}" for r in v[:60]))[:2000]

    @filter.llm_tool(name="memory_recall")
    async def recall_tool(self, event, kind: str = "all", with_date: bool = False) -> str:
        """翻本子。想知道以前记过什么、或对方提到早年的事时调它。

        Args:
            kind(string): all 全翻，rule 只看规矩，fact 只看事情。默认 all。
            with_date(boolean): 平时不用传。只在一件事上冒出两个说法、要看清谁先谁后时传 true。
        """
        n = load_note(event.unified_msg_origin)
        sd = bool(with_date) or bool(conf().get("show_date"))
        def _f(i):
            return (i["d"] + " · " + i["t"]) if sd else i["t"]
        parts = []
        if kind in ("all", "rule"):
            parts.append("【规矩】\n" + ("\n".join("- " + _f(i) for i in n["rule"]) or "（空）"))
        if kind in ("all", "fact"):
            parts.append("【事情】\n" + ("\n".join("- " + _f(i) for i in n["fact"]) or "（空）"))
        return ("\n\n".join(parts) or "本子是空的")[:2400]

    @filter.llm_tool(name="memory_detail")
    async def detail_tool(self, event, query: str) -> str:
        """捞当时的原话。本子里每条只留一句骨架，想不起前因后果、或者要核对细节时用这个。

        Args:
            query(string): 关键词，两三个字最好，比如「开源」「图库」「开机」。
        """
        umo = event.unified_msg_origin
        n = load_note(umo)
        q = str(query).strip()
        if not q:
            return "关键词是空的"
        pool = n["rule"] + n["fact"]
        hits = [i for i in pool if q in i["t"] and i.get("r")]
        if not hits:
            old = [i for i in pool if q in i["t"]]
            if old:
                return "这几条是早先的账，那时还没留细节，柜子里捞不到原文：\n" + "\n".join(
                    "- " + i["t"] for i in old[:6]
                )
            return f"本子里没有「{q}」"
        touch(umo, hits)
        ev = vault_dir(umo) / "事件"
        out, seen = [], set()
        for i in hits:
            r = str(i["r"])
            if r in seen:
                continue
            seen.add(r)
            fp = ev / (r + ".md")
            if not fp.exists():
                body = "（柜子里这份丢了）"
            else:
                whole = fp.read_text(encoding="utf-8")
                marks = [ln.strip() for ln in whole.splitlines() if q in ln]
                if marks:
                    body = "命中：\n" + "\n".join("► " + ln for ln in marks[:8]) + f"\n（整份在柜子 {r}）"
                else:
                    body = whole[:700]
            out.append(f"【{i['d']} · {i['t']} · 柜 {r}】\n{body}")
            if len(seen) >= 3:
                break
        return "\n\n".join(out) or f"没捞到「{q}」的原文"

    @filter.llm_tool(name="memory_lookup")
    async def lookup_tool(self, event, query: str, with_date: bool = False) -> str:
        """在本子里搜关键词。本子翻出来太长、或只记得某个名字/某个词时用这个。

        Args:
            query(string): 关键词，两三个字最好，比如「生日」「散步」「Steam」。
            with_date(boolean): 平时不用传。查到两条对不上、要分辨哪条新哪条旧时才传 true。
        """
        n = load_note(event.unified_msg_origin)
        q = str(query).strip()
        if not q:
            return "关键词是空的"
        sd = bool(with_date) or bool(conf().get("show_date"))
        def _f(tag, i):
            return f"[{tag}] " + ((i["d"] + " · " + i["t"]) if sd else i["t"])
        rr = [i for i in n["rule"] if q in i["t"]]
        rf = [i for i in n["fact"] if q in i["t"]]
        hit = [_f("规矩", i) for i in rr] + [_f("事情", i) for i in rf]
        touch(event.unified_msg_origin, rr + rf)
        hit += [f"卡·{nm}：{(ln[0] if ln else '')[:44]}" for nm, ln in task_search(q)]
        return ("\n".join(hit) or f"本子里没有「{q}」")[:2000]

    @filter.command("m3_date")
    async def date_cmd(self, event):
        """带日期的清单：平时本子不亮日期，要判新旧、看先后的时候用这条"""
        umo = event.unified_msg_origin
        n = load_note(umo)
        rows = [f"[规] {i['d']} · {i['t']}" for i in n["rule"]]
        rows += [f"[事] {i['d']} · {i['t']}" for i in n["fact"]]
        if not rows:
            yield event.plain_result("本子是空的")
            return
        note = "（面板里开了常显）" if conf().get("show_date") else "（只亮这一次，不改设置）"
        segs, buf = [], f"带日期共 {len(rows)} 条{note}"
        for r in rows:
            if len(buf) + len(r) > 1100:
                segs.append(buf)
                buf = ""
            buf += "\n" + r
        segs.append(buf)
        for s in segs:
            yield event.plain_result(s)

    @filter.command("m3_all")
    async def go_all(self, event):
        """老路子：整段上下文压一遍（留着手动兜底）"""
        umo = event.unified_msg_origin
        msgs = to_msgs(self._last.get(umo, []))
        t = "\n".join(f"{w or r}: {x}" for r, x, w in msgs)
        if len(t) < 20:
            yield event.plain_result("没抓到东西，先说两句再试")
            return
        res = await self._compress(umo, t)
        if not res:
            yield event.plain_result("拿不到模型")
            return
        f, body = self._save(
            f"{time.strftime('%Y%m%d_%H%M%S')}_all.md", f"# 全量 {len(t)} 字\n\n", res
        )
        yield event.plain_result(f"存好 {f.name}\n\n" + body[:900])

    @filter.command("m3_task")
    async def task_cmd(self, event, arg: str = ""):
        """/m3_task 列 | 新 标题 | 记 标题 一句话 | 完 标题"""
        a = (arg or "").strip()
        if not a or a in ("列", "list"):
            rows = await asyncio.to_thread(task_list)
            if not rows:
                yield event.plain_result("还没有任务卡。开一张：/m3_task 新 标题")
            else:
                yield event.plain_result("\n".join(("- " + n + ("（完）" if dn else "")) for n, dn in rows[:20]))
            return
        act, _, rest = a.partition(" ")
        rest = rest.strip()
        if act in ("新", "open") and rest:
            await asyncio.to_thread(task_open, rest)
            yield event.plain_result(f"开好卡了：{_task_slug(rest)}\n要记一笔就 /m3_task 记 {rest} 做到哪了")
        elif act in ("记", "log"):
            name, _, note = rest.partition(" ")
            if not name or not note:
                yield event.plain_result("这样写：/m3_task 记 任务名 这一笔写什么")
            else:
                await asyncio.to_thread(task_append, name, note)
                yield event.plain_result(f"记上了：{name}")
        elif act in ("完", "done"):
            name = rest or ""
            if not name:
                yield event.plain_result("要写任务名：/m3_task 完 任务名")
            else:
                await asyncio.to_thread(task_done, name)
                yield event.plain_result(f"标完成了：{name}")
        else:
            yield event.plain_result("/m3_task 列 | 新 标题 | 记 标题 一句话 | 完 标题")

    @filter.llm_tool(name="memory_task")
    async def task_tool(self, event, action: str, title: str = "", note: str = "") -> str:
        """手头的活单独开一张卡，记进度和卡点。接一个要干一阵子的活就开卡，有进展就记一笔，做完了标完成；卡片能被搜到，不提也不删。

        Args:
            action(string): open=开卡, log=记一笔, done=标完成, list=看还有哪些没做完
            title(string): 任务名，短，比如「开源v0.1.0」「记忆插件冷热分层」
            note(string): action=log 时这一笔写什么：做到哪、卡在哪、改了什么
        """
        act = str(action or "").strip().lower()
        title = str(title or "").strip()
        if act in ("open", "开"):
            if not title:
                return "要开卡得给个任务名"
            await asyncio.to_thread(task_open, title)
            return f"开好卡了：{_task_slug(title)}"
        if act in ("log", "记"):
            if not title or not note:
                return "记一笔要「任务名」+「写什么」"
            await asyncio.to_thread(task_append, title, note)
            return "记上了：" + title
        if act in ("done", "完"):
            if not title:
                return "要标完成得给个任务名"
            await asyncio.to_thread(task_done, title)
            return "标完成了：" + title
        rows = await asyncio.to_thread(task_list)
        if not rows:
            return "还没有任务卡"
        return "\n".join(("- " + n + ("（完）" if d else "")) for n, d in rows[:20])
