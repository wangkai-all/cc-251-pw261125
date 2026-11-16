# 多来源行政区划沿革对账演示（虚构数据）

> ⚠ 本项目中的所有地区、来源、文件和通知均为**虚构演示数据**，用于说明多来源、
> 错峰发布场景下的沿革对账方法，**不冒充任何官方资料**。

## 要解决的问题

三个本地来源各自独立发布、发布时间不一致：

| 来源 | 目录 | 内容 |
| --- | --- | --- |
| 地名登记册 `names` | `data/sources/names/` | 地区名称与上级归属 |
| 边界引用档案 `boundaries` | `data/sources/boundaries/` | 边界测绘引用 |
| 沿革通知汇编 `notices` | `data/sources/notices/` | 拆分 / 合并 / 改名 / 名称复用通知 |

系统必须做到：

1. **按业务日期点查**当时的地区，并一路追溯到原始报文证据；新代码、新批次不能
   替换历史事实（历史版本只追加、不回写）。
2. **一地拆两地、名称复用、来源冲突**在证据未对齐前保持 `proposed` 并进入待决队列，
   不自动猜测关系。
3. 在「先发布可信区域（early）」与「等待整批对齐（aligned）」之间显式选择，并在发布
   说明中写清覆盖代价与未对齐范围。
4. 任何发布都**不会同时统计父地区和拆分子地区**（双计数硬守卫）。
5. **人工裁决、持续采集、历史回补并行**：旧批次回补或重新采集不能撤销已记录的
   人工裁决，已发布版本不可变。
6. 采集失败只留痕、不污染数据；修复后可续跑。

## 技术选型

纯 Python 3.11 标准库 + SQLite，无第三方依赖。

```
gazetteer/
├── schema.sql        # 原始证据 / 版本 / 沿革 / 裁决 / 发布 五组表
├── db.py             # 连接与建库
├── source.py         # 三个独立来源注册
├── ingest.py         # 采集：校验、哈希去重、失败留痕、原始报文不可变
├── build.py          # 对账重建：双时间版本、沿革关联、待决队列、裁决重放
├── adjudication.py   # 人工裁决（只追加，不删证据）
├── query.py          # 业务日期点查与证据链
├── release.py        # 不可变发布：early / aligned 两策略 + 双计数守卫
├── web.py            # http.server 真实网页（含裁决表单）
└── cli.py            # 命令行入口
data/
├── sources/          # 在线采集目录（分阶段投放）
└── source-fixtures/  # 滞后批次、坏文件/修复文件、历史回补卷
scripts/demo.sh       # 八段式端到端演示
tests/                # 15 个 unittest（含 HTTP 层）
```

## 快速开始

```bash
# 一键端到端演示（隔离的 .demo/ 目录，可重复运行）
bash scripts/demo.sh

# 手动执行
python3 -m gazetteer.cli init
python3 -m gazetteer.cli ingest      # 坏文件只会产生 failed 记录
python3 -m gazetteer.cli build
python3 -m gazetteer.cli pending
python3 -m gazetteer.cli release 2021-08-01 early
python3 -m gazetteer.cli release 2023-06-01 aligned   # 有待决时会被拒绝
python3 -m gazetteer.cli lookup 510301 2020-06-01     # 点查历史
python3 -m gazetteer.cli web                          # http://127.0.0.1:8000/

# 测试
python3 -m unittest discover -s tests -v
```

环境变量 `GAZETTEER_HOME` / `GAZETTEER_SOURCES` / `GAZETTEER_DB` 可重定向全部数据位置。

## 演示剧情（对应 `scripts/demo.sh` 八段）

1. 首批三来源数据入库；2021 拆分通知已到，但登记册与边界批次滞后 → 拆分关系
   `proposed`，来源缺口进入待决。
2. `early` 发布把拆分整组（父 + 两子）暂缓，其余地区照常发布；`aligned` 因未对齐
   被拒绝，发布说明列出覆盖代价。
3. 滞后的登记/边界批次到达，重新 build 后拆分**自动确认**。
4. 损坏的边界文件采集**失败留痕**（`ingest_run.status=failed`，零原始记录写入），
   修复文件续跑成功。
5. 同一业务日期两个名称批次冲突（龙泉新区 / 龙泉区）→ 保留最早发布值、冲突入队；
   “江南”名称复用检测入队。
6. 人工裁决：字段覆盖为「龙泉区」（原始报文仍保留）；名称复用判为同名不同体
   （`rejected`）。
7. 待决清零，`aligned` 发布成功；点查 2020 年仍返回历史名「龙泉镇」，证明历史未被
   当前名称替换。
8. 2018 年合并**历史回补卷**晚到：产生新的来源缺口待决，但旧裁决全部保留、两个已
   发布版本仍在且不可重复创建。

## 关键不变量（由 `tests/` 固化）

- 原始报文（`raw_record.payload` + SHA-256）永不改写；裁决只在读取时叠加覆盖值。
- 版本区间 `[valid_from, valid_to)` 半开区间；事实变化是「关闭旧版本 + 开新版本」。
- `release` / `release_item` 一经插入不可更新；同日期同策略重复创建直接拒绝。
- 双计数守卫：同一已确认拆分/合并组的父侧与子侧禁止同时出现在一个发布中。
- 每次 `build` 都重放全部 `adjudication`，因此持续采集与回补无法撤销人工裁决。
