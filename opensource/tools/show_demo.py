"""开源包演示：状态 → 记忆 → 召回 → 提醒块 → 头段。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from huanzao import db, prompt, retrieve  # noqa: E402
from huanzao.cli import pick_memories  # noqa: E402

BASE = Path(__file__).parent.parent
con = db.init(str(BASE / "out" / "demo.db"))
con.row_factory = __import__("sqlite3").Row

print("=" * 66)
print("① 状态")
print("=" * 66)
c = db.counts(con)
for k in ("mem", "entity", "affect", "edges"):
    if k in c:
        print("  %-8s %s" % (k, c[k]))

print()
print("=" * 66)
print("② 八条记忆（原文 → 第一人称）")
print("=" * 66)
for r in con.execute("SELECT mem_node, fp_ver, raw, first_person FROM mem_src ORDER BY mem_node"):
    print("  #%s  [%s]" % (r["mem_node"], r["fp_ver"]))
    print("    原文：%s" % str(r["raw"]).replace("\n", " ")[:60])
    print("    记忆：%s" % str(r["first_person"]).replace("\n", " ")[:70])
    print()

print("=" * 66)
print("③ 召回演示")
print("=" * 66)


def mem_text(mid):
    r = con.execute("SELECT first_person, raw FROM mem_src WHERE mem_node=?",
                    (mid,)).fetchone()
    if not r:
        return "(缺)"
    return str(r["first_person"] or r["raw"]).replace("\n", " ")[:56]


for q in ("星星", "伞", "书店"):
    hits, picked = pick_memories(con, q, limit=3)
    words = [h["body"] if hasattr(h, "keys") else h for h in hits]
    print("  问「%s」→ 命中元素 %s" % (q, words[:6] or "(无，走原文兜底)"))
    for m in picked[:3]:
        mid = m.get("mem")
        print("      #%-3s %s" % (mid, mem_text(mid)))
    print()

print("=" * 66)
print("④ 提醒块（引擎 render_tail 的真实输出）")
print("=" * 66)
try:
    hits, picked = pick_memories(con, "星星", limit=3)
    block = prompt.render_tail(con, "星星", picked)
    print(block[:600])
except Exception as e:
    print("  (render_tail 失败：%s: %s)" % (type(e).__name__, str(e)[:80]))

print()
print("=" * 66)
print("⑤ 头段")
print("=" * 66)
h = prompt.build_head(con)
print(h[:600])
print()
print("  ... 共 %d 字，指纹 %s" % (len(h), prompt.digest(h)))
print("  预计 token：%s" % prompt.estimate_tokens(h))
