-- MVP 建表脚本：可重复执行（全部 IF NOT EXISTS）
-- 设计口径：单库多租户，所有业务表带 tenant_id；知识库与图谱只存合成数据。

CREATE TABLE IF NOT EXISTS tenants (
  tenant_id   TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  plan        TEXT NOT NULL DEFAULT 'trial',
  status      TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','suspended')),
  created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
  user_id      TEXT PRIMARY KEY,
  tenant_id    TEXT NOT NULL REFERENCES tenants(tenant_id),
  name         TEXT NOT NULL,
  role         TEXT NOT NULL,
  tier         TEXT NOT NULL DEFAULT 'L1',
  api_key_hash TEXT NOT NULL UNIQUE,
  status       TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled')),
  created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_users_tenant ON users(tenant_id);

CREATE TABLE IF NOT EXISTS knowledge_docs (
  doc_id     TEXT PRIMARY KEY,
  tenant_id  TEXT NOT NULL REFERENCES tenants(tenant_id),
  title      TEXT NOT NULL,
  doc_type   TEXT NOT NULL,
  source     TEXT NOT NULL,
  version    TEXT NOT NULL DEFAULT 'v1',
  synthetic  INTEGER NOT NULL DEFAULT 1 CHECK (synthetic IN (0,1)),
  status     TEXT NOT NULL DEFAULT 'active',
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_docs_tenant ON knowledge_docs(tenant_id, created_at);

CREATE TABLE IF NOT EXISTS knowledge_chunks (
  chunk_id   TEXT PRIMARY KEY,
  doc_id     TEXT NOT NULL REFERENCES knowledge_docs(doc_id) ON DELETE CASCADE,
  tenant_id  TEXT NOT NULL,
  seq        INTEGER NOT NULL,
  content    TEXT NOT NULL,
  embedding  TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON knowledge_chunks(doc_id);
CREATE INDEX IF NOT EXISTS idx_chunks_tenant ON knowledge_chunks(tenant_id);

-- 全文索引：body/title 存"逐字加空格"后的文本，解决中文单字成词问题
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
  chunk_id UNINDEXED,
  tenant_id UNINDEXED,
  title,
  body,
  tokenize = 'unicode61'
);

CREATE TABLE IF NOT EXISTS wiki_nodes (
  node_id    TEXT PRIMARY KEY,
  tenant_id  TEXT NOT NULL REFERENCES tenants(tenant_id),
  kind       TEXT NOT NULL,
  label      TEXT NOT NULL,
  summary    TEXT NOT NULL DEFAULT '',
  props      TEXT NOT NULL DEFAULT '{}',
  synthetic  INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  UNIQUE (tenant_id, kind, label)
);

CREATE TABLE IF NOT EXISTS wiki_edges (
  edge_id     TEXT PRIMARY KEY,
  tenant_id   TEXT NOT NULL REFERENCES tenants(tenant_id),
  src_id      TEXT NOT NULL REFERENCES wiki_nodes(node_id) ON DELETE CASCADE,
  dst_id      TEXT NOT NULL REFERENCES wiki_nodes(node_id) ON DELETE CASCADE,
  relation    TEXT NOT NULL,
  weight      REAL NOT NULL DEFAULT 1.0,
  evidence_id TEXT,
  created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_edges_src ON wiki_edges(tenant_id, src_id);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON wiki_edges(tenant_id, dst_id);

CREATE TABLE IF NOT EXISTS agent_runs (
  run_id      TEXT PRIMARY KEY,
  tenant_id   TEXT NOT NULL,
  user_id     TEXT NOT NULL,
  question    TEXT NOT NULL,
  status      TEXT NOT NULL,
  answer      TEXT,
  citations   TEXT,
  trace       TEXT,
  model       TEXT,
  degraded    INTEGER NOT NULL DEFAULT 0,
  latency_ms  INTEGER,
  created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_tenant ON agent_runs(tenant_id, created_at);

CREATE TABLE IF NOT EXISTS audit (
  audit_id    TEXT PRIMARY KEY,
  tenant_id   TEXT,
  actor_id    TEXT,
  actor_role  TEXT,
  action      TEXT NOT NULL,
  object_type TEXT NOT NULL,
  object_id   TEXT,
  before_json TEXT,
  after_json  TEXT,
  request_id  TEXT,
  created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_time ON audit(created_at);
CREATE INDEX IF NOT EXISTS idx_audit_tenant ON audit(tenant_id, created_at);
