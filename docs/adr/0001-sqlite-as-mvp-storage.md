# SQLite 作为 MVP 存储（而非 PostgreSQL）

---
status: accepted
---

原始需求是 trace 落 PostgreSQL，但目标是 Docker 一键部署整个项目：引入 PG 意味着 compose 多一个数据库容器、健康检查依赖与密钥配置。MVP 的数据形态（单写者、低频追加、两张小表）完全在 SQLite 能力范围内，且零外部依赖。代价是放弃并发写扩展与丰富查询能力；若后续出现多实例部署或聚合分析需求，需要迁移到独立数据库服务，届时 repository 层是唯一需要改动的位置。

## Considered Options

- PostgreSQL：更可扩展，但部署复杂度与 MVP 目标冲突
- SQLite（选定）：`docker compose up` 即包含全部状态，数据挂一个 volume 即持久化
