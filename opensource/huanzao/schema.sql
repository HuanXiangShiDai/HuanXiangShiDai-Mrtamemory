-- 元图：只有点和边。没有「带字段的记录」。
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS nodes (
    id    INTEGER PRIMARY KEY,
    kind  TEXT NOT NULL,          -- entity|pred|mem|motif|modal|center|combo|rule|sense|affect
    body  TEXT NOT NULL DEFAULT '',
    uniq  TEXT NOT NULL UNIQUE    -- 外部身份：kind|规范化值
);
CREATE INDEX IF NOT EXISTS ix_nodes_kind ON nodes(kind);

CREATE TABLE IF NOT EXISTS edges (
    id  INTEGER PRIMARY KEY,
    src INTEGER NOT NULL,
    dst INTEGER NOT NULL,
    rel TEXT NOT NULL DEFAULT '',   -- 谓词（RDF 三元里的 p）
    UNIQUE(src, dst, rel)
);
CREATE INDEX IF NOT EXISTS ix_edges_src ON edges(src);
CREATE INDEX IF NOT EXISTS ix_edges_dst ON edges(dst);
CREATE INDEX IF NOT EXISTS ix_edges_rel ON edges(rel);

-- 正文外挂：图的真相只有点和边，正文不进图。
CREATE TABLE IF NOT EXISTS mem_src (
    mem_node   INTEGER PRIMARY KEY,
    src_id     TEXT,
    raw        TEXT,
    first_person TEXT,
    fp_ver     TEXT,
    when_iso   TEXT,
    order_key  TEXT,
    provenance TEXT
);

-- 抽取切片（宽进）：一提出来就全留，判定只决定入不入图。
CREATE TABLE IF NOT EXISTS extract_run (
    id        INTEGER PRIMARY KEY,
    mem_node  INTEGER NOT NULL,
    mode      TEXT NOT NULL,        -- rules | llm
    rule_ver  TEXT NOT NULL,
    payload   TEXT NOT NULL,        -- JSON
    created   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS ix_run_mem ON extract_run(mem_node, mode, rule_ver);

-- 派生层：可随时重算，丢了不影响真相。
CREATE TABLE IF NOT EXISTS derived_df (
    node_id INTEGER PRIMARY KEY,
    df      INTEGER NOT NULL,
    total   INTEGER NOT NULL,
    idf     REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT
);

-- 会话头：一次定死的那段字节。缓存认的就是它的指纹。
CREATE TABLE IF NOT EXISTS session (
    id        INTEGER PRIMARY KEY,
    head      TEXT NOT NULL,
    head_hash TEXT NOT NULL,
    created   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
