# 监管物资保管服务

该项目为监管仓、证物室和受控物资保管点提供服务端 API，覆盖人员授权、物资分类、批次登记、收发记录、审批、预警、审计日志与统计报表。数据保存在 SQLite，所有测试和接口验收均可在单个 Linux 应用容器内离线完成。

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

## 案件化授权

保管记录（货物）归属于**案件（Case）**，所有访问面——列表、详情、导出、操作日志查询——统一经 `apps.cases.authz` 判定：

- 管理员（`admin`/`superadmin`）不受案件范围限制，用于全量审计；
- 普通用户仅在存在**当前版本**成员关系、案件 `active` 且请求时刻位于授权时间窗内时，才能访问该案件名下的货物及其出入库/预警/审批记录；一人多案件时取范围并集；
- 成员关系为**仅追加的双时间版本链**（`CaseMembership`）：加入、调整时间窗、退出都产生新版本；退出后下一次请求立即失效（不依赖 JWT 过期），历史版本永久保留供审计；
- **合并**只迁移资源归属、不迁移成员关系，原成员须显式加入目标案件；**拆分**仅迁出被选货物；两个操作均单事务完成，无中间泄露窗口；
- 每次允许/拒绝写入 `AccessDecisionLog`，拒绝记录固化所依据的成员关系版本外键与快照（`membership_version_number`/`membership_snapshot`/`case_version`），管理员可在 `/api/access-decisions/?decision=deny` 追溯。

主要接口：

| 路径 | 说明 |
| --- | --- |
| `GET/POST /api/cases/` | 案件列表（成员只见在办且自己所属）/ 管理员建案 |
| `GET /api/cases/<id>/memberships/` | 成员关系版本链 |
| `POST /api/cases/<id>/members/join/` `…/adjust/` `…/leave/` | 加入 / 调整时间窗 / 退出（追加版本） |
| `POST /api/cases/<id>/merge/` `…/split/` | 案件合并 / 拆分 |
| `GET /api/cases/<id>/events/` | 案件生命周期事件 |
| `GET /api/access-decisions/` | 授权判定审计（仅管理员） |
| `GET /api/goods/` `…/stock-in/` `…/stock-out/` `…/warnings/` `…/approvals/` | 保管记录，均按案件范围收窄，支持 `?case=`（越权案件返回 403） |
| `GET /api/export/` `GET /api/dashboard/` `GET /api/daily-report/` | 导出/统计同样按案件范围 |
| `GET /api/auth/logs/` | 操作日志，按案件范围可审计（含退出成员的历史操作） |

## 容器

```bash
docker build -t custody-service .
docker run --rm custody-service
```
