"""义项：同一个词形，不同的意思——用向量分开。

试过两种向量：

- **元素共现向量**（图里现成的元素当维度）：在这个密度下几乎全正交，
  「记忆库」那四条记忆两两余弦都是 0.0 —— 分开不了。
- **字二元组 + IDF 向量**（记忆正文当维度）：同一组样本上，
  问系统时「骨架定稿」胜出（0.051 对 0.043），问乱码时「乱码那条」胜出（0.081 对 0.035）。

所以用后者。IDF 是关键：越稀的字组越值钱，「乱码」这种词才压得住「记忆」。
"""
from __future__ import annotations

import math
import re
import sqlite3

MERGE_COS = 0.12        # 两条记忆的正文向量够近，算同一个义项


def bigrams(text: str) -> dict[str, float]:
    clean = re.sub(r"\s+", "", text or "")
    out: dict[str, float] = {}
    for i in range(len(clean) - 1):
        g = clean[i:i + 2]
        out[g] = out.get(g, 0.0) + 1.0
    return out


def _cos(a: dict[str, float], b: dict[str, float]) -> float:
    keys = set(a) & set(b)
    if not keys:
        return 0.0
    dot = sum(a[k] * b[k] for k in keys)
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    return dot / (na * nb) if na and nb else 0.0


def _texts(con: sqlite3.Connection) -> dict[int, str]:
    rows = con.execute("SELECT mem_node, COALESCE(first_person, raw) t FROM mem_src").fetchall()
    return {int(r["mem_node"]): (r["t"] or "") for r in rows}


def idf(con: sqlite3.Connection, rebuild: bool = False) -> dict[str, float]:
    """字组的 IDF，存表里；图一变就重算。"""
    con.executescript(
        "CREATE TABLE IF NOT EXISTS bigram_idf (gram TEXT PRIMARY KEY, df INTEGER, total INTEGER);")
    if not rebuild:
        rows = con.execute("SELECT gram, df, total FROM bigram_idf").fetchall()
        if rows:
            total = max(int(rows[0]["total"]), 1)
            return {str(r["gram"]): math.log(total / (1 + int(r["df"]))) for r in rows}

    texts = _texts(con)
    total = len(texts) or 1
    df: dict[str, int] = {}
    for t in texts.values():
        for g in bigrams(t):
            df[g] = df.get(g, 0) + 1
    con.execute("DELETE FROM bigram_idf")
    con.executemany("INSERT INTO bigram_idf(gram, df, total) VALUES(?,?,?)",
                    [(g, n, total) for g, n in df.items()])
    con.commit()
    return {g: math.log(total / (1 + n)) for g, n in df.items()}


def vec(text: str, table: dict[str, float]) -> dict[str, float]:
    """字组向量。词频取对数（1+log tf）：同一个词在一条记忆里出现八遍，
    不该比出现两遍重四倍——不然短而反复的句子会把所有查询都吸过去。"""
    out = {}
    for g, tf in bigrams(text).items():
        w = 1.0 + math.log(tf) if tf > 1 else 1.0
        out[g] = w * table.get(g, 0.0)
    return out


def nearest(con: sqlite3.Connection, query: str, mems: list[int],
            table: dict[str, float] | None = None) -> list[tuple[int, float]]:
    """这些记忆里，哪一条跟这句话最像。"""
    table = table or idf(con)
    qv = vec(query, table)
    texts = _texts(con)
    scored = [(m, _cos(qv, vec(texts.get(m, ""), table))) for m in mems]
    scored.sort(key=lambda x: -x[1])
    return scored


def senses(con: sqlite3.Connection, surface: str, table: dict[str, float] | None = None) -> list[dict]:
    """同一个词形出现过的记忆，按正文向量聚成簇——一簇一个义项。"""
    table = table or idf(con)
    rows = con.execute(
        "SELECT DISTINCT e.src FROM edges e JOIN nodes n ON n.id=e.dst "
        "WHERE n.body=? AND n.kind IN ('entity','affect') AND e.rel IN ('has','has:lead')",
        (surface,)).fetchall()
    mems = [int(r["src"]) for r in rows]
    if not mems:
        return []
    texts = _texts(con)
    vecs = {m: vec(texts.get(m, ""), table) for m in mems}

    # 聚簇用「相对近」而不是绝对阈值：稀疏向量两两余弦普遍只有 0.0x，
    # 卡死一个绝对值会把所有记忆都拆成单条——那不是义项，那是没聚。
    sim = {(a, b): _cos(vecs[a], vecs[b]) for i, a in enumerate(mems) for b in mems[i + 1:]}

    def best_of(m: int) -> float:
        return max([v for (a, b), v in sim.items() if m in (a, b)] or [0.0])

    clusters: list[list[int]] = []
    for m in mems:
        cut = max(0.04, 0.65 * best_of(m))
        for cl in clusters:
            if any(sim.get((m, o), sim.get((o, m), 0.0)) >= cut for o in cl):
                cl.append(m)
                break
        else:
            clusters.append([m])

    out = []
    for i, cl in enumerate(clusters, 1):
        centroid: dict[str, float] = {}
        for m in cl:
            for k, v in vecs[m].items():
                centroid[k] = centroid.get(k, 0.0) + v / len(cl)
        out.append({"sense": i, "mems": cl, "vec": centroid})
    out.sort(key=lambda x: -len(x["mems"]))
    return out


def build(con: sqlite3.Connection, word: str | None = None, verbose: bool = False) -> int:
    """把义项写进表（派生数据）。"""
    import json
    # 旧版这张表带 vec 列，结构不同——直接重建，反正是派生数据
    cols = [r["name"] for r in con.execute("PRAGMA table_info(sense)").fetchall()]
    if cols and "vec" in cols:
        con.execute("DROP TABLE sense")
    con.executescript(
        "CREATE TABLE IF NOT EXISTS sense ("
        "  surface TEXT NOT NULL, sense_no INTEGER NOT NULL, mems TEXT NOT NULL,"
        "  size INTEGER NOT NULL, updated TEXT NOT NULL DEFAULT (datetime('now','localtime')),"
        "  PRIMARY KEY(surface, sense_no));")
    table = idf(con, rebuild=True)
    if word:
        targets = [word]
    else:
        targets = [r["body"] for r in con.execute(
            "SELECT n.body FROM nodes n JOIN edges e ON e.dst=n.id "
            "WHERE n.kind IN ('entity','affect') AND e.rel IN ('has','has:lead') "
            "GROUP BY n.id HAVING COUNT(DISTINCT e.src) >= 2")]
    total = 0
    for w in targets:
        cl = senses(con, w, table)
        con.execute("DELETE FROM sense WHERE surface=?", (w,))
        for i, c in enumerate(cl, 1):
            con.execute("INSERT INTO sense(surface, sense_no, mems, size) VALUES(?,?,?,?)",
                        (w, i, json.dumps(c["mems"]), len(c["mems"])))
        total += len(cl)
        if verbose and len(cl) > 1:
            print(f"  {w}: {len(cl)} 个义项")
    con.commit()
    return total


def assign(con: sqlite3.Connection, surface: str, mem: int,
           table: dict[str, float] | None = None, floor: float = 0.04) -> int:
    """写进去的那一刻就归簇：像哪一义，就进哪一义；都不像，就新开一个。"""
    import json
    table = table or idf(con)
    con.executescript(
        "CREATE TABLE IF NOT EXISTS sense ("
        "  surface TEXT NOT NULL, sense_no INTEGER NOT NULL, mems TEXT NOT NULL,"
        "  size INTEGER NOT NULL, updated TEXT NOT NULL DEFAULT (datetime('now','localtime')),"
        "  PRIMARY KEY(surface, sense_no));")
    texts = _texts(con)
    target = vec(texts.get(mem, ""), table)
    rows = con.execute("SELECT sense_no, mems, size FROM sense WHERE surface=? ORDER BY sense_no",
                       (surface,)).fetchall()

    best_no, best_sim = None, -1.0
    for r in rows:
        mems = json.loads(r["mems"])
        centroid: dict[str, float] = {}
        for m in mems:
            v = vec(texts.get(m, ""), table)
            for k, val in v.items():
                centroid[k] = centroid.get(k, 0.0) + val / len(mems)
        s = _cos(target, centroid)
        if s > best_sim:
            best_no, best_sim = int(r["sense_no"]), s

    if best_no is not None and best_sim >= floor:
        mems = json.loads(con.execute(
            "SELECT mems FROM sense WHERE surface=? AND sense_no=?", (surface, best_no)).fetchone()["mems"])
        if mem not in mems:
            mems.append(mem)
            con.execute("UPDATE sense SET mems=?, size=?, updated=datetime('now','localtime') "
                        "WHERE surface=? AND sense_no=?",
                        (json.dumps(mems), len(mems), surface, best_no))
            con.commit()
        return best_no

    new_no = (int(rows[-1]["sense_no"]) + 1) if rows else 1
    con.execute("INSERT INTO sense(surface, sense_no, mems, size) VALUES(?,?,?,1)",
                (surface, new_no, json.dumps([mem])))
    con.commit()
    return new_no


def merge_clusters(con: sqlite3.Connection, threshold: float = 0.35,
                   table: dict[str, float] | None = None, verbose: bool = False) -> int:
    """把说同一件事的两簇并成一簇：两簇质心的余弦够近就并。"""
    import json
    table = table or idf(con)
    texts = _texts(con)
    surfaces = [r["surface"] for r in con.execute(
        "SELECT surface FROM sense GROUP BY surface HAVING COUNT(*) > 1")]
    merged_total = 0
    for word in surfaces:
        rows = con.execute("SELECT sense_no, mems FROM sense WHERE surface=? ORDER BY sense_no",
                           (word,)).fetchall()
        clusters = [{"no": int(r["sense_no"]), "mems": json.loads(r["mems"])} for r in rows]
        for c in clusters:
            cent: dict[str, float] = {}
            for m in c["mems"]:
                v = vec(texts.get(m, ""), table)
                for k, val in v.items():
                    cent[k] = cent.get(k, 0.0) + val / len(c["mems"])
            c["cent"] = cent

        changed = True
        while changed:
            changed = False
            for i in range(len(clusters)):
                for j in range(i + 1, len(clusters)):
                    if _cos(clusters[i]["cent"], clusters[j]["cent"]) < threshold:
                        continue
                    clusters[i]["mems"] = list(dict.fromkeys(clusters[i]["mems"] + clusters[j]["mems"]))
                    for k, val in clusters[j]["cent"].items():
                        clusters[i]["cent"][k] = clusters[i]["cent"].get(k, 0.0) + val
                    clusters.pop(j)
                    merged_total += 1
                    changed = True
                    break
                if changed:
                    break

        con.execute("DELETE FROM sense WHERE surface=?", (word,))
        for i, c in enumerate(clusters, 1):
            con.execute("INSERT INTO sense(surface, sense_no, mems, size) VALUES(?,?,?,?)",
                        (word, i, json.dumps(c["mems"]), len(c["mems"])))
        if verbose and len(clusters) < len(rows):
            print(f"  {word}: {len(rows)} → {len(clusters)}")
    con.commit()
    return merged_total


def pick_mems(con: sqlite3.Connection, surface: str, text: str,
              table: dict[str, float] | None = None) -> list[int]:
    """这个词形在这句话里最像哪一义，返回那一簇的记忆。"""
    table = table or idf(con)
    cl = senses(con, surface, table)
    if not cl:
        return []
    qv = vec(text, table)
    best, score = None, -1.0
    for c in cl:
        s = _cos(qv, c["vec"])
        if s > score:
            best, score = c, s
    return list(best["mems"]) if best else []


ASK_TMPL = (
    "她心里浮起两件事，问它们哪一件更贴近这句话的意思。\n"
    "这句话：{q}\n"
    "A：{a}\n"
    "B：{b}\n"
    "只回一个字母：A 或 B；两件都不沾边就回 N。"
)


def decide(con: sqlite3.Connection, query: str, pair: list[int],
           timeout: int = 20) -> int | None:
    """向量分不清的时候，让她自己判——只两件，一句话，一个字母。

    这是唯一一处「每轮可能多花一次调用」的地方，所以只在差距极小时才叫它。
    """
    from . import llm
    texts = _texts(con)
    a, b = pair[0], pair[1]
    try:
        ans = llm.chat(
            [{"role": "user", "content": ASK_TMPL.format(
                q=query[:80], a=texts.get(a, "")[:70], b=texts.get(b, "")[:70])}],
            temperature=0.0, max_tokens=4, timeout=timeout, json_mode=False)
    except Exception:
        return None
    letter = str(ans or "").strip().upper()[:1]
    if letter == "A":
        return a
    if letter == "B":
        return b
    return None


def describe(con: sqlite3.Connection, surface: str, text: str = "") -> dict:
    table = idf(con)
    cl = senses(con, surface, table)
    texts = _texts(con)
    qv = vec(text, table) if text else {}
    out = []
    for c in cl:
        sim = round(_cos(qv, c["vec"]), 3) if qv else None
        out.append({"sense": c["sense"], "mems": len(c["mems"]), "score": sim,
                    "sample": texts.get(c["mems"][0], "")[:40]})
    return {"surface": surface, "senses": out}
