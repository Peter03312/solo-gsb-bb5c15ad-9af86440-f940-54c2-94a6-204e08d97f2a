"""证据撤回承受力分析回归测试（无需真实数据库）。

修复的缺陷：约千条连续“早于”证据构成的合法长链上，最小割搜索（Dinic 阻塞流）
使用递归 DFS，增广路长度即链长，递归深度超过 Python 默认上限（1000）而抛
RecursionError，接口返回服务错误，复核人员拿不到撤回建议。

修复口径：
- 阻塞流 DFS 改为显式栈迭代（当前弧优化与逐路增广语义不变），长链不再受
  递归深度限制；
- 字典序贪心在撤回集合已满时提前结束（剩余预算为 0 时其余证据必然全部保留，
  结果不变），长链上避免近千次冗余最大流；
- 既有口径全部保持：原关系与最优路径、最少撤回数、并列按证据编号序列字典序
  最小、同一期双向连通按一条计数、受保护证据不进建议、受保护子图仍支撑时
  unreachable、原关系未定零撤回、四类结构错误约定、全程只读。

用内存假连接替代 psycopg2 连接（按 SQL 文本分发到内存状态），
从而在无 PostgreSQL 的环境下直接驱动 app.resilience.analyze_retraction_resilience；
假游标只放行分析用到的只读语句，任何写语句都会立即失败（全程只读由此保证）。

运行：python3 tests/test_resilience_retraction_analysis.py（或 pytest 收集 test_* 函数）
"""
from __future__ import annotations

import os
import sys
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
    """按 SQL 文本把查询分发到内存状态（只覆盖承受力分析用到的只读语句）。"""

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
            wanted = set(params[0])
            self._rows = [(s,) for s in sorted(wanted & self.state["strata"])]
        elif "FROM ASSERTIONS" in norm:
            self._rows = [
                (eid, a, b, relation, status)
                for eid, (a, b, relation, status) in sorted(self.state["assertions"].items())
            ]
        else:  # 分析只读路径不应出现其他语句（尤其不允许任何写语句）
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
def make_state(assertions, revision=1, extra_strata=()):
    """assertions: {eid: (a, b, relation, status)}；地层取断言端点并集外加 extra_strata。"""
    strata = set(extra_strata)
    for a, b, _rel, _status in assertions.values():
        strata.add(a)
        strata.add(b)
    return {"revision": revision, "strata": strata, "assertions": dict(assertions)}


def run_analysis(state, stratum_a, stratum_b, protected=(), expected_revision=1):
    """在假连接上执行一次承受力分析（每次调用替换 app.resilience.db_conn）。"""
    original = resilience_mod.db_conn
    resilience_mod.db_conn = lambda: _fake_db_conn(state)
    try:
        body = ResilienceIn(
            expected_revision=expected_revision,
            stratum_a=stratum_a,
            stratum_b=stratum_b,
            protected_evidence=list(protected),
        )
        return resilience_mod.analyze_retraction_resilience(body)
    finally:
        resilience_mod.db_conn = original


def expect_api_error(state, stratum_a, stratum_b, protected=(), expected_revision=1):
    try:
        run_analysis(state, stratum_a, stratum_b, protected, expected_revision)
    except ApiError as exc:
        return exc
    raise AssertionError("expected ApiError, analysis returned normally")


def make_chain(n, prefix="LC", stratum_prefix="C", relation="earlier_than"):
    """n 条连续证据构成的长链：C0000 < C0001 < ... < C{n}（或同关系同向连接）。"""
    return {
        f"{prefix}-{i:04d}": (f"{stratum_prefix}{i:04d}", f"{stratum_prefix}{i + 1:04d}", relation, "active")
        for i in range(n)
    }


# ------------------------------------------------------------------ 用例
def test_long_chain_thousand_earlier_evidence_no_recursion_error():
    """修复核心：约千条（1200）连续“早于”证据长链，默认递归上限下不再报服务错误。

    修复前：Dinic 阻塞流递归 DFS 深度 = 链长，超过 Python 默认递归上限 1000，
    抛 RecursionError（接口 500）。修复后：迭代实现，正常返回撤回建议。
    """
    n = 1200
    state = make_state(make_chain(n), revision=7)
    old_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(1000)  # 显式固定为解释器默认上限，保证回归判定确定
    try:
        resp = run_analysis(state, "C0000", f"C{n:04d}", expected_revision=7)
    finally:
        sys.setrecursionlimit(old_limit)
    assert resp["status"] == "retractions_found", resp["status"]
    assert resp["reachable"] is True
    assert resp["original_relation"] == "earlier_than"
    assert len(resp["original_evidence_path"]) == n  # 原最优路径即整条长链
    assert resp["original_evidence_path"][0] == "LC-0000"
    assert resp["original_evidence_path"][-1] == f"LC-{n - 1:04d}"
    assert len(resp["original_path_detail"]) == n
    # 链上任撤一条即未定；并列取证据编号序列字典序最小 -> 最小编号
    assert resp["minimum_retraction_count"] == 1
    assert resp["suggested_retractions"] == ["LC-0000"]
    assert resp["simulated_relation"] == "undetermined"
    assert resp["simulated_relation_label"] == "未定"
    assert resp["simulated_evidence_path"] == []
    assert resp["simulated_path_detail"] == []
    assert resp["revision"] == 7 and resp["current_revision"] == 7


def test_long_chain_same_period_also_supported():
    """同一期无向边长链（1500 条）同样不受递归深度限制；双向连通按一条计数。"""
    n = 1500
    state = make_state(make_chain(n, prefix="SP", stratum_prefix="P", relation="same_period"))
    old_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(1000)
    try:
        resp = run_analysis(state, "P0000", f"P{n:04d}")
    finally:
        sys.setrecursionlimit(old_limit)
    assert resp["status"] == "retractions_found"
    assert resp["original_relation"] == "same_period"
    assert resp["minimum_retraction_count"] == 1  # 无向边割值为 1，不按双向计两次
    assert resp["suggested_retractions"] == ["SP-0000"]
    assert resp["simulated_relation"] == "undetermined"


def test_long_chain_protected_prefix_excluded_from_suggestion():
    """长链上前 9 条证据受保护：建议跳过受保护证据，取其后最小编号。"""
    n = 1200
    state = make_state(make_chain(n), revision=3)
    protected = [f"LC-{i:04d}" for i in range(9)]
    old_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(1000)
    try:
        resp = run_analysis(state, "C0000", f"C{n:04d}", protected=protected, expected_revision=3)
    finally:
        sys.setrecursionlimit(old_limit)
    assert resp["status"] == "retractions_found"
    assert resp["minimum_retraction_count"] == 1
    assert resp["suggested_retractions"] == ["LC-0009"]
    assert not (set(resp["suggested_retractions"]) & set(protected))
    assert resp["protected_evidence"] == protected  # 请求内去重排序后回显
    assert resp["simulated_relation"] == "undetermined"


def test_long_chain_fully_protected_is_unreachable():
    """整条长链受保护：受保护子图仍支撑关系 -> unreachable 与剩余路径。"""
    n = 1200
    state = make_state(make_chain(n), revision=4)
    protected = [f"LC-{i:04d}" for i in range(n)]
    old_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(1000)
    try:
        resp = run_analysis(state, "C0000", f"C{n:04d}", protected=protected, expected_revision=4)
    finally:
        sys.setrecursionlimit(old_limit)
    assert resp["status"] == "unreachable"
    assert resp["reachable"] is False
    assert resp["minimum_retraction_count"] is None
    assert resp["suggested_retractions"] == []
    assert resp["simulated_relation"] == "earlier_than"  # 替代证据路径仍成立，不判失效
    assert len(resp["remaining_evidence_path"]) == n
    assert resp["remaining_evidence_path"][0] == "LC-0000"
    assert len(resp["remaining_path_detail"]) == n


def test_parallel_paths_lexicographically_smallest_set():
    """README 口径：直达 RV-4 与混合链 RV-1,RV-2,RV-3 互不共边，最少撤 2 条，
    并列取证据编号序列字典序最小 [RV-1, RV-4]。"""
    state = make_state(
        {
            "RV-1": ("T1", "T2", "same_period", "active"),
            "RV-2": ("T2", "T3", "earlier_than", "active"),
            "RV-3": ("T3", "T4", "earlier_than", "active"),
            "RV-4": ("T1", "T4", "earlier_than", "active"),
        }
    )
    resp = run_analysis(state, "T1", "T4")
    assert resp["status"] == "retractions_found"
    assert resp["minimum_retraction_count"] == 2
    assert resp["suggested_retractions"] == ["RV-1", "RV-4"]
    assert resp["original_relation"] == "earlier_than"
    assert resp["original_evidence_path"] == ["RV-4"]  # 证据数最少者优先
    assert resp["simulated_relation"] == "undetermined"


def test_same_period_edge_counts_as_one_retraction():
    """同一期边是无向边：割中该证据只计 1 次（不按双向连通计两次）。"""
    state = make_state(
        {
            "A1": ("T1", "T2", "same_period", "active"),
            "Z1": ("T2", "T3", "earlier_than", "active"),
        }
    )
    resp = run_analysis(state, "T1", "T3")
    assert resp["status"] == "retractions_found"
    assert resp["minimum_retraction_count"] == 1
    # 字典序最小解即同一期证据 A1：撤回它（计 1 条）等价类拆开，关系即未定
    assert resp["suggested_retractions"] == ["A1"]
    assert resp["simulated_relation"] == "undetermined"


def test_protected_path_still_supporting_returns_unreachable():
    """受保护证据单独仍支撑某条路径：unreachable，附仍成立的最优证据路径。"""
    state = make_state(
        {
            "RV-1": ("T1", "T2", "same_period", "active"),
            "RV-2": ("T2", "T3", "earlier_than", "active"),
            "RV-3": ("T3", "T4", "earlier_than", "active"),
            "RV-4": ("T1", "T4", "earlier_than", "active"),
        }
    )
    resp = run_analysis(state, "T1", "T4", protected=["RV-4"])
    assert resp["status"] == "unreachable"
    assert resp["reachable"] is False
    assert resp["minimum_retraction_count"] is None
    assert resp["suggested_retractions"] == []
    assert resp["simulated_relation"] == "earlier_than"
    assert resp["remaining_evidence_path"] == ["RV-4"]
    assert [d["evidence_id"] for d in resp["remaining_path_detail"]] == ["RV-4"]


def test_protected_evidence_never_in_suggestion():
    """受保护证据不得进入建议：保护 RV-1 后改选次优组合 [RV-2, RV-4]。"""
    state = make_state(
        {
            "RV-1": ("T1", "T2", "same_period", "active"),
            "RV-2": ("T2", "T3", "earlier_than", "active"),
            "RV-3": ("T3", "T4", "earlier_than", "active"),
            "RV-4": ("T1", "T4", "earlier_than", "active"),
        }
    )
    resp = run_analysis(state, "T1", "T4", protected=["RV-1"])
    assert resp["status"] == "retractions_found"
    assert resp["minimum_retraction_count"] == 2
    assert resp["suggested_retractions"] == ["RV-2", "RV-4"]
    assert "RV-1" not in resp["suggested_retractions"]
    assert resp["simulated_relation"] == "undetermined"


def test_already_undetermined_returns_zero_retractions():
    """原关系未定：撤回数 0、建议集合为空。"""
    state = make_state(
        {"RV-1": ("T1", "T2", "same_period", "active")},
        extra_strata={"T7"},
    )
    resp = run_analysis(state, "T1", "T7", protected=["RV-1"])
    assert resp["status"] == "already_undetermined"
    assert resp["reachable"] is True
    assert resp["original_relation"] == "undetermined"
    assert resp["minimum_retraction_count"] == 0
    assert resp["suggested_retractions"] == []
    assert resp["simulated_relation"] == "undetermined"
    assert resp["original_evidence_path"] == []


def test_later_than_direction_normalized():
    """反向查询（原关系 later_than）方向自动规整，建议与正向一致。"""
    state = make_state(
        {
            "RV-1": ("T1", "T2", "same_period", "active"),
            "RV-2": ("T2", "T3", "earlier_than", "active"),
            "RV-3": ("T3", "T4", "earlier_than", "active"),
            "RV-4": ("T1", "T4", "earlier_than", "active"),
        }
    )
    resp = run_analysis(state, "T4", "T1")
    assert resp["original_relation"] == "later_than"
    assert resp["original_relation_label"] == "晚于"
    assert resp["status"] == "retractions_found"
    assert resp["minimum_retraction_count"] == 2
    assert resp["suggested_retractions"] == ["RV-1", "RV-4"]
    assert resp["simulated_relation"] == "undetermined"


def test_stale_revision_rejected():
    """修订号过期：409 STALE_REVISION，details 带当前修订号。"""
    state = make_state({"RV-1": ("T1", "T2", "earlier_than", "active")}, revision=5)
    exc = expect_api_error(state, "T1", "T2", expected_revision=1)
    assert exc.status_code == 409
    assert exc.code == "STALE_REVISION"
    assert exc.details["current_revision"] == 5


def test_invalid_stratum_rejected():
    """地层未登记：400 INVALID_STRATUM。"""
    state = make_state({"RV-1": ("T1", "T2", "earlier_than", "active")})
    exc = expect_api_error(state, "T1", "TX")
    assert exc.status_code == 400
    assert exc.code == "INVALID_STRATUM"
    assert exc.details["stratum_id"] == "TX"


def test_duplicate_stratum_rejected():
    """两层相同：400 DUPLICATE_STRATUM。"""
    state = make_state({"RV-1": ("T1", "T2", "earlier_than", "active")})
    exc = expect_api_error(state, "T1", "T1")
    assert exc.status_code == 400
    assert exc.code == "DUPLICATE_STRATUM"
    assert exc.details["stratum_id"] == "T1"


def test_invalid_protected_evidence_rejected():
    """受保护证据不存在或非生效：400 INVALID_PROTECTED_EVIDENCE，details 区分。"""
    state = make_state(
        {
            "RV-1": ("T1", "T2", "earlier_than", "active"),
            "RV-9": ("T1", "T2", "earlier_than", "retracted"),
        }
    )
    exc = expect_api_error(state, "T1", "T2", protected=["RV-404", "RV-9"])
    assert exc.status_code == 400
    assert exc.code == "INVALID_PROTECTED_EVIDENCE"
    assert exc.details["not_found"] == ["RV-404"]
    assert exc.details["not_active"] == ["RV-9"]


def test_response_fields_stay_compatible():
    """既有响应字段保持兼容：成功 / 未定 / 不可达三类响应的键集合不缩水。"""
    state = make_state(
        {
            "RV-1": ("T1", "T2", "same_period", "active"),
            "RV-2": ("T2", "T3", "earlier_than", "active"),
            "RV-3": ("T3", "T4", "earlier_than", "active"),
            "RV-4": ("T1", "T4", "earlier_than", "active"),
        },
        extra_strata={"T7"},
    )
    found = run_analysis(state, "T1", "T4")
    for key in (
        "expected_revision", "current_revision", "revision", "stratum_a", "stratum_b",
        "protected_evidence", "original_relation", "original_relation_label",
        "original_evidence_path", "original_path_detail", "reachable", "status",
        "minimum_retraction_count", "suggested_retractions",
        "simulated_relation", "simulated_relation_label",
        "simulated_evidence_path", "simulated_path_detail",
    ):
        assert key in found, key
    und = run_analysis(state, "T1", "T7")
    for key in ("remaining_evidence_path", "minimum_retraction_count", "suggested_retractions"):
        assert key in und, key
    unreach = run_analysis(state, "T1", "T4", protected=["RV-4"])
    for key in ("remaining_evidence_path", "remaining_path_detail"):
        assert key in unreach, key


ALL_TESTS = [
    test_long_chain_thousand_earlier_evidence_no_recursion_error,
    test_long_chain_same_period_also_supported,
    test_long_chain_protected_prefix_excluded_from_suggestion,
    test_long_chain_fully_protected_is_unreachable,
    test_parallel_paths_lexicographically_smallest_set,
    test_same_period_edge_counts_as_one_retraction,
    test_protected_path_still_supporting_returns_unreachable,
    test_protected_evidence_never_in_suggestion,
    test_already_undetermined_returns_zero_retractions,
    test_later_than_direction_normalized,
    test_stale_revision_rejected,
    test_invalid_stratum_rejected,
    test_duplicate_stratum_rejected,
    test_invalid_protected_evidence_rejected,
    test_response_fields_stay_compatible,
]


def main():
    for test in ALL_TESTS:
        test()
        print(f"PASS {test.__name__}")
    print(f"ALL {len(ALL_TESTS)} RESILIENCE RETRACTION-ANALYSIS TESTS PASSED")


if __name__ == "__main__":
    main()
