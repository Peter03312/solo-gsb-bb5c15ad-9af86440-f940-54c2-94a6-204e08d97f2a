"""证据撤回承受力分析（只读）。

考古人员核对当前地层关系时，需要知道：若允许在**同一正式视图**中撤回若干
生效证据（同一期 / 早于证据均可撤回），最少撤回多少条才能让指定两地层的
**确定关系变为未定**，以及并列时应撤回哪一组。

口径：

- 只从“其余生效证据”中选择撤回：`protected_evidence` 为可为空的受保护生效
  证据编号集合，受保护证据不可撤回；
- 关系由“同一期（无向、可传递）+ 早于（有向）”混合图上的路径支撑。撤回只删
  边、不加边，因此关系变未定 ⇔ 混合图上两个方向的所有路径都被切断（对
  same_period 即两端同一期连通被切断；对 earlier_than/later_than 即该方向的
  全部混合路径被切断）；
- **同一期双向连通按一条证据撤回计数**：混合图中的同一期边是无向边，网络上
  建成正反两条容量 1 的弧，同一期证据的最小割恰为 1；
- 最少撤回数即该混合证据图上的 s–t 最小割（Dinic 单位容量，阻塞流用显式栈
  迭代搜索，千条连续证据的合法长链也不会递归过深）；并列时按证据编号序列
  字典序最小，通过对证据按编号排序后的逐条“强制保留 / 强制删去”可行性测试
  贪心求得（网络骨架一次构建、容量按测试复用，相关子图预过滤后长链仅常数次割计算）；
- 替代证据路径仍成立时不判失效：最小割必须切断全部路径，若受保护证据单独
  仍支撑某条路径（受保护子图仍连通），目标不可达，明确返回 `unreachable`
  并给出仍成立的（字典序最小）证据路径；
- 原关系已为未定时撤回数为 0、建议集合为空。

全程只读：`REPEATABLE READ READ ONLY` 快照事务内读取修订号、地层与生效断言，
不写库、不递增修订号、不产生变化记录；修订号过期（409 STALE_REVISION）、
地层无效（400 INVALID_STRATUM）、两地层相同（400 DUPLICATE_STRATUM）、
受保护证据不存在或非生效（400 INVALID_PROTECTED_EVIDENCE）明确拒绝。
"""
from __future__ import annotations

from collections import deque
from typing import Any, Dict, FrozenSet, List, Optional, Sequence, Tuple

from .db import db_conn
from .derivation import Assertion, Derivation, EARLIER, RELATION_LABELS, SAME, best_path, _build_adj
from .errors import ApiError
from .models import ResilienceIn

KINDS_MIXED = frozenset({SAME, EARLIER})
KINDS_SAME = frozenset({SAME})

INF = 10**9


# ------------------------------------------------------------------ 最小割（Dinic）
class _Edge:
    __slots__ = ("to", "rev", "cap")

    def __init__(self, to: int, rev: int, cap: int) -> None:
        self.to = to
        self.rev = rev
        self.cap = cap


class _Dinic:
    """有向单位容量网络最大流（阻塞流用显式栈迭代实现，不对图深做递归）。

    同一期证据在混合图中是无向边：按惯例加入 (u,v) 与 (v,u) 两条容量 1 的
    独立弧。其 s–t 割值恰为 1（割中该证据至多计一次——反向弧不可能同时穿过
    同一割），即“同一期双向连通按一条证据撤回计数”。

    约千条连续“早于”证据构成的合法长链会让递归式 DFS 触发递归过深，故
    阻塞流搜索全程迭代化：再长的合法链也不会产生 Python 调用栈增长。
    """

    def __init__(self, n: int) -> None:
        self.g: List[List[_Edge]] = [[] for _ in range(n)]

    def add_arc(self, u: int, v: int, cap: int = 1) -> None:
        self.g[u].append(_Edge(v, len(self.g[v]), cap))
        self.g[v].append(_Edge(u, len(self.g[u]) - 1, 0))

    def _bfs(self, s: int, t: int, level: List[int]) -> bool:
        for i in range(len(level)):
            level[i] = -1
        level[s] = 0
        q = deque([s])
        while q:
            v = q.popleft()
            for e in self.g[v]:
                if e.cap > 0 and level[e.to] < 0:
                    level[e.to] = level[v] + 1
                    q.append(e.to)
        return level[t] >= 0

    def _send_one(self, s: int, t: int, budget: int, level: List[int], it: List[int]) -> int:
        """在当前分层图上用显式栈找一条 s→t 增广路并推送流量（迭代版 DFS）。

        语义与递归实现逐帧等价：
        - 栈帧 (v, edge) 为当前节点及其在本帧选用的出边；
        - 沿“容量>0 且下一层”的边深入；到达 t 时取路径上最小残量推送，
          残量饱和的最深帧下次重入时通过当前弧指针换下一条边；
        - 无路可走的节点把当前弧指针置到末尾（等价递归版返回 0 后外层
          while 跳过该节点），回溯到父帧继续试探。
        """
        # path_v[i] / path_e[i]：增广路上的第 i 个节点与第 i 条边
        path_v: List[int] = [s]
        path_e: List[_Edge] = []
        while path_v:
            v = path_v[-1]
            if v == t:
                pushed = min(budget, min(e.cap for e in path_e))
                for e in path_e:
                    e.cap -= pushed
                    self.g[e.to][e.rev].cap += pushed
                return pushed
            advanced = False
            arcs = self.g[v]
            while it[v] < len(arcs):
                e = arcs[it[v]]
                if e.cap > 0 and level[e.to] == level[v] + 1:
                    path_v.append(e.to)
                    path_e.append(e)
                    advanced = True
                    break
                it[v] += 1
            if not advanced:
                # 等价递归版该节点返回 0：从路径上摘除，父帧当前边试探完毕后前进
                it[v] = len(arcs)
                path_v.pop()
                if path_e:
                    path_e.pop()
                if path_v:
                    it[path_v[-1]] += 1
        return 0

    def max_flow(self, s: int, t: int, limit: Optional[int] = None) -> int:
        flow = 0
        level = [-1] * len(self.g)
        cap_limit = limit if limit is not None else INF
        while flow < cap_limit and self._bfs(s, t, level):
            it = [0] * len(self.g)
            while flow < cap_limit:
                f = self._send_one(s, t, cap_limit - flow, level, it)
                if f == 0:
                    break
                flow += f
        return flow


def _mixed_neighbors(
    assertions: Sequence[Assertion],
) -> Tuple[Dict[str, List[str]], Dict[str, List[str]]]:
    """混合证据图的前向 / 反向邻接（同一期正反两向，早于仅前向）。"""
    out: Dict[str, List[str]] = {}
    back: Dict[str, List[str]] = {}
    for a in assertions:
        out.setdefault(a.a, []).append(a.b)
        back.setdefault(a.b, []).append(a.a)
        if a.relation == SAME:
            out.setdefault(a.b, []).append(a.a)
            back.setdefault(a.a, []).append(a.b)
    return out, back


def _reachable(start: str, adj: Dict[str, List[str]]) -> FrozenSet[str]:
    seen = {start}
    q = deque([start])
    while q:
        v = q.popleft()
        for w in adj.get(v, ()):  # 同一期 / 早于混合邻接
            if w not in seen:
                seen.add(w)
                q.append(w)
    return frozenset(seen)


class _CutProblem:
    """一次分析内可复用的混合证据割问题。

    网络骨架（节点编号、边规格、证据排序）只构建一次；贪心字典序选解时的
    每条“强制保留 / 强制删去”可行性测试只重置容量，不再重排节点与证据。

    节点预过滤：只保留“位于某条 s→t 混合路径上”的节点（从 s 前向可达 ∩
    能到达 t）。删边只可能让路径变少、强制保留不新增边，故该相关子图对
    所有可行性测试都充分，与 s–t 连通无关的证据永不进入割、也无需逐条测试。
    """

    def __init__(self, assertions: Sequence[Assertion], source: str, target: str) -> None:
        self.source = source
        self.target = target
        out, back = _mixed_neighbors(assertions)
        forward = _reachable(source, out)
        backward = _reachable(target, back)
        relevant = forward & backward
        nodes = sorted(relevant | {source, target})
        self.idx: Dict[str, int] = {s: i for i, s in enumerate(nodes)}
        # 网络只实例化一次：每条容量弧登记 (前向弧, 反向残量弧, 证据编号)。
        # 同一期证据登记两条方向相反的独立弧。可行性测试只重置容量，不再分配。
        self._net = _Dinic(len(nodes))
        self._arcs: List[Tuple["_Edge", "_Edge", str]] = []
        self.ids: List[str] = []
        seen_ids = set()
        for a in sorted(assertions, key=lambda x: x.evidence_id):
            if a.a not in self.idx or a.b not in self.idx:
                continue  # 不在任何 s→t 路径上：不可能进入割
            u, v = self.idx[a.a], self.idx[a.b]
            self._add_registered_arc(u, v, a.evidence_id)
            if a.relation == SAME:
                # 同一期无向双向连通：正反两条独立容量弧，撤回一条证据即切断
                self._add_registered_arc(v, u, a.evidence_id)
            if a.evidence_id not in seen_ids:
                seen_ids.add(a.evidence_id)
                self.ids.append(a.evidence_id)
        self.ids = tuple(self.ids)  # type: ignore[assignment]
        self._relevant_count = len(seen_ids)

    def _add_registered_arc(self, u: int, v: int, eid: str) -> None:
        before_v = len(self._net.g[v])
        before_u = len(self._net.g[u])
        self._net.add_arc(u, v, 1)
        fwd = self._net.g[u][-1]
        rev = self._net.g[v][before_v]
        assert fwd.rev == before_v and rev.rev == before_u
        self._arcs.append((fwd, rev, eid))

    def min_cut_value(
        self,
        forced_keep: FrozenSet[str] = frozenset(),
        forced_cut: FrozenSet[str] = frozenset(),
        max_needed: Optional[int] = None,
    ) -> int:
        """约束下的最小割值（INF 表示只能切断受保护 / 强制保留证据，目标不可达）。

        max_needed 给定时只需回答“割值是否 ≤ max_needed”：最大流最多推
        max_needed+1 即停（超限值本身按 INF 处理交给调用方比较），字典序贪心
        的每条可行性测试因此只做常数次分层而不必求满流。

        网络与弧对象在所有测试间复用：本方法只重置容量与残量（强制删去置 0、
        强制保留置 INF、其余为 1），千条证据 × 千次可行性测试也不重复分配。
        """
        cuttable = self._relevant_count - sum(
            1 for eid in self.ids if eid in forced_keep or eid in forced_cut
        )
        for fwd, rev, eid in self._arcs:
            if eid in forced_cut:
                fwd.cap = 0  # 强制删去：两个方向的容量都归零
            elif eid in forced_keep:
                fwd.cap = INF  # 强制保留：割不能经过它
            else:
                fwd.cap = 1
            rev.cap = 0
        s, t = self.idx[self.source], self.idx[self.target]
        # 可切断边总数 +1 作为“不可达”判定阈值：流量超过可切断边数必走保留边；
        # 调用方只关心是否不超过 max_needed 时再提前截断。
        limit = min(cuttable, max_needed) + 1 if max_needed is not None else cuttable + 1
        flow = self._net.max_flow(s, t, limit=limit)
        if flow > cuttable:
            return INF
        return flow

    def lexicographically_smallest_min_cut(
        self,
        protected: FrozenSet[str],
        cut_size: int,
    ) -> List[str]:
        """在所有规模为 cut_size 的最小撤回集合中，取证据编号序列字典序最小者。

        对可撤回证据按编号升序逐条贪心：尝试把当前编号**纳入**撤回集合（最小元素
        越小，排序后的序列字典序越小），若在“已选删去 + 当前编号”基础上仍能把割
        扩展到 cut_size 条（约束下的最小割值不超过剩余预算），则选它；否则它必须
        保留。已选满 cut_size 条后其余证据必保留，立即结束。全局最小性保证可行性
        测试中的最小割不会小于剩余预算。
        """
        kept: set = set(protected)
        cut: set = set()

        for eid in self.ids:
            if len(cut) >= cut_size:
                break  # 割已齐备，剩余证据全部保留
            if eid in protected:
                continue
            budget = cut_size - len(cut) - 1
            val = self.min_cut_value(
                forced_keep=kept,
                forced_cut=cut | {eid},
                max_needed=budget,
            )
            if val <= budget:
                cut.add(eid)
            else:
                kept.add(eid)
        return sorted(cut)


# ------------------------------------------------------------------ 承受力分析
def _path_details(rows_by_id: Dict[str, Tuple[str, str, str, str]], path: Optional[Sequence[str]]) -> List[Dict[str, Any]]:
    if not path:
        return []
    return [
        {
            "evidence_id": eid,
            "stratum_a": rows_by_id[eid][0],
            "stratum_b": rows_by_id[eid][1],
            "relation": rows_by_id[eid][2],
            "status": rows_by_id[eid][3],
        }
        for eid in path
        if eid in rows_by_id
    ]


def analyze_retraction_resilience(body: ResilienceIn) -> Dict[str, Any]:
    """只读分析：求让两地层关系变为未定所需的最少生效证据撤回。"""
    stratum_a, stratum_b = body.stratum_a, body.stratum_b
    if stratum_a == stratum_b:
        raise ApiError(
            400,
            "DUPLICATE_STRATUM",
            f"两个地层编号相同: {stratum_a}（须为两个不同的已登记地层）",
            {"stratum_id": stratum_a},
        )

    protected_input = sorted(set(body.protected_evidence))

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY ISOLATION LEVEL REPEATABLE READ")

            cur.execute("SELECT revision FROM revision_counter WHERE id = TRUE")
            current = cur.fetchone()[0]
            if body.expected_revision != current:
                raise ApiError(
                    409,
                    "STALE_REVISION",
                    f"修订号过期：请求携带 {body.expected_revision}，当前为 {current}",
                    {"current_revision": current},
                )

            cur.execute("SELECT stratum_id FROM strata WHERE stratum_id = ANY(%s)", ([stratum_a, stratum_b],))
            known = {r[0] for r in cur.fetchall()}
            for s in (stratum_a, stratum_b):
                if s not in known:
                    raise ApiError(400, "INVALID_STRATUM", f"地层未登记: {s}", {"stratum_id": s})

            cur.execute(
                "SELECT evidence_id, stratum_a, stratum_b, relation, status "
                "FROM assertions ORDER BY evidence_id"
            )
            all_rows = cur.fetchall()

    rows_by_id: Dict[str, Tuple[str, str, str, str]] = {r[0]: (r[1], r[2], r[3], r[4]) for r in all_rows}

    not_found = [eid for eid in protected_input if eid not in rows_by_id]
    not_active = [eid for eid in protected_input if eid in rows_by_id and rows_by_id[eid][3] != "active"]
    if not_found or not_active:
        raise ApiError(
            400,
            "INVALID_PROTECTED_EVIDENCE",
            "受保护证据无效：须为当前视图中生效的证据编号",
            {"not_found": not_found, "not_active": not_active},
        )

    protected = frozenset(protected_input)
    assertions = [
        Assertion(evidence_id=r[0], a=r[1], b=r[2], relation=r[3])
        for r in all_rows
        if r[4] == "active"
    ]
    derivation = Derivation(assertions)
    relation, original_path = derivation.relation(stratum_a, stratum_b)

    common: Dict[str, Any] = {
        "expected_revision": body.expected_revision,
        "current_revision": current,
        "revision": current,
        "stratum_a": stratum_a,
        "stratum_b": stratum_b,
        "protected_evidence": protected_input,
        "original_relation": relation,
        "original_relation_label": RELATION_LABELS[relation],
        "original_evidence_path": original_path or [],
        "original_path_detail": _path_details(rows_by_id, original_path),
    }

    if relation == "undetermined":
        common.update(
            {
                "reachable": True,
                "status": "already_undetermined",
                "minimum_retraction_count": 0,
                "suggested_retractions": [],
                "simulated_relation": "undetermined",
                "simulated_relation_label": RELATION_LABELS["undetermined"],
                "remaining_evidence_path": None,
            }
        )
        return common

    # 关系方向规整到混合图：later_than 时交换两端，使原证据路径方向与网络一致
    if relation == "later_than":
        source, target = stratum_b, stratum_a
    else:
        source, target = stratum_a, stratum_b

    # 受保护子图是否仍单独支撑该关系（可达 ⇒ 目标无法失效）
    kept = [a for a in assertions if a.evidence_id in protected]
    kinds = KINDS_SAME if relation == "same_period" else KINDS_MIXED
    surviving = best_path(_build_adj(kept, kinds), source, target)

    if surviving is not None:
        common.update(
            {
                "reachable": False,
                "status": "unreachable",
                "minimum_retraction_count": None,
                "suggested_retractions": [],
                "simulated_relation": relation,
                "simulated_relation_label": RELATION_LABELS[relation],
                "remaining_evidence_path": list(surviving),
                "remaining_path_detail": _path_details(rows_by_id, surviving),
            }
        )
        return common

    # 无受保护约束的全局最小割（force 全部受保护证据保留）
    problem = _CutProblem(assertions, source, target)
    cut_size = problem.min_cut_value(forced_keep=protected)
    if cut_size >= INF:
        # 理论不可达：受保护可达已在上面处理；防御性返回不可达
        common.update(
            {
                "reachable": False,
                "status": "unreachable",
                "minimum_retraction_count": None,
                "suggested_retractions": [],
                "simulated_relation": relation,
                "simulated_relation_label": RELATION_LABELS[relation],
                "remaining_evidence_path": None,
            }
        )
        return common

    chosen = problem.lexicographically_smallest_min_cut(protected, cut_size)

    # 模拟撤回：替代路径仍成立则关系不失效；最小割保证两个方向均被切断
    remaining = [a for a in assertions if a.evidence_id not in frozenset(chosen)]
    sim_relation, sim_path = Derivation(remaining).relation(stratum_a, stratum_b)

    common.update(
        {
            "reachable": True,
            "status": "retractions_found",
            "minimum_retraction_count": cut_size,
            "suggested_retractions": chosen,
            "simulated_relation": sim_relation,
            "simulated_relation_label": RELATION_LABELS[sim_relation],
            "simulated_evidence_path": sim_path or [],
            "simulated_path_detail": _path_details(rows_by_id, sim_path),
        }
    )
    return common
