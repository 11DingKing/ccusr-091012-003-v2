# 监管物资保管服务

该项目为监管仓、证物室和受控物资保管点提供服务端 API，覆盖人员授权、物资分类、批次登记、收发记录、审批、预警、审计日志与统计报表。数据保存在 SQLite，所有测试和接口验收均可在单个 Linux 应用容器内离线完成。

## 案件成员关系与资源范围授权

跨部门联合调查期间，保管记录按**案件**隔离，普通成员只能访问“分配给自己的案件 + 授权时间段”内的记录。列表、详情、导出、仪表盘/日报、操作日志查询统一走 `apps.cases.authorization.ScopeAuthorizer`，不存在可绕过的独立入口。

- **版本化成员关系（SCD2）**：`CaseMembership` 每行是一个半开时间窗 `[valid_from, valid_to)`。授予/调整新开版本、旧版本保留；人员退出案件只关闭开放版本——**新访问立即停止**，但版本与历史操作日志永不删除，可随时审计。
- **资源归属**：货物、入库、出库记录挂 `case`；预警经货物、审批经出库记录间接归属同一案件。未挂案件的历史数据仅管理员可见。
- **一人多案**：可见范围为各案件授权窗口的**并集**，互相看不到其他案件记录。
- **合并/拆分**：资源在事务中迁移并记录 `CaseLineage`；成员关系**不沿谱系继承**，必须在目标案件重新授权，从机制上杜绝跨案件泄漏。
- **访问判定留痕**：每次允许/拒绝写一条 `AccessDecision`，拒绝时保存命中的成员关系版本与 JSON 快照。管理员可在 `/api/access-decisions/` 按用户、案件、资源类型、判定结果检索，回答“某次拒绝依据的是哪一版成员关系”。
- **管理员**（superadmin/admin）不受案件范围限制，但其判定同样记录在案。

### 主要接口

| 接口 | 说明 |
| --- | --- |
| `GET/POST /api/cases/` | 案件列表（成员只见授权案件）/ 管理员建案 |
| `GET /api/cases/<id>/memberships/` | 查看成员关系全部历史版本 |
| `POST /api/cases/<id>/memberships/` | 管理员授予/调整成员（`user_id`,`valid_from`,`valid_to`，新开版本） |
| `DELETE /api/cases/<id>/memberships/` | 人员退出案件（`user_id`，立即关闭生效版本） |
| `POST /api/cases/merge/`、`POST /api/cases/split/` | 案件合并 / 拆分（仅管理员） |
| `GET /api/cases/lineage/` | 合并/拆分谱系 |
| `GET /api/access-decisions/` | 访问判定记录（仅管理员，支持 `decision/user/case/resource_type` 过滤） |
| `GET /api/goods/`、`/api/stock-in/`、`/api/stock-out/`、`/api/warnings/`、`/api/approvals/` 及 `<id>/` | 保管记录列表/详情，统一案件范围 |
| `POST /api/stock-in/`、`POST /api/stock-out/` | 登记记录须携带 `case` 且当前持有该案件授权 |
| `GET /api/export/?type=goods|stock_in|stock_out` | 导出，范围与列表完全一致 |
| `GET /api/auth/logs/` | 操作日志查询，统一范围；成员另可自查本人历史操作 |


## 运行环境

- Python 3.11
- Django REST Framework
- SQLite

## 安装与初始化

```bash
python -m pip install -r backend/requirements.txt
cd backend
python manage.py migrate --run-syncdb
```

## 测试

```bash
cd backend
pytest -q
```

## 编译检查

```bash
python -m compileall -q backend
```

## API 验收

```bash
cd backend
python manage.py migrate --run-syncdb
python manage.py shell -c "from rest_framework.test import APIClient; from apps.authentication.models import User; u=User.objects.create_user('smoke','safe-pass',role='admin'); c=APIClient(); r=c.post('/api/auth/login/',{'username':'smoke','password':'safe-pass'},format='json'); print(r.status_code, bool(r.json()['data']['token']))"
```

## 容器

```bash
docker build -t custody-service .
docker run --rm custody-service
```
