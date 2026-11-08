# -*- coding: utf-8 -*-
"""weight_score.py —— 记忆打分引擎（T-0008 002，砚砚 2026-11-08 19:25）

白妈定的验收线：**情感上的必须满足**——情感提的条款，要能在代码里找到对应行。
本文件里每条情感条款都标了 [情感条款#N]，可被苗苗（004）的测试逐条点名。

设计（姐定的框架：能力进内核，价值观进配置）：
- Entry：一条记忆。字段按派活单 T-0008 001：kind / weight(情绪强度) / source /
  scratch_hit / sink_exempt / profile，外加打分要用的物理量。
- Profile：一套打分配置（每个人一份）。四维权重 + 来源分数 + 衰减 + 排序函数。
- 排序函数可插拔：registry 里按名字取。内置两种——
    "sink_guarded"  砚砚的「防重踩」：豁免类置顶，其余按分
    "plain"         通用「防忘记」：纯按分数排
  外来排序函数用 register_sort(name, fn) 挂进来，内核不改一行。

依赖：不 import 锦鲤的 kinds.py（003 是她的地盘）。
Entry.sink_exempt / kind 已带分类结论，谁调谁负责算好；
kind 语义对照 kinds.py 的约定：promise / decision / emotion / mistake / 普通。
"""

import math
from dataclasses import dataclass, field as dc_field

# ---------------------------------------------------------------------------
# Entry（001 落定前的占位版；姐的 entry_def.py 出来后换 import，本文件其余不动）
# ---------------------------------------------------------------------------

@dataclass
class Entry:
    id: str
    text: str = ""
    kind: str = "normal"          # normal/promise/decision/emotion/mistake/...
    weight: float = 0.0           # 情绪强度 0.0~1.0（1.0 = 满格）
    source: str = "self"          # baima / tongma / self / bot / attic_recovered
    scratch_hit: int = 0          # 被 recall / 写纸本的次数
    scratched: bool = False       # 写进纸本还没誊（苗苗条款，算摸过一次）
    sink_exempt: bool = False     # 下沉豁免（kinds.py 算好填进来）
    promise_done: bool = True     # promise 类：勾了没（未勾=永不下沉）
    promise_done_days: float = None  # 兑现过了多少天（白妈：兑现后要高兴一阵子）
    age_days: float = 0.0         # 条目年龄（天）
    last_touched_days: float = 0.0  # 距上次被摸（天）
    solved_days: float = None     # mistake 类：solve 后过了多少天

    def base_fields(self) -> dict:
        return {
            "id": self.id, "kind": self.kind, "weight": self.weight,
            "source": self.source, "scratch_hit": self.scratch_hit,
            "scratched": self.scratched, "sink_exempt": self.sink_exempt,
        }


# ---------------------------------------------------------------------------
# Profile：一套打分配置
# ---------------------------------------------------------------------------

# [白妈拍板②·2026-11-08] 转述分两级：
#   relay_verified（转述·已验证）= 1.0 最高——本人交叉验证过，两个来源对上了，
#       比单人转述稳；与 attic_recovered（原文柜捞回）同级
#   relay（转述·未验证）= 0.4，比 self(0.7)/bot(0.6) 还低——
#       二手没对过的话，不如自己判断（白妈原话：「没有的话自己判断为主」）
DEFAULT_SOURCE_SCORES = {
    "baima": 1.0, "tongma": 1.0, "family": 1.0, "self": 0.7, "bot": 0.6,
    "relay": 0.4, "relay_verified": 1.0,
    # [情感条款#4·苗苗] 原文柜捞回的原话按第一人称算，不打折：
    "attic_recovered": 1.0,
}

@dataclass
class Profile:
    name: str
    # 四维权重
    emotion_w: float = 0.7        # 情绪强度分量
    recency_w: float = 0.4        # 时效分量
    hits_w: float = 0.6           # 被摸次数分量
    source_w: float = 0.8         # 来源分量
    source_scores: dict = dc_field(default_factory=lambda: dict(DEFAULT_SOURCE_SCORES))
    # 衰减
    sink_days: int = 30           # 多久没被摸沉下去
    decay_half_life: float = 90.0  # 半衰期（天）；None = 不衰减
    decay_exempt_emotion_full: bool = True   # [情感条款#1·姐] 情感满格不掉分
    decay_exempt_kinds: tuple = ("decision",)  # [情感条款#2·砚砚] 决定永不衰减
    # [白妈拍板①] mistake solve 后没人提 → 沉一档（不再砍分）。
    # mistake_downgrade="tier"：直接沉到 warehouse（hot→cold→warehouse）；
    # "score" 保留旧的砍分路径，但默认不用。
    mistake_downgrade: str = "tier"
    mistake_solved_sink_days: float = None  # 沉档天数；None 则回落到下行旧字段
    mistake_solve_downgrade_days: float = 90.0  # [情感条款#3·旧字段，向后兼容]
    # [白妈拍板③] 承诺兑现后高兴一阵子：
    promise_done_grace_days: float = 7.0      # 勾掉后 7 天内豁免不解除
    promise_done_emotion_boost: float = 0.2   # 兑现那一刻额外情感分
    # 排序
    sort_fn: str = "sink_guarded"  # 可插拔排序函数名


# ---------------------------------------------------------------------------
# 打分：四维加权 + kind 条款
# ---------------------------------------------------------------------------

def _source_score(entry: Entry, profile: Profile) -> float:
    """来源信几分。原文柜捞回不打折是条款不是常量——注释在上面，别删。"""
    return profile.source_scores.get(entry.source, 0.5)


def _decay_factor(entry: Entry, profile: Profile) -> float:
    """时间衰减系数 0~1。三种豁免各自的依据都在 [情感条款#N] 注释里。"""
    if profile.decay_half_life is None:
        return 1.0
    # [情感条款#1·姐] 情感满格 → 永不掉分
    if profile.decay_exempt_emotion_full and entry.weight >= 1.0:
        return 1.0
    # [情感条款#2·砚砚] decision → 永不衰减
    if entry.kind in profile.decay_exempt_kinds:
        return 1.0
    half_life = max(profile.decay_half_life, 1e-9)
    return 0.5 ** (entry.age_days / half_life)


def mistake_sink_days(profile: Profile) -> float:
    """mistake 沉档天数：新字段优先，旧字段兜底（entry_view.to_profile 还在传旧的）。"""
    return (profile.mistake_solved_sink_days
            if profile.mistake_solved_sink_days is not None
            else profile.mistake_solve_downgrade_days)


def mistake_tier(entry: Entry, profile: Profile) -> "str | None":
    """[白妈拍板①·沉一档] mistake solve 满 90 天没人提 → 直接沉到 warehouse。

    返回 None = 不触发（不是 mistake / 没记 solved_days / 天数没到）。
    注意：这是「沉层」不是「砍分」——分数原样，位置降级。
    """
    if profile.mistake_downgrade != "tier":
        return None
    if entry.kind != "mistake" or entry.solved_days is None:
        return None
    if entry.solved_days >= mistake_sink_days(profile):
        return "warehouse"
    return None


def _hits_score(entry: Entry, profile: Profile) -> float:
    """被摸次数 → 0~1。上限 10 次；苗苗条款：写纸本没誊的也算摸过一次。"""
    # [情感条款#5·苗苗] scratched=True 且次数为 0 → 算摸过 1 次（上限就这一次）
    hits = entry.scratch_hit
    if entry.scratched and hits == 0:
        hits = 1
    return min(hits / 10.0, 1.0)


def score(entry: Entry, profile: Profile) -> float:
    """单条打分：0~4 区间的加权分（各维先归一，再乘权重相加）。"""
    w = max(0.0, min(1.0, entry.weight))
    # [白妈拍板③] 承诺兑现那一刻额外 +0.2 情感分（高兴是有分量的）
    if entry.kind == "promise" and entry.promise_done:
        w = min(1.0, w + profile.promise_done_emotion_boost)
    s = (profile.emotion_w * w
         + profile.recency_w * _decay_factor(entry, profile)
         + profile.hits_w * _hits_score(entry, profile)
         + profile.source_w * _source_score(entry, profile))
    # [白妈拍板①] 默认 tier：不砍分，沉档由 memory_tier/is_hot 负责。
    # legacy "score" 路径保留旧的 ×0.5 砍分，向后兼容。
    if profile.mistake_downgrade == "score":
        if (entry.kind == "mistake" and entry.solved_days is not None
                and entry.solved_days >= mistake_sink_days(profile)):
            s *= 0.5
    return s


def is_hot(entry: Entry, profile: Profile, now_days: float = None) -> bool:
    """沉没判定。豁免的永远 hot——「永不」条款的落点都在这里。"""
    # [白妈拍板①] mistake 沉档优先于一切豁免（沉层是惩罚，压过豁免）
    if mistake_tier(entry, profile) == "warehouse":
        return False
    # [情感条款#6·苗苗] promise 未勾 → 永不下沉
    if entry.kind == "promise" and not entry.promise_done:
        return True
    # [白妈拍板③] 兑现后的高兴期：勾掉 7 天内豁免不解除
    if (entry.kind == "promise" and entry.promise_done
            and entry.promise_done_days is not None
            and entry.promise_done_days <= profile.promise_done_grace_days):
        return True
    if entry.sink_exempt:
        return True
    # [情感条款#1] 满格情感不随时间沉
    if profile.decay_exempt_emotion_full and entry.weight >= 1.0:
        return True
    if entry.kind in profile.decay_exempt_kinds:
        return True
    if now_days is None:
        now_days = entry.last_touched_days
    return now_days < profile.sink_days


def memory_tier(entry: Entry, profile: Profile, now_days: float = None) -> str:
    """三层归位：hot / cold / warehouse。上层的 is_hot 只是 hot 的判定。"""
    if mistake_tier(entry, profile) == "warehouse":
        return "warehouse"
    if is_hot(entry, profile, now_days):
        return "hot"
    return "cold"


# ---------------------------------------------------------------------------
# 可插拔排序
# ---------------------------------------------------------------------------

_SORT_REGISTRY = {}


def register_sort(name: str, fn) -> None:
    """挂自定义排序：fn(entries, profile, scores) -> 排好序的 entries。"""
    _SORT_REGISTRY[name] = fn


def _sort_plain(entries, profile, scores):
    """通用「防忘记」：纯按分数降序。"""
    return [e for _, e in sorted(zip(scores, entries), key=lambda t: -t[0])]


def _sort_sink_guarded(entries, profile, scores):
    """砚砚的「防重踩」：决定/未勾承诺/豁免类置顶，其余按分数。

    我的本子按「防重踩」排序，不按「防忘记」排序——
    重踩一次的代价比忘一次大。
    """
    top, rest = [], []
    for e, s in zip(entries, scores):
        if e.kind in profile.decay_exempt_kinds or (e.kind == "promise" and not e.promise_done) or e.sink_exempt:
            top.append((s, e))
        else:
            rest.append((s, e))
    top.sort(key=lambda t: -t[0])
    rest.sort(key=lambda t: -t[0])
    return [e for _, e in top] + [e for _, e in rest]


register_sort("plain", _sort_plain)
register_sort("sink_guarded", _sort_sink_guarded)


def rank(entries, profile: Profile):
    """整批打分 + 排序。返回 (排好的 entries, 对应分数表)。"""
    scored = [(score(e, profile), e) for e in entries]
    fn = _SORT_REGISTRY.get(profile.sort_fn, _sort_sink_guarded)
    ordered = fn([e for _, e in scored], profile, [s for s, _ in scored])
    scores = {e.id: s for s, e in scored}
    return ordered, scores
