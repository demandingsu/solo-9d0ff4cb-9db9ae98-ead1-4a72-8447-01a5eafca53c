# 线上展馆观众参观路径 API

从浏览器上报的 `open` / `close` 事件还原匿名观众的实际参观路径，避免把未关闭页面
计作完整参观；支持乱序补报、重传去重与数据同意撤回。

另提供**旧卡检索**：旧编目系统导出的品名是一串四位十六进制字码，按批次+卡号录入，
每批独立维护整张「字码 → Unicode 文本」对照表；凭批次+卡号可查原码与未映射位置，
按完整品名可从索引查到所有已完整转写的同名卡。

技术栈：Python 3.11 + FastAPI + SQLite（WAL 模式），Docker Compose 一键启动。

## 快速启动

```bash
docker compose up --build
# 打开交互文档：
#   Swagger UI  http://localhost:8000/docs
#   ReDoc       http://localhost:8000/redoc
# 健康检查     http://localhost:8000/health

# 装入旧卡检索的本地演示数据（可重复执行，会重置演示批次）：
docker compose exec api python -m app.demo_data
```

SQLite 数据文件存放在命名卷 `exhibition-data`（容器内 `/data/exhibition.db`），
容器重建不丢数据。

本地（无 Docker）运行：

```bash
pip install -r requirements.txt
EXHIBITION_DB=./exhibition.db uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/events` | 上报单个事件 |
| POST | `/events/batch` | 批量上报，单事务原子提交 |
| GET | `/visitors/{visitor_id}/path` | 查询观众参观路径 |
| POST | `/visitors/{visitor_id}/withdraw` | 撤回数据使用同意（body 可省略，或传 `{"visitor_id": "..."}`） |
| POST | `/legacy/batches/{batch_id}/cards` | 录入登记卡（品名字码序列），同卡号重录视为更新 |
| GET | `/legacy/batches/{batch_id}/cards/{card_no}` | 查原码序列、逐位转写与未映射位置 |
| PUT | `/legacy/batches/{batch_id}/mapping-table` | 整表更换本批对照表（与索引重建同一事务） |
| GET | `/legacy/batches/{batch_id}/mapping-table` | 查批次现行对照表 |
| GET | `/legacy/cards/by-name?name=...&batch_id=` | 按完整品名查同名卡（仅已完整转写） |
| GET | `/health` | 健康检查 |

事件字段：`event_id`（全局唯一，重传复用同一号）、`visitor_id`（匿名观众号）、
`page_view_id`（页面浏览号，同一次浏览的开关共享）、`exhibit_id`（展品号）、
`event_type`（`open` / `close`）、`event_time`（**带时区的 UTC ISO-8601**，
如 `2026-11-18T08:00:00Z`；无时区时间戳返回 400）。

### 上报响应语义

- `stored`：首次入库
- `duplicate`：`event_id` 已存在，幂等忽略（即使重传内容被篡改，也以首次内容为准）
- `rejected_withdrawn`：该观众已撤回同意，事件不入库，路径不会因此恢复

HTTP 状态码：成功受理固定 `201`，具体结果看响应体 `status`；参数/时间非法为
`422` / `400`。

### 路径响应

按 `page_view_id` 配对开关，同一观众按 **open 时间** 排序（`sequence` 从 1 连续编号）：

- 有关闭：`dwell_known=true`，`dwell_seconds` 为停留秒数
- 缺关闭：仍计为一次参观，但 `closed_at=null`、`dwell_known=false`、`dwell_seconds=null`（停留未知）
- 只有 close 没有 open 的孤儿事件不产生参观项
- close 早于 open 的异常数据：保留关闭时间，但停留时长标记未知

## 旧卡检索（legacy）

旧编目系统把品名导出为一串**四位十六进制字码**（如 `0B02 0A01 0A02`），
每个字码要查批次对照表才能得到一段 Unicode 文本，拼接后才是今天的品名。

- **录入**：`POST /legacy/batches/{batch_id}/cards`，body `{"card_no": "0004", "codes": ["0B02", "0A01", "0A02", "0A03", "0A06"]}`。
  字码大小写不敏感，统一按大写存储；同批次同卡号重录会替换原码序列并重新转写。
- **换表**：`PUT /legacy/batches/{batch_id}/mapping-table`，body 为整张对照表
  `{"entries": {"0A01": "青", ...}}`。每批一张现行表，各批互不影响；
  与现行表完全一致时不产生新版本（`status=unchanged`）。
- **查原码**：`GET /legacy/batches/{batch_id}/cards/{card_no}` 返回原码序列、
  逐位文本（`text=null` 即未映射）与 `unmapped_positions`（0 起）。
- **查同名卡**：`GET /legacy/cards/by-name?name=明青花`（可加 `batch_id` 限定批次）
  精确匹配完整品名，只返回**已完整转写**的卡；缺映射的卡不进索引。

### 设计要点（旧卡检索）

1. **原码与转写分离**：`legacy_cards` 原样保存字码序列；`legacy_card_index`
   是派生转写（逐位文本、完整品名、未映射位置、依据的表版本）。品名索引只含
   `complete=1` 的卡。
2. **对照表和索引一起生效**：换表 = 同一 `BEGIN IMMEDIATE` 事务内「停用旧表 +
   写入新表 + 重建受影响卡的索引行」。读请求各在一个快照事务内，只会看到
   旧表+旧索引或新表+新索引；换表失败整体回滚，旧表结果照常可查。
3. **只重建受影响的卡**：`legacy_card_codes` 是字码倒排（批次, 卡号, 位置, 字码），
   换表时按变动字码 `IN` 精确选出要重建的卡——包括此前因缺映射而未进品名索引的
   卡（缺码补齐的来源），其余卡不读不写。换表响应里的 `affected_cards` /
   `became_complete` / `became_incomplete` 即本次重建范围与结果。
4. **对照表版本历史**：每次真实换表产生新版本（`legacy_mapping_tables`），
   部分唯一索引保证每批至多一张 active 表；回滚旧表 = 把旧内容再 PUT 一次。

### 旧卡检索演示（/docs）

```bash
docker compose up --build
docker compose exec api python -m app.demo_data   # 装入演示批次 M1987 / B2001
```

演示数据：批次 `M1987` 的对照表 v1 缺 `0A06`/`0A07` 两个映射，且 `0A02` 与
`0A08` 都映射为「花」（旧系统异体重复码），共 7 张卡；批次 `B2001` 独立。

在 http://localhost:8000/docs 的「旧卡检索」分组里（录入/换表的请求体已内置
演示样例，Try it out 即可），或用 curl：

```bash
# 1) 缺码卡：0004 第 4 位 0A06 未映射，按名查不到
curl 'http://localhost:8000/legacy/batches/M1987/cards/0004'
curl -G 'http://localhost:8000/legacy/cards/by-name' --data-urlencode 'name=清青花瓷罐'

# 2) 同名卡：0006 与 0007 都叫「明青花」
curl -G 'http://localhost:8000/legacy/cards/by-name' --data-urlencode 'name=明青花'

# 3) 换 v2 表：补 0A06→罐、0A07→砚，改 0A05→盌、0A08→卉
curl -X PUT 'http://localhost:8000/legacy/batches/M1987/mapping-table' \
  -H 'Content-Type: application/json' -d '{"entries":{
    "0B01":"明","0B02":"清","0A01":"青","0A02":"花","0A03":"瓷",
    "0A04":"瓶","0A05":"盌","0A06":"罐","0A07":"砚","0A08":"卉"}}'
# 响应：affected_cards=["0002","0004","0005","0007"]，became_complete=["0004","0005"]

# 4) 缺码补齐：0004 → 「清青花瓷罐」，可按名查到
curl -G 'http://localhost:8000/legacy/cards/by-name' --data-urlencode 'name=清青花瓷罐'

# 5) 同名卡换表后分开：「明青花」只剩 0006，0007 改叫「明青卉」
curl -G 'http://localhost:8000/legacy/cards/by-name' --data-urlencode 'name=明青花'
curl -G 'http://localhost:8000/legacy/cards/by-name' --data-urlencode 'name=明青卉'

# 6) 失败换表：非法字码 → 422，旧表与查询结果不变
curl -X PUT 'http://localhost:8000/legacy/batches/M1987/mapping-table' \
  -H 'Content-Type: application/json' -d '{"entries":{"0A01":"青","BAD!":"x"}}'
```

## 设计要点（如何满足需求）

1. **重传不重复计数**：`events.event_id` 为主键；插入前先查存在性，存在即返回
   `duplicate`，整段逻辑在同一事务内。
2. **乱序补报照常处理**：路径不做物化存储，每次查询从已提交事件实时配对、排序。
   后补的较早 open 会在下次查询时自然改变路径顺序；后补的 close 会把“停留未知”
   变成已知时长。
3. **撤回后不可恢复**：`withdrawals` 表持久化撤回标记；撤回在一个 `BEGIN IMMEDIATE`
   事务内完成「删除该观众全部事件 + 写撤回标记」。此后任何上传（包括旧事件重放）
   在同一事务内先查撤回标记，一律 `rejected_withdrawn`。
4. **交错操作基于同一已提交状态、不露半份路径**：
   - SQLite 开 WAL，写请求统一 `BEGIN IMMEDIATE` 立即拿写锁，配合 `busy_timeout`
     串行化所有写事务；
   - 读请求在单个 `BEGIN DEFERRED` 快照事务里完成「撤回检查 + 事件扫描」，
     要么看到撤回前的完整状态，要么看到撤回后的空路径；
   - 路径是查询时推导的，不存在“路径表更新一半”的中间态；
   - 批量上报为单事务，任一事件非法则整批回滚。
5. **线程/进程并发**：每请求一个独立 SQLite 连接（`check_same_thread=False`
   以适配 uvicorn 线程池，连接不在请求间共享）；多个 uvicorn worker 共享同一
   数据卷时由 SQLite 文件锁保证安全。

## 目录结构

```
app/
  config.py    环境变量配置（DB 路径、busy timeout）
  db.py        连接、schema、WAL/pragma、事务管理
  models.py    Pydantic 请求/响应模型（参观路径）
  crud.py      领域逻辑：入库去重、开关配对、路径推导、撤回
  legacy_models.py  Pydantic 模型（旧卡检索，带 /docs 演示样例）
  legacy.py    旧卡检索：录入、整表换表、派生索引、路由
  demo_data.py 本地演示数据（python -m app.demo_data，可重复执行）
  main.py      FastAPI 路由与生命周期
tests/
  conftest.py
  test_api.py      参观路径 15 项功能/并发测试
  test_legacy.py   旧卡检索 15 项功能/并发测试
  stress_http.py   针对运行中服务的真实 HTTP 并发压测（手动运行）
Dockerfile
docker-compose.yml
```

## 自测

```bash
pip install -r requirements-dev.txt
pytest tests/ -v
```

覆盖：正常配对与停留时长、乱序（close 先到）与更早事件补报导致的重排、
缺关闭标记未知及补关后转已知、孤儿 close、`event_id` 重传（含篡改内容）、
批量去重与整批原子回滚、撤回后路径消失/重复撤回幂等/旧事件重放与新事件均被拒、
多线程上传-查询-撤回交错（进程内 TestClient 与真实 HTTP 两种）、
无时区时间拒绝、时区偏移归一化、负停留异常、WAL 与主键的存储层校验。

旧卡检索（`tests/test_legacy.py`，15 项）覆盖：录入即转写与按名索引、
批次无表时全未映射、未映射位置查询与索引排除、**缺码补齐**（换表新增映射后
缺码卡变完整且可按名查到）、**同名卡换表后分开**（异体重复码纠正后两卡分名）、
增量重建范围（`affected_cards` 精确断言、未涉及卡不动、相同表 unchanged）、
删映射导致完整卡变不完整、失败换表（422 与换表中途崩溃两种）旧表旧索引不变、
批次间对照表独立、重录替换原码并改变重建范围、字码校验与大小写归一、
404 与精确匹配语义、多线程换表-查询交错（同名卡对要么同现要么同不现）。

## 已知限制 / 未完成项

- 本开发沙箱没有 Docker 守护进程，`Dockerfile` 与 `docker-compose.yml` 已编写并做
  YAML/结构校验，但**未在真实 Docker 中构建运行**；容器外已用 uvicorn 单进程与
  `--workers 2` 两种方式完成功能与并发验证。
- 同一 `page_view_id` 若出现多个 open，取最早 open；多个 close 取最早 close，
  异常上报不报错但只保留一组配对。
- 没有鉴权/限流与分页；观众号、事件号、批次号、卡号均按字符串处理，未做格式白名单。
- 撤回是硬删除事件数据（仅保留撤回标记本身），不做审计留档。
- 旧卡检索：录入按单卡进行，无批量导入接口；品名索引为精确匹配，无模糊/分词检索；
  对照表版本历史保留但无专用回滚接口（把旧表内容重新 PUT 即回滚）；删除卡片、
  删除批次的接口未提供。
