"""证据撤回承受力分析在合法长链上的回归测试（无需真实数据库）。

修复点：约千条连续“早于”证据构成的合法长链曾让最小割搜索的递归 DFS
递归过深（RecursionError -> 服务 500），复核人员拿不到撤回建议。修复后
阻塞流改为显式栈迭代搜索，且网络骨架一次构建、可行性测试只重置容量。

本测试用内存假连接替代 psycopg2 连接（按 SQL 文本分发到内存状态），
直接驱动 app.resilience.analyze_retraction_resilience，覆盖：

- 千条连续“早于”长链 / 千条连续“同一期”长链：不再递归过深，正确给出
  最少撤回数 1、字典序最小建议，模拟撤回后关系未定；
- 反向（later_than）自动规整；两条互不共边长链的最小割为 2 且字典序最小；
- 受保护证据单独仍支撑整条路径 -> unreachable + remaining_evidence_path；
  受保护证据只挡住字典序最小时改取次小；受保护证据不进建议；
- 原关系未定 -> already_undetermined、0 撤回、空集合；
- 同期 + 早于混合长链按一条同期证据计数；
- 过期修订 / 未登记地层 / 相同地层 / 受保护证据不存在或非生效按既有约定拒绝；
- 只读：任何非查询语句出现即失败。

运行：python3 tests/test_retraction_resilience_long_chain.py（或 pytest 收集）
"""
from __future__ import annotations

import os
import sys
import time
import types
from contextlib import contextmanager

# app.db 顶部 import psycopg2 / psycopg2.pool；本测试不需要真实驱动
_psycopg2 = types.ModuleType("psycopg2")
_psycopg2_pool = types.ModuleType("psycopg2.pool")
_psycopg2.pool = _psycopg2_pool
sys.modules.setdefault("psycopg2", _psycopg2)
sys.modules.setdefault("psycopg2.pool", _psycopg2_pool)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import resilience as resilience_mod  # noqa: E402
from app.errors import ApiError  # noqa: E402
from app.models import ResilienceIn  # noqa: E402


# ------------------------------------------------------------------ 内存假连接
class FakeCursor:
    """按 SQL 文本把查询分发到内存状态（只覆盖承受力分析只读路径用到的语句）。"""

    def __init__(self, state):
        self.state = state
        self._rows = []

    def execute(self, sql, params=None):
        norm = " ".join(sql.split()).upper()
        if norm.startswith("SET TRANSACTION"):
            self._rows = []
        elif "FROM REVISION_COUNTER" in norm:
            self._rows = [(self.state["revision"],)]
        elif "FROM STRATA" in norm:
            wanted = set(params[0]) if params else set()
            self._rows = [(s,) for s in sorted(wanted & self.state["strata"])]
        elif "FROM ASSERTIONS" in norm:
            self._rows = [
                (eid, a, b, relation, status)
                for eid, (a, b, relation, status) in sorted(self.state["assertions"].items())
            ]
        else:  # 分析只读，不应出现其他语句（尤其不允许任何写语句）
            raise AssertionError(f"unexpected SQL in read-only resilience analysis: {sql}")

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _CursorCM:
    def __init__(self, cursor):
        self._cursor = cursor

    def __enter__(self):
        return self._cursor

    def __exit__(self, *exc):
        return False


class FakeConn:
    def __init__(self, state):
        self._state = state

    def cursor(self):
        return _CursorCM(FakeCursor(self._state))


@contextmanager
def _fake_db_conn(state):
    yield FakeConn(state)


# ------------------------------------------------------------------ 测试工具
def make_state(assertions, strata=None, revision=1):
    """assertions: {eid: (a, b, relation, status)}。未显式给地层时取断言端点全集。"""
    endpoint_strata = set()
    for a, b, _rel, _status in assertions.values():
        endpoint_strata.add(a)
        endpoint_strata.add(b)
    all_strata = endpoint_strata | set(strata or ())
    return {"revision": revision, "strata": all_strata, "assertions": dict(assertions)}


def run_analysis(state, a, b, protected=(), expected_revision=None):
    """在假连接上执行一次只读承受力分析。"""
    original = resilience_mod.db_conn
    resilience_mod.db_conn = lambda: _fake_db_conn(state)
    try:
        body = ResilienceIn(
            expected_revision=state["revision"] if expected_revision is None else expected_revision,
            stratum_a=a,
            stratum_b=b,
            protected_evidence=list(protected),
        )
        return resilience_mod.analyze_retraction_resilience(body)
    finally:
        resilience_mod.db_conn = original


def expect_api_error(state, a, b, protected=(), expected_revision=None):
    try:
        run_analysis(state, a, b, protected, expected_revision)
    except ApiError as exc:
        return exc
    raise AssertionError("expected ApiError, analysis returned normally")


def chain_assertions(prefix, n, relation="earlier_than", node_prefix="L"):
    """n 条连续证据把 L0..Ln 串成一条合法长链。"""
    return {
        f"{prefix}{i:05d}": (f"{node_prefix}{i}", f"{node_prefix}{i + 1}", relation, "active")
        for i in range(n)
    }


# ------------------------------------------------------------------ 修复场景：长链不递归过深
def test_thousand_earlier_chain_does_not_recurse_and_finds_one_retraction():
    """约千条连续“早于”证据的合法长链：修复前 RecursionError(500)，修复后给出建议。"""
    n = 1000
    state = make_state(chain_assertions("E", n, "earlier_than"))
    t0 = time.time()
    resp = run_analysis(state, "L0", f"L{n}")
    elapsed = time.time() - t0
    assert resp["status"] == "retractions_found", resp
    assert resp["original_relation"] == "earlier_than"
    assert len(resp["original_evidence_path"]) == n
    assert resp["original_evidence_path"][0] == "E00000"
    assert resp["original_evidence_path"][-1] == f"E{n - 1:05d}"
    assert len(resp["original_path_detail"]) == n
    assert resp["minimum_retraction_count"] == 1
    assert resp["suggested_retractions"] == ["E00000"]  # 唯一链上字典序最小
    assert resp["simulated_relation"] == "undetermined"
    assert resp["simulated_evidence_path"] == []
    assert resp["simulated_path_detail"] == []
    assert elapsed < 10.0, f"长链分析过慢: {elapsed:.2f}s"  # 修复后应远低于超时阈值


def test_longer_chain_still_linear_safe():
    """更长（数千条）的连续链同样安全：显式栈不随链深增长。"""
    n = 3000
    state = make_state(chain_assertions("E", n, "earlier_than", node_prefix="N"))
    resp = run_analysis(state, "N0", f"N{n}")
    assert resp["status"] == "retractions_found"
    assert resp["minimum_retraction_count"] == 1
    assert resp["suggested_retractions"] == ["E00000"]
    assert resp["simulated_relation"] == "undetermined"


def test_thousand_same_period_chain_counts_one_evidence():
    """约千条连续“同一期”证据：无向连通按一条证据撤回计数，最少 1 条。"""
    n = 1000
    state = make_state(chain_assertions("S", n, "same_period", node_prefix="C"))
    resp = run_analysis(state, "C0", f"C{n}")
    assert resp["original_relation"] == "same_period"
    assert len(resp["original_evidence_path"]) == n
    assert resp["status"] == "retractions_found"
    assert resp["minimum_retraction_count"] == 1
    assert resp["suggested_retractions"] == ["S00000"]
    assert resp["simulated_relation"] == "undetermined"


def test_reverse_query_normalizes_later_than():
    """反向查询（later_than）自动规整方向，结果与正向一致。"""
    n = 1000
    state = make_state(chain_assertions("E", n, "earlier_than"))
    resp = run_analysis(state, f"L{n}", "L0")
    assert resp["original_relation"] == "later_than"
    assert resp["status"] == "retractions_found"
    assert resp["minimum_retraction_count"] == 1
    assert resp["suggested_retractions"] == ["E00000"]
    assert resp["simulated_relation"] == "undetermined"


# ------------------------------------------------------------------ 并列字典序最小
def test_two_disjoint_long_chains_min_cut_two_lex_smallest():
    """两条互不共边的长链：最少撤回 2 条，取各链字典序最小证据。"""
    m = 300
    assertions = {}
    for chain in ("P", "Q"):
        for i in range(m):
            assertions[f"{chain}{i:05d}"] = (
                f"{chain}{i}", f"{chain}{i + 1}", "earlier_than", "active"
            )
    assertions["ZA-P"] = ("ZA", "P0", "earlier_than", "active")
    assertions["ZA-Q"] = ("ZA", "Q0", "earlier_than", "active")
    assertions["P-ZB"] = (f"P{m}", "ZB", "earlier_than", "active")
    assertions["Q-ZB"] = (f"Q{m}", "ZB", "earlier_than", "active")
    state = make_state(assertions)
    resp = run_analysis(state, "ZA", "ZB")
    assert resp["status"] == "retractions_found"
    assert resp["minimum_retraction_count"] == 2
    # 每条链各撤一条；按证据编号字符串字典序（'-'=0x2D 先于 '0'=0x30），
    # 两组可选割边（ZA-P/P00xxx/P-ZB × ZA-Q/Q00xxx/Q-ZB）中字典序最小的
    # 一对是两个汇聚连接器 [P-ZB, Q-ZB]。
    assert resp["suggested_retractions"] == ["P-ZB", "Q-ZB"], resp["suggested_retractions"]
    assert resp["simulated_relation"] == "undetermined"


def test_mixed_same_and_earlier_long_chain_one_cut():
    """同期 + 早于混合长链：任一边都唯一成割，最少 1 条，字典序最小入选。"""
    n = 1000
    assertions = {}
    for i in range(n):
        rel = "same_period" if i % 3 == 0 else "earlier_than"
        assertions[f"M{i:05d}"] = (f"X{i}", f"X{i + 1}", rel, "active")
    state = make_state(assertions)
    resp = run_analysis(state, "X0", f"X{n}")
    assert resp["original_relation"] == "earlier_than"
    assert resp["minimum_retraction_count"] == 1
    assert resp["suggested_retractions"] == ["M00000"]
    assert resp["simulated_relation"] == "undetermined"


# ------------------------------------------------------------------ 受保护证据
def test_fully_protected_long_chain_is_unreachable_with_remaining_path():
    """受保护证据自身仍支撑整条长链：unreachable，最小撤回数 null，附剩余路径。"""
    n = 1000
    assertions = chain_assertions("S", n, "same_period", node_prefix="C")
    state = make_state(assertions)
    protected = [f"S{i:05d}" for i in range(n)]
    resp = run_analysis(state, "C0", f"C{n}", protected=protected)
    assert resp["status"] == "unreachable"
    assert resp["reachable"] is False
    assert resp["minimum_retraction_count"] is None
    assert resp["suggested_retractions"] == []
    assert resp["simulated_relation"] == "same_period"
    remaining = resp["remaining_evidence_path"]
    assert len(remaining) == n
    assert remaining[0] == "S00000"
    assert remaining[-1] == f"S{n - 1:05d}"
    assert len(resp["remaining_path_detail"]) == n


def test_protecting_lex_minimum_edges_shifts_suggestion():
    """两条长链的各自第一条内部边都被保护：改取字典序最小的其余割边，受保护证据不进建议。

    两条 P/Q 长链各需撤一条；P00000/Q00000 受保护后，按证据编号字典序最小的
    一组割边为两个汇聚连接器 [P-ZB, Q-ZB]（字典序小于链上其他内部证据）。
    """
    m = 300
    assertions = {}
    for chain in ("P", "Q"):
        for i in range(m):
            assertions[f"{chain}{i:05d}"] = (
                f"{chain}{i}", f"{chain}{i + 1}", "earlier_than", "active"
            )
    assertions["ZA-P"] = ("ZA", "P0", "earlier_than", "active")
    assertions["ZA-Q"] = ("ZA", "Q0", "earlier_than", "active")
    assertions["P-ZB"] = (f"P{m}", "ZB", "earlier_than", "active")
    assertions["Q-ZB"] = (f"Q{m}", "ZB", "earlier_than", "active")
    state = make_state(assertions)
    resp = run_analysis(state, "ZA", "ZB", protected=["P00000", "Q00000"])
    assert resp["minimum_retraction_count"] == 2
    assert resp["suggested_retractions"] == ["P-ZB", "Q-ZB"], resp["suggested_retractions"]
    assert set(resp["suggested_retractions"]).isdisjoint({"P00000", "Q00000"})
    assert resp["simulated_relation"] == "undetermined"
    # 回显的受保护集合自动去重、排序
    assert resp["protected_evidence"] == ["P00000", "Q00000"]


def test_protected_connector_edges_shift_to_internal_first_edges():
    """两个汇聚连接器被保护时，改取两条链的首条内部边 [P00000, Q00000]。"""
    m = 300
    assertions = {}
    for chain in ("P", "Q"):
        for i in range(m):
            assertions[f"{chain}{i:05d}"] = (
                f"{chain}{i}", f"{chain}{i + 1}", "earlier_than", "active"
            )
    assertions["ZA-P"] = ("ZA", "P0", "earlier_than", "active")
    assertions["ZA-Q"] = ("ZA", "Q0", "earlier_than", "active")
    assertions["P-ZB"] = (f"P{m}", "ZB", "earlier_than", "active")
    assertions["Q-ZB"] = (f"Q{m}", "ZB", "earlier_than", "active")
    state = make_state(assertions)
    resp = run_analysis(state, "ZA", "ZB", protected=["P-ZB", "Q-ZB"])
    assert resp["minimum_retraction_count"] == 2
    assert resp["suggested_retractions"] == ["P00000", "Q00000"], resp["suggested_retractions"]
    assert resp["simulated_relation"] == "undetermined"


def test_protected_middle_edge_of_unique_chain_still_cuttable_elsewhere():
    """唯一长链上保护中间一条边：仍可在别处切断，最小值 1，建议不含受保护证据。"""
    n = 500
    state = make_state(chain_assertions("E", n, "earlier_than", node_prefix="U"))
    resp = run_analysis(state, "U0", f"U{n}", protected=[f"E{n // 2:05d}"])
    assert resp["status"] == "retractions_found"
    assert resp["minimum_retraction_count"] == 1
    assert resp["suggested_retractions"] == ["E00000"]
    assert f"E{n // 2:05d}" not in resp["suggested_retractions"]


# ------------------------------------------------------------------ 原关系未定
def test_already_undetermined_returns_zero_retractions():
    """原关系未定：already_undetermined、撤回数 0、空集合。"""
    state = make_state(
        {"E1": ("A", "B", "earlier_than", "active")},
        strata={"A", "B", "ISO"},
    )
    resp = run_analysis(state, "A", "ISO", protected=["E1"])
    assert resp["status"] == "already_undetermined"
    assert resp["minimum_retraction_count"] == 0
    assert resp["suggested_retractions"] == []
    assert resp["simulated_relation"] == "undetermined"
    assert resp["original_evidence_path"] == []


# ------------------------------------------------------------------ 既有错误约定
def test_stale_revision_rejected():
    state = make_state({"E1": ("A", "B", "earlier_than", "active")})
    exc = expect_api_error(state, "A", "B", expected_revision=999)
    assert exc.status_code == 409 and exc.code == "STALE_REVISION"
    assert exc.details == {"current_revision": 1}


def test_invalid_stratum_rejected():
    state = make_state({"E1": ("A", "B", "earlier_than", "active")})
    exc = expect_api_error(state, "A", "NOPE")
    assert exc.status_code == 400 and exc.code == "INVALID_STRATUM"
    assert exc.details == {"stratum_id": "NOPE"}


def test_duplicate_stratum_rejected():
    state = make_state({"E1": ("A", "B", "earlier_than", "active")})
    exc = expect_api_error(state, "A", "A")
    assert exc.status_code == 400 and exc.code == "DUPLICATE_STRATUM"
    assert exc.details == {"stratum_id": "A"}


def test_protected_evidence_not_found_rejected():
    state = make_state({"E1": ("A", "B", "earlier_than", "active")})
    exc = expect_api_error(state, "A", "B", protected=["MISSING"])
    assert exc.status_code == 400 and exc.code == "INVALID_PROTECTED_EVIDENCE"
    assert exc.details == {"not_found": ["MISSING"], "not_active": []}


def test_protected_evidence_not_active_rejected():
    state = make_state({"E1": ("A", "B", "earlier_than", "active"),
                        "E2": ("B", "C", "earlier_than", "retracted")})
    exc = expect_api_error(state, "A", "B", protected=["E2", "MISSING"])
    assert exc.status_code == 400 and exc.code == "INVALID_PROTECTED_EVIDENCE"
    assert exc.details == {"not_found": ["MISSING"], "not_active": ["E2"]}


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} tests passed")
