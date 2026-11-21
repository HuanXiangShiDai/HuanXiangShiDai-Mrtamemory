"""最底层：存真的。

`wire_log.jsonl` 是插件在请求链最后一站记下的原始报文——
**发出去的 messages（含工具声明、工具调用与结果）和收回来的 chunk，一字不改。**

这里把它们收进图：一条报文 = 一条记忆，`raw` 存原始 JSON，`first_person` 存人读得懂的
渲染（他说的、她说的、调了哪个工具、回了什么），元素从渲染里抽。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from . import db, extract, turns
from .canon import canon
from .lexicon import AFFECT

MAX_ELEMENTS = 14


def _text_of(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    out = []
    for b in content or []:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text":
            out.append(str(b.get("text") or ""))
        elif b.get("type") == "tool-call":
            out.append(f"[用了 {b.get('name')}]")
        elif b.get("type") == "tool-result":
            out.append(f"[结果 {str(b.get('content'))[:80]}]")
    return "\n".join(x for x in out if x)


def render(record: dict) -> str:
    req = record.get("request") or {}
    lines = [f"（发出去：{req.get('provider')}/{req.get('model')}，工具 {len(req.get('tools') or [])} 个）"]
    for m in req.get("messages") or []:
        role = m.get("role")
        t = _text_of(m).strip()
        if not t:
            continue
        who = "他" if role == "user" else ("我" if role == "assistant" else role)
        lines.append(f"{who}：{t}")
    reply = "".join(str(c.get("text") or "") for c in record.get("response") or []
                    if isinstance(c, dict) and c.get("type") == "text-delta")
    if reply:
        lines.append(f"（收回来）：{reply}")
    return "\n".join(lines)


def ingest(con: sqlite3.Connection, path: str, limit: int = 50, verbose: bool = True) -> dict:
    from . import extract as _ex
    _ex.load_stop_from_rules(con)          # 抽取口径与 turns.ingest 保持一致
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"没有报文文件：{p}")
    rows = [ln for ln in p.read_text(encoding="utf-8", errors="ignore").splitlines() if ln.strip()]
    rows = rows[-limit:]
    added = 0
    for ln in rows:
        try:
            rec = json.loads(ln)
        except json.JSONDecodeError:
            continue
        # 只要完整报文：同时有 request 和 response。半截行（写坏了、被截断）一律跳过
        if not isinstance(rec, dict) or not isinstance(rec.get("request"), dict) \
                or not isinstance(rec.get("response"), list):
            continue
        digest = str(rec.get("digest") or hashlib.md5(ln.encode()).hexdigest()[:16])
        uniq = f"mem|wire|{digest}"
        if db.get_node(con, uniq):
            continue
        text = render(rec)
        if not text.strip():
            continue
        mem = db.node(con, "mem", "报文：" + text.split("\n")[1][:18] if "\n" in text else "报文",
                      uniq=uniq)
        con.execute(
            "INSERT OR REPLACE INTO mem_src(mem_node, src_id, raw, first_person, fp_ver, "
            "when_iso, order_key, provenance) VALUES(?,?,?,?,?,?,?,?)",
            (mem, digest, ln, text, "verbatim", "live", turns._next_key(con), "wire"))
        try:
            _, items = extract.run(text[:4000], "rules")
        except Exception:
            items = []
        for it in items[:MAX_ELEMENTS]:
            surface = canon(str(it.get("surface", "")))
            if not surface:
                continue
            kind = "affect" if (str(it.get("kind")) == "affect" or surface in AFFECT) else "entity"
            el = db.node(con, kind, surface, uniq=f"{kind}|{surface}")
            if el != mem:
                db.edge(con, mem, el, "has")
        con.commit()
        added += 1
    if verbose:
        print(f"报文 {len(rows)} 条 → 新收进图 {added} 条（原文一字未改）")
    return {"scanned": len(rows), "added": added}


def ingest_messages(con, path: str, limit: int = 30, verbose: bool = True) -> dict:
    """把一次真实请求里的对话（他一句、我一句）逐轮收进图。

    用报文里的 messages 当来源：**未经改写的原文**，连工具声明都还在。
    """
    from . import db, turns as turns_mod
    from . import extract as _ex
    _ex.load_stop_from_rules(con)          # 同上：图内的停用表要生效
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"没有报文文件：{p}")
    rows = [json.loads(ln) for ln in p.read_text(encoding="utf-8", errors="ignore").splitlines() if ln.strip()]
    rows = [r for r in rows if isinstance(r.get("request"), dict)]
    if not rows:
        raise SystemExit("报文里没有完整的 request")
    rec = rows[-1]
    JUNK = ("<system-reminder>", "Time sampled while preparing", "【此刻浮上来的】",
            "Browser time zone")
    msgs = []
    for m in rec["request"].get("messages") or []:
        if m.get("role") not in ("user", "assistant"):
            continue
        t = _text_of(m).strip()
        if any(t.startswith(j) or j in t[:40] for j in JUNK):
            continue
        msgs.append(m)
    pairs = []
    pending = None
    her_parts = []
    for m in msgs:
        text = _text_of(m).strip()
        if not text or text.startswith("<system-reminder>"):
            continue
        if m["role"] == "user":
            if pending is not None:
                pairs.append((pending, "\n".join(x for x in her_parts if x)))
            pending = text
            her_parts = []
        elif pending is not None:
            if not text.startswith("[用了"):
                her_parts.append(text)
    if pending is not None:
        pairs.append((pending, "\n".join(x for x in her_parts if x)))
    pairs = pairs[-limit:]

    added = 0
    for user_text, her_text in pairs:
        raw = f"他：{user_text}\n我：{her_text}".strip()
        uniq = "mem|turnwire|" + str(abs(hash(raw)) % 10**10)
        if db.get_node(con, uniq):
            continue
        mem = db.node(con, "mem", "对话：" + (user_text or her_text)[:20], uniq=uniq)
        con.execute(
            "INSERT OR REPLACE INTO mem_src(mem_node, src_id, raw, first_person, fp_ver, "
            "when_iso, order_key, provenance) VALUES(?,?,?,?,?,?,?,?)",
            (mem, uniq.split("|", 2)[-1], raw, raw, "verbatim", "live",
             turns_mod._next_key(con), "turn-wire"))
        # 这条记忆发生在哪天：用这条记录自己的时间，不用正文里提到的日子
        from datetime import datetime as _dt
        _day = str(rec.get("at") or "")[:10].replace("-", "") or _dt.now().strftime("%Y%m%d")
        _key = turns_mod._next_key(con)
        _parts = _key.split(":")
        if len(_parts) >= 4:
            _parts[3] = _day
            _key = ":".join(_parts)
        con.execute("UPDATE mem_src SET order_key=? WHERE mem_node=?", (_key, mem))

        try:
            from . import times as times_mod
            times_mod.assign_own(con, mem)
            times_mod.link(con, mem, raw)
        except Exception:
            pass

        # 接进中心：对话碎片离中心太远就检索不到，等于没记。
        # 用 dialogue 边直连中心，靠检索侧的「碎片默认不浮现」来防止打扰。
        center = con.execute("SELECT id FROM nodes WHERE kind='center' LIMIT 1").fetchone()
        if center:
            db.edge(con, int(center["id"]), mem, "dialogue")
        # 先抽象成一句因果，再从抽象里抽元素；原文照留不动
        brief = extract.abstract_turn(raw)
        if brief:
            con.execute("UPDATE mem_src SET abstract=? WHERE mem_node=?", (brief, mem))
        try:
            _, items = extract.run((brief + "\n\n" + raw) if brief else raw, "llm")
        except Exception:
            items = []
        for it in items[:MAX_ELEMENTS]:
            surface = canon(str(it.get("surface", "")))
            if not surface:
                continue
            kind = "affect" if (str(it.get("kind")) == "affect" or surface in AFFECT) else "entity"
            extract.link_element(con, mem, surface, kind, "has")
        con.commit()
        added += 1
    if verbose:
        print(f"从报文里收了 {added} 轮对话（原文照留）")
    return {"added": added, "scanned": len(pairs)}
