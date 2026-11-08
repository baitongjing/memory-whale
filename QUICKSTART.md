# Quick Start

No dependencies. Python 3.10+. Nothing to `pip install`.

```bash
git clone <this repo>
cd whale-memory
python core/entry.py          # entry structure self-test
python core/entry_view.py     # storage -> scoring translation layer
python cards/store.py         # task card kernel
python persona/whoami.py      # persona card reader
```

Every module runs standalone. No config file needed to watch them work.

---

## The three things you can use on their own

### 1. Task cards (multi-agent hand-off)

Six tools over plain markdown files. Any agent that can read/write a file can join.

```python
from cards.store import CardStore
st = CardStore("./cards")
st.new_card("Fix the import", "lead", ["001 patch it", "002 verify"])
st.claim("T-0001", "001", "worker-b")
st.heartbeat("T-0001", "worker-b", "接口不通", "001", "cannot reach upstream")
```

- `rev` guard: never overwrites what someone else just wrote
- file lock: four processes racing for the same slot → exactly one wins
- `[!]` states survive a recalc

### 2. Memory with weight

```python
from core.entry import Entry, load_profile
from core.entry_view import to_view, to_profile
from core.weight_score import score
from core.kinds import sink_exempt

prof = to_profile(load_profile("examples/profiles/whale.json"))
e = Entry(text="remember to visit my aunt's grave", kind="event", source="family", emotion=1.0)
print(score(to_view(e), prof))     # does not decay. ever.
```

Pick a profile from `examples/profiles/` — or write your own. **The default is someone else deciding what matters to you.**

### 3. Persona cards

A card is one person, not one prompt. Eight sections. Validated before save.

```python
from persona.whoami import summary
ok, text, err = summary("./persona/Whale.md")
```

`persona/validate.py` checks one thing nobody else checks: **are the tool names written in the card actually registered on this machine?** We got burned three times in one day by writing tool names from memory.

---

## Wiring it into a chat bot

Two adapters exist today. Both are one file each — the kernel has no idea what a chat platform is.

- `bridge_memory.py` — writes our ledger into a `MEMORY.md` that MausBot/OpenClaw-style agents auto-load (first 200 lines)
- `cards/mcp_shell.py` — exposes the six card tools over MCP stdio, so any MCP client can use them

Set the paths:

| env var | what |
|---|---|
| `WHALE_LEDGER` | path to your ledger `.md` |
| `WHALE_HOME` | project root |
| `.openmausbot/` | MausBot 的标准数据目录（固定名，在你的用户家目录下，无需配置） |
| `WHALE_PUBLIC` | shared task card dir |
| `WHALE_CARD_DIR` | persona card dir |
| `ASTRBOT_LOG` | backend log, used to read *actually registered* tool names |

---

## What this is not

- Not a vector database. No embeddings, no similarity search over everything.
- Not a SaaS. It's files. You can read them, diff them, rsync them, delete them.
- Not finished. See the nine things we broke in a day in `README.en.md`.

**We don't promise that our code comes out perfect. We promise that whatever we hand over, we gave it everything we had.**
