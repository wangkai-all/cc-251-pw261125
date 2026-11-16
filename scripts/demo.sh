#!/usr/bin/env bash
# End-to-end staged demo. Everything runs against an isolated .demo home,
# so repeated runs are reproducible and never touch other data.
#
# Storyline (all fictional):
#   1. initial batches arrive for the three independent sources;
#   2. a 2021 split notice arrives before its name/boundary evidence,
#      so the relationship stays proposed and early release holds the
#      whole parent+children group back (no double counting);
#   3. the lagging evidence arrives, the split auto-confirms;
#   4. a corrupt boundary file fails collection, then a fixed file recovers;
#   5. a name conflict and a name-reuse case require human adjudication;
#   6. late historical backfill (2018 merge) is collected afterwards and
#      cannot revoke the decisions already made.
set -euo pipefail

cd "$(dirname "$0")/.."
export GAZETTEER_HOME="$PWD/.demo"
export GAZETTEER_SOURCES="$PWD/data/sources"
export GAZETTEER_DB="$GAZETTEER_HOME/gazetteer.db"
export PYTHONPATH="$PWD"
rm -rf "$GAZETTEER_HOME"
mkdir -p "$GAZETTEER_HOME"

# Files staged into the live source tree during the demo are removed on
# exit so the tree stays clean for the next run.
cleanup() {
  rm -f data/sources/names/N-2021.json \
        data/sources/boundaries/B-2021.json \
        data/sources/boundaries/B-2023-bad.json \
        data/sources/boundaries/B-2023.json \
        data/sources/names/N-2018-backfill.json \
        data/sources/notices/G-2018-backfill.json
}
trap cleanup EXIT

run() { echo; echo "── $*"; python3 -m gazetteer.cli "$@"; }

run init
run ingest
run build

echo; echo "### 1) 拆分通知先行、登记/边界滞后：关系应为 proposed"
run links
run pending

echo; echo "### 2) early 策略先发布可信区域（拆分组整组暂缓，父子不会同时统计）"
run release 2021-08-01 early
echo; echo "### aligned 策略因未对齐被拒绝"
run release 2021-08-01 aligned || true

echo; echo "### 3) 滞后证据到达：登记册与边界批次补到，拆分自动确认"
cp data/source-fixtures/names/N-2021.json data/sources/names/
cp data/source-fixtures/boundaries/B-2021.json data/sources/boundaries/
run ingest
run build
run links

echo; echo "### 4) 坏边界文件采集失败（失败留痕，不影响其他来源），修复后续跑成功"
cp data/source-fixtures/boundaries/B-2023-bad.json data/sources/boundaries/B-2023-bad.json
run ingest --file boundaries:data/sources/boundaries/B-2023-bad.json
run runs
rm data/sources/boundaries/B-2023-bad.json
cp data/source-fixtures/boundaries/B-2023-fixed.json data/sources/boundaries/B-2023.json
run ingest --file boundaries:data/sources/boundaries/B-2023.json
run build

echo; echo "### 5) 名称冲突（龙泉新区 vs 龙泉区）与名称复用（江南）进入待决"
run pending
run lookup 510301 2022-02-01

echo; echo "### 6) 人工裁决：字段冲突以龙泉区为准；名称复用判为同名不同体"
REUSE_LINK_ID=$(python3 - "$GAZETTEER_DB" <<'PY'
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
row = conn.execute("SELECT link_id FROM evolution_link WHERE link_type='reuse' LIMIT 1").fetchone()
print(row[0])
PY
)
run decide-field 510301 2022-01-01 name 龙泉区 "以2022-06勘误卷为准，登记册首发名称属误报"
run decide-reuse "$REUSE_LINK_ID" reject "锦山市江南区与龙泉市江南镇仅同名，沿革主体不同"
run build
echo; echo "## 待决队列应清零（同日冲突只保留一个版本，边界证据成功挂接，不再产生孤儿）"
run pending
run lookup 510301 2022-02-01

echo; echo "### 7) 整批对齐后 aligned 发布；点查证明历史未被当前名称覆盖"
run release 2023-06-01 aligned
run lookup 510301 2020-06-01
ALIGNED_ID=$(python3 -c "import sqlite3,os;print(sqlite3.connect(os.environ['GAZETTEER_DB']).execute(\"SELECT release_id FROM release WHERE policy='aligned' ORDER BY release_id DESC LIMIT 1\").fetchone()[0])")
run show-release "$ALIGNED_ID"

echo; echo "### 8) 历史回补（2018 合并卷）晚到：重建后旧裁决仍有效、已发布版本仍在"
cp data/source-fixtures/names/N-2018-backfill.json data/sources/names/
cp data/source-fixtures/notices/G-2018-backfill.json data/sources/notices/
run ingest
run build
run links
run sources
echo; echo "## 回补带来新的来源缺口（2018 两镇缺边界），但不撤销任何旧裁决"
run pending
python3 -c "import sqlite3,os;c=sqlite3.connect(os.environ['GAZETTEER_DB']);print('发布版本仍存在:', [r[0] for r in c.execute('SELECT release_id FROM release ORDER BY release_id')]);print('复用裁决仍为 rejected:', [r[0] for r in c.execute(\"SELECT link_id,status FROM evolution_link WHERE link_type='reuse'\")]);print('人工裁决条数:', c.execute('SELECT COUNT(*) FROM adjudication').fetchone()[0])"

echo; echo "演示完成。网页查看："
echo "  GAZETTEER_HOME=$GAZETTEER_HOME PYTHONPATH=$PWD python3 -m gazetteer.cli web"
