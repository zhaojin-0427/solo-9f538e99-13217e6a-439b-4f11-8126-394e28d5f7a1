# 实验室批次漂移核查（Flask + SQLite）

质控员上传测量 CSV、发布批次版本，选择已发布的基线/待检批次并填写 MAD 倍数阈值，
系统按设备计算基线中位数与 MAD，识别待检批次中**持续 ≥3 个相邻样本**的偏离异常段，
所有核查结论固化所用批次版号，更正重算不覆盖旧结论。

## 一、启动

### Docker Compose（推荐）

```bash
docker compose up -d --build
# 打开 http://localhost:8000
```

SQLite 数据文件保存在命名卷 `drift-data`（容器内 `/data/drift.db`），
重建容器不丢数据；停止清理：`docker compose down`（加 `-v` 同时删除数据卷）。

### 本地运行

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python run.py          # http://localhost:8000，数据库默认 ./data/drift.db
```

## 二、CSV 格式

UTF-8（兼容 BOM），首行表头，支持中英文列名：

| 列 | 别名 | 说明 |
|---|---|---|
| `batch` | 批次 / batch_name | 批次名 |
| `device` | 设备 / device_id | 设备编号 |
| `sample_seq` | 样本序号 / seq | 整数序号 |
| `utc_ts` | UTC时刻 / timestamp | ISO-8601 UTC，如 `2026-11-08T00:10:00Z` |
| `metric_value` | 指标值 / value | 有限数值 |

一个文件可包含多个批次，系统按批次分别生成新版本。示例见 `samples/`。

## 三、业务规则

### 上传与发布
- 上传只生成 **draft（草稿）** 版本；更正数据请重新上传，自动递增版本号。
- 已发布（published）版本不可变、不可重复发布；核查只能选择已发布版本。
- **同批次同设备同样本序号异值** → 整份文件拒绝（HTTP 409），不写入任何数据；
  完全重复行自动去重并计数；其它字段非法同样整份拒绝。
- 所有写操作在单事务中完成，写入失败整体回滚，当前结果不变
  （上传表单含“模拟写入失败”勾选项，API 用 `?simulate_fail=1` 验证）。

### 异常段判定（按设备独立计算）
1. 基线统计：中位数 `median` 与中位数绝对偏差 `MAD`，
   阈值带 = `median ± mad_multiplier × MAD`。
2. 待检样本按 UTC 时刻排序，落在带外即为偏离点。
3. **连续 ≥3 个相邻偏离点**才构成异常段；以下情况断开：
   - 样本回到阈值带内；
   - 缺测：样本序号不连续（缺号即断）；
   - UTC 时刻不严格递增（持平/倒退即断）；
   - 设备切换：不同设备分别成段。
4. `MAD=0`（基线为常量/高度集中）时阈值带退化为中位数点，
   任何不等于中位数的测量都判偏离，避免除零漏报。
5. 基线中不存在的设备跳过判定并在结果中标注，不产生异常段。

### 版本固化与重算
- 每条核查记录保存基线/待检的**批次名+版本号**与 MAD 倍数，旧结论永久可查。
- “用最新发布版重算”生成**新核查记录**（`supersedes` 指向旧记录），
  通过设备级数据哈希（SHA-256）比对，只把基线/待检数据实际变化的设备
  标记为“受影响设备”；草稿版本不参与，系统始终取最新已发布版。

### 查询
- 页面 `/segments` 与 `GET /api/segments` 支持按设备、批次名、
  偏离方向（high/low）、最小持续样本数过滤，结果带所属核查与版号。

## 四、HTTP API 摘要

| 方法 | 路径 | 作用 |
|---|---|---|
| POST | `/api/uploads`（multipart `file`，或 JSON `{"csv": "..."}`） | 上传，返回新建草稿版本 |
| POST | `/api/batches/<name>/publish?version=N` | 发布（version 可省略=最新） |
| GET | `/api/batches` | 批次版本列表 |
| POST | `/api/checks` | `{baseline_batch, target_batch, mad_multiplier}` 创建核查 |
| POST | `/api/checks/<id>/rerun` | 按最新发布版重算，返回受影响设备 |
| GET | `/api/checks` / `/api/checks/<id>` | 核查列表 / 详情（含设备结果与异常段） |
| GET | `/api/segments` | 异常段查询（device_id/batch_name/direction/min_samples） |

## 五、自测

```bash
PYTHONPATH=<flask 安装目录>:. python -m unittest discover -s tests -v
```

`tests/test_app.py` 共 15 个用例（当前全部通过），覆盖：
- **基线污染**：草稿不可用于核查；后续上传/草稿不改变已发布基线与历史结论；
  发布更正版后重算才切换版号，旧核查原样保留；
- **断段规则**：回带内、缺测断号、UTC 非递增、设备切换、≥3 样本门槛、MAD=0；
- **失败提交**：模拟写入失败 500 回滚后批次/数据不变；同键异值 409 整份拒绝；
  完全重复去重；缺列拒绝；
- **版本化重算**：待检/基线更正后的受影响设备识别、版号固化、无变化重算；
- 异常段过滤查询与网页渲染。

另外用 `samples/` 做过真实 HTTP 端到端冒烟：DEV-A 正确识别两段
（序号 3–5、10–12，中间被回带与缺测断开），DEV-B 无异常段。

## 六、设计说明与未完成项

- 单 gunicorn worker + 多线程 + SQLite WAL/`busy_timeout`，适配内网低并发核查场景；
  高并发多写场景需换 PostgreSQL 等。
- 未做用户登录/权限控制（需求未提及）；生产部署请通过反向代理加认证并修改 `SECRET_KEY`。
- “失败提交”的自测通过服务层注入点模拟（`simulate_fail`），未做磁盘故障/断电级演练。
- 未引入 Celery 等异步任务：CSV 上传限制 16MB，全量内存解析后单事务写入；
  超大文件需改为流式/分块处理。
- 未做国际化与分页；异常段查询在数据量很大时需要补分页索引。
