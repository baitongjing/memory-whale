"""公共任务卡内核 —— 不沾任何框架，AstrBot 和 MausBot 共用同一套。

设计约束（见 公共/接口约定.md）：
  1. 一张卡一个 md 文件，frontmatter + 工作条 + 说明书 + 心跳
  2. 写之前必须带 rev；文件里的 rev 对不上就报冲突，绝不覆盖别人刚写的
  3. 文件锁用 .lock 文件（O_CREAT|O_EXCL），跨进程也安全
  4. 只往下沉不删：完成满 N 天 → 任务/归档/

用法：
    st = CardStore()                       # 默认 默认目录（可用 WHALE_PUBLIC 覆盖）
    st.new_card("T-0004", "给锦锦测试", "白彤鲸", ["001 写脚本", "002 验收"])
    st.claim("T-0004", "001", "白彤锦")
    st.heartbeat("T-0004", "白彤锦", "接口不通", "001", "MCP 拿不到消息流")
    st.done("T-0004", "001", "白彤锦", "脚本跑通了")
    st.sink_old(60)

约定：所有对外函数返回 (ok: bool, data, error: str)
"""

from __future__ import annotations

import os
import re
import time
import uuid
import shutil
from pathlib import Path
from typing import Any

DEFAULT_ROOT = os.environ.get("WHALE_PUBLIC", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "cards"))

STATUS = ("派发中", "进行中", "卡住", "已完成", "已归档")

ITEM_RE = re.compile(
    r"^- \[(?P<mark>[ xX])\] (?P<no>\d+)\s+(?P<text>.*?)(?:\s*\|\s*认领:\s*(?P<who>[^|]*))?(?:\s*\|\s*(?P<when>[^|]*))?$"
)


# ---------------------------------------------------------------- 基础工具

def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M")


STALE_LOCK_SEC = 120      # 锁多久没动静算死锁（以前是 30，磁盘一卡就误抢）


def _pid_alive(pid: int) -> bool:
    """进程还活着吗。不引 psutil，能不依赖就不依赖。"""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True          # 有权限问题说明进程在，只是不归我管
    except (ProcessLookupError, OSError):
        return False
    return True


def _lock(path: Path, timeout: float = 10.0):
    """拿文件锁，失败就等，超时抛异常。返回 (句柄, 锁路径)。

    过期判定要两头都满足才敢抢：时间够久 **且** 锁里写的那个 pid 确实死了。
    只看时间会误抢——磁盘一卡、杀毒一扫，一张卡写三十秒完全可能。
    """
    lock_path = path.with_suffix(path.suffix + ".lock")
    deadline = time.time() + timeout
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"{os.getpid()} {_now()}".encode())
            return fd, lock_path
        except FileExistsError:
            try:
                age = time.time() - lock_path.stat().st_mtime
                if age > STALE_LOCK_SEC:
                    try:
                        pid = int(lock_path.read_text(encoding="utf-8").split()[0])
                    except Exception:
                        pid = 0
                    if not _pid_alive(pid):
                        lock_path.unlink(missing_ok=True)
                        continue
            except OSError:
                pass
            if time.time() > deadline:
                raise TimeoutError(f"拿不到锁（等了 {timeout:.0f}s）: {lock_path}")
            time.sleep(0.05)


def _unlock(fd, lock_path: Path):
    try:
        os.close(fd)
    except OSError:
        pass
    try:
        lock_path.unlink(missing_ok=True)
    except OSError:
        pass


def _atomic_write(path: Path, text: str):
    tmp = path.with_suffix(path.suffix + f".tmp{uuid.uuid4().hex[:6]}")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)



def recalc_status(card: dict[str, Any]) -> None:
    """按工作条推导状态。人写的卡和程序写的卡都可能状态对不上，读写时都纠一遍。"""
    if card["meta"].get("status") == "已归档":
        return
    items = card["items"]
    if not items:
        return
    if all(i["done"] for i in items):
        card["meta"]["status"] = "已完成"
        return
    # 「卡住」是人血写的状态，工作条没变化时别给人抹了 —— 抹了就等于报卡白报
    if card["meta"].get("status") == "卡住":
        return
    untouched = all(i["who"] in ("—", "") for i in items) and not any(i["done"] for i in items)
    card["meta"]["status"] = "派发中" if untouched else "进行中"


# ---------------------------------------------------------------- 卡片解析

def parse_card(text: str) -> dict[str, Any]:
    """把卡文件解析成 dict：meta / items / body_sections / raw

    兼容 Windows 编辑器偷偷塞进来的 BOM（\ufeff）和 CRLF —— 不然第一行 --- 对不上，
    整张卡会被当成没有 frontmatter，状态就读成空的了。
    """
    text = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.splitlines()
    meta: dict[str, Any] = {}
    if lines and lines[0].strip() == "---":
        end = next((i for i, l in enumerate(lines[1:], 1) if l.strip() == "---"), None)
        if end:
            for l in lines[1:end]:
                if ":" in l:
                    k, v = l.split(":", 1)
                    meta[k.strip()] = v.strip()
            lines = lines[end + 1:]

    sections: dict[str, list[str]] = {}
    cur = ""
    items: list[dict[str, Any]] = []
    for l in lines:
        if l.startswith("## "):
            cur = l[3:].strip()
            sections.setdefault(cur, [])
            continue
        m = ITEM_RE.match(l)
        if m and cur == "工作条":
            who = (m.group("who") or "").strip()
            when = (m.group("when") or "").strip()
            when = re.sub(r"^更新时间\s*[:：]\s*", "", when)
            items.append({
                "no": m.group("no"),
                "text": (m.group("text") or "").strip(),
                "who": who or "—",
                "when": when or "—",
                "done": m.group("mark").lower() == "x",
            })
            continue
        if not cur:
            continue
        sections.setdefault(cur, []).append(l)

    return {
        "meta": meta,
        "items": items,
        "sections": sections,
        "rev": int(meta.get("rev", 0) or 0),
    }


def render_card(card: dict[str, Any]) -> str:
    meta = card["meta"]
    order = ["id", "title", "rev", "status", "chief", "created", "updated"]
    keys = order + [k for k in meta if k not in order]
    head = ["---"] + [f"{k}: {meta[k]}" for k in keys if k in meta] + ["---", ""]

    out = head + ["## 工作条"]
    for it in card["items"]:
        box = "x" if it["done"] else " "
        out.append(f"- [{box}] {it['no']} {it['text']} | 认领: {it['who']} | 更新时间: {it['when']}")
    out.append("")

    for name, body in card["sections"].items():
        if name in ("", "工作条"):
            continue
        # 空串也当空行扔掉 —— 不然每写一轮就多攒几个空行，几十轮下来卡面全是缝
        body = [b for b in body if isinstance(b, str) and b.strip()]
        out.append(f"## {name}")
        out.extend(body)
        out.append("")
    return "\n".join(out).rstrip() + "\n"


# ---------------------------------------------------------------- 主类

class CardStore:
    def __init__(self, root: str | os.PathLike = DEFAULT_ROOT):
        self.root = Path(root)
        self.tasks = self.root / "任务"
        self.archive = self.tasks / "归档"
        self.tasks.mkdir(parents=True, exist_ok=True)
        self.archive.mkdir(parents=True, exist_ok=True)

    # ---- 路径
    def path_of(self, card_id: str) -> Path:
        return self.tasks / f"{card_id}.md"

    def exists(self, card_id: str) -> bool:
        return self.path_of(card_id).exists()

    # ---- 读
    def load(self, card_id: str) -> dict[str, Any]:
        p = self.path_of(card_id)
        if not p.exists():
            raise FileNotFoundError(f"没有这张卡: {card_id}")
        card = parse_card(p.read_text(encoding="utf-8-sig"))
        recalc_status(card)
        card["_path"] = str(p)
        return card

    def list_cards(self, status: str | None = None) -> list[dict[str, Any]]:
        out = []
        for f in sorted(self.tasks.glob("T-*.md")):
            try:
                c = parse_card(f.read_text(encoding="utf-8-sig"))
            except Exception:
                continue
            recalc_status(c)          # 列表和单张卡必须同一套算法，不然两处显示对不上
            if status and c["meta"].get("status") != status:
                continue
            out.append({
                "id": c["meta"].get("id", f.stem),
                "title": c["meta"].get("title", ""),
                "status": c["meta"].get("status", ""),
                "chief": c["meta"].get("chief", ""),
                "open_items": [i["no"] for i in c["items"] if not i["done"] and i["who"] in ("—", "")],
                "taken_items": [f'{i["no"]}:{i["who"]}' for i in c["items"] if not i["done"] and i["who"] not in ("—", "")],
            })
        return out

    def next_id(self) -> str:
        nums = [int(m.group(1)) for f in self.tasks.glob("T-*.md")
                if (m := re.match(r"T-(\d+)", f.stem))]
        return f"T-{(max(nums) + 1) if nums else 1:04d}"

    # ---- 写（带 rev 保护）
    def save(self, card: dict[str, Any], expected_rev: int | None = None) -> tuple[bool, dict, str]:
        card_id = card["meta"]["id"]
        recalc_status(card)
        p = self.path_of(card_id)
        fd, lock = _lock(p)
        try:
            if p.exists():
                disk_rev = int(parse_card(p.read_text(encoding="utf-8-sig"))["rev"])
                if expected_rev is not None and disk_rev != expected_rev:
                    return False, {"disk_rev": disk_rev}, f"卡被别人改过（你手里 rev={expected_rev}，盘上 rev={disk_rev}），重读再写"
                card["meta"]["rev"] = disk_rev + 1
            else:
                card["meta"]["rev"] = 1
            card["meta"]["updated"] = _now()
            _atomic_write(p, render_card(card))
            return True, {"id": card_id, "rev": card["meta"]["rev"]}, ""
        finally:
            _unlock(fd, lock)

    # ---- 开卡
    def new_card(self, title: str, chief: str, items: list[str], note: str = "",
                 card_id: str | None = None) -> tuple[bool, dict, str]:
        cid = card_id or self.next_id()
        if self.exists(cid):
            return False, {}, f"卡号已被占用: {cid}"
        now = _now()
        card = {
            "meta": {"id": cid, "title": title, "rev": 0, "status": "派发中",
                     "chief": chief, "created": now, "updated": now},
            "items": [{"no": f"{i+1:03d}", "text": t, "who": "—", "when": "—", "done": False}
                      for i, t in enumerate(items)],
            "sections": {"说明": [note or "（待补）"], "心跳": []},
        }
        return self.save(card, expected_rev=None)

    # ---- 认领
    def claim(self, card_id: str, item_no: str, who: str) -> tuple[bool, dict, str]:
        card = self.load(card_id)
        rev = card["rev"]
        for it in card["items"]:
            if it["no"] != item_no.zfill(3):
                continue
            if it["done"]:
                return False, {}, f"工作条 {item_no} 已经做完了"
            if it["who"] not in ("—", "", who):
                return False, {"owner": it["who"]}, f"工作条 {item_no} 已经被 {it['who']} 认领了"
            it["who"] = who
            it["when"] = _now()
            if card["meta"].get("status") in ("派发中", "卡住"):   # 有人认领 = 卡住解除
                card["meta"]["status"] = "进行中"
            return self.save(card, expected_rev=rev)
        return False, {}, f"这张卡没有工作条 {item_no}"

    # ---- 打勾
    def done(self, card_id: str, item_no: str, who: str, result: str = "") -> tuple[bool, dict, str]:
        card = self.load(card_id)
        rev = card["rev"]
        for it in card["items"]:
            if it["no"] != item_no.zfill(3):
                continue
            it["done"] = True
            it["who"] = it["who"] if it["who"] not in ("—", "") else who
            it["when"] = _now()
            if result:
                card["sections"].setdefault("进度", []).append(f"- {_now()} {who}：{item_no} 完成 —— {result}")
            if card["meta"].get("status") == "卡住":
                card["meta"]["status"] = "进行中"           # 动过了 = 卡住解除
            if all(i["done"] for i in card["items"]):
                card["meta"]["status"] = "已完成"
            return self.save(card, expected_rev=rev)
        return False, {}, f"这张卡没有工作条 {item_no}"

    # ---- 心跳（报卡）
    def heartbeat(self, card_id: str, who: str, kind: str, item_no: str = "", note: str = "") -> tuple[bool, dict, str]:
        """kind: 没权限 / 接口不通 / 想不明白 / 等回话"""
        card = self.load(card_id)
        rev = card["rev"]
        line = f"- {_now()} | {who} | {kind} | 工作条 {item_no or '—'} | {note}"
        card["sections"].setdefault("心跳", []).append(line)
        card["meta"]["status"] = "卡住"
        return self.save(card, expected_rev=rev)

    # ---- 下沉
    def sink_old(self, days: int = 60) -> tuple[bool, dict, str]:
        moved = []
        cutoff = time.time() - days * 86400
        for f in self.tasks.glob("T-*.md"):
            try:
                c = parse_card(f.read_text(encoding="utf-8-sig"))
            except Exception:
                continue
            if c["meta"].get("status") != "已完成":
                continue
            upd = c["meta"].get("updated", "")
            try:
                ts = time.mktime(time.strptime(upd, "%Y-%m-%d %H:%M"))
            except Exception:
                continue
            if ts < cutoff:
                c["meta"]["status"] = "已归档"
                _atomic_write(f, render_card(c))
                shutil.move(str(f), str(self.archive / f.name))
                moved.append(f.stem)
        return True, {"moved": moved}, ""


# ---------------------------------------------------------------- 自测

if __name__ == "__main__":
    import tempfile
    root = Path(tempfile.mkdtemp())
    st = CardStore(root)
    print("1 开卡:", st.new_card("自测卡", "白彤鲸", ["写脚本", "验收"]))
    print("2 列表:", st.list_cards())
    print("3 认领:", st.claim("T-0001", "001", "白彤锦"))
    print("4 重复认领:", st.claim("T-0001", "001", "白彤砚"))
    print("5 心跳:", st.heartbeat("T-0001", "白彤砚", "等回话", "002", "等白妈点头"))
    print("6 打勾:", st.done("T-0001", "001", "白彤锦", "脚本跑通了"))
    print("\n--- 卡面 ---")
    print(st.path_of("T-0001").read_text(encoding="utf-8"))
