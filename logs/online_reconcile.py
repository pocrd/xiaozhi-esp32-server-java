#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""线上全流程对帐脚本（TTS / STT / LLM）。

从 ``logs/dialogue/xiaozhi-dialogue*.log`` 中提取 TTS / STT / LLM 三类对帐日志
（统一为 ``[KIND] xxx对帐 - Key: value, ...`` 结构）：

    [TTS] 语音合成对帐 - SessionId: <sid>, DeviceId: <did>, RequestId: <rid>, Chars: <n>
    [STT] 语音识别对帐 - SessionId: <sid>, DeviceId: <did>, RequestId: <rid>, Chars: <n>
    [LLM] 大模型对帐 - SessionId: <sid>, DeviceId: <did>, RequestId: <rid>, Model: <m>, PromptTokens: <p>, CompletionTokens: <c>, TotalTokens: <t>

与阿里云百炼审计日志对帐。为避免每次联网查询，云端数据按天缓存到
``logs/aliyun_data/audit-YYYY-MM-DD.jsonl``，对帐阶段纯离线。

匹配策略（按 kind 分流）：
  - LLM：metadata.getId() 即服务端 request_id，走 requestId 精确匹配（失败自动回退时间窗）。
  - TTS/STT：DashScope WS 的 RequestId 是客户端随机 ID、不落审计库，改走「时间窗(+字符数)」匹配。

子命令：
  summary    扫描本地日志并汇总（天/设备/会话维度）
  pull       从 SLS 拉取云端审计日志到本地缓存（按天增量）
  reconcile  默认先自动 pull（增量，已完整缓存的天会跳过），再本地 vs 云端对帐；--no-pull 走纯离线
  report     默认全流程：同步 → 全量对帐(TTS/STT/LLM) → 导出 HTML 报告

依赖：
  - summary / reconcile：仅 Python 3.8+ 标准库
  - pull：额外需要 ``pip install aliyun-log-python-sdk``

常用示例：
    # 1) 汇总本地日志
    python3 online_reconcile.py summary
    python3 online_reconcile.py summary --since 2026-09-01 --device device031 --csv detail.csv

    # 2) 拉取云端审计日志（自动识别本地日志日期 − 已缓存日期 = 待拉取）
    python3 online_reconcile.py pull
    python3 online_reconcile.py pull --day 2026-09-23 --day 2026-09-24
    python3 online_reconcile.py pull --force       # 已缓存的天也重拉（去重合并）

    # 3) 本地对帐（默认先自动增量 pull；--no-pull 走纯离线）
    python3 online_reconcile.py reconcile --reconcile-csv diff.csv
    python3 online_reconcile.py reconcile --no-pull --since 2026-09-23 --device device031
    python3 online_reconcile.py reconcile --kind TTS --match-by time --time-tolerance 3
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import os
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, asdict, field
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple

# --------------------------------------------------------------------------- #
# 日志解析
# --------------------------------------------------------------------------- #

# 完整日志行示例（三类统一为 "[KIND] xxx对帐 - Key: value, ..." 结构）：
# 2026-09-23 11:05:24.844 [loomBoundedElastic-1] INFO  c.x.d.playback.FileSynthesizer:69 - [TTS] 语音合成对帐 - SessionId: xxx, DeviceId: yyy, RequestId: zzz, Chars: 42
# ... [STT] 语音识别对帐 - SessionId: xxx, DeviceId: yyy, RequestId: zzz, Chars: 7
# ... [LLM] 大模型对帐 - SessionId: xxx, DeviceId: yyy, RequestId: zzz, Model: qwen-plus, PromptTokens: 30, CompletionTokens: 12, TotalTokens: 42
# 前缀只抓 时间戳/类型/label，"对帐 -" 之后的字段用通用 Key: value 解析（见 _parse_kv），便于扩展。
LINE_RE = re.compile(
    r"""^(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?)   # 时间戳
        .*?                                                            # 线程/级别/类名
        \[(?P<kind>TTS|STT|LLM)\]\s*                                   # 对帐类型
        (?P<label>[^-\n]*?)对帐\s*-\s*                                 # "语音合成对帐 -"
        (?P<rest>.*)$                                                  # 其后的 Key: value 串
    """,
    re.VERBOSE,
)


def _parse_kv(rest: str) -> Dict[str, str]:
    """把 'SessionId: a, DeviceId: b, Chars: 12' 解析为规范化键的字典。

    键统一小写并去除空格/下划线/连字符（复用 _norm_key），故 sessionId/session_id 等价。
    """
    out: Dict[str, str] = {}
    for part in (rest or "").split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        k, _, v = part.partition(":")
        k = _norm_key(k)
        if k:
            out[k] = v.strip()
    return out

TS_FORMATS: Tuple[str, ...] = (
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S",
)


def _parse_ts(raw: str) -> Optional[datetime]:
    text = raw.strip().replace("T", " ")
    for fmt in TS_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    # 兜底：截取到秒
    try:
        return datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


@dataclass(frozen=True)
class TtsRecord:
    timestamp: datetime
    kind: str          # TTS / STT / LLM
    label: str         # 例如 "语音合成"
    session_id: str
    device_id: str
    request_id: str
    source: str        # 日志文件路径
    line_no: int       # 1-based 行号
    # 用量字段（按 kind 含义不同）：
    chars: Optional[int] = None              # TTS 合成字符数 / STT 识别文本字符数
    model: Optional[str] = None              # LLM 模型名
    prompt_tokens: Optional[int] = None      # LLM 输入 token
    completion_tokens: Optional[int] = None  # LLM 输出 token
    total_tokens: Optional[int] = None       # LLM 总 token

    @property
    def day(self) -> str:
        return self.timestamp.strftime("%Y-%m-%d")

    @property
    def hour(self) -> str:
        return self.timestamp.strftime("%Y-%m-%d %H:00")


def iter_log_files(paths: Sequence[Path]) -> Iterator[Path]:
    for p in paths:
        if p.is_dir():
            # 目录：递归匹配 *.log / *.log.*（覆盖滚动归档）
            for f in sorted(p.rglob("*.log*")):
                if f.is_file():
                    yield f
        elif p.is_file():
            yield p
        else:
            print(f"[warn] 跳过不存在的路径：{p}", file=sys.stderr)


def parse_file(path: Path, kinds: Iterable[str]) -> Iterator[TtsRecord]:
    kind_set = {k.upper() for k in kinds}
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line_no, line in enumerate(fh, 1):
                if "对帐" not in line:
                    continue
                m = LINE_RE.match(line)
                if not m:
                    continue
                kind = m.group("kind").upper()
                if kind not in kind_set:
                    continue
                ts = _parse_ts(m.group("ts"))
                if ts is None:
                    print(f"[warn] 无法解析时间戳：{path}:{line_no}", file=sys.stderr)
                    continue
                kv = _parse_kv(m.group("rest"))
                rid = kv.get("requestid", "")
                if rid == "-":
                    rid = ""
                yield TtsRecord(
                    timestamp=ts,
                    kind=kind,
                    label=m.group("label").strip(),
                    session_id=kv.get("sessionid", ""),
                    device_id=kv.get("deviceid", ""),
                    request_id=rid,
                    source=str(path),
                    line_no=line_no,
                    chars=_to_int(kv.get("chars")),
                    model=kv.get("model") or None,
                    prompt_tokens=_to_int(kv.get("prompttokens")),
                    completion_tokens=_to_int(kv.get("completiontokens")),
                    total_tokens=_to_int(kv.get("totaltokens")),
                )
    except OSError as e:
        print(f"[warn] 读取失败 {path}: {e}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# 过滤与聚合
# --------------------------------------------------------------------------- #

def _parse_day(text: str) -> date:
    return datetime.strptime(text, "%Y-%m-%d").date()


def filter_records(
    records: Iterable[TtsRecord],
    since: Optional[date],
    until: Optional[date],
    devices: Optional[Sequence[str]],
    sessions: Optional[Sequence[str]],
) -> List[TtsRecord]:
    dev_set = set(devices) if devices else None
    ses_set = set(sessions) if sessions else None
    out: List[TtsRecord] = []
    for r in records:
        d = r.timestamp.date()
        if since and d < since:
            continue
        if until and d > until:
            continue
        if dev_set and r.device_id not in dev_set:
            continue
        if ses_set and r.session_id not in ses_set:
            continue
        out.append(r)
    out.sort(key=lambda x: x.timestamp)
    return out


DimKey = Tuple[str, ...]


def aggregate(records: Sequence[TtsRecord], dims: Sequence[str]) -> "dict[DimKey, Counter]":
    """按给定维度组合聚合，返回 {dim_key: Counter(metric -> value)}。"""
    grouped: "dict[DimKey, Counter]" = defaultdict(Counter)
    for r in records:
        key_parts: List[str] = []
        for d in dims:
            if d == "day":
                key_parts.append(r.day)
            elif d == "hour":
                key_parts.append(r.hour)
            elif d == "device":
                key_parts.append(r.device_id)
            elif d == "session":
                key_parts.append(r.session_id)
            elif d == "kind":
                key_parts.append(r.kind)
            else:
                raise ValueError(f"未知聚合维度：{d}")
        key = tuple(key_parts)
        c = grouped[key]
        c["requests"] += 1
        c["unique_requests"] = len({r.request_id})  # 会被下面覆盖，占位
        # 独立收集 unique
    # 二次扫描计算独立 requestId / sessionId
    unique_req: "dict[DimKey, set]" = defaultdict(set)
    unique_ses: "dict[DimKey, set]" = defaultdict(set)
    unique_dev: "dict[DimKey, set]" = defaultdict(set)
    for r in records:
        key_parts: List[str] = []
        for d in dims:
            if d == "day":
                key_parts.append(r.day)
            elif d == "hour":
                key_parts.append(r.hour)
            elif d == "device":
                key_parts.append(r.device_id)
            elif d == "session":
                key_parts.append(r.session_id)
            elif d == "kind":
                key_parts.append(r.kind)
        key = tuple(key_parts)
        unique_req[key].add(r.request_id)
        unique_ses[key].add(r.session_id)
        unique_dev[key].add(r.device_id)
    for key, c in grouped.items():
        c["unique_requests"] = len(unique_req[key])
        c["unique_sessions"] = len(unique_ses[key])
        c["unique_devices"] = len(unique_dev[key])
    return grouped


def find_duplicate_request_ids(records: Sequence[TtsRecord]) -> "dict[str, List[TtsRecord]]":
    buckets: "dict[str, List[TtsRecord]]" = defaultdict(list)
    for r in records:
        buckets[r.request_id].append(r)
    return {rid: rs for rid, rs in buckets.items() if len(rs) > 1}


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #

def print_summary(records: Sequence[TtsRecord], top_n: int) -> None:
    if not records:
        print("未匹配到任何对帐日志。请检查 --log-glob / --since / --until / --device 参数。")
        return
    first, last = records[0], records[-1]
    unique_req = {r.request_id for r in records}
    unique_ses = {r.session_id for r in records}
    unique_dev = {r.device_id for r in records}
    print("=" * 78)
    print(f"总记录数        : {len(records)}")
    print(f"唯一 RequestId  : {len(unique_req)}" + ("" if len(unique_req) == len(records) else "  (存在重复!)"))
    print(f"唯一 SessionId  : {len(unique_ses)}")
    print(f"唯一 DeviceId   : {len(unique_dev)}")
    print(f"时间范围        : {first.timestamp}  →  {last.timestamp}")
    print(f"日志文件数      : {len({r.source for r in records})}")
    print("=" * 78)

    per_day = Counter(r.day for r in records)
    print("按天统计：")
    for day in sorted(per_day):
        print(f"  {day}  {per_day[day]:>6} 条")

    print(f"\nTop {top_n} 设备（按请求数）：")
    for dev, cnt in Counter(r.device_id for r in records).most_common(top_n):
        print(f"  {dev:<24}  {cnt:>6}")

    dups = find_duplicate_request_ids(records)
    if dups:
        print(f"\n[!] 检测到 {len(dups)} 个重复 RequestId（示例最多 5 个）：")
        for rid, rs in list(dups.items())[:5]:
            stamps = ", ".join(str(x.timestamp) for x in rs)
            print(f"  {rid}  ×{len(rs)}  @ {stamps}")
    print()


def print_usage_summary(records: Sequence[TtsRecord]) -> None:
    """按 kind 汇总本地用量（TTS/STT 字符数、LLM tokens），不跨单位相加。"""
    by_kind: Dict[str, List[TtsRecord]] = defaultdict(list)
    for r in records:
        by_kind[r.kind].append(r)
    lines: List[str] = []
    for kind in ("TTS", "STT", "LLM"):
        rs = by_kind.get(kind)
        if not rs:
            continue
        if kind in ("TTS", "STT"):
            chars = [r.chars for r in rs if r.chars is not None]
            lines.append(f"  [{kind:<3}] 记录={len(rs):<5} 带字符数={len(chars):<5} 字符总数={sum(chars)}")
        else:
            pt = sum(r.prompt_tokens or 0 for r in rs)
            ct = sum(r.completion_tokens or 0 for r in rs)
            tt = sum(r.total_tokens or 0 for r in rs)
            lines.append(f"  [{kind:<3}] 记录={len(rs):<5} prompt={pt}  completion={ct}  total={tt}")
    if lines:
        print("本地用量汇总（按 kind，不跨单位相加）：")
        for ln in lines:
            print(ln)
        print()


def print_grouped(records: Sequence[TtsRecord], dims: Sequence[str], top_n: int) -> None:
    grouped = aggregate(records, dims)
    header = " | ".join(dims) + " || requests | uniq_req | uniq_ses | uniq_dev"
    print("-" * len(header))
    print(header)
    print("-" * len(header))
    rows = sorted(grouped.items(), key=lambda kv: (-kv[1]["requests"], kv[0]))
    for key, c in rows[:top_n] if top_n > 0 else rows:
        print(" | ".join(key) + f" || {c['requests']:>8} | {c['unique_requests']:>8} | "
                                 f"{c['unique_sessions']:>8} | {c['unique_devices']:>8}")
    if top_n > 0 and len(rows) > top_n:
        print(f"... 其余 {len(rows) - top_n} 组已省略（--top 0 查看全部）")
    print()


def export_csv(records: Sequence[TtsRecord], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow([
            "timestamp", "date", "hour", "kind", "label",
            "session_id", "device_id", "request_id", "source", "line_no",
        ])
        for r in records:
            w.writerow([
                r.timestamp.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                r.day, r.hour, r.kind, r.label,
                r.session_id, r.device_id, r.request_id, r.source, r.line_no,
            ])
    print(f"[ok] 已导出 CSV：{path}（{len(records)} 行）")


def export_json(records: Sequence[TtsRecord], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = []
    for r in records:
        d = asdict(r)
        d["timestamp"] = r.timestamp.isoformat(timespec="milliseconds")
        d["day"] = r.day
        d["hour"] = r.hour
        payload.append(d)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    print(f"[ok] 已导出 JSON：{path}（{len(records)} 条）")


def export_request_ids(records: Sequence[TtsRecord], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    seen = set()
    lines: List[str] = []
    for r in records:
        if r.request_id in seen:
            continue
        seen.add(r.request_id)
        lines.append(r.request_id)
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    print(f"[ok] 已导出 RequestId 列表：{path}（{len(lines)} 个）")


# --------------------------------------------------------------------------- #
# SLS 对帐（阿里云百炼审计日志）
# --------------------------------------------------------------------------- #

def load_env_file(path: Path) -> Dict[str, str]:
    """宽容解析 .env：同时支持 ``Key: value`` / ``Key=value`` / ``export Key=value``。"""
    env: Dict[str, str] = {}
    if not path or not path.exists():
        return env
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        for sep in (":", "="):
            if sep in line:
                k, _, v = line.partition(sep)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k:
                    env[k] = v
                break
    return env


def _norm_key(k: str) -> str:
    return re.sub(r"[\s_\-]+", "", k).lower()


def env_lookup(env: Dict[str, str], *aliases: str) -> Optional[str]:
    """大小写/下划线/空格不敏感的多别名查找；再兜底到 os.environ。"""
    norm = {_norm_key(k): v for k, v in env.items()}
    for a in aliases:
        v = norm.get(_norm_key(a))
        if v:
            return v
    for a in aliases:
        v = os.environ.get(a)
        if v:
            return v
    return None


def parse_logstore_path(text: str) -> Tuple[str, str]:
    """把 ``project/logstore`` 拆成 (project, logstore)。"""
    text = (text or "").strip()
    if "/" not in text:
        raise ValueError(f"LogStore 应为 'project/logstore' 格式，实际：{text!r}")
    proj, _, ls = text.partition("/")
    proj, ls = proj.strip(), ls.strip()
    if not proj or not ls:
        raise ValueError(f"LogStore 项目名/日志库名不能为空：{text!r}")
    return proj, ls


def infer_endpoint_from_project(project: str, default_region: str = "cn-beijing") -> str:
    """从 project 名中推断 region，返回 SLS Endpoint。"""
    m = re.search(r"(cn-[a-z]+(?:-\d+)?|ap-[a-z\-]+|us-[a-z\-]+|eu-[a-z\-]+|me-[a-z\-]+)", project)
    region = m.group(1) if m else default_region
    return f"{region}.log.aliyuncs.com"


@dataclass(frozen=True)
class RemoteAuditRecord:
    """阿里云百炼审计日志（SLS）中的一条记录。

    用量字段按百炼 usage JSON 的常见结构拆分抽取，避免不同计费单位混用：
      - ``characters``    ：仅当模型确实按字符计费时才有值（如 cosyvoice 的 usage.characters）
      - ``input_tokens`` / ``output_tokens`` / ``total_tokens``：token 计费用的模型（LLM、qwen-audio-tts 等）
      - ``duration_sec``  ：按秒计费的模型（paraformer 等 STT）
    历史上 ``characters`` 曾回退到 total_tokens，造成 LLM 成本被高估 1000 倍，已修正为严格提取。
    """
    request_id: str
    timestamp: Optional[datetime]
    model: Optional[str]
    status: Optional[str]
    error_code: Optional[str]
    characters: Optional[int]
    api_key: Optional[str]
    workspace: Optional[str]
    raw: Dict[str, Any] = field(default_factory=dict)
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    duration_sec: Optional[float] = None


def _to_int(v: Any) -> Optional[int]:
    if v is None or v == "":
        return None
    if isinstance(v, int):
        return v
    m = re.search(r"-?\d+", str(v))
    return int(m.group(0)) if m else None


def _pick(d: Dict[str, Any], *names: str) -> Optional[str]:
    lower = {_norm_key(k): v for k, v in d.items()}
    for n in names:
        v = lower.get(_norm_key(n))
        if v not in (None, ""):
            return str(v)
    return None


def _extract_usage(raw: Dict[str, Any]) -> Dict[str, Any]:
    """从百炼审计记录的 ``usage`` JSON / 顶层字段中抽取各类用量，不回退不混用。

    返回字典包含：``characters`` / ``input_tokens`` / ``output_tokens`` / ``total_tokens`` / ``duration_sec``，
    没有的项为 None。典型样本：
      - paraformer-realtime-v2  → ``{"duration":3}``                       → duration_sec=3
      - qwen3.5-flash           → ``{"input_tokens":961,"output_tokens":82,"total_tokens":1043}``
      - qwen-audio-3.1-tts-flash→ ``{"input_tokens":73,"output_tokens":213,"total_tokens":286,"characters":137}``
      - cosyvoice-v3-flash      → ``{"characters":123}``                    → characters=123
    """
    out: Dict[str, Any] = {
        "characters": None, "input_tokens": None, "output_tokens": None,
        "total_tokens": None, "duration_sec": None,
    }
    # 1) 先试顶层字段
    for src_key, dst_key in (("characters", "characters"),
                              ("input_tokens", "input_tokens"),
                              ("output_tokens", "output_tokens"),
                              ("total_tokens", "total_tokens")):
        n = _to_int(_pick(raw, src_key))
        if n is not None:
            out[dst_key] = n
    # 2) 再试 usage JSON
    usage_raw = _pick(raw, "usage")
    usage_obj: Dict[str, Any] = {}
    if usage_raw:
        try:
            parsed = json.loads(usage_raw)
            if isinstance(parsed, dict):
                usage_obj = parsed
        except (ValueError, TypeError):
            usage_obj = {}
    for src_key, dst_key in (("characters", "characters"),
                              ("input_tokens", "input_tokens"),
                              ("output_tokens", "output_tokens"),
                              ("total_tokens", "total_tokens"),
                              ("duration", "duration_sec")):
        if out[dst_key] is None:
            v = usage_obj.get(src_key)
            if dst_key == "duration_sec":
                try:
                    out[dst_key] = float(v) if v is not None else None
                except (ValueError, TypeError):
                    out[dst_key] = None
            else:
                out[dst_key] = _to_int(v)
    # total_tokens 如果缺失但有 input/output，推导一下（仅用于展示，不影响计费）
    if out["total_tokens"] is None and out["input_tokens"] is not None and out["output_tokens"] is not None:
        out["total_tokens"] = out["input_tokens"] + out["output_tokens"]
    return out


def _extract_characters(raw: Dict[str, Any]) -> Optional[int]:
    """只返回真正的字符数，不再回退到 tokens（历史 bug：造成 LLM 成本高估 1000×）。"""
    return _extract_usage(raw).get("characters")


# --------------------------------------------------------------------------- #
# 模型单价表（以阿里云百炼 2026 年公开定价/账单反推为依据）
#
# kind 取值：
#   token   ：按 input/output 分别计费（元/百万 tokens）
#   token_blended：按 total_tokens 单一单价计费（元/百万 tokens）
#   chars   ：按万字符计费（元/万字符）
#   seconds ：按秒计费（元/秒）
#
# 默认值来源：
#   - qwen-audio-3.1-tts-flash：已写入 memory（输入 1.5/百万，输出 12/百万）
#   - qwen3.5-flash：从账单 CSV 反推（0.0002 元/千tokens = 0.2 元/百万 tokens）
#   - cosyvoice-*：账单显示 Cosy语音合成定价 1元/万字
#   - paraformer-realtime-v2：账单显示免费额度，单价未知，默认采用官方标价 0.00024元/秒
# 可通过 --price-file 指定 JSON 覆盖（结构：{"<model>": {"kind":..., ...}}）。
MODEL_PRICES: Dict[str, Dict[str, Any]] = {
    # TTS—token 计费（百聆语音系列）
    "qwen-audio-3.1-tts-flash": {"kind": "token", "input": 1.5, "output": 12.0},
    "qwen3-tts-flash":          {"kind": "token", "input": 1.5, "output": 12.0},
    "qwen3-tts-instruct-flash": {"kind": "token", "input": 1.5, "output": 12.0},
    # TTS—字符计费
    "cosyvoice-v3-flash":       {"kind": "chars", "price": 1.0},
    "cosyvoice-v2":             {"kind": "chars", "price": 2.0},
    "cosyvoice-v1":             {"kind": "chars", "price": 2.0},
    "sambert-zhichu-v1":        {"kind": "chars", "price": 0.8},
    # STT—秒计费
    "paraformer-realtime-v2":   {"kind": "seconds", "price": 0.00024},
    "paraformer-realtime-v1":   {"kind": "seconds", "price": 0.00024},
    "paraformer-v2":            {"kind": "seconds", "price": 0.00024},
    "gummy-realtime-v1":        {"kind": "seconds", "price": 0.00033},
    # LLM
    "qwen3.5-flash":            {"kind": "token_blended", "price": 0.2},   # 账单反推
    "qwen-flash":               {"kind": "token", "input": 0.15, "output": 1.5},
    "qwen-turbo":               {"kind": "token", "input": 0.3,  "output": 0.6},
    "qwen-plus":                {"kind": "token", "input": 0.8,  "output": 2.0},
    "qwen-max":                 {"kind": "token", "input": 20.0, "output": 60.0},
    "qwen3.8-max":              {"kind": "token", "input": 20.0, "output": 60.0},
}
# 未录入模型的保守默认（按 kind 分类），避免静默漏算。
DEFAULT_PRICE_BY_KIND: Dict[str, Dict[str, Any]] = {
    "TTS": {"kind": "chars",   "price": 2.0},
    "STT": {"kind": "seconds", "price": 0.00024},
    "LLM": {"kind": "token_blended", "price": 1.0},   # 1元/百万 tokens
    "OTHER": {"kind": "token_blended", "price": 1.0},
}


def lookup_price(model: Optional[str]) -> Tuple[Dict[str, Any], bool]:
    """返回 (单价配置, 是否为已知模型)。未知模型时按分类默认估算。"""
    m = (model or "").strip().lower()
    if m in MODEL_PRICES:
        return MODEL_PRICES[m], True
    # 前缀匹配（例如 qwen-plus-2025-04-28 → qwen-plus）
    for k, v in MODEL_PRICES.items():
        if m.startswith(k):
            return v, True
    return DEFAULT_PRICE_BY_KIND.get(classify_model(model), DEFAULT_PRICE_BY_KIND["OTHER"]), False


def estimate_cost(usage: Dict[str, Any], price: Dict[str, Any]) -> Tuple[float, str]:
    """根据用量 + 单价配置估算成本，返回 (金额元, 公式描述)。

    不可计算（缺少必要用量字段）时返回 (0.0, "")，便于上层过滤。
    """
    kind = price.get("kind", "")
    if kind == "token":
        pi = float(price.get("input", 0.0))
        po = float(price.get("output", 0.0))
        it = usage.get("input_tokens")
        ot = usage.get("output_tokens")
        if it is None and ot is None:
            return 0.0, ""
        it = it or 0
        ot = ot or 0
        cost = it / 1_000_000.0 * pi + ot / 1_000_000.0 * po
        return cost, f"in {it}×{pi} + out {ot}×{po} 元/百万"
    if kind == "token_blended":
        p = float(price.get("price", 0.0))
        tt = usage.get("total_tokens")
        if tt is None:
            return 0.0, ""
        return tt / 1_000_000.0 * p, f"total {tt} tokens × {p} 元/百万"
    if kind == "chars":
        p = float(price.get("price", 0.0))
        c = usage.get("characters")
        if c is None:
            return 0.0, ""
        return c / 10_000.0 * p, f"{c} 字符 × {p} 元/万字"
    if kind == "seconds":
        p = float(price.get("price", 0.0))
        d = usage.get("duration_sec")
        if d is None:
            return 0.0, ""
        return float(d) * p, f"{d:.0f}s × {p} 元/秒"
    return 0.0, ""


def estimate_cost_for_record(rec: "RemoteAuditRecord") -> Tuple[float, str, bool]:
    """封装：对单条云端记录估算成本，返回 (cost, formula, 是否已知模型)。"""
    usage = _extract_usage(rec.raw or {})
    price, known = lookup_price(rec.model)
    cost, formula = estimate_cost(usage, price)
    return cost, formula, known


# 模型名→类型分类（与 csv_reconcile.py 口径一致）
TTS_MODEL_PAT = re.compile(r"tts|cosyvoice|sambert|qwen-audio|qwen3-tts", re.IGNORECASE)
STT_MODEL_PAT = re.compile(r"paraformer|asr|gummy|sensevoice|recogni", re.IGNORECASE)
LLM_MODEL_KEYS = ("qwen", "gpt", "llm", "max", "plus", "flash", "turbo", "deepseek")


def classify_model(model: Optional[str]) -> str:
    """按模型名把云端审计记录归类为 TTS/STT/LLM/OTHER。

    判定顺序不可颠倒：qwen3-tts-flash 同时含 'flash'(LLM关键词) 与 'tts'，必须先判 TTS。
    """
    m = (model or "").lower()
    if not m:
        return "OTHER"
    if TTS_MODEL_PAT.search(m):
        return "TTS"
    if STT_MODEL_PAT.search(m):
        return "STT"
    if any(k in m for k in LLM_MODEL_KEYS):
        return "LLM"
    return "OTHER"


def _remote_align_time(r: RemoteAuditRecord, use_end: bool = True) -> Optional[datetime]:
    """计算云端记录用于时间对齐的时刻。

    - use_end=True：start + duration（合成/识别结束时刻），与本地对帐行打印时刻对齐，
      实测偏差可小到 ~20ms。
    - use_end=False：仅 start（开始时刻）。

    优先用毫秒级 start_unix_timestamp 保留精度，回退到已解析的 timestamp。
    duration 单位：百炼审计为毫秒。
    """
    raw = r.raw or {}
    start_ms = _to_int(raw.get("start_unix_timestamp"))
    start_dt: Optional[datetime] = None
    if start_ms and start_ms > 1_000_000_000_000:  # 毫秒级
        try:
            start_dt = datetime.fromtimestamp(start_ms / 1000.0)
        except (OSError, ValueError):
            start_dt = None
    if start_dt is None:
        start_dt = r.timestamp
    if start_dt is None:
        return None
    if use_end:
        dur = _to_int(_pick(raw, "duration", "cost", "elapsed"))
        if dur is not None:
            return start_dt + timedelta(milliseconds=dur)
    return start_dt


def fetch_sls_audit_logs(
    endpoint: str,
    ak_id: str,
    ak_secret: str,
    project: str,
    logstore: str,
    since: datetime,
    until: datetime,
    request_ids: Optional[Sequence[str]] = None,
    model_regex: Optional[str] = None,
    page_size: int = 100,
    batch_size: int = 40,
    max_records: int = 200000,
    verbose: bool = False,
) -> List[RemoteAuditRecord]:
    """从 SLS 拉取百炼审计日志。

    - ``request_ids`` 非空：按 requestId 分批精确查询（推荐，快、量少）
    - ``request_ids`` 为空：使用 ``*`` 拉取时间窗内全量（可发现 remote_only）
    """
    try:
        from aliyun.log import LogClient, GetLogsRequest  # type: ignore
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(
            "未安装 aliyun-log-python-sdk。请先执行：pip install aliyun-log-python-sdk"
        ) from e

    client = LogClient(endpoint, ak_id, ak_secret)
    from_ts = int(since.timestamp())
    to_ts = int(until.timestamp())
    if to_ts <= from_ts:
        raise ValueError(f"SLS 查询时间窗非法：since={since} until={until}")

    if request_ids:
        ids = list(dict.fromkeys(request_ids))
        batches = [ids[i:i + batch_size] for i in range(0, len(ids), batch_size)]
        queries = [
            " OR ".join(f'request_id: "{rid}"' for rid in batch)
            for batch in batches
        ]
    else:
        queries = ["*"]

    model_re = re.compile(model_regex, re.IGNORECASE) if model_regex else None

    out: List[RemoteAuditRecord] = []
    seen_ids = set()
    skipped_by_model = 0
    for qi, q in enumerate(queries, 1):
        offset = 0
        while True:
            req = GetLogsRequest(project, logstore, from_ts, to_ts, "", q, page_size, offset, False)
            resp = client.get_logs(req)
            logs = list(resp.get_logs() or [])
            for lg in logs:
                try:
                    contents = dict(lg.get_contents())
                except Exception:
                    contents = {}
                rid = _pick(contents, "request_id", "requestId", "RequestId")
                if not rid:
                    continue
                if model_re is not None:
                    model_val = _pick(contents, "model", "model_name", "modelName") or ""
                    if not model_re.search(model_val):
                        skipped_by_model += 1
                        continue
                if rid in seen_ids:
                    continue
                seen_ids.add(rid)
                # 时间戳：百炼审计日志使用 start_time(字符串) / start_unix_timestamp(毫秒)，
                # 兼容常见别名后兜底到 SDK 的 get_time()
                ts: Optional[datetime] = None
                # 1) 毫秒精度 unix 时间戳
                for tk in ("start_unix_timestamp", "end_unix_timestamp", "__time__", "call_time"):
                    n = _to_int(contents.get(tk))
                    if n and n > 1_000_000_000:
                        if n > 10_000_000_000:  # 毫秒 → 秒
                            n //= 1000
                        try:
                            ts = datetime.fromtimestamp(n)
                            break
                        except (OSError, ValueError):
                            pass
                # 2) 字符串时间（百炼实际字段为 start_time，形如 2026-09-23 14:30:52.105）
                if ts is None:
                    for tk in ("start_time", "end_time", "time", "timestamp"):
                        sv = contents.get(tk)
                        if not sv:
                            continue
                        ts = _parse_ts(str(sv))
                        if ts:
                            break
                # 3) SDK 内置时间
                if ts is None:
                    try:
                        ts = datetime.fromtimestamp(int(lg.get_time()))
                    except Exception:
                        ts = None
                out.append(RemoteAuditRecord(
                    request_id=rid,
                    timestamp=ts,
                    model=_pick(contents, "model", "model_name", "modelName"),
                    status=_pick(contents, "status", "status_code", "statusCode", "http_status_code"),
                    error_code=_pick(contents, "error_code", "errorCode"),
                    characters=_extract_characters(contents),
                    api_key=_pick(contents, "api_key", "api_key_id", "apiKey", "apikey_id"),
                    workspace=_pick(contents, "workspace", "workspace_id", "workspaceId"),
                    raw=contents,
                    **{k: v for k, v in _extract_usage(contents).items()
                       if k in ("input_tokens", "output_tokens", "total_tokens", "duration_sec") and v is not None},
                ))
                if len(out) >= max_records:
                    if verbose:
                        print(f"[sls] 已达 max_records={max_records}，提前结束", file=sys.stderr)
                    return out
            if len(logs) < page_size:
                break
            offset += page_size
        if verbose:
            print(f"[sls] 批次 {qi}/{len(queries)} 完成，累计 {len(out)} 条", file=sys.stderr)
    if verbose and model_re is not None and skipped_by_model:
        print(f"[sls] 已按 model 正则 {model_regex!r} 过滤掉 {skipped_by_model} 条", file=sys.stderr)
    return out


@dataclass
class ReconcileReport:
    matched: List[Tuple[TtsRecord, RemoteAuditRecord]]
    local_only: List[TtsRecord]
    remote_only: List[RemoteAuditRecord]


def reconcile(
    local: Sequence[TtsRecord],
    remote: Sequence[RemoteAuditRecord],
) -> ReconcileReport:
    remote_by_id: Dict[str, RemoteAuditRecord] = {}
    for r in remote:
        remote_by_id.setdefault(r.request_id, r)
    matched: List[Tuple[TtsRecord, RemoteAuditRecord]] = []
    local_only: List[TtsRecord] = []
    hit_ids = set()
    for lr in local:
        rr = remote_by_id.get(lr.request_id)
        if rr is not None:
            matched.append((lr, rr))
            hit_ids.add(lr.request_id)
        else:
            local_only.append(lr)
    remote_only = [r for rid, r in remote_by_id.items() if rid not in hit_ids]
    return ReconcileReport(matched=matched, local_only=local_only, remote_only=remote_only)


def reconcile_by_time(
    local: Sequence[TtsRecord],
    remote: Sequence[RemoteAuditRecord],
    tolerance_sec: float = 5.0,
    use_end: bool = True,
    use_chars: bool = False,
) -> ReconcileReport:
    """时间窗匹配：requestId 对不上时（DashScope WS）的兜底方案。

    以「本地对帐行时刻 ≈ 云端 start+duration」做最近邻贪婪 1:1 配对（tolerance_sec 内）。
    use_chars=True 时（TTS），在时间差相近时用 |本地chars - 云端characters| 做次级排序键，
    让同秒内多次调用也能按字符数正确配对。
    """
    rem_align: List[Tuple[RemoteAuditRecord, Optional[datetime]]] = [
        (r, _remote_align_time(r, use_end=use_end)) for r in remote
    ]
    rem_valid = [j for j, (_, at) in enumerate(rem_align) if at is not None]
    loc_valid = [i for i, lr in enumerate(local) if lr.timestamp is not None]

    # 容差内所有候选对：主键=时间差，次键=字符差（use_chars）
    pairs: List[Tuple[float, float, int, int]] = []
    for i in loc_valid:
        lt = local[i].timestamp
        lc = local[i].chars
        for j in rem_valid:
            at = rem_align[j][1]
            tdiff = abs((lt - at).total_seconds())
            if tdiff > tolerance_sec:
                continue
            cdiff = 0.0
            if use_chars and lc is not None:
                rc = rem_align[j][0].characters
                cdiff = float(abs(lc - rc)) if rc is not None else float(tolerance_sec)
            pairs.append((tdiff, cdiff, i, j))
    pairs.sort(key=lambda x: (x[0], x[1]))

    used_l: Set[int] = set()
    used_r: Set[int] = set()
    match_map: Dict[int, int] = {}
    for tdiff, cdiff, i, j in pairs:
        if i in used_l or j in used_r:
            continue
        used_l.add(i)
        used_r.add(j)
        match_map[i] = j

    matched: List[Tuple[TtsRecord, RemoteAuditRecord]] = []
    for i in sorted(match_map, key=lambda x: local[x].timestamp):
        matched.append((local[i], rem_align[match_map[i]][0]))
    local_only = [local[i] for i in range(len(local)) if i not in used_l]
    remote_only = [rem_align[j][0] for j in range(len(rem_align)) if j not in used_r]
    return ReconcileReport(matched=matched, local_only=local_only, remote_only=remote_only)


def _remote_total_tokens(r: RemoteAuditRecord) -> Optional[int]:
    """从云端审计记录提取 total_tokens（LLM 用量交叉校验用）。"""
    raw = r.raw or {}
    usage = _pick(raw, "usage")
    if usage:
        try:
            obj = json.loads(usage)
            if isinstance(obj, dict):
                for k in ("total_tokens", "totalTokens"):
                    n = _to_int(obj.get(k))
                    if n is not None:
                        return n
        except (ValueError, TypeError):
            pass
    return _to_int(_pick(raw, "total_tokens"))


def print_reconcile_report(rep: ReconcileReport, chars_unit_price: float = 2.0,
                            mode: str = "requestId", kind: str = "",
                            use_end: bool = True) -> None:
    total_local = len(rep.matched) + len(rep.local_only)
    total_remote = len(rep.matched) + len(rep.remote_only)
    mode_label = "时间窗匹配(time)" if mode == "time" else "requestId 精确匹配"
    title = "对帐结果（本地日志 vs 阿里云百炼审计日志）"
    if kind:
        title += f" — [{kind}]"
    print("=" * 78)
    print(f"{title} — 匹配方式：{mode_label}")
    print("-" * 78)
    print(f"本地记录数            : {total_local}")
    print(f"云端记录数            : {total_remote}")
    print(f"匹配 matched          : {len(rep.matched)}")
    print(f"仅本地 local_only     : {len(rep.local_only)}"
          "   ← 云端未落库/失败/超保留期/延迟到达"
          if rep.local_only else
          f"仅本地 local_only     : {len(rep.local_only)}")
    print(f"仅云端 remote_only    : {len(rep.remote_only)}"
          "   ← 本地日志轮转丢失/跨机部署遗漏/AK 被其它项目复用"
          if rep.remote_only else
          f"仅云端 remote_only    : {len(rep.remote_only)}")
    if rep.matched:
        diffs: List[float] = []
        for lr, rr in rep.matched:
            if not lr.timestamp:
                continue
            ref = _remote_align_time(rr, use_end=use_end) if mode == "time" else rr.timestamp
            if ref:
                diffs.append(abs((lr.timestamp - ref).total_seconds()))
        if diffs:
            diffs.sort()
            avg = sum(diffs) / len(diffs)
            p50 = diffs[len(diffs) // 2]
            p95 = diffs[min(int(len(diffs) * 0.95), len(diffs) - 1)]
            lbl = "对齐残差(秒)" if mode == "time" else "时间偏差(秒)"
            print(f"{lbl}        : avg={avg:.3f}  p50={p50:.3f}  p95={p95:.3f}  max={max(diffs):.3f}")
        # 本地用量（按 kind）
        local_chars = [lr.chars for lr, _ in rep.matched if lr.chars is not None]
        if local_chars:
            print(f"本地字符总数          : {sum(local_chars)}")
        # 云端用量 + 成本（按模型分别估算，不混用单位）
        cost_by_model: Dict[str, Dict[str, Any]] = defaultdict(
            lambda: {"n": 0, "cost": 0.0, "chars": 0, "in_tok": 0, "out_tok": 0,
                     "total_tok": 0, "sec": 0.0, "known": True, "price_kind": ""})
        for _, rr in rep.matched:
            u = _extract_usage(rr.raw or {})
            price, known = lookup_price(rr.model)
            c, _ = estimate_cost(u, price)
            m = rr.model or "unknown"
            b = cost_by_model[m]
            b["n"] += 1
            b["cost"] += c
            b["known"] = b["known"] and known
            b["price_kind"] = price.get("kind", "")
            b["chars"] += u.get("characters") or 0
            b["in_tok"] += u.get("input_tokens") or 0
            b["out_tok"] += u.get("output_tokens") or 0
            b["total_tok"] += u.get("total_tokens") or 0
            b["sec"] += u.get("duration_sec") or 0.0
        if cost_by_model:
            total_cost = sum(v["cost"] for v in cost_by_model.values())
            print(f"云端成本估算          : ¥{total_cost:.4f}  （按模型单价表，--chars-unit-price 已废弃）")
            for m, v in sorted(cost_by_model.items(), key=lambda kv: -kv[1]["cost"]):
                tag = "" if v["known"] else "  [默认价]"
                if v["price_kind"] == "token":
                    detail = f"in={v['in_tok']} out={v['out_tok']}"
                elif v["price_kind"] == "token_blended":
                    detail = f"total={v['total_tok']} tokens"
                elif v["price_kind"] == "chars":
                    detail = f"{v['chars']} 字符"
                elif v["price_kind"] == "seconds":
                    detail = f"{v['sec']:.0f}s"
                else:
                    detail = ""
                print(f"    {m:<28} n={v['n']:<4} {detail:<28} ¥{v['cost']:.4f}{tag}")
        # 云端用量汇总（保留字符/tokens 展示，便于交叉校验）
        r_chars = sum(rr.characters for _, rr in rep.matched if rr.characters is not None)
        if r_chars:
            print(f"云端字符总数          : {r_chars}")
        # LLM token 用量交叉校验
        ltok = [lr.total_tokens for lr, _ in rep.matched if lr.total_tokens is not None]
        rtok = [x for x in (_remote_total_tokens(rr) for _, rr in rep.matched) if x is not None]
        if ltok or rtok:
            print(f"本地 total_tokens     : {sum(ltok) if ltok else 0}")
            print(f"云端 total_tokens     : {sum(rtok) if rtok else 0}")
        # 云端状态码分布
        status_cnt = Counter((rr.status or "unknown") for _, rr in rep.matched)
        print(f"云端状态分布          : {dict(status_cnt)}")
        # 模型分布
        model_cnt = Counter((rr.model or "unknown") for _, rr in rep.matched)
        print(f"云端模型分布          : {dict(model_cnt)}")
    print("=" * 78)


_RECONCILE_CSV_HEADER = [
    "match_status", "kind", "match_method",
    "local_request_id", "remote_request_id",
    "local_time", "remote_start_time", "remote_align_time", "match_diff_sec",
    "device_id", "session_id", "model", "status", "error_code",
    "local_chars", "remote_characters",
    "prompt_tokens", "completion_tokens", "total_tokens",
    "api_key", "workspace",
]


def _fmt_dt(dt: Optional[datetime], ms: bool = False) -> str:
    if not dt:
        return ""
    return dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] if ms else dt.strftime("%Y-%m-%d %H:%M:%S")


def _reconcile_csv_rows(rep: ReconcileReport, kind: str, mode: str,
                         use_end: bool) -> List[List[Any]]:
    method = "time" if mode == "time" else "requestId"
    rows: List[List[Any]] = []
    for lr, rr in rep.matched:
        align = _remote_align_time(rr, use_end=use_end)
        ref = align if mode == "time" else rr.timestamp
        diff = ""
        if lr.timestamp and ref:
            diff = f"{(lr.timestamp - ref).total_seconds():.3f}"
        rows.append([
            "matched", kind or lr.kind, method,
            lr.request_id, rr.request_id,
            _fmt_dt(lr.timestamp, ms=True), _fmt_dt(rr.timestamp), _fmt_dt(align, ms=True), diff,
            lr.device_id, lr.session_id,
            rr.model or lr.model or "", rr.status or "", rr.error_code or "",
            lr.chars if lr.chars is not None else "",
            rr.characters if rr.characters is not None else "",
            lr.prompt_tokens if lr.prompt_tokens is not None else "",
            lr.completion_tokens if lr.completion_tokens is not None else "",
            lr.total_tokens if lr.total_tokens is not None else "",
            rr.api_key or "", rr.workspace or "",
        ])
    for lr in rep.local_only:
        rows.append([
            "local_only", kind or lr.kind, method,
            lr.request_id, "",
            _fmt_dt(lr.timestamp, ms=True), "", "", "",
            lr.device_id, lr.session_id, lr.model or "", "", "",
            lr.chars if lr.chars is not None else "", "",
            lr.prompt_tokens if lr.prompt_tokens is not None else "",
            lr.completion_tokens if lr.completion_tokens is not None else "",
            lr.total_tokens if lr.total_tokens is not None else "",
            "", "",
        ])
    for rr in rep.remote_only:
        align = _remote_align_time(rr, use_end=use_end)
        rows.append([
            "remote_only", kind or classify_model(rr.model), method,
            "", rr.request_id,
            "", _fmt_dt(rr.timestamp), _fmt_dt(align, ms=True), "",
            "", "", rr.model or "", rr.status or "", rr.error_code or "",
            "", rr.characters if rr.characters is not None else "",
            "", "", "", rr.api_key or "", rr.workspace or "",
        ])
    return rows


def _export_reconcile_reports(reports: Sequence[Tuple[str, str, ReconcileReport]],
                               path: Path, use_end: bool = True) -> int:
    """将多个 (kind, mode, report) 写入同一 CSV（表头只写一次）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(_RECONCILE_CSV_HEADER)
        for kind, mode, rep in reports:
            rows = _reconcile_csv_rows(rep, kind, mode, use_end)
            w.writerows(rows)
            total += len(rows)
    print(f"[ok] 已导出对帐明细 CSV：{path}（{total} 行）")
    return total


def export_reconcile_csv(rep: ReconcileReport, path: Path,
                          mode: str = "requestId", kind: str = "",
                          use_end: bool = True) -> None:
    _export_reconcile_reports([(kind, mode, rep)], path, use_end=use_end)


def _print_reconcile_totals(reports: Sequence[Tuple[str, str, ReconcileReport]]) -> None:
    if not reports:
        return
    print("=" * 78)
    print("全流程对帐汇总")
    print("-" * 78)
    tm = tl = tr = 0
    for kind, mode, rep in reports:
        m, lo, ro = len(rep.matched), len(rep.local_only), len(rep.remote_only)
        tm += m
        tl += lo
        tr += ro
        print(f"  [{kind:<3}] matched={m:<5} local_only={lo:<5} remote_only={ro:<5} (匹配方式={mode})")
    print("-" * 78)
    print(f"  合计  matched={tm}  local_only={tl}  remote_only={tr}")
    print("=" * 78)


# --------------------------------------------------------------------------- #
# 本地缓存（logs/aliyun_data/audit-YYYY-MM-DD.jsonl）
# --------------------------------------------------------------------------- #

DEFAULT_AUDIT_DIR = Path(__file__).resolve().parent / "aliyun_data"
# 完整缓存：audit-YYYY-MM-DD.jsonl
# 部分缓存：audit-YYYY-MM-DD.partial.jsonl（当天还在进行中，仅拉到当下）
AUDIT_COMPLETE_RE = re.compile(r"^audit-(\d{4}-\d{2}-\d{2})\.jsonl$")
AUDIT_PARTIAL_RE = re.compile(r"^audit-(\d{4}-\d{2}-\d{2})\.partial\.jsonl$")


def audit_file_for_day(day: date, audit_dir: Path = DEFAULT_AUDIT_DIR,
                        partial: bool = False) -> Path:
    suffix = ".partial.jsonl" if partial else ".jsonl"
    return audit_dir / f"audit-{day.strftime('%Y-%m-%d')}{suffix}"


def scan_cached_days(audit_dir: Path = DEFAULT_AUDIT_DIR) -> Tuple[Set[date], Set[date]]:
    """扫描缓存目录，返回 (完整缓存日期集, 部分缓存日期集)。"""
    complete: Set[date] = set()
    partial: Set[date] = set()
    if not audit_dir.exists():
        return complete, partial
    for f in audit_dir.iterdir():
        m = AUDIT_PARTIAL_RE.match(f.name)
        if m:
            try:
                partial.add(datetime.strptime(m.group(1), "%Y-%m-%d").date())
            except ValueError:
                pass
            continue
        m = AUDIT_COMPLETE_RE.match(f.name)
        if m:
            try:
                complete.add(datetime.strptime(m.group(1), "%Y-%m-%d").date())
            except ValueError:
                pass
    return complete, partial


def discover_log_dates(files: Sequence[Path], kinds: Sequence[str]) -> Set[date]:
    """扫描本地日志文件，返回其中出现对帐记录的日期集合。"""
    dates: Set[date] = set()
    kind_set = {k.upper() for k in kinds}
    for f in files:
        try:
            with f.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if "对帐" not in line:
                        continue
                    m = LINE_RE.match(line)
                    if not m:
                        continue
                    if m.group("kind").upper() not in kind_set:
                        continue
                    ts = _parse_ts(m.group("ts"))
                    if ts:
                        dates.add(ts.date())
        except OSError:
            continue
    return dates


def _remote_to_jsonable(r: RemoteAuditRecord) -> Dict[str, Any]:
    return {
        "request_id": r.request_id,
        "timestamp": r.timestamp.isoformat(timespec="milliseconds") if r.timestamp else None,
        "model": r.model,
        "status": r.status,
        "error_code": r.error_code,
        "characters": r.characters,
        "input_tokens": r.input_tokens,
        "output_tokens": r.output_tokens,
        "total_tokens": r.total_tokens,
        "duration_sec": r.duration_sec,
        "api_key": r.api_key,
        "workspace": r.workspace,
        "raw": r.raw,
    }


def _remote_from_jsonable(obj: Dict[str, Any]) -> RemoteAuditRecord:
    ts_raw = obj.get("timestamp")
    ts: Optional[datetime] = None
    if ts_raw:
        try:
            ts = datetime.fromisoformat(ts_raw)
        except ValueError:
            ts = _parse_ts(str(ts_raw))
    raw = obj.get("raw") or {}
    # 用量字段一律从 raw 重新抽取，避免旧缓存里的错误值（如早期将 total_tokens 写入 characters）遗留
    usage = _extract_usage(raw) if raw else {}
    return RemoteAuditRecord(
        request_id=obj.get("request_id", ""),
        timestamp=ts,
        model=obj.get("model"),
        status=obj.get("status"),
        error_code=obj.get("error_code"),
        characters=usage.get("characters"),
        api_key=obj.get("api_key"),
        workspace=obj.get("workspace"),
        raw=raw,
        input_tokens=usage.get("input_tokens"),
        output_tokens=usage.get("output_tokens"),
        total_tokens=usage.get("total_tokens"),
        duration_sec=usage.get("duration_sec"),
    )


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not path.exists():
        return out
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    except OSError as e:
        print(f"[warn] 读取 {path} 失败: {e}", file=sys.stderr)
    return out


def _write_jsonl(path: Path, objs: Iterable[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for obj in objs:
            fh.write(json.dumps(obj, ensure_ascii=False) + "\n")


def save_audit_day(day: date, records: Sequence[RemoteAuditRecord],
                    audit_dir: Path = DEFAULT_AUDIT_DIR,
                    partial: bool = False) -> Tuple[int, int, Path, bool]:
    """将一天的云端记录写入缓存文件，按 requestId 去重合并。

    - ``partial=True``：写到 ``audit-YYYY-MM-DD.partial.jsonl``（当天未结束）
    - ``partial=False``：写到 ``audit-YYYY-MM-DD.jsonl``；若同名 partial 文件存在，
      则将其内容合并过来并删除 partial 文件（升级：partial → complete）

    返回 (新增条数, 去重跳过数, 目标文件路径, 是否发生了升级)。
    """
    audit_dir.mkdir(parents=True, exist_ok=True)
    target = audit_file_for_day(day, audit_dir, partial=partial)
    upgraded = False

    merged: List[Dict[str, Any]] = []
    seen: Set[str] = set()

    # 1) 先合并完整文件已有内容
    for obj in _read_jsonl(target):
        rid = obj.get("request_id")
        if not rid or rid in seen:
            continue
        seen.add(rid)
        merged.append(obj)

    # 2) 若写入完整文件，合并同名 partial 内容并删除
    if not partial:
        partial_file = audit_file_for_day(day, audit_dir, partial=True)
        if partial_file.exists():
            for obj in _read_jsonl(partial_file):
                rid = obj.get("request_id")
                if not rid or rid in seen:
                    continue
                seen.add(rid)
                merged.append(obj)
            try:
                partial_file.unlink()
                upgraded = True
            except OSError as e:
                print(f"[warn] 删除 partial 文件失败 {partial_file}: {e}", file=sys.stderr)

    # 3) 追加本次拉取的新记录
    added = 0
    skipped = 0
    for r in records:
        if not r.request_id:
            continue
        if r.request_id in seen:
            skipped += 1
            continue
        seen.add(r.request_id)
        merged.append(_remote_to_jsonable(r))
        added += 1

    # 4) 时间排序后重写整个文件（保证 partial→complete 升级后内容有序）
    def _sort_key(o: Dict[str, Any]) -> str:
        return str(o.get("timestamp") or "")
    merged.sort(key=_sort_key)
    _write_jsonl(target, merged)
    return added, skipped, target, upgraded


def load_audit_records(days: Optional[Iterable[date]] = None,
                        model_regex: Optional[str] = None,
                        audit_dir: Path = DEFAULT_AUDIT_DIR) -> List[RemoteAuditRecord]:
    """从本地 jsonl 缓存加载审计记录（同时包含 .jsonl 与 .partial.jsonl）。"""
    if not audit_dir.exists():
        return []
    if days is not None:
        files: List[Path] = []
        for d in days:
            for partial in (False, True):
                f = audit_file_for_day(d, audit_dir, partial=partial)
                if f.exists():
                    files.append(f)
    else:
        files = sorted(p for p in audit_dir.iterdir()
                       if AUDIT_COMPLETE_RE.match(p.name) or AUDIT_PARTIAL_RE.match(p.name))
    out: List[RemoteAuditRecord] = []
    model_re = re.compile(model_regex, re.IGNORECASE) if model_regex else None
    seen: Set[str] = set()
    for f in files:
        for obj in _read_jsonl(f):
            rec = _remote_from_jsonable(obj)
            if not rec.request_id or rec.request_id in seen:
                continue
            if model_re is not None:
                mv = rec.model or ""
                if not model_re.search(mv):
                    continue
            seen.add(rec.request_id)
            out.append(rec)
    out.sort(key=lambda r: r.timestamp or datetime.min)
    return out


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _resolve_inputs(log_glob: Optional[str], log_dir: Path, extra_files: Sequence[str]) -> List[Path]:
    if log_glob:
        # 相对项目根解析；若为绝对 glob 也支持
        base = Path.cwd()
        matches = sorted({p for p in base.glob(log_glob) if p.is_file()})
        if not matches:
            print(f"[warn] --log-glob 未匹配到文件：{log_glob}", file=sys.stderr)
        return matches
    paths: List[Path] = [log_dir]
    for f in extra_files:
        paths.append(Path(f))
    return paths


def _split_csv(text: Optional[str]) -> Optional[List[str]]:
    if not text:
        return None
    return [x.strip() for x in text.split(",") if x.strip()]


def build_parser() -> argparse.ArgumentParser:
    default_log_dir = Path(__file__).resolve().parent / "dialogue"
    default_env_file = Path(__file__).resolve().parent / ".env"
    p = argparse.ArgumentParser(
        prog="online_reconcile.py",
        description="线上全流程对帐（TTS/STT/LLM）：本地日志 + 云端审计日志缓存 + 集合对帐",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = p.add_subparsers(dest="cmd")

    # ---- summary ----
    p_sum = sub.add_parser("summary", help="扫描本地日志并汇总（天/设备/会话维度）")
    _add_common_log_args(p_sum, default_log_dir)
    p_sum.set_defaults(func=cmd_summary)

    # ---- pull ----
    p_pull = sub.add_parser("pull", help="从 SLS 拉取云端审计日志到本地缓存（按天增量）")
    p_pull.add_argument("--env-file", type=Path, default=default_env_file,
                        help=f"凭证与数据源配置文件（默认 {default_env_file}）")
    p_pull.add_argument("--audit-dir", type=Path, default=DEFAULT_AUDIT_DIR,
                        help=f"本地缓存目录（默认 {DEFAULT_AUDIT_DIR}）")
    p_pull.add_argument("--log-dir", type=Path, default=default_log_dir,
                        help="本地日志目录，用于推导待拉取日期")
    p_pull.add_argument("--log-glob", type=str, default=None)
    p_pull.add_argument("--file", action="append", default=[])
    p_pull.add_argument("--kind", type=str, default="TTS,STT,LLM",
                        help="对帐类型，逗号分隔，可选 TTS,STT,LLM（默认 TTS,STT,LLM）")
    p_pull.add_argument("--day", action="append", default=[],
                        help="显式指定要拉取的日期 YYYY-MM-DD，可多次使用；不传则从本地日志推导")
    p_pull.add_argument("--force", action="store_true",
                        help="已缓存的日期也重拉（按 requestId 去重合并）")
    p_pull.add_argument("--sls-endpoint", type=str, default=None,
                        help="SLS Endpoint；未指定时从 project 名推断")
    p_pull.add_argument("--sls-model-filter", type=str, default="",
                        help="拉取阶段的 model 正则过滤；默认不过滤（保留全模型，reconcile 时再筛）")
    p_pull.set_defaults(func=cmd_pull)

    # ---- reconcile ----
    p_rec = sub.add_parser("reconcile", help="本地对帐：默认先增量同步云端审计，再本地日志 vs 云端缓存对帐")
    _add_common_log_args(p_rec, default_log_dir)
    p_rec.add_argument("--audit-dir", type=Path, default=DEFAULT_AUDIT_DIR,
                        help=f"本地缓存目录（默认 {DEFAULT_AUDIT_DIR}）")
    p_rec.add_argument("--sls-model-filter", type=str, default="",
                       help="对帐阶段的 model 正则过滤（默认空=全模型，按 kind 自动分类）")
    p_rec.add_argument("--match-by", type=str, default="auto",
                       choices=["auto", "requestId", "time"],
                       help="匹配方式：auto=按 kind 自动(LLM→requestId, TTS/STT→time)；"
                            "requestId=全部按请求ID；time=全部按时间窗")
    p_rec.add_argument("--time-tolerance", type=float, default=5.0,
                       help="time 匹配的时间容差（秒），默认 5.0")
    p_rec.add_argument("--align-start", action="store_true",
                       help="time 匹配改用云端开始时刻对齐（默认用 start+duration 结束时刻）")
    p_rec.add_argument("--reconcile-csv", type=Path, default=None,
                       help="导出对帐明细 CSV（matched / local_only / remote_only）")
    p_rec.add_argument("--chars-unit-price", type=float, default=2.0,
                       help="[已废弃] 旧版字符单价（元/万字符）；现在成本按 MODEL_PRICES 表按模型分别估算，此参数不再生效")
    p_rec.add_argument("--html", type=Path, default=None,
                       help="额外导出 HTML 对帐报告到指定路径")
    # 自动同步（pull）相关：默认执行；--no-pull 关闭走纯离线
    p_rec.add_argument("--env-file", type=Path, default=default_env_file,
                       help=f"凭证与数据源配置文件（默认 {default_env_file}）")
    p_rec.add_argument("--day", action="append", default=[],
                       help="显式指定同步日期 YYYY-MM-DD，可多次；不传则从本地日志推导")
    p_rec.add_argument("--force", action="store_true",
                       help="已完整缓存的日期也重拉（按 requestId 去重合并）")
    p_rec.add_argument("--sls-endpoint", type=str, default=None,
                       help="SLS Endpoint；未指定时从 project 名推断")
    p_rec.add_argument("--no-pull", action="store_true",
                       help="跳过云端同步，直接用现有缓存对帐（离线）")
    p_rec.set_defaults(func=cmd_reconcile)

    # ---- report（默认全流程：同步 → 全量对帐 → HTML）----
    default_html = Path(__file__).resolve().parent / "reconcile_report.html"
    p_rep = sub.add_parser(
        "report", help="默认全流程：同步云端审计 → 全量对帐(TTS/STT/LLM) → 导出 HTML 报告",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    _add_common_log_args(p_rep, default_log_dir)
    # 云端同步（pull）相关
    p_rep.add_argument("--env-file", type=Path, default=default_env_file,
                       help=f"凭证与数据源配置文件（默认 {default_env_file}）")
    p_rep.add_argument("--audit-dir", type=Path, default=DEFAULT_AUDIT_DIR,
                       help=f"本地缓存目录（默认 {DEFAULT_AUDIT_DIR}）")
    p_rep.add_argument("--day", action="append", default=[],
                       help="显式指定同步日期 YYYY-MM-DD，可多次；不传则从本地日志推导")
    p_rep.add_argument("--force", action="store_true", help="已缓存日期也重拉（去重合并）")
    p_rep.add_argument("--sls-endpoint", type=str, default=None,
                       help="SLS Endpoint；未指定时从 project 名推断")
    p_rep.add_argument("--sls-model-filter", type=str, default="",
                       help="model 正则过滤（默认空=全模型，按 kind 自动分类）")
    p_rep.add_argument("--no-pull", action="store_true",
                       help="跳过云端同步，直接用现有缓存对帐（离线）")
    # 对帐（reconcile）相关
    p_rep.add_argument("--match-by", type=str, default="auto",
                       choices=["auto", "requestId", "time"],
                       help="匹配方式：auto=按 kind 自动(LLM→requestId, TTS/STT→time)")
    p_rep.add_argument("--time-tolerance", type=float, default=5.0,
                       help="time 匹配的时间容差（秒），默认 5.0")
    p_rep.add_argument("--align-start", action="store_true",
                       help="time 匹配改用云端开始时刻对齐（默认 start+duration 结束时刻）")
    p_rep.add_argument("--chars-unit-price", type=float, default=2.0,
                       help="[已废弃] 旧版字符单价（元/万字符）；现在成本按 MODEL_PRICES 表按模型分别估算，此参数不再生效")
    p_rep.add_argument("--reconcile-csv", type=Path, default=None,
                       help="同时导出对帐明细 CSV")
    # HTML 输出
    p_rep.add_argument("--html", type=Path, default=default_html,
                       help=f"HTML 报告输出路径（默认 {default_html}）")
    p_rep.add_argument("--no-open", action="store_true",
                       help="生成后不自动用浏览器打开")
    p_rep.set_defaults(func=cmd_report)

    return p


def _add_common_log_args(p: argparse.ArgumentParser, default_log_dir: Path) -> None:
    p.add_argument("--log-dir", type=Path, default=default_log_dir,
                   help=f"日志目录（默认 {default_log_dir}），递归匹配 *.log*")
    p.add_argument("--log-glob", type=str, default=None,
                   help="自定义 glob，例如 'logs/**/*.log'；指定后覆盖 --log-dir")
    p.add_argument("--file", action="append", default=[],
                   help="追加指定单个日志文件，可多次使用")
    p.add_argument("--kind", type=str, default="TTS,STT,LLM",
                   help="对帐类型过滤，逗号分隔（默认 TTS,STT,LLM）")
    p.add_argument("--since", type=str, default=None, help="起始日期 YYYY-MM-DD（含）")
    p.add_argument("--until", type=str, default=None, help="结束日期 YYYY-MM-DD（含）")
    p.add_argument("--device", type=str, default=None, help="按 DeviceId 过滤，逗号分隔")
    p.add_argument("--session", type=str, default=None, help="按 SessionId 过滤，逗号分隔")
    p.add_argument("--by", type=str, default="day,device",
                   help="聚合维度，逗号分隔（默认 day,device）")
    p.add_argument("--top", type=int, default=20, help="分组展示上限，0 表全部（默认 20）")
    p.add_argument("--csv", type=Path, default=None, help="导出明细 CSV")
    p.add_argument("--json", type=Path, default=None, help="导出明细 JSON")
    p.add_argument("--request-ids-only", type=Path, default=None,
                   help="仅导出唯一 RequestId 列表（每行一个）")
    p.add_argument("--quiet", action="store_true", help="只输出汇总，不打印分组表")


def _parse_common_dates(args: argparse.Namespace) -> Tuple[Optional[date], Optional[date]]:
    since = _parse_day(args.since) if getattr(args, "since", None) else None
    until = _parse_day(args.until) if getattr(args, "until", None) else None
    return since, until


def _load_local_records(args: argparse.Namespace) -> Tuple[List[TtsRecord], List[Path]]:
    kinds = [k.strip().upper() for k in (args.kind or "TTS").split(",") if k.strip()]
    since, until = _parse_common_dates(args)
    inputs = _resolve_inputs(args.log_glob, args.log_dir, args.file)
    if not inputs:
        raise RuntimeError("未指定任何日志输入")
    files = list(iter_log_files(inputs))
    if not files:
        raise RuntimeError(f"输入路径下未找到日志文件：{inputs}")
    all_records: List[TtsRecord] = []
    for f in files:
        all_records.extend(parse_file(f, kinds))
    records = filter_records(
        all_records, since=since, until=until,
        devices=_split_csv(args.device),
        sessions=_split_csv(args.session),
    )
    return records, files


# --------------------------------------------------------------------------- #
# 子命令实现
# --------------------------------------------------------------------------- #

def cmd_summary(args: argparse.Namespace) -> int:
    try:
        records, files = _load_local_records(args)
    except (RuntimeError, ValueError) as e:
        print(f"[error] {e}", file=sys.stderr)
        return 2
    print(f"[info] 扫描 {len(files)} 个日志文件，过滤后 {len(records)} 条")
    print_summary(records, top_n=args.top if args.top > 0 else 20)
    print_usage_summary(records)
    dims = [d.strip().lower() for d in (args.by or "").split(",") if d.strip()]
    valid_dims = {"day", "hour", "device", "session", "kind"}
    for d in dims:
        if d not in valid_dims:
            print(f"[error] 未知聚合维度：{d}", file=sys.stderr)
            return 2
    if not args.quiet and dims and records:
        print_grouped(records, dims, top_n=args.top)
    if args.csv:
        export_csv(records, args.csv)
    if args.json:
        export_json(records, args.json)
    if args.request_ids_only:
        export_request_ids(records, args.request_ids_only)
    return 0


def cmd_pull(args: argparse.Namespace) -> int:
    env = load_env_file(args.env_file)
    if not env:
        print(f"[error] 未找到或无法解析 --env-file：{args.env_file}", file=sys.stderr)
        return 2
    ak_id = env_lookup(env, "AccessKey ID", "AccessKeyId", "access_key_id",
                        "ALIYUN_ACCESS_KEY_ID", "ALIYUN_AK_ID")
    ak_secret = env_lookup(env, "AccessKey Secret", "AccessKeySecret", "access_key_secret",
                            "ALIYUN_ACCESS_KEY_SECRET", "ALIYUN_AK_SECRET")
    ls_path = env_lookup(env, "LogStore", "logstore", "SLS_LOGSTORE", "log_store")
    if not (ak_id and ak_secret and ls_path):
        print("[error] .env 缺少 AccessKey ID / AccessKey Secret / LogStore 中的一项", file=sys.stderr)
        return 2
    try:
        project, logstore = parse_logstore_path(ls_path)
    except ValueError as e:
        print(f"[error] {e}", file=sys.stderr)
        return 2
    endpoint = args.sls_endpoint or env_lookup(env, "Endpoint", "SLS_ENDPOINT") \
        or infer_endpoint_from_project(project)
    print(f"[info] SLS project={project}  logstore={logstore}  endpoint={endpoint}")

    audit_dir: Path = args.audit_dir
    audit_dir.mkdir(parents=True, exist_ok=True)

    # 1) 确定待拉取日期
    kinds = [k.strip().upper() for k in (args.kind or "TTS").split(",") if k.strip()]
    if args.day:
        try:
            wanted: Set[date] = {_parse_day(d) for d in args.day}
        except ValueError as e:
            print(f"[error] --day 格式应为 YYYY-MM-DD：{e}", file=sys.stderr)
            return 2
        print(f"[info] 显式指定 {len(wanted)} 天：{sorted(d.isoformat() for d in wanted)}")
    else:
        inputs = _resolve_inputs(args.log_glob, args.log_dir, args.file)
        files = list(iter_log_files(inputs)) if inputs else []
        if not files:
            print(f"[error] 未找到本地日志文件，无法推导待拉取日期；请用 --day 显式指定", file=sys.stderr)
            return 2
        wanted = discover_log_dates(files, kinds)
        print(f"[info] 本地日志覆盖 {len(wanted)} 天：{sorted(d.isoformat() for d in wanted)}")

    cached_complete, cached_partial = scan_cached_days(audit_dir)
    today = date.today()
    if args.force:
        pending = sorted(wanted)
        print(f"[info] --force：强制重拉 {len(pending)} 天（按 requestId 去重合并）")
    else:
        # 完整缓存的天跳过；partial 的天需要重拉（当天未完/上次拉后又有新记录）
        pending = sorted(wanted - cached_complete)
        already_complete = sorted(wanted & cached_complete)
        if already_complete:
            print(f"[info] 已完整缓存 {len(already_complete)} 天（跳过）："
                  f"{[d.isoformat() for d in already_complete]}")
        will_refresh_partial = sorted(d for d in pending if d in cached_partial)
        if will_refresh_partial:
            print(f"[info] 部分缓存 {len(will_refresh_partial)} 天（将增量重拉）："
                  f"{[d.isoformat() for d in will_refresh_partial]}")

    if not pending:
        print("[ok] 所有目标日期均已完整缓存，无需拉取")
        return 0

    print(f"[info] 待拉取 {len(pending)} 天：{[d.isoformat() for d in pending]}")

    # 2) 按天拉取
    total_fetched = 0
    total_added = 0
    total_skipped = 0
    upgraded_days: List[date] = []
    failures: List[Tuple[date, str]] = []
    now = datetime.now()
    for day in pending:
        since_dt = datetime.combine(day, datetime.min.time())
        until_dt = datetime.combine(day, datetime.max.time())
        # 当天尚未结束 → 只拉到当下，并标记为 partial
        is_partial = day >= today
        if until_dt > now:
            until_dt = now
        if since_dt >= until_dt:
            print(f"[pull] {day} 时间窗为空，跳过")
            continue
        tag = "partial" if is_partial else "complete"
        print(f"[pull] {day} [{tag}]  窗口 {since_dt.strftime('%m-%d %H:%M:%S')} → {until_dt.strftime('%m-%d %H:%M:%S')}")
        try:
            remote = fetch_sls_audit_logs(
                endpoint=endpoint, ak_id=ak_id, ak_secret=ak_secret,
                project=project, logstore=logstore,
                since=since_dt, until=until_dt,
                request_ids=None,
                model_regex=(args.sls_model_filter or None),
                verbose=False,
            )
        except RuntimeError as e:
            print(f"  [error] {e}", file=sys.stderr)
            failures.append((day, str(e)))
            continue
        except Exception as e:  # noqa: BLE001
            msg = f"{type(e).__name__}: {e}"
            print(f"  [error] {msg}", file=sys.stderr)
            failures.append((day, msg))
            continue
        added, skipped, target, upgraded = save_audit_day(
            day, remote, audit_dir, partial=is_partial,
        )
        total_fetched += len(remote)
        total_added += added
        total_skipped += skipped
        if upgraded:
            upgraded_days.append(day)
        upgrade_tag = "  [↑ partial→complete]" if upgraded else ""
        print(f"  [ok] 云端 {len(remote)} 条，新增 {added}，去重 {skipped} → {target.name}{upgrade_tag}")

    print("=" * 60)
    ok_days = len(pending) - len(failures)
    print(f"拉取完成：{ok_days}/{len(pending)} 天成功"
          f"，云端合计 {total_fetched} 条，新增 {total_added}，去重 {total_skipped}")
    if upgraded_days:
        print(f"[info] 已升级 {len(upgraded_days)} 天为完整缓存："
              f"{[d.isoformat() for d in upgraded_days]}")
    partial_written = sorted(d for d in pending if d >= today and d not in {f[0] for f in failures})
    if partial_written:
        print(f"[info] 以下 {len(partial_written)} 天为 partial（当天未结束，后续可重拉补齐）："
              f"{[d.isoformat() for d in partial_written]}")
    if failures:
        print(f"[warn] {len(failures)} 天失败：")
        for d, m in failures:
            print(f"  - {d.isoformat()}: {m[:160]}")
        return 3
    return 0


def _compute_reconcile(args: argparse.Namespace):
    """加载本地日志 + 云端缓存，按 kind 分流对帐，只计算不打印报表。

    返回 ``(records, files, remote, reports, remote_other, meta)``：
      - reports：``[(kind, mode, ReconcileReport), ...]``
      - meta：对帐配置与缓存覆盖情况，供控制台报表与 HTML 报表共用。
    cmd_reconcile（控制台）与 cmd_report（HTML）均复用此函数，避免逻辑分叉。
    """
    records, files = _load_local_records(args)
    print(f"[info] 本地日志：扫描 {len(files)} 个文件，过滤后 {len(records)} 条")

    # 仅加载本地记录涉及的那些天的缓存
    days_needed: Set[date] = {r.timestamp.date() for r in records}
    cached_complete, cached_partial = scan_cached_days(args.audit_dir)
    today = date.today()
    missing = sorted(days_needed - cached_complete - cached_partial)
    stale_partial = sorted(d for d in (days_needed & cached_partial) if d < today)
    fresh_partial = sorted(days_needed & cached_partial & {today})
    script_name = Path(__file__).name
    if missing:
        print(f"[warn] 以下 {len(missing)} 天尚未缓存云端数据："
              f"{[d.isoformat() for d in missing]}"
              f"\n        → 建议先运行：python3 {script_name} pull", file=sys.stderr)
    if stale_partial:
        print(f"[warn] 以下 {len(stale_partial)} 天仅有部分缓存（日期已过，可能不完整）："
              f"{[d.isoformat() for d in stale_partial]}"
              f"\n        → 建议重跑：python3 {script_name} pull", file=sys.stderr)
    if fresh_partial:
        print(f"[info] 今天 {today.isoformat()} 为 partial 缓存（当天未结束），对帐结果可能不完整")
    remote = load_audit_records(
        days=(days_needed or None),
        model_regex=(args.sls_model_filter or None),
        audit_dir=args.audit_dir,
    )
    covered = len(days_needed & (cached_complete | cached_partial))
    print(f"[info] 云端缓存加载：{len(remote)} 条（覆盖 {covered}/{len(days_needed)} 天，"
          f"其中完整 {len(days_needed & cached_complete)} 天 / 部分 {len(days_needed & cached_partial)} 天）")

    # 按 kind 分流对帐：LLM→requestId（失败回退时间窗），TTS→时间窗+字符数，STT→时间窗
    match_by = (getattr(args, "match_by", "auto") or "auto").strip().lower()
    use_end = not getattr(args, "align_start", False)
    tolerance = getattr(args, "time_tolerance", 5.0)

    local_by_kind: Dict[str, List[TtsRecord]] = defaultdict(list)
    for r in records:
        local_by_kind[r.kind].append(r)
    remote_by_kind: Dict[str, List[RemoteAuditRecord]] = defaultdict(list)
    remote_other: List[RemoteAuditRecord] = []
    for rr in remote:
        k = classify_model(rr.model)
        if k in ("TTS", "STT", "LLM"):
            remote_by_kind[k].append(rr)
        else:
            remote_other.append(rr)

    reports: List[Tuple[str, str, ReconcileReport]] = []
    for kind in ("TTS", "STT", "LLM"):
        loc = local_by_kind.get(kind, [])
        rem = remote_by_kind.get(kind, [])
        if not loc and not rem:
            continue
        if match_by == "requestid":
            mode = "requestId"
        elif match_by == "time":
            mode = "time"
        else:  # auto
            mode = "requestId" if kind == "LLM" else "time"

        if mode == "requestId":
            rep = reconcile(loc, rem)
            if match_by == "auto" and not rep.matched and loc and rem:
                rep2 = reconcile_by_time(loc, rem, tolerance_sec=tolerance,
                                          use_end=use_end, use_chars=(kind == "TTS"))
                if rep2.matched:
                    print(f"[info] [{kind}] requestId 匹配 0 条，已回退时间窗匹配")
                    rep, mode = rep2, "time"
        else:
            rep = reconcile_by_time(loc, rem, tolerance_sec=tolerance,
                                     use_end=use_end, use_chars=(kind == "TTS"))
        reports.append((kind, mode, rep))

    if remote_other:
        print(f"[info] 云端有 {len(remote_other)} 条无法归类(TTS/STT/LLM 之外)的记录，未参与匹配")

    meta: Dict[str, Any] = {
        "match_by": match_by, "use_end": use_end, "tolerance": tolerance,
        "days_needed": days_needed, "cached_complete": cached_complete,
        "cached_partial": cached_partial, "covered": covered, "today": today,
        "missing": missing, "stale_partial": stale_partial, "fresh_partial": fresh_partial,
        "remote_total": len(remote), "local_total": len(records), "file_count": len(files),
    }
    return records, files, remote, reports, remote_other, meta


def _maybe_auto_pull(args: argparse.Namespace) -> None:
    """在对帐前自动增量拉取云端审计（best-effort）。

    pull 本身已实现：已完整缓存的天跳过、partial 的天重拉并按 requestId 去重合并，
    因此默认开启不会造成重复流量。无 .env / 无网络 / 部分天失败时仅告警，
    不阻断后续对帐（与 cmd_report 保持一致的弹性行为）。
    """
    if getattr(args, "no_pull", False):
        print("[info] --no-pull：跳过云端同步，直接使用现有缓存")
        return
    print("=" * 78)
    print("自动同步云端审计（增量，已完整缓存的天会跳过；--no-pull 可关闭）")
    print("=" * 78)
    try:
        rc = cmd_pull(args)
        if rc != 0:
            print(f"[warn] 云端同步未完全成功（返回码 {rc}），将基于现有缓存继续对帐", file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 云端同步异常：{type(e).__name__}: {e}；将基于现有缓存继续对帐", file=sys.stderr)
    print()


def cmd_reconcile(args: argparse.Namespace) -> int:
    _maybe_auto_pull(args)
    try:
        records, files, remote, reports, remote_other, meta = _compute_reconcile(args)
    except (RuntimeError, ValueError) as e:
        print(f"[error] {e}", file=sys.stderr)
        return 2
    print_summary(records, top_n=args.top if args.top > 0 else 20)
    print_usage_summary(records)
    if not args.quiet:
        dims = [d.strip().lower() for d in (args.by or "").split(",") if d.strip()]
        if dims and records:
            print_grouped(records, dims, top_n=args.top)
    for kind, mode, rep in reports:
        print_reconcile_report(rep, chars_unit_price=args.chars_unit_price,
                                mode=mode, kind=kind, use_end=meta["use_end"])
    _print_reconcile_totals(reports)
    if args.reconcile_csv:
        _export_reconcile_reports(reports, args.reconcile_csv, use_end=meta["use_end"])
    if getattr(args, "html", None):
        export_reconcile_html(reports, args.html, records=records,
                              remote_other=remote_other, meta=meta,
                              chars_unit_price=args.chars_unit_price)
    return 0


# --------------------------------------------------------------------------- #
# HTML 报告导出
# --------------------------------------------------------------------------- #

def _residual_stats(rep: ReconcileReport, mode: str, use_end: bool) -> Optional[Dict[str, float]]:
    """计算 matched 记录的时间对齐残差统计（秒）。与 print_reconcile_report 口径一致。"""
    diffs: List[float] = []
    for lr, rr in rep.matched:
        if not lr.timestamp:
            continue
        ref = _remote_align_time(rr, use_end=use_end) if mode == "time" else rr.timestamp
        if ref:
            diffs.append(abs((lr.timestamp - ref).total_seconds()))
    if not diffs:
        return None
    diffs.sort()
    n = len(diffs)
    return {"count": n, "avg": sum(diffs) / n, "p50": diffs[n // 2],
            "p95": diffs[min(int(n * 0.95), n - 1)], "max": max(diffs)}


def _kind_html_stats(rep: ReconcileReport, mode: str, use_end: bool,
                     chars_unit_price: float) -> Dict[str, Any]:
    """汇总单个 kind 的展示指标（计数/匹配率/残差/字符/token/分布/成本）。

    成本按模型单价表分别估算，不混用单位（chars_unit_price 已废弃，仅为向后兼容保留）。
    """
    matched = len(rep.matched)
    local_total = matched + len(rep.local_only)
    remote_total = matched + len(rep.remote_only)
    local_chars = sum(lr.chars for lr, _ in rep.matched if lr.chars is not None)
    remote_chars = sum(rr.characters for _, rr in rep.matched if rr.characters is not None)
    local_tokens = sum(lr.total_tokens for lr, _ in rep.matched if lr.total_tokens is not None)
    rtoks = [x for x in (_remote_total_tokens(rr) for _, rr in rep.matched) if x is not None]
    remote_tokens = sum(rtoks)
    # 按模型聚合云端用量与成本
    by_model: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"n": 0, "cost": 0.0, "chars": 0, "in_tok": 0, "out_tok": 0,
                 "total_tok": 0, "sec": 0.0, "known": True, "price_kind": ""})
    total_cost = 0.0
    for _, rr in rep.matched:
        u = _extract_usage(rr.raw or {})
        price, known = lookup_price(rr.model)
        c, _ = estimate_cost(u, price)
        m = rr.model or "unknown"
        b = by_model[m]
        b["n"] += 1
        b["cost"] += c
        b["known"] = b["known"] and known
        b["price_kind"] = price.get("kind", "")
        b["chars"] += u.get("characters") or 0
        b["in_tok"] += u.get("input_tokens") or 0
        b["out_tok"] += u.get("output_tokens") or 0
        b["total_tok"] += u.get("total_tokens") or 0
        b["sec"] += u.get("duration_sec") or 0.0
        total_cost += c
    remote_sec = sum(v["sec"] for v in by_model.values())
    return {
        "matched": matched, "local_only": len(rep.local_only), "remote_only": len(rep.remote_only),
        "local_total": local_total, "remote_total": remote_total,
        "match_rate": (matched / local_total * 100.0) if local_total else 0.0,
        "residual": _residual_stats(rep, mode, use_end),
        "local_chars": local_chars, "remote_chars": remote_chars,
        "remote_sec": remote_sec,
        "cost": total_cost,
        "cost_by_model": dict(by_model),
        "local_tokens": local_tokens, "remote_tokens": remote_tokens,
        "has_chars": any(lr.chars is not None for lr, _ in rep.matched)
                     or any(rr.characters is not None for _, rr in rep.matched),
        "has_tokens": bool(rtoks) or any(lr.total_tokens is not None for lr, _ in rep.matched),
        "has_seconds": remote_sec > 0,
        "status_dist": Counter((rr.status or "unknown") for _, rr in rep.matched),
        "model_dist": Counter((rr.model or "unknown") for _, rr in rep.matched),
    }


_HTML_CSS = """
:root{--bg:#f5f6f8;--card:#fff;--ink:#1f2933;--muted:#6b7280;--line:#e5e7eb;
--ok:#16a34a;--warn:#d97706;--err:#dc2626;--accent:#2563eb}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);line-height:1.5;font-size:14px;
font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif}
.wrap{max-width:1240px;margin:0 auto;padding:24px}
header.top{background:linear-gradient(135deg,#1e3a8a,#2563eb);color:#fff;border-radius:14px;
padding:22px 26px;box-shadow:0 6px 20px rgba(37,99,235,.25)}
header.top h1{margin:0 0 6px;font-size:22px}
header.top .sub{opacity:.92;font-size:13px}
.badges{margin-top:12px;display:flex;flex-wrap:wrap;gap:8px}
.badge{background:rgba(255,255,255,.18);border:1px solid rgba(255,255,255,.35);
padding:3px 10px;border-radius:999px;font-size:12px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px;margin:20px 0}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px;
box-shadow:0 1px 3px rgba(0,0,0,.05)}
.card .k{font-size:12px;color:var(--muted)}
.card .v{font-size:26px;font-weight:700;margin-top:4px}
.v.ok{color:var(--ok)}.v.warn{color:var(--warn)}.v.err{color:var(--err)}
h2.sec{font-size:17px;margin:28px 0 12px;padding-left:10px;border-left:4px solid var(--accent)}
h3.kt{margin:0 0 10px;font-size:15px}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);
border-radius:10px;overflow:hidden}
th,td{padding:8px 10px;text-align:left;border-bottom:1px solid var(--line);font-size:13px}
th{background:#f3f4f6;font-weight:600}
tbody tr:nth-child(even){background:#fafafa}
tbody tr:hover{background:#eff6ff}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
tr.total{font-weight:700;background:#eef2ff}
.pill{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;font-weight:600}
.pill.matched{background:#dcfce7;color:#166534}
.pill.local_only{background:#fef3c7;color:#92400e}
.pill.remote_only{background:#fee2e2;color:#991b1b}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px}
.stat{background:#fbfcfe;border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.stat .k{font-size:12px;color:var(--muted)}.stat .v{font-size:18px;font-weight:600;margin-top:2px}
details{margin-top:12px;background:var(--card);border:1px solid var(--line);border-radius:10px}
summary{cursor:pointer;padding:10px 14px;font-weight:600;font-size:13px}
.scroll{max-height:460px;overflow:auto;border-top:1px solid var(--line)}
.note{color:var(--muted);font-size:12px;margin-top:8px}
.muted{color:var(--muted)}
.empty{padding:20px;text-align:center;color:var(--muted)}
footer{margin-top:30px;color:var(--muted);font-size:12px;border-top:1px solid var(--line);padding-top:14px}
"""


def export_reconcile_html(reports: Sequence[Tuple[str, str, ReconcileReport]], path: Path,
                          *, records: Optional[Sequence[TtsRecord]] = None,
                          remote_other: Optional[Sequence[RemoteAuditRecord]] = None,
                          meta: Optional[Dict[str, Any]] = None,
                          chars_unit_price: float = 2.0,
                          detail_row_limit: int = 3000) -> Path:
    """将对帐结果渲染为自包含 HTML（内联 CSS，无外部依赖）便于浏览器查看。"""
    meta = meta or {}
    records = list(records or [])
    remote_other = list(remote_other or [])
    use_end = meta.get("use_end", True)
    _esc = html.escape
    gen = datetime.now()
    ts_all = [r.timestamp for r in records if r.timestamp]
    date_from = min(ts_all).strftime("%Y-%m-%d %H:%M:%S") if ts_all else "-"
    date_to = max(ts_all).strftime("%Y-%m-%d %H:%M:%S") if ts_all else "-"
    match_by = meta.get("match_by", "auto")
    tolerance = meta.get("tolerance", 5.0)
    align = "结束时刻(start+duration)" if use_end else "开始时刻(start)"
    file_count = meta.get("file_count", len({r.source for r in records}))

    tot_m = sum(len(rep.matched) for _, _, rep in reports)
    tot_lo = sum(len(rep.local_only) for _, _, rep in reports)
    tot_ro = sum(len(rep.remote_only) for _, _, rep in reports)
    tot_local = tot_m + tot_lo
    overall_rate = (tot_m / tot_local * 100.0) if tot_local else 0.0

    H: List[str] = []
    H.append("<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>")
    H.append("<meta name='viewport' content='width=device-width,initial-scale=1'>")
    H.append(f"<title>线上全流程对帐报告 · {_esc(date_from)} ~ {_esc(date_to)}</title>")
    H.append(f"<style>{_HTML_CSS}</style></head><body><div class='wrap'>")

    # 顶部标题栏
    H.append("<header class='top'><h1>线上全流程对帐报告 · TTS / STT / LLM</h1>")
    H.append(f"<div class='sub'>本地服务端日志 ↔ 阿里云百炼审计日志　|　生成时间 {gen.strftime('%Y-%m-%d %H:%M:%S')}</div>")
    H.append("<div class='badges'>")
    H.append(f"<span class='badge'>数据范围：{_esc(date_from)} ~ {_esc(date_to)}</span>")
    H.append(f"<span class='badge'>匹配策略：{_esc(str(match_by))}</span>")
    H.append(f"<span class='badge'>时间容差：{tolerance}s</span>")
    H.append(f"<span class='badge'>对齐口径：{_esc(align)}</span>")
    H.append(f"<span class='badge'>本地日志文件：{file_count} 个</span>")
    H.append("</div></header>")

    # KPI 卡片
    H.append("<div class='cards'>")
    H.append(f"<div class='card'><div class='k'>匹配成功 matched</div><div class='v ok'>{tot_m}</div></div>")
    H.append(f"<div class='card'><div class='k'>仅本地 local_only</div><div class='v warn'>{tot_lo}</div></div>")
    H.append(f"<div class='card'><div class='k'>仅云端 remote_only</div><div class='v err'>{tot_ro}</div></div>")
    H.append(f"<div class='card'><div class='k'>总体匹配率</div><div class='v'>{overall_rate:.1f}%</div></div>")
    H.append("</div>")

    # 对帐总览表
    H.append("<h2 class='sec'>对帐总览</h2>")
    H.append("<table><thead><tr><th>类型</th><th>匹配方式</th><th class='num'>本地</th>"
             "<th class='num'>云端</th><th class='num'>matched</th><th class='num'>local_only</th>"
             "<th class='num'>remote_only</th><th class='num'>匹配率</th></tr></thead><tbody>")
    if not reports:
        H.append("<tr><td colspan='8' class='empty'>无对帐数据（本地日志或云端缓存为空）</td></tr>")
    for kind, mode, rep in reports:
        st = _kind_html_stats(rep, mode, use_end, chars_unit_price)
        H.append("<tr>"
                 f"<td><b>{kind}</b></td><td>{'时间窗' if mode == 'time' else 'requestId'}</td>"
                 f"<td class='num'>{st['local_total']}</td><td class='num'>{st['remote_total']}</td>"
                 f"<td class='num'>{st['matched']}</td><td class='num'>{st['local_only']}</td>"
                 f"<td class='num'>{st['remote_only']}</td><td class='num'>{st['match_rate']:.1f}%</td>"
                 "</tr>")
        H.append("".join([]))
    if reports:
        H.append(f"<tr class='total'><td>合计</td><td></td><td class='num'>{tot_local}</td>"
                 f"<td class='num'>{tot_m + tot_ro}</td><td class='num'>{tot_m}</td>"
                 f"<td class='num'>{tot_lo}</td><td class='num'>{tot_ro}</td>"
                 f"<td class='num'>{overall_rate:.1f}%</td></tr>")
    H.append("</tbody></table>")

    # 本地用量汇总
    by_kind: Dict[str, List[TtsRecord]] = defaultdict(list)
    for r in records:
        by_kind[r.kind].append(r)
    H.append("<h2 class='sec'>本地用量汇总（按 kind，不跨单位相加）</h2>")
    H.append("<table><thead><tr><th>类型</th><th class='num'>记录数</th><th class='num'>带用量记录</th>"
             "<th class='num'>字符总数</th><th class='num'>prompt</th><th class='num'>completion</th>"
             "<th class='num'>total tokens</th></tr></thead><tbody>")
    any_row = False
    for kind in ("TTS", "STT", "LLM"):
        rs = by_kind.get(kind)
        if not rs:
            continue
        any_row = True
        if kind in ("TTS", "STT"):
            chars = [r.chars for r in rs if r.chars is not None]
            H.append(f"<tr><td><b>{kind}</b></td><td class='num'>{len(rs)}</td>"
                     f"<td class='num'>{len(chars)}</td><td class='num'>{sum(chars)}</td>"
                     "<td class='num muted'>-</td><td class='num muted'>-</td><td class='num muted'>-</td></tr>")
        else:
            pt = sum(r.prompt_tokens or 0 for r in rs)
            ct = sum(r.completion_tokens or 0 for r in rs)
            tt = sum(r.total_tokens or 0 for r in rs)
            wt = sum(1 for r in rs if r.total_tokens is not None)
            H.append(f"<tr><td><b>{kind}</b></td><td class='num'>{len(rs)}</td>"
                     f"<td class='num'>{wt}</td><td class='num muted'>-</td>"
                     f"<td class='num'>{pt}</td><td class='num'>{ct}</td><td class='num'>{tt}</td></tr>")
    if not any_row:
        H.append("<tr><td colspan='7' class='empty'>无本地记录</td></tr>")
    H.append("</tbody></table>")

    # 分类型明细
    H.append("<h2 class='sec'>分类型明细</h2>")
    for kind, mode, rep in reports:
        st = _kind_html_stats(rep, mode, use_end, chars_unit_price)
        H.append("<div class='card' style='margin-bottom:16px'>")
        H.append(f"<h3 class='kt'>{kind} <span class='muted' style='font-weight:400;font-size:13px'>"
                 f"· 匹配方式：{'时间窗' if mode == 'time' else 'requestId'}</span></h3>")
        H.append("<div class='grid2'>")
        H.append(f"<div class='stat'><div class='k'>matched</div><div class='v ok'>{st['matched']}</div></div>")
        H.append(f"<div class='stat'><div class='k'>local_only</div><div class='v warn'>{st['local_only']}</div></div>")
        H.append(f"<div class='stat'><div class='k'>remote_only</div><div class='v err'>{st['remote_only']}</div></div>")
        H.append(f"<div class='stat'><div class='k'>匹配率</div><div class='v'>{st['match_rate']:.1f}%</div></div>")
        res = st["residual"]
        if res:
            lbl = "对齐残差" if mode == "time" else "时间偏差"
            H.append(f"<div class='stat'><div class='k'>{lbl}(秒) avg/p50/p95/max</div>"
                     f"<div class='v' style='font-size:14px'>{res['avg']:.3f} / {res['p50']:.3f} / "
                     f"{res['p95']:.3f} / {res['max']:.3f}</div></div>")
        if st["has_chars"]:
            H.append(f"<div class='stat'><div class='k'>字符数 本地/云端</div>"
                     f"<div class='v' style='font-size:16px'>{st['local_chars']} / {st['remote_chars']}</div></div>")
        if st["has_seconds"]:
            H.append(f"<div class='stat'><div class='k'>云端语音时长</div>"
                     f"<div class='v' style='font-size:16px'>{st['remote_sec']:.0f} s</div></div>")
        if st["has_tokens"]:
            H.append(f"<div class='stat'><div class='k'>total_tokens 本地/云端</div>"
                     f"<div class='v' style='font-size:16px'>{st['local_tokens']} / {st['remote_tokens']}</div></div>")
        # 云端成本（按模型单价表估算，不混用单位）
        if st["cost"] > 0 or st["cost_by_model"]:
            H.append(f"<div class='stat'><div class='k'>云端成本估算（按模型单价）</div>"
                     f"<div class='v' style='font-size:16px'>￥{st['cost']:.4f}</div></div>")
        H.append("</div>")
        # 按模型拆分成本明细
        if st["cost_by_model"]:
            H.append("<div class='note' style='margin-top:6px'><b>成本明细（按模型）</b>：")
            parts = []
            for m, v in sorted(st["cost_by_model"].items(), key=lambda kv: -kv[1]["cost"]):
                pk = v["price_kind"]
                if pk == "token":
                    detail = f"in={v['in_tok']} out={v['out_tok']}"
                elif pk == "token_blended":
                    detail = f"total={v['total_tok']} tok"
                elif pk == "chars":
                    detail = f"{v['chars']} 字符"
                elif pk == "seconds":
                    detail = f"{v['sec']:.0f}s"
                else:
                    detail = ""
                tag = "" if v["known"] else " <span style='color:#d97706'>[默认价]</span>"
                parts.append(f"{_esc(m)} n={v['n']} {detail} → ￥{v['cost']:.4f}{tag}")
            H.append("；".join(parts))
            H.append("</div>")
        if st["status_dist"]:
            dist = "，".join(f"{_esc(str(k))}×{v}" for k, v in st["status_dist"].most_common())
            H.append(f"<div class='note'>云端状态分布：{dist}</div>")
        if st["model_dist"]:
            mdist = "，".join(f"{_esc(str(k))}×{v}" for k, v in st["model_dist"].most_common())
            H.append(f"<div class='note'>云端模型分布：{mdist}</div>")
        rows = _reconcile_csv_rows(rep, kind, mode, use_end)
        total_rows = len(rows)
        trunc = "" if total_rows <= detail_row_limit else f"，仅显示前 {detail_row_limit} 行"
        H.append(f"<details><summary>查看对帐明细（{total_rows} 行{trunc}）</summary>")
        H.append("<div class='scroll'><table><thead><tr>")
        for hd in _RECONCILE_CSV_HEADER:
            H.append(f"<th>{_esc(hd)}</th>")
        H.append("</tr></thead><tbody>")
        for row in rows[:detail_row_limit]:
            status = str(row[0])
            H.append(f"<tr><td><span class='pill {_esc(status)}'>{_esc(status)}</span></td>")
            for cell in row[1:]:
                H.append(f"<td>{_esc('' if cell is None else str(cell))}</td>")
            H.append("</tr>")
        H.append("</tbody></table></div></details></div>")

    if remote_other:
        H.append(f"<div class='note' style='margin-top:14px'>另有 {len(remote_other)} 条云端记录无法归类到 "
                 "TTS/STT/LLM（模型名不匹配），未参与对帐。</div>")

    H.append("<footer><b>字段说明</b><br>"
             "· <b>local_only</b>：仅本地日志有、云端审计无 —— 可能云端未落库/调用失败/超保留期/延迟到达。<br>"
             "· <b>remote_only</b>：仅云端审计有、本地日志无 —— 可能本地日志轮转丢失/跨机部署遗漏/AK 被其它项目复用。<br>"
             "· <b>对齐残差</b>：本地对帐行时刻与云端 start+duration 的绝对差，越小说明时间匹配越准。<br>"
             "· 匹配策略 auto：LLM 按 requestId 精确匹配（失败回退时间窗）；TTS/STT 因 DashScope WS 的 "
             "RequestId 是客户端随机 ID、不落审计库，改按时间窗（TTS 叠加字符数）匹配。"
             "</footer>")
    H.append("</div></body></html>")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(H), encoding="utf-8")
    print(f"[ok] 已生成 HTML 对帐报告：{path.resolve()}")
    print(f"     浏览器打开：{path.resolve().as_uri()}")
    return path


def cmd_report(args: argparse.Namespace) -> int:
    """默认全流程：① 按本地服务端日志同步云端审计 → ② 全量对帐 → ③ 导出 HTML。

    云端同步为 best-effort：无 .env / 无网络 / 部分天失败时，仅告警并基于现有缓存继续对帐，
    保证总能产出一份可查看的 HTML（与用户“默认进入就能看结果”的诉求一致）。
    """
    # ① 同步云端数据
    print("=" * 78)
    print("步骤 1/3　同步阿里云审计数据（按本地服务端日志覆盖的日期增量拉取）")
    print("=" * 78)
    if getattr(args, "no_pull", False):
        print("[info] --no-pull：跳过云端同步，直接使用现有缓存")
    else:
        try:
            rc = cmd_pull(args)
            if rc != 0:
                print(f"[warn] 云端同步未完全成功（返回码 {rc}），将基于现有缓存继续对帐", file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            print(f"[warn] 云端同步异常：{type(e).__name__}: {e}；将基于现有缓存继续对帐", file=sys.stderr)

    # ② 全量对帐
    print("\n" + "=" * 78)
    print("步骤 2/3　全量对帐（TTS / STT / LLM）")
    print("=" * 78)
    try:
        records, files, remote, reports, remote_other, meta = _compute_reconcile(args)
    except (RuntimeError, ValueError) as e:
        print(f"[error] {e}", file=sys.stderr)
        return 2
    print_usage_summary(records)
    for kind, mode, rep in reports:
        print_reconcile_report(rep, chars_unit_price=args.chars_unit_price,
                                mode=mode, kind=kind, use_end=meta["use_end"])
    _print_reconcile_totals(reports)

    # ③ 导出 HTML（可选同时导出明细 CSV）
    print("\n" + "=" * 78)
    print("步骤 3/3　生成 HTML 报告")
    print("=" * 78)
    html_path: Path = args.html
    export_reconcile_html(reports, html_path, records=records,
                          remote_other=remote_other, meta=meta,
                          chars_unit_price=args.chars_unit_price)
    if getattr(args, "reconcile_csv", None):
        _export_reconcile_reports(reports, args.reconcile_csv, use_end=meta["use_end"])
    if not getattr(args, "no_open", False):
        try:
            import webbrowser
            if webbrowser.open(html_path.resolve().as_uri()):
                print("[ok] 已在默认浏览器打开报告")
        except Exception:  # noqa: BLE001
            pass
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    argv = list(argv) if argv is not None else sys.argv[1:]
    subcommands = {"summary", "pull", "reconcile", "report"}
    # 默认无子命令（或直接以选项开头，如 --since/--html）时，进入 report 全流程：
    # 同步云端 → 全量对帐 → 导出 HTML。仅 -h/--help 保留顶层帮助。
    if not argv:
        argv = ["report"]
    elif argv[0] not in subcommands and argv[0] not in ("-h", "--help"):
        argv = ["report"] + argv
    args = parser.parse_args(argv)
    if not getattr(args, "cmd", None):
        parser.print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
