"""元素抽取：规则版（离线、确定性）与 LLM 版（正式）。"""
from __future__ import annotations

import json
import re

from . import llm
from .lexicon import AFFECT, PRED, STOP
from .normalize import clauses, cjk_runs, quoted

RULE_VER = "rules-v0.1"
LLM_VER = "llm-v0.4"

KINDS = ("person", "place", "object", "concept", "affect", "action", "sense", "quote")

# 代词与自称不是元素：她是叙述者，不是自己记忆里的一个物件
# 代词不是噪音，是**指代**：第一人称改写之后，"我"就是她本人，"他"就是他。
# 丢掉它们，因果链就断了（"我问他" 变成一句没有人的话）。
# 所以把它们接成锚点，而不是删掉：
PRONOUN_ANCHOR = {
    "我": "center", "自己": "center", "中心人物": "center",
    "他": "他", "你": "他", "用户": "他", "对方": "他",
    "我们": "我们", "大家": "大家", "所有人": "大家",
    "他们": "他们", "她们": "她们", "你们": "你们", "它": "它", "她": "她",
}
_DROP = set()

_STOP1 = {w for w in STOP if len(w) == 1}
_STOP_MULTI = sorted((w for w in STOP if len(w) > 1), key=len, reverse=True)
_CJK = re.compile(r"[\u4e00-\u9fff]+")

# 图里 `rule|元素选取` 的停用词。由 `load_stop_from_rules(con)` 填进来。
# 为什么要这一步：`STOP` 是代码常量，"哪些片段不成为元素"本来是写死的；
# 搬进规则之后，它**能被触发规则改写**——抽取规则第一次进了图的管辖。
_EXTRA_MULTI: list[str] = []


def set_extra_stop(words) -> None:
    global _EXTRA_MULTI
    _EXTRA_MULTI = sorted({str(w) for w in words if len(str(w)) >= 2}, key=len, reverse=True)


def load_stop_from_rules(con) -> int:
    """从图里读一次停用词。调用方在 ingest 等入口调一次即可。"""
    try:
        from . import rules
        words = rules.element_stop_words(con)
    except Exception:
        words = set()
    set_extra_stop(words)
    return len(_EXTRA_MULTI)


def _split_by_stop(run: str) -> list[str]:
    s = run
    for w in _EXTRA_MULTI + _STOP_MULTI:      # 规则里的词优先（更长、更针对）
        if w and w in s:
            s = s.replace(w, "|")
    buf: list[str] = []
    out: list[str] = []
    for ch in s:
        if ch == "|" or ch in _STOP1:
            if buf:
                out.append("".join(buf))
                buf = []
        else:
            buf.append(ch)
    if buf:
        out.append("".join(buf))
    return [p for p in out if len(p) >= 2]


def _pick_kind(surface: str) -> str:
    if surface in AFFECT:
        return "affect"
    if surface in PRED:
        return "action"
    if surface.endswith(("处", "里", "边", "上", "下", "中", "外", "间", "旁")):
        return "place"
    return "entity"


def rules_elements(text: str) -> list[dict]:
    """确定性抽取：同一段字，永远同一批元素。"""
    bag: dict[str, dict] = {}

    def add(surface: str, kind: str, score: float, role: str = "has") -> None:
        if not surface:
            return
        item = bag.get(surface)
        if item:
            item["repeat"] += 1
            item["score"] = max(item["score"], score)
            return
        bag[surface] = {"surface": surface, "kind": kind, "role": role,
                        "repeat": 1, "score": score}

    for idx, clause in enumerate(clauses(text)):
        base = 2.0 if idx == 0 else 1.0
        for q in quoted(clause):
            add(q, "quote", base * 1.3)
        for word, _aff in AFFECT.items():
            if word in clause:
                add(word, "affect", base * 1.2)
        for run in cjk_runs(clause):
            for piece in _split_by_stop(run):
                for size in (8, 6, 4):
                    if len(piece) >= size:
                        piece = piece[:size]
                        break
                add(piece, _pick_kind(piece), base)

    ranked = sorted(bag.values(), key=lambda x: (-x["score"], -len(x["surface"])))
    out = ranked[:8]
    if out:
        out[0]["role"] = "lead"
    return out


_SYS = (
    "你把一段第一人称的自传体记忆拆成『元素』。元素是这张记忆图里的地址，"
    "必须是可指认的东西，不是形容词。只输出 JSON，不要解释。"
)

_USER = """记忆：{text}

请拆出 3 到 8 个元素，规则：
0. 逐句来：把原文每个小句（按逗号、句号切开）里最主要的那**一个**元素挑出来，
   按出现顺序排列，一个小句最多一个，总共不超过 6 个——数量与顺序都由原文决定，不由你决定；
1. surface 必须是原文里**逐字出现**的片段，2 到 8 字，不许改写、不许概括、不许拼接；
2. 要覆盖：人（person）、地点（place）、物件（object）、概念（concept）、
   感受（affect）、动作（action）、感官细节（sense）、原话（quote）；
   动作类最多 1 个，原话引用最多 1 条（只取开头不超过 12 字）；
3. 抽象形容词不要——除非它本身就是一个情绪（那时 kind 用 affect）；
4. role 只有两种：lead（这条记忆的主角元素，最多一个）、has；
5. 同一元素出现多次时填 repeat 次数，不要重复列两遍；
6. 只输出：{{"elements":[{{"surface":"","kind":"","role":"has","repeat":1}}]}}

kind 只能是 person/place/object/concept/affect/action/sense/quote 之一。"""


def llm_elements(text: str) -> list[dict]:
    raw = llm.chat(
        [{"role": "system", "content": _SYS}, {"role": "user", "content": _USER.format(text=text[:1200])}],
        temperature=0.0, max_tokens=700,
    )
    data = json.loads(raw)
    items = data.get("elements") or []
    out: list[dict] = []
    seen: set[str] = set()
    for it in items:
        surface = str(it.get("surface", "")).strip()
        if not surface or surface in seen or surface in _DROP:
            continue
        if not _CJK.search(surface) and not quoted(surface):
            continue
        kind = str(it.get("kind", "entity")).strip()
        if surface in AFFECT:          # 情绪以词表为准，避免两次跑出两种类目
            kind = "affect"
        elif kind == "quote" and surface not in text:
            kind = "quote"
        else:
            kind = _pick_kind(surface)   # 类目由规则定，不由模型定——不然排序会跟着漂
        if kind not in KINDS:
            kind = "entity"
        seen.add(surface)
        out.append({
            "surface": surface,
            "kind": kind,
            "role": "lead" if it.get("role") == "lead" else "has",
            "repeat": max(1, min(int(it.get("repeat") or 1), 5)),
            "score": 2.0 if it.get("role") == "lead" else 1.0,
        })

    # 宽进严出：模型只提候选，选哪几个由确定性规则定。
    # 两次结果要能对上，靠的是「同一批候选 -> 同一套规则 -> 同一批 5 个」。
    kept = []
    for x in out:
        s = x["surface"]
        if s in text:
            kept.append(x)
            continue
        cut = s[:12]
        if cut in text and len(cut) >= 2:
            x["surface"] = cut
            kept.append(x)

    kind_w = {"person": 3.0, "place": 3.0, "object": 3.0, "concept": 2.8,
              "affect": 2.6, "sense": 1.8, "action": 1.2, "quote": 1.0}
    for x in kept:
        pos = text.find(x["surface"])
        x["pick"] = (kind_w.get(x["kind"], 2.0)
                     + 0.25 * min(len(x["surface"]), 6)
                     + (1.0 if pos < len(text) / 3 else 0.0))
    kept.sort(key=lambda x: (-x["pick"], text.find(x["surface"]), x["surface"]))
    return kept[:6]


def run(text: str, mode: str) -> tuple[str, list[dict]]:
    if mode == "rules":
        ver, items = RULE_VER, rules_elements(text)
    elif mode == "llm":
        ver, items = LLM_VER, llm_elements(text)
    else:
        raise SystemExit(f"未知模式：{mode}")
    # 图内停用表**对两条抽取器都生效**。
    # `rules` 那条在切分阶段就排除了，`llm` 那条不走 _split_by_stop，
    # 所以必须在出口统一再滤一遍——否则同一个入口（turns.ingest 优先用 llm）
    # 会让规则形同虚设（实测踩到：「够用」在 rules 口径下被排除，在 llm 口径下漏了）。
    if _EXTRA_MULTI:
        stop = set(_EXTRA_MULTI)
        items = [x for x in items if str(x.get("surface", "")) not in stop]
    return ver, items


def link_element(con, mem: int, surface: str, kind: str, rel: str = "has") -> int:
    """把一个元素接到记忆上。

    代词特殊对待：不新建节点，接到**锚点**——
    「我」就是中心（她本人），「他／你／用户」是同一个「他」。
    这样因果链上的人一直在，而不会因为指代词写法不同碎成一地。
    """
    from . import db

    target = PRONOUN_ANCHOR.get(surface)
    if target == "center":
        row = con.execute("SELECT id FROM nodes WHERE kind='center' LIMIT 1").fetchone()
        if row:
            el = int(row["id"])
            db.edge(con, mem, el, rel)
            return el
        return 0
    if target:
        el = db.node(con, "entity", target, uniq=f"entity|{target}")
        if el != mem:
            db.edge(con, mem, el, rel)
        return el
    el = db.node(con, kind, surface, uniq=f"{kind}|{surface}")
    if el != mem:
        db.edge(con, mem, el, rel)
    return el


def abstract_turn(text: str) -> str:
    """先把这一轮抽象成一句「谁做了什么、结果如何」，再据此抽元素。

    原文照留；抽象只用来当索引——因果在抽象里最清楚，在细节里最容易断。
    """
    prompt = (
        "把下面这一轮对话抽象成一句中文，讲清因果：谁做了什么、对方怎么回应、结果是什么。"
        "不超过 60 字，不要评价，不要复述原话。\n\n" + text[:1500]
    )
    try:
        out = llm.chat([{"role": "user", "content": prompt}],
                       temperature=0.0, max_tokens=120, json_mode=False)
        line = " ".join(str(out or "").split())
        return line[:120]
    except Exception:
        return ""
