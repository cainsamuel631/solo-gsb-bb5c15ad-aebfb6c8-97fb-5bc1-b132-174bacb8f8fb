"""跨 cue 的动态规划断句与约束校验。

输入：解析后的 cue 列表与三项阈值（每行最大字数 max_chars、
最小停留 min_dwell_ms、最大阅读速度 cps_max）。

处理思路：
1. 把相邻/重叠 cue 的 token 流拼接成"段"(segment)；cue 之间的间隙若
   小于等于 merge_gap_ms 也视为可跨句（时间轴轻微重叠或紧邻）。
2. 在段内用动态规划重排边界：候选切点为 token 边界，每个新 cue 的时长
   从原始时间轴按字符比例推算（尊重原始语速），并保证切点在时间轴上
   单调不减；目标是最小化「超字数、停留过短、阅读速度过快」三类惩罚。
3. 用最小代价回溯得到新 cue 列表；若某个 cue 在任意可行切点下都无法
   满足约束，将其记入 unsolvable，并给出具体原因（最小不可满足区段）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .parser import (
    Cue,
    NO_LEAD_PUNCT,
    detect_lang,
    is_latin_word,
    join_tokens,
    tokenize,
    wrap_lines,
)

INF = float("inf")
EPS = 1e-9


@dataclass
class Issue:
    cue_index: int                 # 修订后 cue 的下标（-1 表示属于段级问题）
    type: str                      # overlong / dwell / speed / unsolvable
    severity: str                  # error / warning
    message: str
    detail: Dict = field(default_factory=dict)


@dataclass
class CueResult:
    start_ms: int
    end_ms: int
    text: str
    original_indices: List[int]    # 由哪些原始 cue 的内容构成
    issues: List[Issue] = field(default_factory=list)

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms

    @property
    def cps(self) -> float:
        n = len(self.text.replace("\n", ""))
        return n * 1000.0 / max(self.duration_ms, 1)

    @property
    def max_line_len(self) -> int:
        return max((len(ln) for ln in self.text.split("\n")), default=0)


@dataclass
class AnalysisResult:
    cues: List[CueResult]
    issues: List[Issue]
    language: str
    stats: Dict


# ---------------------------------------------------------------------------
# 分段
# ---------------------------------------------------------------------------


@dataclass
class _Seg:
    """一个可在内部重新分配边界的连续区段。"""

    cue_ids: List[int]
    tokens: List[str]
    # 每个 token 的来源 cue
    owner: List[int]
    t0: int
    t1: int
    timeline: List[int]            # 每个 token 边界在时间轴上的位置


def _segments(cues: List[Cue], merge_gap_ms: int) -> List[_Seg]:
    """按时间轴把 cue 划分为可跨界重组的段。

    间隙 <= merge_gap_ms 的相邻 cue 合并处理；否则各自独立成段。
    """
    segments: List[_Seg] = []
    cur: Optional[_Seg] = None
    for idx, cue in enumerate(cues):
        toks = tokenize(cue.text)
        if not toks:
            continue
        if cur is None or cue.start_ms - cur.t1 > merge_gap_ms:
            cur = _Seg(
                cue_ids=[idx],
                tokens=list(toks),
                owner=[idx] * len(toks),
                t0=cue.start_ms,
                t1=cue.end_ms,
                timeline=[],
            )
            segments.append(cur)
        else:
            cur.cue_ids.append(idx)
            cur.tokens.extend(toks)
            cur.owner.extend([idx] * len(toks))
            # 时间轴可能重叠/包含，取外包区间
            cur.t0 = min(cur.t0, cue.start_ms)
            cur.t1 = max(cur.t1, cue.end_ms)
    for seg in segments:
        seg.timeline = _build_timeline(seg, cues)
    return segments


def _build_timeline(seg: _Seg, cues: List[Cue]) -> List[int]:
    """为每个 token 边界（含 0 与末尾）估算时间轴位置（毫秒）。

    先在每个原始 cue 内按 token 权重（拉丁词含尾随空格）线性插值，
    再用 owner 映射到全局下标。跨 cue 重叠时取较早值以保证单调不减。
    """
    n = len(seg.tokens)
    pos = [seg.t0] * (n + 1)

    # 每个 owner（原始 cue）在全局 token 数组中的 (first, last_inclusive)
    first_last: Dict[int, List[int]] = {}
    for gi, owner in enumerate(seg.owner):
        if owner not in first_last:
            first_last[owner] = [gi, gi]
        else:
            first_last[owner][1] = gi

    for cue_id, (first, last) in first_last.items():
        cue = cues[cue_id]
        ids = list(range(first, last + 1))
        weights = [_weight(seg.tokens[i]) for i in ids]
        total = sum(weights) or 1
        acc = 0
        for k, i in enumerate(ids):
            frac = acc / total
            pos[i] = max(pos[i], int(round(cue.start_ms + frac * (cue.end_ms - cue.start_ms))))
            acc += weights[k]
        pos[last + 1] = max(pos[last + 1], cue.end_ms)

    # 强制单调不减（处理重叠时间轴）
    for i in range(1, n + 1):
        if pos[i] < pos[i - 1]:
            pos[i] = pos[i - 1]
    # 段两端对齐
    pos[0] = seg.t0
    if pos[n] < seg.t1:
        pos[n] = max(seg.t1, pos[n - 1])
    return pos


def _weight(token: str) -> int:
    """token 在源 cue 文本中所占的显示宽度（拉丁词补尾随空格）。"""
    return len(token) + (1 if is_latin_word(token) else 0)


# ---------------------------------------------------------------------------
# 动态规划
# ---------------------------------------------------------------------------


@dataclass
class _Thresholds:
    max_chars: int
    min_dwell_ms: int
    cps_max: float
    merge_gap_ms: int
    max_lines: int = 2          # 每条 cue 最多行数（通用字幕规范，固定 2 行）
    soft_cps: float = 0.0      # 超过即计 warning（取阈值的 0.9）


def _cue_cost(
    tokens: List[str],
    start_ms: int,
    end_ms: int,
    th: _Thresholds,
) -> tuple:
    """返回 (总代价, 问题描述列表)。end<=start 视为不可行。"""
    if end_ms <= start_ms:
        return INF, []
    text = join_tokens(tokens)
    lines = wrap_lines(text, th.max_chars)
    over = sum(max(0, len(ln) - th.max_chars) for ln in lines)
    dwell = end_ms - start_ms
    char_count = len(text)
    cps = char_count * 1000.0 / dwell

    cost = 0.0
    problems = []

    if over:
        # 每个超长字符的代价较高，并对多行累计
        cost += over * 50
        longest = max(lines, key=len)
        problems.append(
            ("overlong", f"行长 {len(longest)} 字符，超过每行 {th.max_chars} 字符上限",
             {"max_line_len": len(longest), "limit": th.max_chars, "lines": lines})
        )

    # 一条 cue 最多 max_lines 行；超出意味着必须拆分（在可拆分时由 DP 处理，
    # 真正无法拆分时成为"最小区段"问题）
    extra_lines = max(0, len(lines) - th.max_lines)
    if extra_lines:
        cost += extra_lines * 400
        longest = max(lines, key=len)
        problems.append(
            ("overlong", f"共 {len(lines)} 行，超过每 cue 最多 {th.max_lines} 行上限"
             f"（每行 {th.max_chars} 字），需拆分到相邻 cue",
             {"max_line_len": len(longest), "limit": th.max_chars,
              "lines": lines, "line_count": len(lines), "max_lines": th.max_lines})
        )

    if dwell < th.min_dwell_ms:
        deficit = th.min_dwell_ms - dwell
        cost += deficit * 0.2
        problems.append(
            ("dwell", f"停留 {dwell} ms，短于最小停留 {th.min_dwell_ms} ms",
             {"duration_ms": dwell, "min_dwell_ms": th.min_dwell_ms})
        )

    if cps > th.cps_max + EPS:
        excess = cps - th.cps_max
        cost += excess * 8
        problems.append(
            ("speed", f"阅读速度 {cps:.1f} 字符/秒，超过 {th.cps_max:g} 字符/秒上限",
             {"cps": round(cps, 2), "cps_max": th.cps_max, "chars": char_count})
        )
    elif cps > th.soft_cps + EPS:
        problems.append(
            ("speed_warn", f"阅读速度 {cps:.1f} 字符/秒，接近上限 {th.cps_max:g} 字符/秒",
             {"cps": round(cps, 2), "cps_max": th.cps_max, "chars": char_count})
        )

    # 轻微惩罚：cue 数量偏离原始 cue 数（鼓励不要无谓拆分/合并）
    return cost, problems


def _solve_segment(seg: _Seg, th: _Thresholds):
    """对单个段做 DP。

    状态 dp[i] = 把前 i 个 token 切分完毕的最小代价；
    转移 dp[j] = min over i<j of dp[i] + cost(tokens[i:j], t[i], t[j])。
    为控制复杂度，j 向前最多回溯 max_lookback 个 token。

    返回 (切分点列表, 每段 problems, 是否存在不可满足)。
    """
    n = len(seg.tokens)
    tl = seg.timeline

    # 结构代价参数：
    #   NEW_CUE     —— 每增加一个新 cue 的固定代价（鼓励少切）
    #   CROSS_BOUND —— 切点跨越原始 cue 边界的代价（鼓励沿用原边界）
    # 只在违规惩罚（overlong 50/字、dwell 0.2/ms、speed 8 倍）足以
    # 抵偿结构代价时，DP 才会拆分或跨 cue 重分配。
    NEW_CUE = 5.0
    CROSS_BOUND = 8.0
    START_PUNCT_PENALTY = 100.0  # 新 cue 以句末标点开头（切点错位）的代价

    # owner 变化次数的前缀和：change_pref[k] = 前 k 个 token 处的边界数
    change_pref = [0] * (n + 1)
    for k in range(1, n):
        change_pref[k + 1] = change_pref[k] + (1 if seg.owner[k] != seg.owner[k - 1] else 0)

    # 单条新 cue 的合理字符上限（允许比 max_lines 多一行，否则 DP 无法
    # 评估"放不下"的候选；超出行数会受到重罚）
    longest_token = max((len(t) for t in seg.tokens), default=0)
    hard_cap = max(th.max_chars * (th.max_lines + 1), longest_token)
    max_lookback = max(th.max_chars * 3, 60)

    # 第一刀必须不早于…… 不做约束；但要求时间非递减（timeline 已单调）
    dp = [INF] * (n + 1)
    back = [-1] * (n + 1)
    dp[0] = 0.0
    best_problems: Dict[int, list] = {}

    for j in range(1, n + 1):
        lo = max(0, j - max_lookback)
        best_i = -1
        best_c = INF
        best_p: list = []
        acc_len = 0
        for i in range(j - 1, lo - 1, -1):
            tok = seg.tokens[i]
            acc_len += _weight(tok)
            if acc_len > hard_cap and i < j - 1:
                break
            if dp[i] >= INF:
                continue
            c, probs = _cue_cost(seg.tokens[i:j], tl[i], tl[j], th)
            if c >= INF:
                continue
            crosses = change_pref[j] - change_pref[i + 1]
            # 切点不应落在句末标点之前（避免新 cue 以 ". ," 等开头）
            start_penalty = START_PUNCT_PENALTY if i > 0 and seg.tokens[i] in NO_LEAD_PUNCT else 0.0
            total = dp[i] + c + NEW_CUE + crosses * CROSS_BOUND + start_penalty
            if total < best_c - 1e-9:
                best_c = total
                best_i = i
                best_p = probs
        if best_i < 0:
            # 兜底：退化为单 token cue（即使违规也要产出切分）
            i = j - 1
            c, probs = _cue_cost(seg.tokens[i:j], tl[i], tl[j], th)
            if c >= INF or dp[i] >= INF:
                # 时间轴完全坍塌，强制 1ms
                probs = [
                    ("unsolvable", "该位置时间轴无可用时长，无法安排任何停留",
                     {"token": seg.tokens[i]})
                ]
                c = 10_000
                if dp[i] >= INF:
                    dp[i] = 10_000 * i  # 给个有限值使回溯可继续
            dp[j] = dp[i] + c
            back[j] = i
            best_problems[j] = probs
        else:
            dp[j] = best_c
            back[j] = best_i
            best_problems[j] = best_p

    # 回溯
    cuts = [n]
    j = n
    while back[j] > 0:
        j = back[j]
        cuts.append(j)
    cuts.reverse()
    cuts = [0] + cuts if cuts[0] != 0 else cuts
    return cuts, best_problems


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def analyze(
    cues: List[Cue],
    max_chars: int = 21,
    min_dwell_ms: int = 1000,
    cps_max: float = 17.0,
    merge_gap_ms: int = 250,
) -> AnalysisResult:
    """执行断句重排与校验。"""
    th = _Thresholds(
        max_chars=max_chars,
        min_dwell_ms=min_dwell_ms,
        cps_max=cps_max,
        merge_gap_ms=merge_gap_ms,
        soft_cps=cps_max * 0.9,
    )

    all_text = " ".join(c.text for c in cues)
    language = detect_lang(all_text)

    segments = _segments(cues, merge_gap_ms)
    results: List[CueResult] = []
    issues: List[Issue] = []

    for seg in segments:
        cuts, prob_map = _solve_segment(seg, th)

        for k in range(len(cuts) - 1):
            i, j = cuts[k], cuts[k + 1]
            toks = seg.tokens[i:j]
            s, e = seg.timeline[i], seg.timeline[j]
            if e <= s:
                e = s + 1
            text = join_tokens(toks)
            lines = wrap_lines(text, max_chars)
            owners = sorted(set(seg.owner[i:j]))
            cue_index = len(results)
            cr = CueResult(
                start_ms=s,
                end_ms=e,
                text="\n".join(lines),
                original_indices=owners,
            )

            had_error = False
            for ptype, msg, detail in prob_map.get(j, []):
                if ptype == "speed_warn":
                    iss = Issue(cue_index, "speed", "warning", msg, detail)
                elif ptype == "unsolvable":
                    iss = Issue(cue_index, "unsolvable", "error", msg, detail)
                    had_error = True
                else:
                    iss = Issue(cue_index, ptype, "error", msg, detail)
                    had_error = True
                cr.issues.append(iss)
                issues.append(iss)

            # 仍有硬伤的 cue 即「无法满足约束的最小区段」：DP 已把切分
            # 细化到最小代价粒度，附上区段范围与原因解释。
            if had_error:
                name_map = {
                    "overlong": "行长",
                    "dwell": "停留时间",
                    "speed": "阅读速度",
                    "unsolvable": "时间轴",
                }
                violated = list(dict.fromkeys(
                    name_map.get(x.type, x.type)
                    for x in cr.issues if x.severity == "error"
                ))
                for iss_ in cr.issues:
                    if iss_.severity != "error":
                        continue
                    iss_.detail.setdefault("original_cue_range", [owners[0], owners[-1]])
                    iss_.detail.setdefault("segment_text", text)
                    iss_.detail.setdefault(
                        "reason_summary",
                        "该最小区段在当前时间轴下无论边界如何重分配，"
                        f"仍无法满足：{'、'.join(violated)} 约束",
                    )
            results.append(cr)

    stats = _build_stats(cues, results, issues)
    return AnalysisResult(cues=results, issues=issues, language=language, stats=stats)


def _build_stats(orig: List[Cue], results: List[CueResult], issues: List[Issue]) -> Dict:
    errors = [i for i in issues if i.severity == "error"]
    warnings = [i for i in issues if i.severity == "warning"]
    by_type: Dict[str, int] = {}
    for i in issues:
        by_type[i.type] = by_type.get(i.type, 0) + 1
    return {
        "original_cue_count": len(orig),
        "revised_cue_count": len(results),
        "error_count": len(errors),
        "warning_count": len(warnings),
        "by_type": by_type,
        "total_duration_ms": max((c.end_ms for c in results), default=0)
        - min((c.start_ms for c in results), default=0),
    }


def render_webvtt(result: AnalysisResult) -> str:
    """把修订后的 cue 渲染为 WebVTT。"""
    from .parser import format_timestamp

    out = ["WEBVTT", ""]
    for i, cr in enumerate(result.cues, 1):
        out.append(str(i))
        out.append(f"{format_timestamp(cr.start_ms)} --> {format_timestamp(cr.end_ms)}")
        out.extend(cr.text.split("\n"))
        out.append("")
    return "\n".join(out)
