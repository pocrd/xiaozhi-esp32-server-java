#!/usr/bin/env python3
"""阿里云百炼消费明细（BSS 账单 CSV）粗略对帐脚本。

数据源：``logs/aliyun_data/*consumedetailbill*.csv``（费用中心手动导出）。
该账单**不含 requestId**，粒度为「分钟 + 模型 + token 类型」，因此只能做
**聚合级粗略对帐**：按天/模型汇总 token 用量与金额，再与本地 TTS 请求数交叉比对量级。

与 ``online_reconcile.py`` 的分工：
  - online_reconcile.py：请求级对帐（本地日志 ↔ SLS 审计日志，LLM 按 requestId、TTS/STT 按时间窗+用量）
  - csv_reconcile.py：金额/用量级粗略对帐（本地日志 ↔ BSS 消费账单），交叉验证总量与成本

常用示例：
    # 自动识别 aliyun_data 下的账单 CSV，汇总并与本地日志交叉对帐
    python3 csv_reconcile.py

    # 指定账单文件与日期区间
    python3 csv_reconcile.py --csv aliyun_data/xxx_consumedetailbillv2.csv --since 2026-09-21

    # 只看某个模型，导出汇总 CSV
    python3 csv_reconcile.py --model-filter qwen-audio --export-csv bill-summary.csv

    # 跳过本地日志交叉对帐（纯账单汇总）
    python3 csv_reconcile.py --no-cross

依赖：仅 Python 3.8+ 标准库（本地日志交叉对帐会复用同目录的 online_reconcile.py）。
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, date
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# 复用同目录的本地日志解析能力（用于交叉对帐）
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import online_reconcile as tr  # type: ignore
    HAVE_TR = True
except Exception:  # noqa: BLE001
    tr = None  # type: ignore
    HAVE_TR = False

DEFAULT_DATA_DIR = Path(__file__).resolve().parent / "aliyun_data"
DEFAULT_LOG_DIR = Path(__file__).resolve().parent / "dialogue"
BILL_GLOB = "*consumedetailbill*.csv"
DEFAULT_MODEL_FILTER = ""   # 默认不过滤，展示全模型（TTS/STT/LLM）各自计费方式

# csv 模块对超大字段（用量详情 JSON）需要放开限制
csv.field_size_limit(10 * 1024 * 1024)


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #

@dataclass
class BillRow:
    """从账单 CSV 解析出的一行关键信息。"""
    detail_id: str = ""
    consume_time: Optional[datetime] = None   # 账单信息/消费时间（分钟粒度）
    bill_date: Optional[date] = None          # 账单信息/账单日期 YYYYMMDD
    fee_type: str = ""                        # 账单信息/费用类型（免费额度/云资源按量费用...）
    product_name: str = ""                    # 产品信息/商品名称
    model: str = ""                           # 从实例ID列提取的模型名
    model_type: str = "OTHER"                 # TTS / STT / LLM / OTHER
    token_type: str = ""                      # *_input_token / *_output_token / 空
    usage_raw: float = 0.0                    # 用量信息/抵扣前用量（原始值）
    usage_unit: str = ""                      # 用量信息/用量单位（千tokens/万字/秒）
    usage: float = 0.0                        # 归一化后用量
    base_unit: str = ""                       # 归一化后单位（tokens/字符/秒）
    list_price: float = 0.0                   # 费用信息/目录总价
    payable: float = 0.0                      # 应付信息/应付金额（含税）


@dataclass
class Agg:
    """聚合器：按「归一化单位」分别累加用量，不同单位不混合相加。"""
    records: int = 0
    minutes: set = field(default_factory=set)                 # distinct 消费时间（分钟）
    usage: Dict[str, float] = field(default_factory=dict)     # base_unit → 总用量
    input_usage: Dict[str, float] = field(default_factory=dict)
    output_usage: Dict[str, float] = field(default_factory=dict)
    free_records: int = 0                                     # 费用类型=免费额度 的行数
    free_usage: Dict[str, float] = field(default_factory=dict)
    list_price: float = 0.0
    payable: float = 0.0

    def add(self, r: BillRow) -> None:
        self.records += 1
        if r.consume_time:
            self.minutes.add(r.consume_time.replace(second=0, microsecond=0))
        u = r.base_unit or "?"
        self.usage[u] = self.usage.get(u, 0.0) + r.usage
        if "input" in r.token_type:
            self.input_usage[u] = self.input_usage.get(u, 0.0) + r.usage
        elif "output" in r.token_type:
            self.output_usage[u] = self.output_usage.get(u, 0.0) + r.usage
        if "免费" in r.fee_type:
            self.free_records += 1
            self.free_usage[u] = self.free_usage.get(u, 0.0) + r.usage
        self.list_price += r.list_price
        self.payable += r.payable


# --------------------------------------------------------------------------- #
# CSV 解析
# --------------------------------------------------------------------------- #

def _find_col(header: Sequence[str], *keywords: str) -> int:
    """按关键字（全部命中）在表头中定位列索引，找不到返回 -1。"""
    for i, name in enumerate(header):
        n = name.replace(" ", "")
        if all(k.replace(" ", "") in n for k in keywords):
            return i
    return -1


def _to_float(v: Any) -> float:
    if v is None:
        return 0.0
    s = str(v).strip().replace(",", "")
    if not s:
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def _parse_consume_time(v: Any) -> Optional[datetime]:
    s = str(v or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S", "%Y%m%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _parse_bill_date(v: Any) -> Optional[date]:
    s = str(v or "").strip()
    if not s:
        return None
    for fmt in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def parse_spec(spec: str) -> Tuple[str, str]:
    """从 产品信息/规格 中提取 (model, token_type)。

    形如 ``;llm-33cfnl3dvbcx0dir;qwen-audio-3.1-tts-flash;tts_output_token;0``。
    """
    parts = [p.strip() for p in (spec or "").split(";") if p.strip()]
    model = ""
    token_type = ""
    for i, p in enumerate(parts):
        if p.endswith("_token") or "token" in p.lower():
            token_type = p
            if i > 0:
                model = parts[i - 1]
            break
    if not model:
        # 回退：挑一个像模型名的片段
        for p in parts:
            low = p.lower()
            if any(k in low for k in ("qwen", "cosyvoice", "sambert", "tts", "paraformer", "gpt")):
                model = p
                break
    return model, token_type


# 不同模型计费单位不同，归一化为可读基础单位（不可跨单位相加）
UNIT_NORMALIZE: Dict[str, Tuple[str, float]] = {
    "千tokens": ("tokens", 1000.0),
    "千token": ("tokens", 1000.0),
    "tokens": ("tokens", 1.0),
    "token": ("tokens", 1.0),
    "万字": ("字符", 10000.0),
    "万字符": ("字符", 10000.0),
    "字": ("字符", 1.0),
    "字符": ("字符", 1.0),
    "秒": ("秒", 1.0),
    "次": ("次", 1.0),
}

TTS_PAT = re.compile(r"tts|cosyvoice|sambert", re.IGNORECASE)
STT_PAT = re.compile(r"paraformer|asr|gummy|sensevoice|recogni|听", re.IGNORECASE)
LLM_KEYS = ("qwen", "gpt", "llm", "max", "plus", "flash", "turbo", "deepseek")


def normalize_usage(value: float, unit: str) -> Tuple[float, str]:
    """把用量按单位归一化：千tokens→tokens、万字→字符、秒→秒。返回 (值, 基础单位)。"""
    key = (unit or "").strip().replace(" ", "")
    if key in UNIT_NORMALIZE:
        name, factor = UNIT_NORMALIZE[key]
        return value * factor, name
    if key.startswith("千"):
        return value * 1000.0, (key[1:] or key)
    if key.startswith("万"):
        return value * 10000.0, (key[1:] or key)
    return value, (key or "?")


def classify_model(model: str) -> str:
    """根据模型名判定类型：TTS / STT / LLM / OTHER。"""
    m = (model or "").lower()
    if not m:
        return "OTHER"
    if TTS_PAT.search(m):
        return "TTS"
    if STT_PAT.search(m):
        return "STT"
    if any(k in m for k in LLM_KEYS):
        return "LLM"
    return "OTHER"


def parse_bill_csv(path: Path) -> List[BillRow]:
    rows: List[BillRow] = []
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.reader(fh)
        try:
            header = next(reader)
        except StopIteration:
            return rows
        c_id = _find_col(header, "账单明细ID")
        c_ct = _find_col(header, "消费时间")
        c_bd = _find_col(header, "账单日期")
        c_fee = _find_col(header, "费用类型")
        c_prod = _find_col(header, "商品名称")
        # 模型名与 token 类型藏在「资源信息/实例ID（出账粒度）」列，形如
        # ;llm-xxx;qwen-audio-3.1-tts-flash;tts_output_token;0
        c_spec = _find_col(header, "实例ID")
        if c_spec < 0:
            c_spec = _find_col(header, "规格")
        c_ub = _find_col(header, "抵扣前用量")
        c_unit = _find_col(header, "用量单位")
        c_list = _find_col(header, "目录总价")
        c_pay = _find_col(header, "应付金额")
        for raw in reader:
            if not raw or len(raw) < 5:
                continue

            def cell(idx: int) -> str:
                return raw[idx].strip() if 0 <= idx < len(raw) else ""

            spec = cell(c_spec)
            model, token_type = parse_spec(spec)
            ub = _to_float(cell(c_ub))
            unit = cell(c_unit)
            base_usage, base_unit = normalize_usage(ub, unit)
            r = BillRow(
                detail_id=cell(c_id),
                consume_time=_parse_consume_time(cell(c_ct)),
                bill_date=_parse_bill_date(cell(c_bd)),
                fee_type=cell(c_fee),
                product_name=cell(c_prod),
                model=model,
                model_type=classify_model(model),
                token_type=token_type,
                usage_raw=ub,
                usage_unit=unit,
                usage=base_usage,
                base_unit=base_unit,
                list_price=_to_float(cell(c_list)),
                payable=_to_float(cell(c_pay)),
            )
            rows.append(r)
    return rows


# --------------------------------------------------------------------------- #
# 聚合与展示
# --------------------------------------------------------------------------- #

def day_of(r: BillRow) -> Optional[date]:
    if r.bill_date:
        return r.bill_date
    if r.consume_time:
        return r.consume_time.date()
    return None


def filter_rows(rows: Sequence[BillRow], model_re: Optional[re.Pattern],
                 since: Optional[date], until: Optional[date]) -> List[BillRow]:
    out: List[BillRow] = []
    for r in rows:
        if model_re is not None and not model_re.search(r.model or ""):
            continue
        d = day_of(r)
        if since and (d is None or d < since):
            continue
        if until and (d is None or d > until):
            continue
        out.append(r)
    return out


def _fmt_num(n: float) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    if n >= 1:
        return f"{n:.0f}"
    return f"{n:.2f}"


def _fmt_usage(d: Dict[str, float]) -> str:
    """把多单位用量拼成可读字符串，如 ``165K tokens`` / ``1.2万字符`` / ``3.6K 秒``。"""
    if not d:
        return "-"
    parts = [f"{_fmt_num(v)}{u}" for u, v in sorted(d.items(), key=lambda kv: -kv[1]) if v]
    return " + ".join(parts) if parts else "-"


def _usage_flat(d: Dict[str, float]) -> str:
    """导出用：``tokens=165000; 字符=120000``。"""
    return "; ".join(f"{u}={v:.0f}" for u, v in sorted(d.items(), key=lambda kv: -kv[1]) if v)


def _agg_by_day(rows: Sequence[BillRow]) -> Dict[date, Agg]:
    out: Dict[date, Agg] = defaultdict(Agg)
    for r in rows:
        d = day_of(r)
        if d:
            out[d].add(r)
    return dict(out)


def print_model_summary(rows: Sequence[BillRow], top: int) -> None:
    by_model: Dict[str, Agg] = defaultdict(Agg)
    mtype: Dict[str, str] = {}
    for r in rows:
        key = r.model or "(未知模型)"
        by_model[key].add(r)
        mtype[key] = r.model_type
    print("\n按模型汇总（用量按各自计费单位区分，不跨单位相加）：")
    hdr = (f"  {'模型':<28}{'类型':<6}{'记录':>6}{'分钟':>6}  "
           f"{'用量':<20}{'input':<14}{'output':<14}{'目录价':>10}{'应付':>9}")
    print(hdr)
    print("  " + "-" * 108)
    items = sorted(by_model.items(), key=lambda kv: kv[1].records, reverse=True)
    if top > 0:
        items = items[:top]
    for model, a in items:
        print(f"  {model:<28}{mtype.get(model,''):<6}{a.records:>6}{len(a.minutes):>6}  "
              f"{_fmt_usage(a.usage):<20}{_fmt_usage(a.input_usage):<14}{_fmt_usage(a.output_usage):<14}"
              f"{a.list_price:>10.4f}{a.payable:>9.4f}")


def print_type_summary(rows: Sequence[BillRow]) -> None:
    by_type: Dict[str, Agg] = defaultdict(Agg)
    for r in rows:
        by_type[r.model_type].add(r)
    print("\n按模型类型汇总：")
    order = [t for t in ("TTS", "STT", "LLM", "OTHER") if t in by_type]
    for t in order:
        a = by_type[t]
        print(f"  {t:<6} 记录 {a.records:>6}  分钟 {len(a.minutes):>6}  "
              f"用量 {_fmt_usage(a.usage):<26} 目录价 {a.list_price:>10.4f}  应付 {a.payable:>9.4f}")


def print_day_summary(rows: Sequence[BillRow], top: int) -> Dict[date, Agg]:
    by_day = _agg_by_day(rows)
    print("\n按天汇总（用量含多种计费单位，分单位展示）：")
    hdr = f"  {'日期':<12}{'记录':>6}{'分钟':>6}  {'用量':<30}{'目录价':>10}{'应付':>9}"
    print(hdr)
    print("  " + "-" * 76)
    items = sorted(by_day.items())
    shown = items if top <= 0 else items[-top:]
    for d, a in shown:
        print(f"  {d.isoformat():<12}{a.records:>6}{len(a.minutes):>6}  "
              f"{_fmt_usage(a.usage):<30}{a.list_price:>10.4f}{a.payable:>9.4f}")
    return by_day


def print_fee_type_summary(rows: Sequence[BillRow]) -> None:
    by_fee: Dict[str, Agg] = defaultdict(Agg)
    for r in rows:
        by_fee[r.fee_type or "(空)"].add(r)
    print("\n按费用类型汇总：")
    for fee, a in sorted(by_fee.items(), key=lambda kv: kv[1].records, reverse=True):
        print(f"  {fee:<14} 记录 {a.records:>6}   用量 {_fmt_usage(a.usage):<26}"
              f"   目录价 {a.list_price:>10.4f}   应付 {a.payable:>9.4f}")


# --------------------------------------------------------------------------- #
# 与本地日志交叉对帐（粗略）
# --------------------------------------------------------------------------- #

def collect_local_day_counts(log_dir: Path, kinds: Sequence[str],
                              since: Optional[date], until: Optional[date]) -> Dict[date, int]:
    """统计本地日志中每天的 TTS 请求数（按 requestId 计数）。"""
    if not HAVE_TR:
        return {}
    inputs = [log_dir]
    try:
        files = list(tr.iter_log_files(inputs))
    except Exception:  # noqa: BLE001
        return {}
    counts: Dict[date, int] = defaultdict(int)
    kind_upper = [k.upper() for k in kinds]
    for f in files:
        try:
            recs = tr.parse_file(f, kind_upper)
        except Exception:  # noqa: BLE001
            continue
        for r in recs:
            d = r.timestamp.date()
            if since and d < since:
                continue
            if until and d > until:
                continue
            counts[d] += 1
    return dict(counts)


def print_cross_reconcile(tts_by_day: Dict[date, Agg], local_counts: Dict[date, int]) -> None:
    print("\n与本地日志交叉对帐（仅 TTS；账单无 requestId，只比对量级）：")
    hdr = (f"  {'日期':<12}{'本地请求':>9}{'账单记录':>9}{'账单分钟':>9}  "
           f"{'TTS用量':<22}{'目录价':>9}{'应付':>9}")
    print(hdr)
    print("  " + "-" * 84)
    all_days = sorted(set(tts_by_day) | set(local_counts))
    tot_local = 0
    for d in all_days:
        a = tts_by_day.get(d)
        lc = local_counts.get(d, 0)
        tot_local += lc
        recs = a.records if a else 0
        mins = len(a.minutes) if a else 0
        usage = _fmt_usage(a.usage) if a else "-"
        lp = a.list_price if a else 0.0
        pay = a.payable if a else 0.0
        print(f"  {d.isoformat():<12}{lc:>9}{recs:>9}{mins:>9}  {usage:<22}{lp:>9.4f}{pay:>9.4f}")
    print(f"\n  本地 TTS 请求合计：{tot_local}")
    if not local_counts:
        print("  [warn] 本地日志未统计到请求数（未找到 online_reconcile.py 或日志为空）")
    print("  [note] cosyvoice 按字符计费、qwen-audio-tts 按 token 计费，用量不可直接相加；"
          "请求数与账单分钟数仅供量级参考。")


# --------------------------------------------------------------------------- #
# 导出
# --------------------------------------------------------------------------- #

def export_summary_csv(bill_by_day: Dict[date, Agg], tts_by_day: Dict[date, Agg],
                        local_counts: Dict[date, int], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    all_days = sorted(set(bill_by_day) | set(tts_by_day) | set(local_counts))
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["date", "local_tts_requests",
                     "bill_records", "bill_minutes", "bill_usage",
                     "tts_records", "tts_minutes", "tts_usage",
                     "list_price", "payable"])
        for d in all_days:
            a = bill_by_day.get(d, Agg())
            t = tts_by_day.get(d, Agg())
            w.writerow([
                d.isoformat(), local_counts.get(d, 0),
                a.records, len(a.minutes), _usage_flat(a.usage),
                t.records, len(t.minutes), _usage_flat(t.usage),
                f"{a.list_price:.4f}", f"{a.payable:.4f}",
            ])
    print(f"\n[ok] 已导出按天汇总：{path}（{len(all_days)} 行）")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _parse_day(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    return datetime.strptime(s.strip(), "%Y-%m-%d").date()


def find_bill_csvs(data_dir: Path, explicit: Optional[Path]) -> List[Path]:
    if explicit:
        return [explicit] if explicit.exists() else []
    if not data_dir.exists():
        return []
    return sorted(data_dir.glob(BILL_GLOB))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="csv_reconcile.py",
        description="阿里云百炼消费明细（BSS 账单 CSV）粗略对帐：按天/模型汇总 token 与金额，并与本地日志交叉比对。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--csv", type=Path, default=None,
                   help="显式指定账单 CSV；不传则在 --csv-dir 下自动匹配 *consumedetailbill*.csv")
    p.add_argument("--csv-dir", type=Path, default=DEFAULT_DATA_DIR,
                   help=f"账单 CSV 目录（默认 {DEFAULT_DATA_DIR}）")
    p.add_argument("--model-filter", type=str, default=DEFAULT_MODEL_FILTER,
                   help="模型名正则过滤（默认空=全模型）；例如 'tts|cosyvoice' 只看 TTS")
    p.add_argument("--since", type=str, default=None, help="起始日期 YYYY-MM-DD（含）")
    p.add_argument("--until", type=str, default=None, help="结束日期 YYYY-MM-DD（含）")
    p.add_argument("--by", type=str, default="model,type,day",
                   help="汇总维度，逗号分隔，可选 model,type,day,fee（默认 model,type,day）")
    p.add_argument("--top", type=int, default=0, help="每个维度展示上限，0 表全部（默认 0）")
    p.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR,
                   help=f"本地日志目录，用于交叉对帐（默认 {DEFAULT_LOG_DIR}）")
    p.add_argument("--kind", type=str, default="TTS", help="交叉对帐时统计的本地对帐类型（默认 TTS）")
    p.add_argument("--no-cross", action="store_true", help="跳过与本地日志的交叉对帐")
    p.add_argument("--export-csv", type=Path, default=None, help="导出按天汇总 CSV")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    bills = find_bill_csvs(args.csv_dir, args.csv)
    if not bills:
        print(f"[error] 未找到账单 CSV（--csv={args.csv} --csv-dir={args.csv_dir}）", file=sys.stderr)
        return 2

    try:
        since = _parse_day(args.since)
        until = _parse_day(args.until)
    except ValueError as e:
        print(f"[error] 日期格式应为 YYYY-MM-DD：{e}", file=sys.stderr)
        return 2

    model_re = re.compile(args.model_filter, re.IGNORECASE) if args.model_filter else None

    all_rows: List[BillRow] = []
    for b in bills:
        print(f"[info] 解析账单：{b.name}")
        try:
            rows = parse_bill_csv(b)
        except Exception as e:  # noqa: BLE001
            print(f"[error] 解析 {b} 失败：{type(e).__name__}: {e}", file=sys.stderr)
            return 3
        print(f"       → {len(rows)} 行")
        all_rows.extend(rows)

    if not all_rows:
        print("[warn] 账单为空")
        return 0

    # 全量时间范围
    days_all = [day_of(r) for r in all_rows if day_of(r)]
    if days_all:
        print(f"\n[info] 账单总行数 {len(all_rows)}，时间范围 {min(days_all)} ~ {max(days_all)}")

    filtered = filter_rows(all_rows, model_re, since, until)
    filt_desc = args.model_filter or "(全部模型)"
    print(f"[info] 过滤后 {len(filtered)} 行（模型匹配 {filt_desc}"
          f"{f'，{since}~{until}' if (since or until) else ''}）")
    if not filtered:
        print("[warn] 过滤后无数据")
        return 0

    dims = [d.strip().lower() for d in (args.by or "").split(",") if d.strip()]
    if "model" in dims:
        print_model_summary(filtered, args.top)
    if "type" in dims:
        print_type_summary(filtered)
    bill_by_day: Dict[date, Agg] = {}
    if "day" in dims:
        bill_by_day = print_day_summary(filtered, args.top)
    if "fee" in dims:
        print_fee_type_summary(filtered)
    if not bill_by_day:
        bill_by_day = _agg_by_day(filtered)

    # 交叉对帐只用 TTS 行
    tts_by_day = _agg_by_day([r for r in filtered if r.model_type == "TTS"])

    need_local = (not args.no_cross) or (args.export_csv is not None)
    local_counts: Dict[date, int] = {}
    if need_local:
        kinds = [k.strip() for k in (args.kind or "TTS").split(",") if k.strip()]
        local_counts = collect_local_day_counts(args.log_dir, kinds, since, until)

    if not args.no_cross:
        print_cross_reconcile(tts_by_day, local_counts)

    if args.export_csv:
        export_summary_csv(bill_by_day, tts_by_day, local_counts, args.export_csv)

    return 0


if __name__ == "__main__":
    sys.exit(main())
