"""跑一遍完整流程：吃语料 → 看长出了什么 → 召回 → 生成提醒块。

    python examples/run_demo.py

只用标准库，通过 CLI 子进程调用，不依赖包内部的函数签名——
这样这个脚本本身就是「用这个库」的示范。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # opensource/
STORY = ROOT / "examples" / "story"
DB = ROOT / "out" / "demo.db"

QUERIES = [
    "她愿意等谁",       # 命中「愿意等」
    "星星和距离",       # 命中「星星」，能召回两条
    "旧书店",           # 命中「旧书店」
    "伞修好了吗",       # 会空：单字「伞」不成为元素。抽取是有损的，这条是有意留的例子
]


def run(*args: str) -> str:
    argv = [sys.executable, "-m", "huanzao.cli", *args]
    got = subprocess.run(argv, capture_output=True, text=True,
                         encoding="utf-8", cwd=str(ROOT))
    if got.returncode != 0:
        print("失败：%s\n%s" % (" ".join(argv[1:]), got.stderr.strip()[:400]))
    return got.stdout.strip()


def main() -> None:
    DB.parent.mkdir(parents=True, exist_ok=True)
    if DB.exists():
        DB.unlink()

    print("① init")
    print("   " + run("init", "--db", str(DB)).replace("\n", "\n   "))

    print("\n② ingest（八段语料）")
    for f in sorted(STORY.glob("0[1-9].md")):        # 00-说明.md 是文档，不是语料
        text = f.read_text(encoding="utf-8").strip()
        run("ingest", "--db", str(DB), "--text", text)
        print("   %s  %d 字" % (f.name, len(text)))

    print("\n③ status")
    print("   " + run("status", "--db", str(DB)).replace("\n", "\n   "))

    print("\n④ recall")
    for q in QUERIES:
        out = run("recall", "--db", str(DB), "--text", q)
        print("   「%s」" % q)
        for line in out.splitlines()[:3]:
            print("      " + line.strip()[:70])

    print("\n⑤ remind（可以注入上下文的那一块）")
    block = run("remind", "--db", str(DB), "--text", QUERIES[0])
    print("   " + block.replace("\n", "\n   ")[:600])

    print("\n数据库：%s（%.1f KB）" % (DB.relative_to(ROOT), DB.stat().st_size / 1024))


if __name__ == "__main__":
    main()
