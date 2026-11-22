# 仪器电压读数比对（Flask + SQLite）

质控员判断几台仪器的电压读数能否混用：上传批次草稿并发布，系统按仪器比对方式
在发布时一次性固化每对仪器的统计索引；之后按版号选两台仪器、输入非负整数微伏
容差，查询共测样品数、绝对差超过容差的样品数以及相差最大的样本序号。
更正数据重新上传产生新版本，旧版统计永久可查。换容差只查已存的统计索引，
不遍历原始读数或成对样品。

## 一、启动

### Docker Compose（推荐）

```bash
docker compose up -d --build
# 打开 http://localhost:8000
```

SQLite 数据文件保存在命名卷 `volt-data`（容器内 `/data/volt.db`），
重建容器不丢数据；停止清理：`docker compose down`（加 `-v` 同时删除数据卷）。

### 本地运行

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python run.py          # http://localhost:8000，数据库默认 ./data/volt.db
```

## 二、CSV 格式

UTF-8（兼容 BOM），首行表头，支持中英文列名：

| 列 | 别名 | 说明 |
|---|---|---|
| `batch` | 批次 / batch_name | 批次名 |
| `device` | 仪器 / 设备 / device_id | 仪器编号 |
| `sample_seq` | 样本序号 / seq | 整数序号；同批相同序号代表同一件标样 |
| `microvolt` | 电压读数 / 微伏 / uv / value | **整数微伏**，绝对值 ≤ 1,000,000 |

一个文件可包含多个批次，系统按批次分别生成草稿版本。示例见 `samples/`
（`target.csv` 是同批次的更正数据，用于演示版本化）。

约束：

- 每批最多 **100,000 条读数**、最多 **6 台仪器**，超限整份文件拒绝；
- 同批同仪器同样本序号异值 → 整份文件拒绝（HTTP 409），不写入任何数据；
  完全重复行自动去重并计数；其它字段非法同样整份拒绝。

## 三、业务规则

### 上传与发布

- 上传只生成 **draft（草稿）** 版本；草稿不可用于比对。
- 发布时在**单个事务**内完成：扫描该版原始读数，对每对仪器只取双方都测过的
  样品（按样本序号匹配），计算并固化：
  - 共测样品数 `common_count`；
  - 最大绝对差 `max_abs_diff` 及对应样本序号 `max_diff_seq`
    （最大差并列时取样本序号最小者）；
  - 绝对差值 → 出现次数的**直方图索引** `diff_histogram`。
- 统计生成或保存失败 → 事务回滚，新版**不会被发布**，版本保持草稿，
  修正后可重新发布（上传/发布表单含“模拟失败”勾选项，API 用
  `?simulate_fail=1` 验证回滚）。
- 已发布（published）版本不可变、不可重复发布。

### 容差查询（只读统计索引）

选择已发布批次版号 + 两台仪器 + 非负整数微伏容差后：

- 共测数、最大绝对差与样本序号直接读 `pair_stat`；
- “绝对差超过容差的样品数”是对直方图索引的一条聚合查询：
  `SELECT SUM(freq) FROM diff_histogram WHERE pair_stat_id=? AND abs_diff > ?`。
  容差比较为**严格大于**：绝对差恰好等于容差的样品不计入超差；
- 整个查询只访问 `pair_stat` 与 `diff_histogram`，不访问原始读数表 `reading`，
  因此换容差的代价只与直方图桶数有关、与读数总量无关
  （10 万条读数满规模实测约 1 ms/次）；
- 两台仪器没有共测样品时，`common_count=0` 并直接提示
  “两台仪器没有共测样品，无法比对”；
- 仪器顺序不影响结果：仪器对按编号字典序规范化存储与查询。

### 版本固化与更正

- 统计随批次版本固化。更正数据请重新上传生成新版本草稿，发布后产生独立统计；
- 旧版本及其统计原样保留、永久可查，比对时显式指定版号。

## 四、HTTP API 摘要

| 方法 | 路径 | 作用 |
|---|---|---|
| POST | `/api/uploads`（multipart `file`，或 JSON `{"csv": "..."}`） | 上传，返回新建草稿版本 |
| POST | `/api/batches/<name>/publish[?version=N&simulate_fail=1]` | 发布草稿并固化仪器对统计（省略 version=最新） |
| GET | `/api/batches` | 批次版本列表 |
| GET | `/api/batches/<name>/versions/<N>/devices` | 该已发布版本的仪器列表（网页下拉联动） |
| GET | `/api/batches/<name>/versions/<N>/pair-stats` | 该版本全部仪器对统计 |
| GET | `/api/compare?batch_name=&version=&device_x=&device_y=&tolerance=` | 容差查询（只读统计索引） |

`/api/compare` 返回示例：

```json
{
  "batch_name": "B-2026-V1", "version": 1,
  "device_a": "METER-1", "device_b": "METER-2",
  "tolerance_uv": 1,
  "common_count": 5, "over_tolerance_count": 4,
  "max_abs_diff": 3, "max_diff_seq": 5,
  "has_common": true, "message": null
}
```

无共测样品时 `has_common=false`、`message` 给出提示，HTTP 状态仍为 200；
草稿/不存在的版号或仪器对返回 404，非法参数返回 400。

## 五、网页

- `/` 上传 CSV（草稿）、发布版本、查看批次版本表；
- `/compare` 选已发布批次版号与两台仪器（版号→仪器下拉联动，数据随页面下发），
  填写容差后服务端渲染共测数、超差数与最大相差样本序号；无共测样品时显示提示。

## 六、自测

```bash
pip install -r requirements.txt
python -m unittest -v tests.test_app
```

`tests/test_app.py` 共 26 个用例，覆盖：

- 共测匹配、共测数/超差数/最大相差样本、并列取最小序号、容差边界
  （`diff == tolerance` 不算超差）、负数读数取绝对差、仪器顺序规范化；
- **索引-only 保证**：通过 `EXPLAIN QUERY PLAN` 与 SQLite trace 双重断言，
  容差查询不访问 `reading` 表；
- 无共测样品提示（API 与网页）；草稿不可比对；重复发布 409；
- 发布失败回滚（无统计残留、版本保持草稿、可重试）、上传失败不产生新版本；
- 更正后新版统计变化且旧版统计仍可查；
- 整数微伏与 ±1,000,000 范围、10 万条/6 台仪器上限及边界、同键异值整份拒绝、
  完全重复去重、缺列拒绝、中文表头、多批次文件；
- 网页渲染、表单发布/比对、设备与仪器对 API。

另有 10 万条满规模性能冒烟（脚本未入库）：上传约 1s、发布统计约 1.5s、
容差查询约 1ms/次。

## 七、设计说明与未完成项

- 单 gunicorn worker + 多线程 + SQLite WAL/`busy_timeout`，适配内网低并发场景；
  高并发多写场景需换 PostgreSQL 等。
- 未做用户登录/权限控制（需求未提及）；生产部署请通过反向代理加认证并修改
  `SECRET_KEY`。
- “失败提交”通过服务层注入点（`simulate_fail`）模拟，未做磁盘故障/断电级演练。
- 未引入异步任务：CSV 上传限制 16MB，全量内存解析后单事务写入；
  满规模发布统计在秒级完成。超大文件需改为流式/分块处理。
- 原始读数表 `reading` 保留用于审计与重算，但任何容差查询都不会读取它。
