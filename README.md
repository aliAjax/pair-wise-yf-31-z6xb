# 航班中断恢复系统

独立的 Python 标准库项目，用 SQLite 保存机场、飞机、机组、航线许可、航班、中断事件和恢复方案。系统会校验维护间隔、执勤时限、机场宵禁、航线许可、资源重叠，并计算取消、延误、受影响旅客和错失衔接成本。

## 运行

```bash
python3 app.py --db airline_recovery.db
```

默认监听 `127.0.0.1:8202`，首页为 `/`，健康检查为 `/health`。

身份头：`X-User-Id`、`X-Role`。角色包括 `viewer`、`scheduler`、`ops_manager`、`auditor`。

## 主要接口

- `POST /api/airports`、`/api/aircraft`、`/api/crew`、`/api/permits`：基础资源与约束。
- `POST /api/flights`、`POST /api/disruptions`：创建航班和中断。
- `POST /api/recovery-plans`：一次提交方案及航班调整。
- `POST /api/plans/{id}/assignments`：用 `expected_revision` 临时改派。
- `POST /api/plans/{id}/validate`、`/lock`（等价别名 `/publish`）：校验并原子发布方案，仅运行经理可发布。
- `POST /api/plans/{id}/replan`：把与新生效中断重叠的锁定方案冻结为 `superseded` 历史版本，并从当前航班状态生成唯一的可编辑草稿；未受影响调整原样继承，受影响调整带 `affected_by_json` 标记。重复提交（含两个调度员并发）只返回同一份草稿，不改写航班。
- `GET /api/disruptions/{id}/compare`：比较恢复方案成本（历史版本不参与推荐）。
- `POST /api/flights/{id}/cancel`、`/recover`：取消和人工恢复。
- `GET /api/state`、`GET /api/plans/{id}`：查询状态和影响。

## 方案重排链

1. 锁定方案发布后，若新登记飞机故障、机组超时或机场关闭，其航班在资源或时段上与中断重叠，`validate` 会返回 `aircraft_fault_active`、`crew_timeout_active` 或 `airport_closed`，方案不再显示有效。
2. 调度员或运行经理调用 `replan`：旧锁定方案冻结为 `superseded`（`frozen_at` 落库，缓存指标/评分作废），系统按**当前航班状态**逐航班生成草稿：未受影响调整继承自旧方案（含已累计延误），受影响调整打上 `affected_by_json` 标记，必须重新改派。
3. 部分唯一索引保证每个父方案最多一份草稿；重复 `replan` 返回 `200` 与既有草稿（`deduplicated: true`），并发提交也只留一份，航班不被再次改写。
4. 发布（`lock`/`publish`）是单个原子事务：未通过校验、存在未处理受影响调整或与其他锁定方案资源冲突时，整批回滚、航班不变，草稿保留可整批重试。发布成功以整值写回补偿旧方案改动，延误在种子基线之上累计而非叠加回原始时刻。
5. 历史版本不可再发布或再次重排；其指标实时重算。旧数据库通过加列与加索引的增量迁移自动升级，旧锁定方案可正常发起重排。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 主要局限

时区和机场本地时刻没有引入完整时区数据库；模型使用简化航线许可与宵禁规则。身份头、SQLite 和单进程 HTTP 服务适合原型演示，正式运行需要外部身份系统、共享数据库和更强的跨实例锁。
