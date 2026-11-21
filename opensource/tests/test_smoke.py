"""冒烟测试：不依赖 pytest，直接 `python tests/test_smoke.py`。

测三件事：
1. 包能导入、版本对；
2. init → ingest → recall → status 整条链通；
3. 「只增不改」这条纪律没被破坏（ingest 两次，第二次不该重复长记忆）。
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run(*args: str) -> tuple[int, str]:
    got = subprocess.run([sys.executable, "-m", "huanzao.cli", *args],
                         capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT))
    return got.returncode, (got.stdout + got.stderr).strip()


def main() -> int:
    fails: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        if cond:
            print("  PASS %s" % name)
        else:
            print("  FAIL %s  %s" % (name, detail))
            fails.append(name)

    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / "t.db")

        print("① 导入")
        import huanzao
        check("版本 0.1.0", huanzao.__version__ == "0.1.0", huanzao.__version__)

        print("② init")
        code, out = run("init", "--db", db)
        check("init 退出码 0", code == 0, out[:120])

        print("③ ingest")
        text = "父亲教我看星星。他说，看得见的那颗不一定是最近的。"
        code, out = run("ingest", "--db", db, "--text", text)
        check("ingest 退出码 0", code == 0, out[:120])
        check("写进 1 条记忆", "写进 1 条记忆" in out, out[:120])

        print("④ recall")
        code, out = run("recall", "--db", db, "--text", "谁教我看星星")
        check("recall 退出码 0", code == 0, out[:120])
        check("召回非空", "没有想起什么" not in out, out[:160])

        print("⑤ status")
        code, out = run("status", "--db", db)
        check("status 退出码 0", code == 0, out[:120])
        check("记忆计数出现", "记忆" in out, out[:120])

        print("⑥ 只增不改：同样的文本再喂一次")
        run("ingest", "--db", db, "--text", text)
        code, out2 = run("status", "--db", db)
        check("记忆数没翻倍", out2 == out, "第一次 %r / 第二次 %r" % (out[:60], out2[:60]))

        print("⑦ 空库不崩")
        db2 = str(Path(tmp) / "empty.db")
        run("init", "--db", db2)
        code, out = run("recall", "--db", db2, "--text", "什么都想不起来的问题")
        check("空库召回也不报错", code == 0, out[:120])

    print()
    if fails:
        print("失败 %d 项：%s" % (len(fails), "、".join(fails)))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
