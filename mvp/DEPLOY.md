# MVP 线上部署手册

本目录是可直接上线的最小服务：FastAPI 单进程 + SQLite，无外部依赖（模型可留空，走检索降级）。
镜像已内置 `Dockerfile`，云平台只需要设一个环境变量就能带演示数据启动。

---

## 0. 部署前已做好的改动（相对原仓库）

| 改动 | 文件 | 作用 |
|---|---|---|
| 监听地址与端口读环境变量 | `run.py` | 云平台注入 `PORT=8080`、`HOST=0.0.0.0` 即可，不再固定 `127.0.0.1:8010` |
| 演示数据可开关 | `run.py` | `SOLAR_DEMO_SEED=1` 时先跑 `scripts/bootstrap.py` 灌合成数据（可重复执行，不覆盖已有） |
| 容器镜像 | `Dockerfile` | `python:3.12-slim` + 依赖层缓存 + `HEALTHCHECK /health` |
| 一键蓝图 | `render.yaml` | Render 选 Blueprint 直接出服务 |

本地验证过的启动方式（与线上一致）：

```bash
HOST=0.0.0.0 PORT=8080 SOLAR_DEMO_SEED=1 python run.py
```

---

## 1. 路线 A：Render（免费，推荐）

1. 打开 https://dashboard.render.com → 用 GitHub 账号登录并授权本仓库
2. **New → Blueprint**，选中仓库 `Emily-0717/pv-asset-inspection`，Render 会自动读根目录 `render.yaml`
   （若 Blueprint 不识别，改走 **New → Web Service**：Language 选 `Docker`，Root Directory `mvp`，Dockerfile `Dockerfile`）
3. Plan 选 **Free**，Region 选 **Singapore**（国内访问比 Oregon 快）
4. 确认环境变量：`SOLAR_DEMO_SEED=1`（Blueprint 已写）
5. Create → 等 2–3 分钟构建

拿到地址后验证：

```
https://<你的服务名>.onrender.com/health                    → {"ok":true,...}
https://<你的服务名>.onrender.com/docs                       → Swagger 交互文档
https://<你的服务名>.onrender.com/wiki/view?api_key=sk_viewer_demo   → 图谱页面
```

免费档的两个已知限制（演示足够，正式交付要换）：

- 15 分钟无请求会休眠，冷启动约 30–60 秒；
- 磁盘是临时的，重启后数据库重建并重新灌种子数据（演示数据本来就是合成+可重建）。

## 2. 路线 B：Zeabur / Railway / Sealos（国内访问快）

同样是 Docker 项目，配置项一致：

| 项 | 值 |
|---|---|
| 构建方式 | Dockerfile |
| 构建上下文 / Root | `mvp` |
| 端口 | `8080`（平台若注入 `PORT`，代码会自动跟随） |
| 环境变量 | `SOLAR_DEMO_SEED=1` |

Zeabur 用 GitHub 登录 → 新建 Project → Add Service → Git，选本仓库、把 Root 设为 `mvp` 即可。

## 3. 路线 C：自己的服务器（有公网 IP 时）

```bash
git clone https://github.com/Emily-0717/pv-asset-inspection.git
cd pv-asset-inspection/mvp

docker build -t solar-mvp .
docker run -d --name solar-mvp --restart=always \
  -p 8080:8080 \
  -e SOLAR_DEMO_SEED=1 \
  -v solar-mvp-data:/app/data \
  solar-mvp
```

`-v` 那行是把 SQLite 落到具名卷，重启不丢数据；公网访问再在前面挂 Nginx + HTTPS。

---

## 4. 演示调用示例

```bash
BASE=https://<你的地址>
KEY=sk_asset_demo            # 演示用：资产经理·顾明岚

curl $BASE/health
curl -H "X-API-Key: $KEY" $BASE/auth/me

# 分流建议（只给候选，不落结论）
curl -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"unit_code":"S-01","actual_kwh":5200,"theoretical_kwh":6300,
       "irradiance":780,"alarms":[],"days_since_cleaning":22,
       "shading_logged":false,"data_quality_ok":true}' \
  $BASE/triage/advise

# Agent 问答（未配模型时返回知识库原文摘录 + 引用）
curl -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"question":"晴天发电偏低但无告警，怎么排查积灰？","top_k":3}' \
  $BASE/agent/ask
```

---

## 5. 上线前必须知道的三件事

1. **演示 Key 等于公开的管理员密码。** `scripts/bootstrap.py` 里的 `sk_platform_demo` / `sk_admin_demo` 等写在仓库中，公开演示站点谁都能用。对外展示时要么换成自建 Key（`POST /admin/users` 创建后返回一次），要么在平台前面加一层 Basic Auth。
2. **模型 Key 只能走环境变量。** `config.yaml` 里 `model.api_key` 保持空，改成 `${SOLAR_MODEL_API_KEY}` 占位，在平台的环境变量里填真实 Key；代码会自动展开 `${VAR}`。
3. **SQLite 只适合单进程单副本。** 不要开多 worker、不要多副本共享同一库文件，WAL 也扛不住并发写；要扩副本必须先换 Postgres。

---

## 6. 建议的环境变量清单

| 变量 | 必填 | 说明 |
|---|---|---|
| `PORT` | 平台自动 | 容器监听端口，默认 8080 |
| `HOST` | 否 | 默认已写 `0.0.0.0`（镜像内） |
| `SOLAR_DEMO_SEED` | 演示必填 | `1` = 首次启动灌合成数据 |
| `SOLAR_MVP_CONFIG` | 否 | 镜像内已写 `/app/config.yaml` |
| `SOLAR_MODEL_API_KEY` | 接模型时填 | 配合 `config.yaml` 中的 `${SOLAR_MODEL_API_KEY}` |
