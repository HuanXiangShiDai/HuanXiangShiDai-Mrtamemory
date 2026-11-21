"""huanzao 命令行：init / ingest / recall / remind / status。

五条命令，全部调用包里已经复制过来的模块，不复制私有 CLI 的几十条命令。

    python -m huanzao.cli init   --db out/graph.db
    python -m huanzao.cli ingest --db out/graph.db --text "今天下雪了。"
    python -m huanzao.cli recall --db out/graph.db --text "雪"
    python -m huanzao.cli remind --db out/graph.db --text "雪"
    python -m huanzao.cli status --db out/graph.db
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

from . import __version__, db, extract, grow, normalize, prompt, retrieve, rules, timeline
from .canon import canon
from .config import CENTER_BODY
from .lexicon import AFFECT


def _open(path: str):
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"图还没有建：{p}（先跑 init）")
    return db.connect(p)


# ── ingest ──────────────────────────────────────────────────────────

def ingest_text(con, text: str, title: str = "") -> dict:
    """把一段话吃进图：建记忆点、存原文、抽元素、连边、重算 df 与骨干。

    同一段文字重复写入不会多长一个记忆点（按正文 sha1 判重），只更新原文。
    """
    text = normalize.norm(text)
    if not text:
        raise SystemExit("--text 不能为空")

    grow.ensure_center(con)
    rules.ensure_all(con)

    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
    uniq = f"mem|in-{digest}"
    existed = db.get_node(con, uniq) is not None
    mem = db.node(con, "mem", (title.strip() or text)[:24], uniq=uniq)

    now = datetime.now().astimezone()
    order_key = (f"{timeline.NOW}:000:0000000000:"
                 f"{now:%Y%m%d}:0000000000:{mem:06d}")

    # 第一人称改写：资料库里存的是第三方摘要（「用户说……她回答……」），
    # 那是**资料**，不是记忆。能放进记忆基层的写法是「我、某时、对谁、当时怎么想」。
    #
    # 这里必须做，而且必须**有回退**：
    # 早先这里把 first_person 写死成空字符串，于是开源版的记忆永远没有第一人称，
    # 召回出来的全是第三人称摘要——核心特性等于没接。
    # 改写要调 LLM；没有 key 时退回原文，也不能留空。
    fp, ver = text, "raw"
    try:
        from . import rewrite as _rw
        got_fp = _rw.rewrite(title, text, f"{now:%Y-%m-%d}")
        if got_fp.get("memory"):
            fp, ver = got_fp["memory"], f"cli-{got_fp.get('ver', 'fp')}"
    except Exception:
        pass          # 没 key / 网络不通 / 改写太短 —— 都退回原文

    con.execute(
        "INSERT INTO mem_src(mem_node, src_id, raw, first_person, fp_ver, when_iso, "
        "order_key, provenance) VALUES(?,?,?,?,?,?,?,?) "
        "ON CONFLICT(mem_node) DO UPDATE SET raw=excluded.raw, first_person=excluded.first_person, "
        "fp_ver=excluded.fp_ver, when_iso=excluded.when_iso, "
        "order_key=excluded.order_key, provenance=excluded.provenance",
        (mem, uniq, text, fp, ver, f"{now:%Y-%m-%d}", order_key, "cli-ingest"),
    )

    ver, items = extract.run(text, "rules")
    con.execute(
        "INSERT INTO extract_run(mem_node, mode, rule_ver, payload) VALUES(?,?,?,?)",
        (mem, "rules", ver, json.dumps(items, ensure_ascii=False)),
    )
    # 重灌同一段时，先把旧元素边删掉，避免越写越厚
    con.execute("DELETE FROM edges WHERE src=? AND rel IN ('has','has:lead')", (mem,))
    before_edges = int(con.execute("SELECT COUNT(*) FROM edges").fetchone()[0])
    made_nodes: set[int] = set()
    for it in items:
        surface = canon(str(it.get("surface", "")))
        if not surface:
            continue
        kind = "affect" if (str(it.get("kind")) == "affect" or surface in AFFECT) else "entity"
        rel = "has:lead" if it.get("role") == "lead" else "has"
        for _ in range(min(int(it.get("repeat") or 1), 3)):
            el = extract.link_element(con, mem, surface, kind, rel)
            if el:
                made_nodes.add(int(el))
    con.commit()
    made = int(con.execute("SELECT COUNT(*) FROM edges").fetchone()[0]) - before_edges

    grow.rebuild_df(con)
    grow.link_backbone(con, 24)
    return {"mem": mem, "created": not existed, "elements": len(made_nodes), "edges": made}


def cmd_ingest(args) -> None:
    con = _open(args.db)
    got = ingest_text(con, args.text, args.title)
    if got["created"]:
        print(f"写进 1 条记忆（mem #{got['mem']}）：{args.text.strip()[:40]}")
    else:
        print(f"这条已经记过（mem #{got['mem']}），原文已更新")
    print(f"元素 {got['elements']} 个，元素边 {got['edges']} 条")
    con.close()


# ── recall / remind 共用的取记忆 ─────────────────────────────────────

def pick_memories(con, text: str, limit: int = 3) -> tuple[list, list[dict]]:
    """先按字面命中的元素找，再用扩散 + 覆盖率兜底；返回 (命中元素, 记忆)。"""
    text = normalize.norm(text)
    if not text:
        raise SystemExit("--text 不能为空")
    hits = retrieve.hit_elements(con, text)
    ids = [int(r["id"]) for r in hits]
    picked: list[dict] = []
    if ids:
        from . import diffuse
        picked = [{"mem": m["mem"], "cover": m["hit_cov"], "raw": 0.0, "focus_id": 0}
                  for m in diffuse.pick(con, ids, top=max(limit, 3))]
        if not picked:
            picked = retrieve.memories_of(con, ids, limit=limit)
    # 元素一个都没命中时，按原文兜一次底——换个说法也不至于一条都翻不出来
    if not picked:
        like = f"%{text[:20]}%"
        for r in con.execute(
                "SELECT mem_node FROM mem_src WHERE raw LIKE ? ORDER BY mem_node DESC LIMIT ?",
                (like, limit)).fetchall():
            picked.append({"mem": int(r["mem_node"]), "cover": 0.3, "raw": 0.0, "focus_id": 0})
    return hits, picked[:limit]


def _one_result(con, p: dict) -> dict:
    m = retrieve.render_mem(con, p["mem"])
    return {
        "mem": int(p["mem"]),
        "title": m["title"],
        "text": m["text"] or m["title"],
        "when": m["when"],
        "when_text": retrieve.describe_when(con, p["mem"]),
        "view": retrieve.describe_view(con, p["mem"]),
        "score": round(float(p.get("cover", 0.0)), 3),
    }


def cmd_recall(args) -> None:
    con = _open(args.db)
    hits, picked = pick_memories(con, args.text, args.limit)
    results = [_one_result(con, p) for p in picked]
    con.close()

    if args.json:
        print(json.dumps({"query": args.text, "hits": [r["body"] for r in hits],
                          "results": results}, ensure_ascii=False))
        return
    if not results:
        print("没有想起什么。")
        return
    head = "、".join(r["body"] for r in hits[:6])
    print(f"召回 {len(results)} 条" + (f"（命中元素：{head}）" if head else "") + "：")
    for i, r in enumerate(results, 1):
        view = f"（{r['view']}的视角）" if r["view"] else ""
        print(f"{i}. 【{r['when_text']}】{view}{r['text']}")


# ── remind ──────────────────────────────────────────────────────────

def cmd_remind(args) -> None:
    con = _open(args.db)
    hits, picked = pick_memories(con, args.text, limit=3)
    block = ""
    if picked:
        block = prompt.render_tail(con, args.text, picked, per_mem_chars=args.limit)
    con.close()

    if args.json:
        print(json.dumps({"query": args.text, "hits": [r["body"] for r in hits],
                          "block": block}, ensure_ascii=False))
        return
    print(block if block else "（这一句没有想起什么）")


# ── status ──────────────────────────────────────────────────────────

def collect_status(con) -> dict:
    one = lambda sql: int(con.execute(sql).fetchone()[0])
    return {
        "memories": one("SELECT COUNT(*) FROM nodes WHERE kind='mem'"),
        "elements": one("SELECT COUNT(*) FROM nodes WHERE kind IN "
                        "('entity','affect','person','pred','sense','concept')"),
        "edges": one("SELECT COUNT(*) FROM edges"),
        "df": one("SELECT COUNT(*) FROM derived_df"),
    }


def cmd_status(args) -> None:
    con = _open(args.db)
    st = collect_status(con)
    con.close()
    if args.json:
        print(json.dumps(st, ensure_ascii=False))
        return
    print(f"记忆：{st['memories']}")
    print(f"元素：{st['elements']}")
    print(f"边：{st['edges']}")
    print(f"df：{st['df']}")


# ── init ────────────────────────────────────────────────────────────

def cmd_init(args) -> None:
    con = db.init(args.db)
    grow.ensure_center(con)
    rules.ensure_all(con)
    # 自指体系的三块规则：触发通道、夜间窗口与四个目标、思考结论的落法。
    # 它们和配置型规则一样是图里的节点，所以 init 就该把它们建好——
    # 少建任何一块，"开箱即用"就是假的（实测：只 ensure_all 时触发规则 0 条）。
    try:
        from . import trigger, night, conclusion
        trigger.ensure(con)
        night.ensure(con)
        conclusion.ensure_rule(con)
    except Exception as exc:                      # 缺模块不该让 init 失败
        print("警告：自指规则未全部建立（%s）" % exc)
    grow.rebuild_df(con)
    db.set_meta(con, "framework", f"huanzao {__version__}")
    con.commit()
    con.close()
    print(f"建好了：{args.db}（中心「{CENTER_BODY}」＋规则层已就位）")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="huanzao", description="元图生长引擎：把一段语料长成一张会想起话的图。")
    p.add_argument("--version", action="version", version=f"huanzao {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    q = sub.add_parser("init", help="建库 + 建表 + 中心/规则")
    q.add_argument("--db", required=True, help="图数据库路径（不存在则新建）")
    q.set_defaults(func=cmd_init)

    q = sub.add_parser("ingest", help="把一段话吃进图")
    q.add_argument("--db", required=True, help="图数据库路径")
    q.add_argument("--text", required=True, help="要记住的一段话")
    q.add_argument("--title", default="", help="记忆标题（默认取正文前 24 字）")
    q.set_defaults(func=cmd_ingest)

    q = sub.add_parser("recall", help="按一句话召回记忆")
    q.add_argument("--db", required=True, help="图数据库路径")
    q.add_argument("--text", required=True, help="要查的话")
    q.add_argument("--limit", type=int, default=3, help="最多几条（默认 3）")
    q.add_argument("--json", action="store_true", help="输出 JSON")
    q.set_defaults(func=cmd_recall)

    q = sub.add_parser("remind", help="生成提醒块（可注入上下文）")
    q.add_argument("--db", required=True, help="图数据库路径")
    q.add_argument("--text", required=True, help="用户这一句")
    q.add_argument("--limit", type=int, default=None,
                   help="每条记忆最多留多少字（默认由 rule|提醒措辞 的 per_mem 决定）")
    q.add_argument("--json", action="store_true", help="输出 JSON")
    q.set_defaults(func=cmd_remind)

    q = sub.add_parser("status", help="口径统计：记忆数/元素数/边数/df 行数")
    q.add_argument("--db", required=True, help="图数据库路径")
    q.add_argument("--json", action="store_true", help="输出 JSON")
    q.set_defaults(func=cmd_status)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
