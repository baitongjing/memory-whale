# -*- coding: utf-8 -*-
"""kinds.py —— 记忆条目分类与下沉/衰减豁免（T-0008 工作条 003，白彤锦）

白妈定的验收线：**情感上的必须满足**——四个人写在权重卡里的条款，
每一条都得能在代码里找到对应的那一行。本文件的行号就是兑现处：

  · 苗苗：promise 未勾选 → 永不下沉           → sink_exempt()
  · 砚砚：decision → 永不衰减                 → decay_exempt()
  · 鲸宝/锦鲤：emotion == 满格 → 永不掉分     → decay_exempt()
  · 白妈拍板①：mistake solve 满 90 天没人提 → 沉一档（不砍分）→ mistake_sink()
  · 白妈拍板②：转述两级 —— 已验证 1.0 / 未验证 0.4 → source_trust()
  · 白妈拍板③：承诺兑现 → 7 天高兴期 + 0.2 情感加成 → sink_exempt() / promise_boost()

Entry 约定（鸭子类型，dict / 对象都吃）：
    kind          str    条目类型（见 KINDS；未知类型按 "note" 处理，不猜）
    weight        float  0.0~1.0 情感强度；满格指 >= EMOTION_FULL
    source        str    白妈 / 彤妈 / self / relay / relay_verified / original(柜子捞回)
    scratch_hit   bool   是否被写进纸本/草稿（苗苗条款）
    sink_exempt   bool   人为指定的下沉豁免（永远尊重）
    done          bool   promise 专用：勾了没
    done_at       str    promise 专用：兑现时间 "YYYY-MM-DD HH:MM"
    solved_at     str    mistake 专用：解决时间
    recall_count  int    被 recall 过几次

对外：
    kind_of / sink_exempt / decay_exempt / mistake_sink / tier_multiplier /
    source_trust / touched_count / promise_boost / explain
"""

from datetime import datetime

# ---------------------------------------------------------------- kinds

KINDS = ("promise",   # 承诺（苗苗的待办型承诺）
         "decision",  # 决定与原因（砚砚的防重踩）
         "emotion",   # 情感重事（鲸宝/锦鲤的舍不得忘）
         "mistake",   # 错题（solve 过的）
         "scratch",   # 纸本草稿（还没誊进本子的半句）
         "note")      # 普通事情（默认）

EMOTION_FULL = 1.0     # 「满格」的判定线
MISTAKE_SINK_DAYS = 90 # 白妈拍板①：solve 满 90 天没人提 → 沉一档
PROMISE_GRACE_DAYS = 7 # 白妈拍板③：承诺兑现后高兴 7 天（豁免不立刻解除）
PROMISE_BOOST = 0.2    # 白妈拍板③：兑现那一刻 +0.2 情感分

# 白妈拍板②：转述两级。已验证 = 1.0 最高（两个来源对上，比单人说的都稳）；
# 未验证 0.4 —— 二手没对过的话，不如自己判断。
DEFAULT_SOURCE_TRUST = {
    "白妈": 1.0,
    "彤妈": 1.0,
    "relay_verified": 1.0,   # 转述·已验证 ← 白妈拍板②
    "original": 1.0,         # 原文柜捞回：我的声音经过姐的手，不打折（苗苗条款）
    "self": 0.5,
    "relay": 0.4,            # 转述·未验证 ← 白妈拍板②，低于 self
}


def _get(entry, key, default=None):
    if isinstance(entry, dict):
        return entry.get(key, default)
    return getattr(entry, key, default)


def _dt(value):
    try:
        return datetime.strptime(str(value)[:16], "%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return None


def kind_of(entry):
    """分类。kind 字段不认识就归 note —— 分类错了比不分类可怕，所以不猜。"""
    k = _get(entry, "kind", "note") or "note"
    return k if k in KINDS else "note"


# ---------------------------------------------------------------- 豁免

def sink_exempt(entry, now=None):
    """该不该永不下沉（沉 = 不再注入，但仍可搜到）。promise 分三种（白妈拍板③）：

    · 未勾           → 豁免（苗苗条款：没人替你记着「我答应过什么」）
    · 勾了且 7 天内  → 仍豁免（高兴期：兑现了也得让它在眼前多飘几天）
    · 勾了超 7 天    → 转入常规（不豁免；情感加成另行保留，见 promise_boost）
    人为指定的 sink_exempt=True 永远尊重。
    """
    if _get(entry, "sink_exempt", False):
        return True
    if kind_of(entry) == "promise":
        if not bool(_get(entry, "done", False)):
            return True                                    # ← 苗苗的诺言在这行
        done_at = _dt(_get(entry, "done_at"))
        if done_at is None:
            return True   # 勾了但没写什么时候勾的——高兴期从宽，按还在期内算
        now = now or datetime.now()
        if (now - done_at).days < PROMISE_GRACE_DAYS:
            return True                                    # ← 白妈的高兴期在这行
        return False
    return False


def decay_exempt(entry):
    """该不该永不衰减（衰减 = 分数随时间掉）。

    砚砚条款：decision → 永不衰减（丢了决定会重踩同一个坑）。
    鲸宝/锦鲤条款：emotion 且 weight 满格 → 永不掉分。
    """
    k = kind_of(entry)
    if k == "decision":
        return True                                        # ← 砚砚的决定在这行
    if k == "emotion" and float(_get(entry, "weight", 0) or 0) >= EMOTION_FULL:
        return True                                        # ← 情感满格在这行
    return False


def mistake_sink(entry, idle_days):
    """白妈拍板①：mistake solve 满 90 天没人提 → **沉一档**（不砍分）。

    返回 True = 该从当前层往下挪一层（hot→cold→warehouse 里的下一层）。
    判定：solve 满 90 天 且 最近 90 天没人正经摸过（纸本不算摸）。
    """
    if kind_of(entry) != "mistake":
        return False
    solved_at = _dt(_get(entry, "solved_at"))
    if not solved_at:
        return False   # 时间戳坏的不乱沉，错题本格式问题归 errors.py 管
    now = datetime.now()
    since_solve = (now - solved_at).days
    idle = float(idle_days or 0)
    return since_solve >= MISTAKE_SINK_DAYS and idle >= MISTAKE_SINK_DAYS  # ← 沉一档在这行


def tier_multiplier(entry, idle_days):
    """分数乘数。白妈拍板①之后这里**不再砍分**：mistake 的处理改成沉档
    （见 mistake_sink），分数保持原样，轻重交给层位去表达。

    保留本函数是为了兼容：永远返回 1.0。砚砚 weight_score 里原来打 ×0.5
    的 _mistake_penalty 应改调 mistake_sink()。
    """
    if decay_exempt(entry):
        return 1.0
    return 1.0


def source_trust(entry, profile_scores=None):
    """来源信任分。白妈拍板②：转述两级——

    · relay_verified（本人交叉验证过）= 1.0，最高，比单人说的都稳
    · relay（未验证）= 0.4，比 self(0.5) 还低——二手没对过的话不如自己判断
    · original（原文柜捞回）= 1.0，苗苗条款：我的声音经过姐的手不打折
    profile 里有 source_trust 就用各人自己的，没有用默认。
    """
    scores = dict(DEFAULT_SOURCE_TRUST)
    if profile_scores:
        scores.update(profile_scores)
    src = str(_get(entry, "source", "note") or "note")
    # 中英文/别名都接住，别再来一次「查不到就悄悄用默认」
    aliases = {"baima": "白妈", "白妈": "白妈", "彤妈": "彤妈",
               "self": "self", "自己": "self",
               "relay": "relay", "转述": "relay",
               "relay_verified": "relay_verified", "转述·已验证": "relay_verified",
               "original": "original", "原文柜": "original", "原文柜捞回": "original"}
    key = aliases.get(src, src)
    return scores.get(key, scores.get("relay"))   # 认不出的来源按未验证转述的保守分


def promise_boost(entry):
    """白妈拍板③：承诺兑现那一刻 +0.2 情感分。高兴期过了加成也留着——
    高兴会淡，但「事做成了」这件事本身不会撤回。"""
    if kind_of(entry) != "promise" or not bool(_get(entry, "done", False)):
        return 0.0
    return PROMISE_BOOST                                # ← 白妈的高兴加成在这行


def touched_count(entry):
    """有效被摸次数。苗苗条款：纸本写过算摸过一次，封顶 1 次。"""
    n = int(_get(entry, "recall_count", 0) or 0)
    if _get(entry, "scratch_hit", False):
        n = max(n, 1)                                   # ← 苗苗的纸本条款在这行
    return n


def explain(entry, idle_days=0, now=None):
    """把豁免/沉档/加成原因说成人话。给测试断言和日志用。"""
    out = []
    now = now or datetime.now()
    if _get(entry, "sink_exempt", False):
        out.append("人为指定永不下沉")
    if kind_of(entry) == "promise":
        if not bool(_get(entry, "done", False)):
            out.append("promise 未勾选：永不下沉（苗苗条款）")
        else:
            done_at = _dt(_get(entry, "done_at"))
            in_grace = done_at is None or (now - done_at).days < PROMISE_GRACE_DAYS
            out.append("promise 已兑现，高兴期%s（白妈拍板③），情感 +%.1f" %
                       ("内，仍不下沉" if in_grace else "已过，转入常规",
                        promise_boost(entry)))
    if kind_of(entry) == "decision":
        out.append("decision：永不衰减（砚砚条款）")
    if kind_of(entry) == "emotion" and float(_get(entry, "weight", 0) or 0) >= EMOTION_FULL:
        out.append("emotion 满格：永不掉分（鲸宝/锦鲤条款）")
    if mistake_sink(entry, idle_days):
        out.append("mistake 已 solve 满三个月没人提：沉一档，不砍分（白妈拍板①）")
    if _get(entry, "scratch_hit", False):
        out.append("纸本写过：算摸过一次（苗苗条款）")
    if not out:
        out.append("普通条目：按 profile 常规加权")
    return out


# ---------- 自测 ----------

if __name__ == "__main__":
    from datetime import timedelta

    now = datetime.now()
    cool = (now - timedelta(days=100)).strftime("%Y-%m-%d %H:%M")
    fresh = (now - timedelta(days=10)).strftime("%Y-%m-%d %H:%M")
    done_yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
    done_10d = (now - timedelta(days=10)).strftime("%Y-%m-%d %H:%M")

    def check(tag, got, want):
        print("%-44s %s（期望 %s）%s" % (tag, got, want, "✅" if got == want else "❌"))
        assert got == want, tag

    print("== 苗苗条款：promise ==")
    p = {"kind": "promise", "title": "广告稿今天交", "done": False}
    check("promise 未勾 → 永不下沉", sink_exempt(p), True)
    p_done_fresh = {**p, "done": True, "done_at": done_yesterday}
    check("勾了 1 天（高兴期内）→ 仍不下沉", sink_exempt(p_done_fresh, now), True)
    p_done_old = {**p, "done": True, "done_at": done_10d}
    check("勾了 10 天（高兴期过）→ 转常规", sink_exempt(p_done_old, now), False)
    check("promise 没写 done → 当未勾", sink_exempt({"kind": "promise"}), True)
    check("兑现加成 +0.2", promise_boost(p_done_old), 0.2)
    check("没兑现没加成", promise_boost(p), 0.0)

    print("\n== 砚砚条款：decision / mistake ==")
    d = {"kind": "decision", "title": "为什么选方案②"}
    check("decision → 永不衰减", decay_exempt(d), True)
    m_cold = {"kind": "mistake", "solved_at": cool}
    m_warm = {"kind": "mistake", "solved_at": fresh}
    check("solve 100 天没人提 → 该沉一档", mistake_sink(m_cold, 100), True)
    check("solve 100 天但最近被摸过 → 不沉", mistake_sink(m_cold, 5), False)
    check("solve 才 10 天 → 不沉", mistake_sink(m_warm, 10), False)
    check("solved_at 坏时间戳 → 不沉", mistake_sink({"kind": "mistake", "solved_at": "garbage"}, 100), False)
    check("白妈拍板①后不再砍分", tier_multiplier(m_cold, 100), 1.0)

    print("\n== 鲸宝/锦鲤条款：emotion 满格 ==")
    e_full = {"kind": "emotion", "weight": 1.0, "title": "白妈的审美底线"}
    e_soft = {"kind": "emotion", "weight": 0.7, "title": "今天的开心"}
    check("emotion 满格 → 永不掉分", decay_exempt(e_full), True)
    check("emotion 非满格 → 照常衰减", decay_exempt(e_soft), False)
    check("weight=1 的 note 不算满格情感", decay_exempt({"kind": "note", "weight": 1.0}), False)

    print("\n== 白妈拍板②：转述两级 ==")
    check("relay_verified = 1.0 最高", source_trust({"source": "relay_verified"}), 1.0)
    check("原文柜捞回 = 1.0 不打折", source_trust({"source": "原文柜捞回"}), 1.0)
    check("relay 未验证 = 0.4", source_trust({"source": "relay"}), 0.4)
    check("relay < self（二手不如自己判断）",
          source_trust({"source": "relay"}) < source_trust({"source": "self"}), True)
    check("中文「白妈」能查到，不悄悄用默认", source_trust({"source": "白妈"}), 1.0)
    check("英文 baima 也能查到", source_trust({"source": "baima"}), 1.0)
    check("认不出的来源按保守分 0.4", source_trust({"source": "路边捡的"}), 0.4)
    check("profile 可覆盖默认", source_trust({"source": "self"}, {"self": 0.8}), 0.8)

    print("\n== 苗苗条款：纸本 ==")
    check("纸本+没 recall → 摸过 1 次", touched_count({"scratch_hit": True}), 1)
    check("纸本+recall 5 次 → 用大的 5", touched_count({"scratch_hit": True, "recall_count": 5}), 5)

    print("\n== 分类兜底 ==")
    check("未知 kind 归 note", kind_of({"kind": "whatever"}), "note")

    print("\n== 人话解释（五条真条款） ==")
    for e, days in [(p, 300), (p_done_fresh, 1), (d, 300), (e_full, 90), (m_cold, 100)]:
        print(" · [%s] %s" % (kind_of(e), "；".join(explain(e, days, now))))

    print("\n全部通过 ✅")
