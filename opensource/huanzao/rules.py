"""规则也是图里的节点。

每条规则 = 一个 `kind='rule'` 的点，正文写清它管什么、当前取值多少。
代码**从图里读**，不再把阈值写死在常量里——改规则改的是数据。

参考的做法（不是抄，是对照）：

- **HippoRAG**：把知识图谱当索引，用 Personalized PageRank 做多跳联想，
  对应我们「从中心扩散、沿边传播」的那一半；
- **MemGPT**：虚拟上下文管理，窗口当内存、库当磁盘，分页调入调出，
  对应我们「常驻头段 + 按需展开 + 压缩前存档」的那一半；
- **ACT-R / 扩散激活**：激活沿语义网络扩散、按距离与强度衰减，
  对应我们的漏电、阈值、扇出。

我们没做的：PPR 的全图迭代（我们只在候选周围做有限跳），以及真正的分页调度。
"""
from __future__ import annotations

import re
import sqlite3

from . import db, rule_graph

# ── 操作词：指向「要做的事」，不指向「她记得的事」 ──
OPS = "rule|操作词"
DEFAULT_OPS = [
    "截图", "屏幕", "看一下", "看下", "打开", "关闭", "运行", "执行", "安装", "卸载",
    "代码", "文件", "目录", "路径", "命令", "终端", "日志", "报错", "修复", "改一下",
    "继续", "先", "然后", "现在", "接下来",
]

# ── 话题词：谈的是系统／机制，不是往事 ──
TOPIC = "rule|话题词"
DEFAULT_TOPIC = [
    "记忆", "记忆库", "元图", "元素", "义项", "规则", "上下文", "系统", "工具", "插件",
    "模型", "向量", "权重", "节点", "边", "框架", "文档", "代码", "配置", "接口",
    "调用", "注入", "缓存", "提示词",
]

# ── 中心校验：候选必须离中心够近，且命中元素要拼得齐 ──
CENTER = "rule|中心校验"
DEFAULT_CENTER = {"max_hops": 4, "min_hit_cov": 0.5}

# ── 义项裁定：两条记忆像到这个份上，才去问她 ──
JUDGE = "rule|义项裁定"
DEFAULT_JUDGE = {"margin": 0.02}

# ── 规矩：头段最后那三条，原文照搬（以前写死在 prompt.py 里） ──
MANNERS = "rule|规矩"
DEFAULT_MANNERS = [
    "只说我真记得的事。没经历过的，就直接说不记得、不知道，不许编。",
    "记不清的细节，说不记得细节，不要拿想象补上。",
    "宁可少说一句，也不要编一句。",
]

# ── 提醒措辞：每轮那句提示怎么写 ──
REMIND = "rule|提醒措辞"
DEFAULT_REMIND = {
    "title": "【此刻浮上来的】",
    "footer": "（这些是你心里浮上来的，说不说由你；不合适就放过。）",
    "per_mem": 135,      # 每条记忆最多留多少字
}

# ── 常驻取舍：头段里设定取几条、意象取几条 ──
HEAD_PICK = "rule|常驻取舍"
DEFAULT_HEAD = {"innate": 14, "backbone": 16}

# ── 时间坐标：两套，不混用 ──
# 以前这里只是一句说明（谁都不读）。现在它是真的旋钮：
# now_precision 决定「他提一句钟点时，时刻节点记到多细」——秒 / 分 / 日。
TIME = "rule|时间坐标"
DEFAULT_TIME = {"past": "叙事坐标", "now": "日历", "now_precision": "秒"}

# ── 上下文预算 ──
BUDGET = "rule|上下文预算"
DEFAULT_BUDGET = {"remind_chars": 220, "tail_tokens": 255}

# ── 不入记忆：谈的是工程，不是她的往事 ──
# 这条规则在旧图里躺着、没人读；新图重建时干脆丢了。现在它是真的闸门：
# `is_engineering()` 给所有入口用，命中两个以上才算工程（单字词如「边」不参与，
# 不然「旁边」「边界」全要误伤）。
NOT_MEMORY = "rule|不入记忆"
DEFAULT_NOT_MEMORY = [
    "代码", "编译", "接口", "函数", "变量", "报错", "异常", "日志", "配置", "路径",
    "部署", "插件", "脚本", "命令", "终端", "仓库", "提交", "分支", "环境变量",
    "pipeline", "sqlite", "json", "token", "prompt", "上下文窗口", "缓存", "权重",
    "向量", "节点", "规则节点", "元图", "义项", "元素抽取", "检索", "嵌入", "重启",
    "进程", "端口", "依赖", "版本", "测试", "跑一遍", "调参", "阈值", "截屏", "桥接",
]
ENGINEERING_HITS = 2      # 命中几个词以上，这句话就算在谈工程

# 元素选取的停用表：这些片段**不参与切分**，也就不会被抽成元素。
# 它本来是 `lexicon.STOP` 里的代码常量——于是"抽取规则"不在图里，
# 改不动、审不了。搬进来之后，`extract` 每次切分前读它，
# 而它自己可以被 `rule|触发·加停用` 之类的触发规则改（自指）。
ELEMENT_STOP = "rule|元素选取"
# 开源包**不预设**停用词——具体错在哪儿是每个项目自己的事。
# 空列表时 `ensure_all` 不建这条规则，`element_stop_words` 返回空集，
# 抽取行为与没有这条机制时一致。
DEFAULT_ELEMENT_STOP: list[str] = []


def _list_body(title: str, words: list[str]) -> str:
    return f"{title}：" + "、".join(words)


def _kv_body(title: str, kv: dict) -> str:
    return f"{title}：" + "；".join(f"{k}={v}" for k, v in kv.items())


def _put(con: sqlite3.Connection, uniq: str, body: str, overwrite: bool = False) -> None:
    """补一条规则。**默认只补不改**——

    踩过的坑：`_read` 发现某个规则点不在，就调 ensure_all 把 9 条全写成默认值，
    于是人工调过的 `常驻取舍=24` 被悄悄刷回 14。规则是数据，代码不该顺手改数据。
    真要改规则正文，用 _set()，并且自己知道在改什么。
    """
    node = db.node(con, "rule", body, uniq=uniq)
    if overwrite:
        con.execute("UPDATE nodes SET body=? WHERE id=?", (body, node))
    con.commit()


def _set(con: sqlite3.Connection, uniq: str, body: str) -> None:
    """明确地改一条规则的正文（人工调参走这里）。"""
    _put(con, uniq, body, overwrite=True)


def _read(con: sqlite3.Connection, uniq: str) -> str:
    # 先问规则层「生效的是哪一版」（有 current 边读版本，没有就读锚点）。
    # 这条接通后，set_rule/apply_op 写的新版本才真的生效；读不到再走老路补默认值。
    body = rule_graph.current(con, uniq)
    if body:
        return body
    row = db.get_node(con, uniq)
    if not row:
        ensure_all(con)
        row = db.get_node(con, uniq)
    return str(row["body"]) if row else ""


def ensure_all(con: sqlite3.Connection) -> None:
    _put(con, OPS, _list_body("操作词（命中也不当种子，它们指向要做的事）", DEFAULT_OPS))
    _put(con, TOPIC, _list_body("话题词（谈系统时不当种子，避免翻旧账）", DEFAULT_TOPIC))
    _put(con, CENTER, _kv_body("中心校验（候选离中心几跳、命中要拼到多齐）", DEFAULT_CENTER))
    _put(con, JUDGE, _kv_body("义项裁定（差到多小才让她自己判）", DEFAULT_JUDGE))
    _put(con, BUDGET, _kv_body("上下文预算（提醒块字数、尾段 token）", DEFAULT_BUDGET))
    _put(con, TIME, _kv_body("时间坐标（两套不混用）", DEFAULT_TIME))
    _put(con, MANNERS, "规矩：" + "／".join(DEFAULT_MANNERS))
    _put(con, REMIND, _kv_body("提醒措辞", DEFAULT_REMIND))
    _put(con, HEAD_PICK, _kv_body("常驻取舍", DEFAULT_HEAD))
    _put(con, NOT_MEMORY,
         _list_body("不入记忆（这些是工程，不是她的往事）", DEFAULT_NOT_MEMORY))
    if DEFAULT_ELEMENT_STOP:              # 空列表不建规则（见上方说明）
        _put(con, ELEMENT_STOP,
             _list_body("元素选取（这些片段不参与切分，也就不会被抽成元素）",
                        DEFAULT_ELEMENT_STOP))


def _words(con: sqlite3.Connection, uniq: str, default: list[str]) -> set[str]:
    body = _read(con, uniq)
    _, _, tail = body.partition("：")
    words = {w.strip() for w in tail.replace("\n", "、").split("、") if w.strip()}
    return words or set(default)


def _kv(con: sqlite3.Connection, uniq: str, default: dict) -> dict:
    """解析「键=值」，值一直读到下一个「键=」为止——
    值里本身带分号（措辞就带）也不会被切断。"""
    body = _read(con, uniq)
    _, _, tail = body.partition("：")
    out = dict(default)
    keys = list(default.keys())
    for i, k in enumerate(keys):
        m = re.search(rf"(?:^|[；\n])\s*{re.escape(k)}\s*=", tail)
        if not m:
            continue
        start = m.end()
        nxt = len(tail)
        for k2 in keys[i + 1:]:
            m2 = re.search(rf"[；\n]\s*{re.escape(k2)}\s*=", tail[start:])
            if m2:
                nxt = min(nxt, start + m2.start())
        raw = tail[start:nxt].strip().strip("；")
        if not raw:
            continue
        try:
            out[k] = float(raw) if "." in raw else int(raw)
        except ValueError:
            out[k] = raw
    return out


def op_words(con: sqlite3.Connection) -> set[str]:
    return _words(con, OPS, DEFAULT_OPS)


def topic_words(con: sqlite3.Connection) -> set[str]:
    return _words(con, TOPIC, DEFAULT_TOPIC)


def center_rule(con: sqlite3.Connection) -> dict:
    return _kv(con, CENTER, DEFAULT_CENTER)


def judge_rule(con: sqlite3.Connection) -> dict:
    return _kv(con, JUDGE, DEFAULT_JUDGE)


def time_rule(con: sqlite3.Connection) -> dict:
    return _kv(con, TIME, DEFAULT_TIME)


def budget_rule(con: sqlite3.Connection) -> dict:
    return _kv(con, BUDGET, DEFAULT_BUDGET)


def add_words(con: sqlite3.Connection, uniq: str, *words: str) -> int:
    base = op_words(con) if uniq == OPS else topic_words(con)
    current = list(base)
    for w in words:
        if w and w not in current:
            current.append(w)
    title = "操作词（命中也不当种子，它们指向要做的事）" if uniq == OPS else \
            "话题词（谈系统时不当种子，避免翻旧账）"
    _put(con, uniq, _list_body(title, current))
    return len(current)


def manners(con: sqlite3.Connection) -> list[str]:
    body = _read(con, MANNERS)
    _, _, tail = body.partition("：")
    got = [x.strip() for x in tail.replace("\n", "／").split("／") if x.strip()]
    return got or list(DEFAULT_MANNERS)


def remind_style(con: sqlite3.Connection) -> dict:
    return _kv(con, REMIND, DEFAULT_REMIND)


def head_pick(con: sqlite3.Connection) -> dict:
    return _kv(con, HEAD_PICK, DEFAULT_HEAD)


def not_memory_words(con: sqlite3.Connection) -> set[str]:
    return _words(con, NOT_MEMORY, DEFAULT_NOT_MEMORY)


def element_stop_words(con: sqlite3.Connection) -> set[str]:
    """抽取时不该被当成元素的片段。

    读 `rule|元素选取`（词表型）。图里没有就用默认值——
    这条规则的读者是 `extract`，而它**可以被触发规则改写**，
    所以"元素怎么被选出来"这件事第一次进了图的管辖。
    """
    return _words(con, ELEMENT_STOP, DEFAULT_ELEMENT_STOP)


def is_engineering(con: sqlite3.Connection, text: str) -> bool:
    """这句话是在谈工程，不是在说她记得的事。

    单字词不参与（「边」会误伤「旁边」「边界」）；命中两个以上才算。
    所有「新记忆进图」的入口都该先问它一句。
    """
    t = text or ""
    hits = [w for w in not_memory_words(con) if len(w) >= 2 and w in t]
    return len(hits) >= ENGINEERING_HITS
