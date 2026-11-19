# 晨曦光伏 · 知识增强分流 MVP

面向「晴天发电偏低」的**只读分流与排查辅助**最小可用版本：FastAPI 单进程 + SQLite 裸 SQL + 自建 Agent Harness + RAG + Wiki 知识图谱 + 多租户分层权限。

> 原创合成案例（energy-solar-01~04），不代表真实授权、业务执行或客户验收；不预设根因、不承诺发电增益或收益。

## 1. 四块能力

| 模块 | 实现 | 关键约束 |
|---|---|---|
| FastAPI | `app/main.py` + `app/routers.py`，统一 `{ok,data,error}` 信封 | 错误码集中在 `errors.py`，信封不泄漏堆栈 |
| Harness | `app/services/harness.py`：工具循环、步数预算、工具超时、轨迹落库 | 未配置模型时自动降级为"检索摘录"，仍给引用 |
| 知识库 | RAG（`rag.py`，SQLite FTS5 + 可选向量）+ Wiki 图谱（`wiki.py`）+ LLM（`llm.py`） | 中文二元组分词；引用只认真实 chunk_id |
| 权限 | `security.py`：API Key + 角色能力表 + 租户隔离 | 默认拒绝；跨租户读/写均 403；提权路径被掐断 |

## 2. 目录

```
solar_mvp/
├── app/
│   ├── main.py            # 应用装配、中间件、异常处理
│   ├── routers.py         # 全部 HTTP 路由
│   ├── config.py          # config.yaml 加载与校验
│   ├── errors.py          # 错误码与 AppError
│   ├── response.py        # 统一信封
│   ├── db.py              # sqlite3 连接、建表、审计
│   ├── security.py        # RBAC 能力表、租户隔离
│   ├── schemas.py         # Pydantic v2 入参
│   ├── logging_conf.py    # JSON 行日志
│   ├── services/          # llm / rag / wiki / harness / triage
│   ├── sql/001_init.sql   # 建表脚本（可重复执行）
│   └── static/wiki.html   # 单文件图谱视图
├── scripts/
│   ├── bootstrap.py       # 建库 + 合成样例数据
│   └── verify.py          # 一键自检（9 项）
├── tests/test_api.py      # pytest：12 个用例
├── config.yaml            # 本地配置（演示默认关闭模型）
├── config.example.yaml    # 配置模板（环境变量注入）
└── run.py                 # 启动入口
```

## 3. 快速开始

```powershell
cd solar_mvp
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

python scripts\bootstrap.py        # 建表 + 灌合成样例
python run.py                      # http://127.0.0.1:8010
```

自检：

```powershell
python scripts\verify.py           # 预期最后一行：ALL CHECKS PASSED (9/9)
python -m pytest tests -q          # 预期：12 passed
```

接入真实模型（可选）：把 `config.example.yaml` 里的 `${SOLAR_MODEL_*}` 配好环境变量后复制为 `config.yaml`；`embedding_model` 留空则只走 FTS。

## 4. 演示账号（仅合成环境）

| API Key | 角色 | 层级 | 能做什么 |
|---|---|---|---|
| `sk_platform_demo` | platform_admin | SaaS | 跨租户、建租户、建管理员 |
| `sk_admin_demo` | tenant_admin | L3 | 本租户管人、管知识、看审计 |
| `sk_asset_demo` | asset_manager | L3 | 确认分流建议、看汇总 |
| `sk_safety_demo` | safety_officer | L3 | 安全卡点、看审计 |
| `sk_clean_demo` | cleaning_dispatcher | L2 | 写清洗知识、确认清洗类 |
| `sk_engineer_demo` | data_engineer | L2 | 管数据与知识、建图谱 |
| `sk_inspector_demo` | inspector | L2 | 提交现场知识 |
| `sk_finance_demo` | finance_manager | L1 | 只读汇总 |
| `sk_viewer_demo` | viewer | L1 | 只读 + 问答 |
| `sk_other_demo` | viewer | L1 | 对照租户，用于隔离测试 |

请求头：`X-API-Key: sk_viewer_demo`。

## 5. 接口

| 方法 | 路径 | 权限 |
|---|---|---|
| GET | `/health` | 匿名 |
| GET | `/auth/me` | 任意已登记角色 |
| POST | `/knowledge/docs` | knowledge:write |
| GET | `/knowledge/docs` | knowledge:read（可带 tenant，仅平台管理员） |
| GET | `/knowledge/search?q=` | knowledge:read |
| POST | `/wiki/nodes`、`/wiki/edges` | wiki:write |
| GET | `/wiki/nodes`、`/wiki/graph`、`/wiki/nodes/{id}/neighbors` | wiki:read |
| GET | `/wiki/view?api_key=` | wiki:read（单文件 HTML） |
| POST | `/agent/ask` | agent:ask |
| POST | `/triage/advise` | triage:advise |
| GET/POST/PATCH | `/admin/users` | admin:users |
| POST | `/admin/tenants` | admin:tenants |
| GET | `/audit` | audit:read |

示例：

```powershell
$h = @{ "X-API-Key" = "sk_viewer_demo" }
Invoke-RestMethod http://127.0.0.1:8010/knowledge/search?q=积灰怎么排查 -Headers $h
Invoke-RestMethod http://127.0.0.1:8010/agent/ask -Method Post -Headers $h `
  -ContentType "application/json" -Body '{"question":"积灰怎么排查"}'
```

浏览器打开图谱：`http://127.0.0.1:8010/wiki/view?api_key=sk_viewer_demo`

## 6. 实现要点

- **中文检索**：`unicode61` 会把连续汉字当一个 token，故入库与查询都做"逐字加空格"，查询再拆成二元组用 OR 交给 bm25。
- **证据门槛**：`/triage/advise` 的置信度只由"规则强度 × 证据"决定，图谱里没有对应原因节点时就返回 `confidence=null`、`evidence_status=insufficient`。
- **降级路径**：模型未配置走检索摘录；模型超时/返回不合规直接返回错误码并转人工，不返回半成品答案；回答命中"预计增发/可节省"等词直接拦截。
- **审计**：入库、建图、分流、问答、建人、改角色全部落 `audit`，含 `before/after` 与 `request_id`。
- **合成隔离**：`synthetic_only=true` 时非 synthetic 文档写入返回 409。

## 7. 已知边界

- 分流建议不落库、不确认、不导出工单；双卡点确认与工单草稿属于下一阶段。
- 向量检索为 Python 暴力余弦，规模上限由 `rag.max_scan` 控制，适合单站万级 chunk。
- 认证只有 API Key，无登录态与刷新；生产需接 IdP 或网关。
- 图谱视图为演示级放射布局，无拖拽与编辑。
