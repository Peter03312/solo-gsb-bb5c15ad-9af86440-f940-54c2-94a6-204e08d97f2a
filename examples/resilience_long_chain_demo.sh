#!/usr/bin/env bash
# 考古地层关系核验服务 —— 证据撤回承受力分析：约千条连续“早于”证据长链示例
# （POST /resilience/retraction-analysis）
#
# 修复背景：此前最小割搜索（Dinic 阻塞流）为递归实现，增广路长度等于链长；
# 在约千条连续“早于”证据构成的合法长链上递归深度超过解释器默认上限（1000），
# 接口报服务错误（500），复核人员无法获得撤回建议。修复后阻塞流改为显式栈
# 迭代（语义不变），长链分析在默认递归上限下正常返回，响应口径与短链完全一致。
#
# 演示数据说明：正式批次逐批写入会触发逐修订关系变化追踪（对千条长链的计算
# 成本极高），因此本示例用 SQL 把 1001 个地层与 1000 条连续“早于”证据直接
# 落库（读侧口径与正式批次落库后完全一致）；承受力分析本身只读，与写入路径
# 无关。演示结束时清理全部演示数据，脚本可重复执行。
#
# 前提：docker compose up --build 已启动，应用监听 http://localhost:8000
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8000}"
CHAIN_N="${CHAIN_N:-1000}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE=(docker compose --project-directory "$SCRIPT_DIR/..")
DB_SERVICE="${DB_SERVICE:-db}"
DB_USER="${POSTGRES_USER:-strat}"
DB_NAME="${POSTGRES_DB:-stratigraphy}"
LAST=$(printf '%04d' "$CHAIN_N")

psql_db() { "${COMPOSE[@]}" exec -T "$DB_SERVICE" psql -U "$DB_USER" -d "$DB_NAME" -v ON_ERROR_STOP=1 --quiet; }

# 中途失败也尽力清理演示数据（不掩盖原退出码），避免影响其他示例的重复执行
cleanup_on_exit() {
  psql_db >/dev/null 2>&1 <<'SQL' || true
BEGIN;
DELETE FROM assertions WHERE evidence_id LIKE 'LC-____';
DELETE FROM strata WHERE stratum_id LIKE 'C____';
COMMIT;
SQL
}
trap cleanup_on_exit EXIT

echo "== 0. 服务健康与当前正式修订号 =="
curl -s "$BASE/health"
echo
REV="$(curl -s "$BASE/revision" | python3 -c 'import sys,json;print(json.load(sys.stdin)["revision"])')"
echo "（当前修订号 $REV；长链分析全程只读，演示结束修订号不变）"

echo; echo "== 1. 直插演示数据：$((CHAIN_N + 1)) 个地层（C0000..C${LAST}）+ ${CHAIN_N} 条连续“早于”证据（LC-0000..） =="
echo "   （SQL 直插仅为绕过逐批写入的变化追踪成本；读侧口径与正式批次落库一致）"
python3 - "$CHAIN_N" "$REV" <<'PY' | psql_db
import sys
n, rev = int(sys.argv[1]), int(sys.argv[2])
print("BEGIN;")
# 幂等：先清理上次演示可能遗留的同前缀数据（先断言后地层，顺应外键方向）
print("DELETE FROM assertions WHERE evidence_id LIKE 'LC-____';")
print("DELETE FROM strata WHERE stratum_id LIKE 'C____';")
strata = ", ".join(f"('C{i:04d}', '长链演示区')" for i in range(n + 1))
print(f"INSERT INTO strata (stratum_id, site) VALUES {strata};")
rows = ", ".join(
    f"('LC-{i:04d}', 'C{i:04d}', 'C{i + 1:04d}', 'earlier_than', 'active', {rev})"
    for i in range(n)
)
print(
    "INSERT INTO assertions (evidence_id, stratum_a, stratum_b, relation, status, added_revision)"
    f" VALUES {rows};"
)
print("COMMIT;")
PY
echo "   落库完成（修订号仍为 $REV，未递增）"

echo; echo "== 2. 长链分析：C0000 与 C${LAST}（修复前此处因递归过深返回服务错误） =="
python3 - "$REV" "$CHAIN_N" <<'PY' > /tmp/resilience_chain_req.json
import json, sys
rev, n = int(sys.argv[1]), int(sys.argv[2])
print(json.dumps({
    "expected_revision": rev,
    "stratum_a": "C0000",
    "stratum_b": f"C{n:04d}",
    "protected_evidence": [],
}))
PY
curl -s -o /tmp/resilience_chain_resp.json -w 'HTTP %{http_code}，耗时 %{time_total}s\n' \
  -X POST "$BASE/resilience/retraction-analysis" -H 'Content-Type: application/json' \
  -d @/tmp/resilience_chain_req.json
python3 - "$REV" "$CHAIN_N" <<'PY'
import json, sys
rev, n = int(sys.argv[1]), int(sys.argv[2])
resp = json.load(open("/tmp/resilience_chain_resp.json"))
assert "error" not in resp, resp
assert resp["status"] == "retractions_found", resp["status"]
assert resp["original_relation"] == "earlier_than"
assert len(resp["original_evidence_path"]) == n, len(resp["original_evidence_path"])
assert resp["original_evidence_path"][0] == "LC-0000"
assert resp["minimum_retraction_count"] == 1
assert resp["suggested_retractions"] == ["LC-0000"], resp["suggested_retractions"]
assert resp["simulated_relation"] == "undetermined"
assert resp["revision"] == rev == resp["current_revision"]
print(json.dumps({
    "status": resp["status"],
    "original_relation": resp["original_relation_label"],
    "original_path_len": len(resp["original_evidence_path"]),
    "minimum_retraction_count": resp["minimum_retraction_count"],
    "suggested_retractions": resp["suggested_retractions"],
    "simulated_relation": resp["simulated_relation_label"],
}, ensure_ascii=False, indent=2))
print("长链最小割正常返回：链上任撤 1 条即未定，并列取编号序列字典序最小 -> [LC-0000]")
PY

echo; echo "== 3. 受保护证据不进建议：保护链首 9 条（LC-0000..LC-0008） =="
python3 - "$REV" "$CHAIN_N" <<'PY' > /tmp/resilience_chain_req.json
import json, sys
rev, n = int(sys.argv[1]), int(sys.argv[2])
print(json.dumps({
    "expected_revision": rev,
    "stratum_a": "C0000",
    "stratum_b": f"C{n:04d}",
    "protected_evidence": [f"LC-{i:04d}" for i in range(9)],
}))
PY
curl -s -X POST "$BASE/resilience/retraction-analysis" -H 'Content-Type: application/json' \
  -d @/tmp/resilience_chain_req.json | python3 -c '
import json, sys
resp = json.load(sys.stdin)
assert resp["status"] == "retractions_found", resp
assert resp["minimum_retraction_count"] == 1
assert resp["suggested_retractions"] == ["LC-0009"], resp["suggested_retractions"]
assert resp["simulated_relation"] == "undetermined"
print(json.dumps({
    "protected_evidence": resp["protected_evidence"],
    "suggested_retractions": resp["suggested_retractions"],
    "simulated_relation": resp["simulated_relation_label"],
}, ensure_ascii=False, indent=2))
print("受保护的 LC-0000..LC-0008 不进入建议，改取其后最小编号 LC-0009")'

echo; echo "== 4. 全链受保护：替代证据路径仍成立 -> unreachable（不判失效） =="
python3 - "$REV" "$CHAIN_N" <<'PY' > /tmp/resilience_chain_req.json
import json, sys
rev, n = int(sys.argv[1]), int(sys.argv[2])
print(json.dumps({
    "expected_revision": rev,
    "stratum_a": "C0000",
    "stratum_b": f"C{n:04d}",
    "protected_evidence": [f"LC-{i:04d}" for i in range(n)],
}))
PY
curl -s -X POST "$BASE/resilience/retraction-analysis" -H 'Content-Type: application/json' \
  -d @/tmp/resilience_chain_req.json | python3 -c '
import json, sys
n = int(sys.argv[1])
resp = json.load(sys.stdin)
assert resp["status"] == "unreachable", resp["status"]
assert resp["reachable"] is False
assert resp["minimum_retraction_count"] is None
assert resp["suggested_retractions"] == []
assert resp["simulated_relation"] == "earlier_than"
assert len(resp["remaining_evidence_path"]) == n
print(json.dumps({
    "status": resp["status"],
    "minimum_retraction_count": resp["minimum_retraction_count"],
    "simulated_relation": resp["simulated_relation_label"],
    "remaining_path_len": len(resp["remaining_evidence_path"]),
}, ensure_ascii=False, indent=2))
print("受保护子图仍支撑整条长链：返回 unreachable 与仍成立的 1000 条剩余路径")' "$CHAIN_N"

echo; echo "== 5. 反向查询 C${LAST} 与 C0000（原关系晚于）：方向自动规整，建议相同 =="
python3 - "$REV" "$CHAIN_N" <<'PY' > /tmp/resilience_chain_req.json
import json, sys
rev, n = int(sys.argv[1]), int(sys.argv[2])
print(json.dumps({
    "expected_revision": rev,
    "stratum_a": f"C{n:04d}",
    "stratum_b": "C0000",
    "protected_evidence": [],
}))
PY
curl -s -X POST "$BASE/resilience/retraction-analysis" -H 'Content-Type: application/json' \
  -d @/tmp/resilience_chain_req.json | python3 -c '
import json, sys
resp = json.load(sys.stdin)
assert resp["original_relation"] == "later_than", resp["original_relation"]
assert resp["status"] == "retractions_found"
assert resp["minimum_retraction_count"] == 1
assert resp["suggested_retractions"] == ["LC-0000"]
assert resp["simulated_relation"] == "undetermined"
print(json.dumps({
    "original_relation": resp["original_relation_label"],
    "suggested_retractions": resp["suggested_retractions"],
    "simulated_relation": resp["simulated_relation_label"],
}, ensure_ascii=False, indent=2))'

echo; echo "== 6. 确认分析全程只读：修订号与生效证据数均不变 =="
REV2="$(curl -s "$BASE/revision" | python3 -c 'import sys,json;print(json.load(sys.stdin)["revision"])')"
[ "$REV2" = "$REV" ] && echo "修订号仍为 $REV（未递增）"
curl -s "$BASE/assertions?status=active" | python3 -c '
import json, sys
n = int(sys.argv[1])
rows = json.load(sys.stdin)
chain = [r for r in rows if r["evidence_id"].startswith("LC-")]
assert len(chain) == n, len(chain)
print(f"长链证据 {len(chain)} 条全部仍生效（分析未改断言）")' "$CHAIN_N"

echo; echo "== 7. 清理演示数据（SQL 删除本次直插的地层与证据） =="
psql_db <<'SQL'
BEGIN;
DELETE FROM assertions WHERE evidence_id LIKE 'LC-____';
DELETE FROM strata WHERE stratum_id LIKE 'C____';
COMMIT;
SQL
curl -s "$BASE/assertions?status=active" | python3 -c '
import json, sys
rows = json.load(sys.stdin)
assert not [r for r in rows if r["evidence_id"].startswith("LC-")]
print("演示数据已清理（可重复执行本示例）")'
rm -f /tmp/resilience_chain_req.json /tmp/resilience_chain_resp.json

echo; echo "长链撤回承受力示例执行完毕（约千条连续“早于”证据链上分析正常返回，正式状态未被修改）。"
