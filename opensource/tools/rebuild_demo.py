"""重建开源 demo 库：8 条示例语料走一遍 ingest（含第一人称改写或回退）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from huanzao import db  # noqa: E402
from huanzao.cli import ingest_text  # noqa: E402
from huanzao import llm  # noqa: E402

BASE = Path(__file__).parent.parent
# db.init 才会建 schema；db.connect 只是连上去，空库直接连会没有 mem_src 表
con = db.init(str(BASE / "out" / "demo.db"))

print("LLM 可用（能改写第一人称）：", llm.has_key())
print()

for f in sorted((BASE / "examples" / "story").glob("*.md")):
    text = f.read_text(encoding="utf-8").strip()
    if not text or f.stem.startswith("00"):
        continue          # 00-说明.md 是说明书，不是语料
    got = ingest_text(con, text, f.stem)
    print("· %s → mem #%s，元素 %s，边 %s"
          % (f.stem, got.get("mem"), got.get("elements"), got.get("edges")))

con.commit()

print()
print("=== 落库结果（first_person vs raw）===")
con.row_factory = __import__("sqlite3").Row
for r in con.execute(
        "SELECT mem_node, fp_ver, substr(raw,1,34) raw, substr(first_person,1,46) fp "
        "FROM mem_src ORDER BY mem_node"):
    print("  #%-3s [%-10s] %s" % (r["mem_node"], r["fp_ver"] or "(空)", r["raw"].replace("\n", " ")))
    print("        → %s" % (r["fp"] or "(空)").replace("\n", " "))
