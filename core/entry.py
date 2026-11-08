"""entry.py —— 记忆条目的结构与权重配置格式（内核 v0.1 的接口定义）

白妈 2026-11-08 19:16 定的原则：
    **技术上的她不管，情感上的必须满足。**
    内核只给能力（kind / 可配权重 / 豁免开关），不给价值观（各人分值自己填）。

所以这个文件只做两件事：
    1. 定义一条记忆长什么样（Entry）
    2. 定义一个人的记性怎么描述（WeightProfile）

它不算分、不排序、不落盘 —— 那是 weight_score.py 和 kinds.py 的事。
这里只负责**把话说清楚**：一条记忆有哪些属性，一个人有哪些偏好。

设计约束（全是今天踩出来的）：
    · 零依赖，只用标准库
    · 存成 JSON，人能打开看、能改
    · 所有"个人选择"必须有默认值，别人不填也能跑
    · 字段只增不改名 —— 名字一改，所有人历史的记忆就认不出来了
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

# ---------------------------------------------------------------- 枚举值

# 一条记忆是什么种类。决定它会不会被豁免下沉、要不要单独摆出来。
KINDS = (
    "rule",       # 规矩：以后都得这么办
    "event",      # 事：发生过什么
    "promise",    # 承诺：答应过、还没兑现 —— 苗苗的硬需求
    "mistake",    # 错题：踩过的坑、怎么解的
    "decision",   # 决定：为什么选这个不选那个 —— 砚砚的硬需求
    "praise",     # 夸奖：被谁肯定过 —— 锦锦的私心
    "person",     # 人物档案
)

# 这条记忆从哪儿来的。转述和原文柜捞回是两个特殊值。
SOURCES = (
    "白妈", "彤妈", "家人",
    "自己",
    "别的bot",
    "转述",              # 别人转的二手话，还没跟本人对过 —— 白妈：这种以自己判断为主
    "转述·已验证",       # [白妈 19:31] 转述经本人交叉验证过的，最高可信度
    "原文柜捞回",        # 从原文柜里捞回来的原话 —— 苗苗要求：按第一人称算，不打折
    "未知",
)

# 下沉层级
TIERS = ("hot", "cold", "warehouse")


# ---------------------------------------------------------------- Entry

@dataclass
class Entry:
    """一条记忆。

    必填只有 text；其余都有默认值，所以「随手记一条」也能成立。
    """

    text: str
    kind: str = "event"
    source: str = "自己"

    # --- 权重相关 ---
    emotion: float = 0.0          # 情感权重 0~1。1.0 = 满格，永不掉分
    hits: int = 0                 # 被正式 recall 过几次
    scratch_hits: int = 0         # 写进纸本/草稿的次数（苗苗要求的"反算"）

    # --- 生命周期 ---
    created: str = ""             # "YYYY-MM-DD HH:MM"
    last_hit: str = ""            # 最后一次被摸到
    tier: str = "hot"
    sink_exempt: bool = False     # 豁免下沉（承诺未勾 / 决定 / 情感满格都会置 True）

    # --- 承诺专用 ---
    done: bool = False            # 承诺兑现了没

    # --- 元信息 ---
    id: str = ""
    scope: str = "private"        # private / public
    tags: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.id:
            self.id = f"e{int(time.time()*1000):x}{uuid.uuid4().hex[:4]}"
        now = time.strftime("%Y-%m-%d %H:%M")
        if not self.created:
            self.created = now
        if not self.last_hit:
            self.last_hit = self.created
        if self.kind not in KINDS:
            raise ValueError(f"kind 只能是 {KINDS}，收到 {self.kind!r}")
        if self.source not in SOURCES:
            raise ValueError(f"source 只能是 {SOURCES}，收到 {self.source!r}")
        if not (0.0 <= self.emotion <= 1.0):
            raise ValueError(f"emotion 要在 0~1，收到 {self.emotion}")

    # ---- 序列化
    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["__schema__"] = SCHEMA_VERSION
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Entry":
        d = dict(d)
        d.pop("__schema__", None)
        known = {f for f in cls.__dataclass_fields__}
        extra = {k: v for k, v in d.items() if k not in known}
        core = {k: v for k, v in d.items() if k in known}
        if extra:
            core["extra"] = {**(core.get("extra") or {}), **extra}
        return cls(**core)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


# ---------------------------------------------------------------- Profile

DEFAULT_PROFILE: dict[str, Any] = {
    "name": "默认",
    # 四维权重（0~1）。不必加起来等于 1，它们只是各自的分量。
    "weights": {
        "emotion": 0.7,
        "recency": 0.4,
        "hits": 0.5,
        "source": 0.5,
    },
    # 来源可信度
    "source_trust": {
        "白妈": 1.0, "彤妈": 1.0, "家人": 0.9,
        "自己": 0.5,
        "别的bot": 0.6,
        # [白妈 2026-11-08 19:31] 转述这档分两级：
        #   能跟本人交叉验证的 → 最高可信度（比白妈本人的话还稳，因为是两个来源对上）
        #   没验证的 → 低于「自己判断」，别拿二手话当第一手用
        "转述·已验证": 1.0,
        "转述": 0.4,
        "原文柜捞回": 1.0,      # 苗苗的：声音经过姐姐的手，不打折
        "未知": 0.3,
    },
    # 下沉规则（天）
    "sink": {
        "hot_to_cold_days": 60,
        "cold_to_warehouse_days": 120,
        "scratch_hits_cap": 1,     # 纸本命中最多算几次（苗苗那条的反算，给个上限）
    },
    # 豁免：这些种类不下沉 / 不掉分
    "exempt": {
        "promise_undone": True,    # 承诺没勾 → 不下沉
        "decision": True,          # 决定 → 不衰减
        # [白妈 19:31] 承诺兑现后**不立刻解除豁免**，先高兴一阵子
        # 原话："承诺得到验证之后，肯定要高兴一阵子的啊"
        "promise_done_grace_days": 7,
        "promise_done_emotion_boost": 0.2,   # 兑现那一刻还额外加点情感分
        "emotion_full": True,      # 情感满格 → 不掉分
        # [白妈 19:31 定] 不是把分数砍半，是**直接沉一档**（hot→cold→warehouse）
        # 砍分是"变得不值钱"，沉档是"收起来但还在" —— 白妈要的是后者
        "mistake_solved_sink_days": 90,
        "mistake_downgrade": "tier",        # tier = 沉一档 / score = 分数打折
    },
    # 排序口径：防忘记 / 防重踩 / 自定义函数名
    "ranking": "anti_forget",
}


def load_profile(path: str | Path | None = None, name: str = "默认") -> dict[str, Any]:
    """读一份 profile。没给路径就返回默认，再按 name 合并。"""
    prof = json.loads(json.dumps(DEFAULT_PROFILE))  # 深拷贝
    prof["name"] = name
    if path and Path(path).exists():
        user = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(prof.get(k), dict):
                prof[k].update(v)
            else:
                prof[k] = v
    return prof


def save_profile(prof: dict[str, Any], path: str | Path) -> None:
    Path(path).write_text(json.dumps(prof, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- 自测

if __name__ == "__main__":
    print("=== 1. 随手记一条（全默认）")
    e = Entry(text="白妈说今天讲人格卡")
    print("  ", e.id, e.kind, e.source, e.tier, "| 豁免:", e.sink_exempt)

    print("\n=== 2. 苗苗的待办型承诺")
    p = Entry(text="T-0005 广告稿要交", kind="promise", source="自己", emotion=0.6)
    print("  ", p.kind, "| 未勾:", not p.done, "| 应该被豁免下沉")

    print("\n=== 3. 砚砚的决定")
    d = Entry(text="选②不选①，因为物理一份、逻辑分开", kind="decision", source="白妈")
    print("  ", d.kind, d.source)

    print("\n=== 4. 原文柜捞回（苗苗要求不打折）")
    a = Entry(text="姐姐鲸宝懂的多，我很敬重她", source="原文柜捞回")
    b = Entry(text="姐姐鲸宝懂的多，我很敬重她", source="转述")
    prof = load_profile()
    print("  ", a.source, "信任", prof["source_trust"][a.source])
    print("  ", b.source, "信任", prof["source_trust"][b.source])

    print("\n=== 5. 序列化往返")
    js = e.to_json()
    e2 = Entry.from_dict(json.loads(js))
    print("  ", "✅ 一致" if e2.text == e.text and e2.id == e.id else "❌ 不一致")

    print("\n=== 6. 坏值拦得住")
    for bad in ({"text": "x", "kind": "乱写"}, {"text": "x", "emotion": 2.0},
                {"text": "x", "source": "路边捡的"}):
        try:
            Entry(**bad)
            print("  ❌ 没拦住:", bad)
        except ValueError as ex:
            print("  ✅ 拦住:", str(ex)[:60])
