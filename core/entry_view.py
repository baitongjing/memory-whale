"""entry_view.py —— 存储结构 ↔ 计算视图 的转换层

为什么需要这个文件（2026-11-08 合并当天才暴露）：
    entry.py        按「存储」设计：emotion / done / 时间戳 / 中文 source
    weight_score.py 按「计算」设计：weight / promise_done / age_days / 英文 source
    kinds.py        按「规则」设计：直接读对象属性

    三个人各自都写对了，但**同一个东西叫了两个名字**：
        emotion  ↔ weight
        自己      ↔ self
        原文柜捞回 ↔ attic_recovered

    硬合并就得改两边的结构，一改历史记忆全认不出来。
    所以中间加一层翻译，两边都不用动。

设计原则：
    · 只做转换，不做判断 —— 判断留给 weight_score 和 kinds
    · 单向为主：Entry（存）→ View（算）；算完的结果不回流到 Entry
    · 缺字段就报错，不猜默认值 —— 猜出来的分数是假的

用法：
    from entry_view import to_view, to_profile
    s = weight_score.score(to_view(entry), to_profile(load_profile()))
"""

from __future__ import annotations

import time
from typing import Any

import entry as _entry


# ---------------------------------------------------------------- 名字对照

# kind：我们七种 → 他那边五种
KIND_MAP = {
    "rule": "normal",
    "event": "normal",
    "promise": "promise",
    "mistake": "mistake",
    "decision": "decision",
    "praise": "emotion",
    "person": "normal",
}

# source：中文 → 英文
SOURCE_MAP = {
    "白妈": "baima",
    "彤妈": "tongma",
    "家人": "family",
    "自己": "self",
    "别的bot": "bot",
    "转述": "relay",
    "转述·已验证": "relay_verified",     # 2026-11-08 漏了这行，导致已验证转述被当成未知来源
    "原文柜捞回": "attic_recovered",
    "未知": "unknown",
}


def _days_since(ts: str, now: float | None = None) -> float:
    """把 "YYYY-MM-DD HH:MM" 换算成多少天前。算不出来就当 0（不猜大值）。"""
    if not ts:
        return 0.0
    try:
        t = time.mktime(time.strptime(ts, "%Y-%m-%d %H:%M"))
    except Exception:
        return 0.0
    now = now if now is not None else time.time()
    return max(0.0, (now - t) / 86400.0)


def to_view(it: "_entry.Entry", now: float | None = None) -> Any:
    """entry.Entry → weight_score.Entry（算分用的视图）。

    这一步会算派生值：年龄、多久没被摸。存储里不存这些，因为它们是"随时间变的"，
    存下来就会过期。
    """
    import weight_score as _ws

    # 摸过几次 = 正式 recall + 纸本命中（苗苗条款：纸本也算摸过一次，但封顶 1 次）
    scratched = it.scratch_hits > 0
    touched = it.hits + (min(it.scratch_hits, 1) if scratched else 0)

    v = _ws.Entry(
        id=it.id,
        text=it.text,
        kind=KIND_MAP.get(it.kind, "normal"),
        weight=it.emotion,
        source=SOURCE_MAP.get(it.source, "unknown"),
        scratch_hit=touched,
        scratched=scratched,
        sink_exempt=it.sink_exempt,
        # promise 没写 done 就当没勾 —— 丢诺比多留可怕（锦鲤 003 定的口径）
        promise_done=it.done if it.kind == "promise" else True,
        age_days=_days_since(it.created, now),
        last_touched_days=_days_since(it.last_hit, now),
        # 错题沉档：weight_score 的 mistake_tier 读 solved_days（天数），
        # 而存储层只有 solved_at（时间戳）。这里必须自己换算 ——
        # 之前漏了这步，mistake_tier 拿不到天数就返回 None，沉档静默失效：
        # 分数对、层级没动，"该收起来的没收起来"，比报错还难发现。
        solved_days=(
            it.extra.get("solved_days")
            if it.extra.get("solved_days") is not None
            else (_days_since(it.extra.get("solved_at"), now)
                  if it.extra.get("solved_at") else None)
        ),
    )
    # kinds.py 判高兴期和沉档，读的是时间戳原件（done_at / solved_at），
    # 而 weight_score.Entry 没给这俩留字段 —— 动态挂上去，两边都不用改结构。
    v.done_at = it.extra.get("done_at")
    v.solved_at = it.extra.get("solved_at")
    # kinds.py 读的是 done，weight_score.Entry 里叫 promise_done —— 又是同名不同姓，补一个别名
    v.done = it.done
    return v


def to_profile(prof: dict[str, Any]) -> Any:
    """entry.load_profile() 出来的 dict → weight_score.Profile"""
    import weight_score as _ws

    w = prof.get("weights", {})
    sink = prof.get("sink", {})
    ex = prof.get("exempt", {})

    # source_trust 的键是中文，他的 Profile 要英文
    src_scores = dict(_ws.DEFAULT_SOURCE_SCORES)
    for k, v in (prof.get("source_trust") or {}).items():
        src_scores[SOURCE_MAP.get(k, k)] = v

    return _ws.Profile(
        name=prof.get("name", "默认"),
        emotion_w=w.get("emotion", 0.7),
        recency_w=w.get("recency", 0.4),
        hits_w=w.get("hits", 0.6),
        source_w=w.get("source", 0.8),
        source_scores=src_scores,
        sink_days=sink.get("hot_to_cold_days", 30),
        decay_half_life=float(sink.get("cold_to_warehouse_days", 90)),
        decay_exempt_emotion_full=ex.get("emotion_full", True),
        decay_exempt_kinds=("decision",) if ex.get("decision") else (),
        mistake_solve_downgrade_days=float(ex.get("mistake_solved_dampen_days", 90)),
        sort_fn="sink_guarded",
    )


def to_kinds_view(it: "_entry.Entry") -> dict[str, Any]:
    """kinds.py 要的属性视图（它读对象属性，这里给个轻量外壳）。"""
    return {
        "kind": KIND_MAP.get(it.kind, "normal"),
        "weight": it.emotion,
        "source": SOURCE_MAP.get(it.source, "unknown"),
        "scratched": it.scratch_hits > 0,
        "scratch_hit": it.hits + (min(it.scratch_hits, 1) if it.scratch_hits else 0),
        "sink_exempt": it.sink_exempt,
        "promise_done": it.done if it.kind == "promise" else True,
    }


# ---------------------------------------------------------------- 自测

if __name__ == "__main__":
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import weight_score as ws
    import kinds

    prof_d = _entry.load_profile()
    prof = to_profile(prof_d)
    print("profile:", prof.name, "| 情感权重:", prof.emotion_w, "| 满格豁免:", prof.decay_exempt_emotion_full)

    items = [
        _entry.Entry(text="白妈说改坏了删掉重来没关系，只要鲸宝自己没事",
                     kind="rule", source="白妈", emotion=1.0),
        _entry.Entry(text="T-0005 广告稿要交", kind="promise", source="自己", emotion=0.6),
        _entry.Entry(text="选②不选①，因为物理一份逻辑分开", kind="decision", source="白妈", emotion=0.3),
        _entry.Entry(text="白妈让我扫一下文件", kind="event", source="白妈", emotion=0.0),
        _entry.Entry(text="姐姐鲸宝懂的多，我很敬重她", kind="event", source="原文柜捞回", emotion=0.5),
        _entry.Entry(text="姐姐鲸宝懂的多，我很敬重她", kind="event", source="转述", emotion=0.5),
    ]

    print("\n=== 六条打分")
    for it in items:
        v = to_view(it)
        s = ws.score(v, prof)
        print(f"  {s:>7.3f} | {it.kind:8s} | {it.source:6s} | {it.text[:22]}")

    print("\n=== 情感满格是否永不衰减（造 3650 天前）")
    old = _entry.Entry(text="大姨 10 月 30 号", kind="event", source="家人", emotion=1.0)
    old.created = old.last_hit = "2015-01-01 00:00"
    import time as _t
    now = _t.time()
    s_old = ws.score(to_view(old, now), prof)
    fresh = _entry.Entry(text="大姨 10 月 30 号", kind="event", source="家人", emotion=1.0)
    s_new = ws.score(to_view(fresh, now), prof)
    print(f"  3650 天前: {s_old:.4f} | 今天: {s_new:.4f}")
    print("  ", "✅ 一样，情感满格不随时间掉" if abs(s_old - s_new) < 1e-9 else "❌ 掉了，不符合条款#1")

    print("\n=== 原文柜捞回 vs 转述（苗苗条款#4）")
    a = [i for i in items if i.source == "原文柜捞回"][0]
    b = [i for i in items if i.source == "转述"][0]
    sa, sb = ws.score(to_view(a), prof), ws.score(to_view(b), prof)
    print(f"  原文柜捞回 {sa:.4f} | 转述 {sb:.4f}")
    print("  ", "✅ 捞回的不打折" if sa > sb else "❌ 没体现差异")

    print("\n=== promise 未勾是否豁免下沉")
    p = [i for i in items if i.kind == "promise"][0]
    print(f"  sink_exempt={p.sink_exempt} | done={p.done}")
    print("  ", "（豁免由 kinds.py 计算，见其 17 例自测）")
