#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
设备日志分析脚本 - 交互式 HTML 报告版
分析 device-log*.log 文件，生成交互式 HTML 仪表盘。
"""

import os
import re
import sys
import glob
import json
import argparse
from datetime import datetime
from collections import defaultdict
from typing import List, Dict, Optional
import statistics


# ─── 数据模型 ───────────────────────────────────────────────────────────────

class LogEntry:
    __slots__ = ('timestamp', 'device_id', 'ts', 'level', 'msg_type',
                 'feature', 'segment', 'bytes_count', 'duration_ms', 'speed_bps', 'source_file')
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


# ─── 日志解析 ────────────────────────────────────────────────────────────────

LOG_PREFIX = re.compile(
    r'^(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})\s+'
    r'(?P<level>\w+)\s+-\s+\[设备日志\]\s+'
    r'deviceId=(?P<device_id>[^,]+),\s+'
    r'ts=(?P<ts>\d+),\s+'
    r'level=(?P<level2>\w+),\s+'
    r'(?P<rest>.*)$'
)
DOWNLOAD_RE = re.compile(
    r'^下行\s+(?P<feature>.+?)\s+段=(?P<segment>\d+)\s+'
    r'字节=(?P<bytes>\d+)\s+耗时=(?P<duration>\d+)ms\s+速率=(?P<speed>\d+)B/s$'
)
VOICE_RE = re.compile(
    r'^(?P<feature>算卦)\s+语音字节=(?P<bytes>\d+)\s+'
    r'耗时=(?P<duration>\d+)ms\s+速率=(?P<speed>\d+)B/s$'
)
TEXT_RE = re.compile(
    r'^(?P<feature>.+?)\s+文本字节=(?P<bytes>\d+)\s+'
    r'耗时=(?P<duration>\d+)ms\s+速率=(?P<speed>\d+)B/s$'
)


def parse_line(line: str, source_file: str) -> Optional[LogEntry]:
    line = line.strip()
    if not line:
        return None
    m = LOG_PREFIX.match(line)
    if not m:
        return None
    rest = m.group('rest')
    kw = dict(timestamp=m.group('timestamp'), device_id=m.group('device_id').strip(),
              ts=int(m.group('ts')), level=m.group('level'), source_file=source_file)
    dm = DOWNLOAD_RE.match(rest)
    if dm:
        return LogEntry(msg_type='download', feature=dm.group('feature'),
                        segment=int(dm.group('segment')), bytes_count=int(dm.group('bytes')),
                        duration_ms=int(dm.group('duration')), speed_bps=int(dm.group('speed')), **kw)
    vm = VOICE_RE.match(rest)
    if vm:
        return LogEntry(msg_type='voice', feature=vm.group('feature'), segment=None,
                        bytes_count=int(vm.group('bytes')), duration_ms=int(vm.group('duration')),
                        speed_bps=int(vm.group('speed')), **kw)
    tm = TEXT_RE.match(rest)
    if tm:
        return LogEntry(msg_type='text', feature=tm.group('feature'), segment=None,
                        bytes_count=int(tm.group('bytes')), duration_ms=int(tm.group('duration')),
                        speed_bps=int(tm.group('speed')), **kw)
    return LogEntry(msg_type='other', feature=rest, segment=None,
                    bytes_count=0, duration_ms=0, speed_bps=0, **kw)


def load_all_logs(log_dir: str) -> List[LogEntry]:
    files = sorted(set(glob.glob(os.path.join(log_dir, 'device-log*.log'))))
    entries = []
    for fpath in files:
        with open(fpath, 'r', encoding='utf-8') as f:
            for line in f:
                entry = parse_line(line, os.path.basename(fpath))
                if entry:
                    entries.append(entry)
    entries.sort(key=lambda e: e.timestamp)
    return entries


# ─── 服务端日志解析 ──────────────────────────────────────────────────────────

SERVER_WS_OPEN_RE = re.compile(
    r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}).*'
    r'WebSocket连接建立成功\s+-\s*'
    r'SessionId:\s*([^,]+),\s*DeviceId:\s*(\S+)'
)
SERVER_WS_CLOSE_RE = re.compile(
    r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}).*'
    r'WebSocket连接关闭\s+-\s*'
    r'SessionId:\s*([^,]+),\s*DeviceId:\s*(\S+)'
)


def parse_server_logs(log_dir: str) -> List[Dict]:
    """
    解析服务端 xiaozhi-dialogue*.log 日志，提取 WebSocket 会话。
    返回 [{session_id, device_id, start_ts_ms, end_ts_ms}, ...]
    """
    files = sorted(glob.glob(os.path.join(log_dir, 'xiaozhi-dialogue*.log')))
    # 排除 error 日志
    files = [f for f in files if '-error' not in os.path.basename(f)]

    opens = {}   # session_id -> (timestamp_str, device_id)
    sessions = []

    for fpath in files:
        with open(fpath, 'r', encoding='utf-8') as f:
            for line in f:
                m = SERVER_WS_OPEN_RE.match(line)
                if m:
                    ts_str, sid, dev = m.group(1), m.group(2).strip(), m.group(3).strip()
                    opens[sid] = (ts_str, dev)
                    continue
                m = SERVER_WS_CLOSE_RE.match(line)
                if m:
                    ts_str, sid, dev = m.group(1), m.group(2).strip(), m.group(3).strip()
                    if sid in opens:
                        open_ts_str, open_dev = opens.pop(sid)
                        try:
                            start_dt = datetime.strptime(open_ts_str, '%Y-%m-%d %H:%M:%S.%f')
                            end_dt = datetime.strptime(ts_str, '%Y-%m-%d %H:%M:%S.%f')
                            sessions.append({
                                'session_id': sid,
                                'device_id': open_dev,
                                'start_ts_ms': int(start_dt.timestamp() * 1000),
                                'end_ts_ms': int(end_dt.timestamp() * 1000),
                            })
                        except Exception:
                            pass

    # 未关闭的连接，用最后一条日志时间作为结束时间
    for sid, (ts_str, dev) in opens.items():
        try:
            start_dt = datetime.strptime(ts_str, '%Y-%m-%d %H:%M:%S.%f')
            sessions.append({
                'session_id': sid,
                'device_id': dev,
                'start_ts_ms': int(start_dt.timestamp() * 1000),
                'end_ts_ms': int(start_dt.timestamp() * 1000) + 1000,
            })
        except Exception:
            pass

    sessions.sort(key=lambda s: s['start_ts_ms'])
    return sessions


# ─── 会话聚合 ────────────────────────────────────────────────────────────────

def build_sessions(entries: List[LogEntry]) -> List[Dict]:
    """
    将日志条目聚合为会话。
    会话 = 一次上行(语音/文本提问) + 后续连续下行段(响应)。
    算卦 = 语音提问 → 多段下行响应
    今日运势/八字解密 = 文本提问 → 多段下行响应
    """
    by_dev = defaultdict(list)
    for e in entries:
        by_dev[e.device_id].append(e)

    sessions = []
    for dev_id, dev_entries in by_dev.items():
        dev_entries.sort(key=lambda e: e.timestamp)
        by_feat = defaultdict(list)
        for e in dev_entries:
            by_feat[e.feature].append(e)

        for feature, feat_entries in by_feat.items():
            current = None
            for e in feat_entries:
                if e.msg_type in ('voice', 'text'):
                    # 保存上一个未完成的会话
                    if current and current['dl_segments']:
                        _finalize_session(current, sessions)
                    # 开始新会话
                    current = {
                        'device_id': dev_id, 'feature': feature,
                        'upload_type': e.msg_type, 'upload_ts': e.ts,
                        'upload_bytes': e.bytes_count, 'upload_duration_ms': e.duration_ms,
                        'upload_speed_bps': e.speed_bps,
                        'upload_timestamp': e.timestamp,
                        'date': e.timestamp[:10], 'hour': int(e.timestamp[11:13]),
                        'dl_segments': [],
                    }
                elif e.msg_type == 'download':
                    if current is None:
                        # 无上行记录(如今日运势/八字解密的文本提问未被记录)，下行段直接开始新会话
                        current = {
                            'device_id': dev_id, 'feature': feature,
                            'upload_type': None, 'upload_ts': None,
                            'upload_bytes': 0, 'upload_duration_ms': 0,
                            'upload_speed_bps': 0, 'upload_timestamp': None,
                            'date': e.timestamp[:10], 'hour': int(e.timestamp[11:13]),
                            'dl_segments': [e],
                        }
                    else:
                        expected = len(current['dl_segments']) + 1
                        if e.segment == expected:
                            current['dl_segments'].append(e)
                        else:
                            # 段号不连续，结束当前会话，开始新会话
                            if current['dl_segments']:
                                _finalize_session(current, sessions)
                            current = {
                                'device_id': dev_id, 'feature': feature,
                                'upload_type': None, 'upload_ts': None,
                                'upload_bytes': 0, 'upload_duration_ms': 0,
                                'upload_speed_bps': 0, 'upload_timestamp': None,
                                'date': e.timestamp[:10], 'hour': int(e.timestamp[11:13]),
                                'dl_segments': [e],
                            }

            # 处理最后一个会话
            if current and current['dl_segments']:
                _finalize_session(current, sessions)

    sessions.sort(key=lambda s: s['timestamp'])
    return sessions


def _finalize_session(s: Dict, out: list):
    """将会话聚合结果转为紧凑 dict 并追加到 out"""
    dl_segs = s['dl_segments']
    dl_bytes = sum(e.bytes_count for e in dl_segs)
    dl_dur = sum(e.duration_ms for e in dl_segs)
    dl_speeds = [e.speed_bps for e in dl_segs if e.speed_bps > 0]
    last_seg = dl_segs[-1]
    first_seg = dl_segs[0]
    session_end_ts = last_seg.ts + last_seg.duration_ms

    ttfb = (first_seg.ts - s['upload_ts']) if s['upload_ts'] else None
    e2e = (session_end_ts - s['upload_ts']) if s['upload_ts'] else None

    out.append({
        'sid': len(out),
        'device_id': s['device_id'], 'feature': s['feature'],
        'timestamp': s['upload_timestamp'] or first_seg.timestamp,
        'date': s['date'], 'hour': s['hour'],
        'upload_type': s['upload_type'],
        'upload_bytes': s['upload_bytes'],
        'upload_duration_ms': s['upload_duration_ms'],
        'upload_speed_bps': s['upload_speed_bps'],
        'dl_bytes': dl_bytes, 'dl_duration_ms': dl_dur,
        'dl_segments': len(dl_segs),
        'dl_speed_avg': round(sum(dl_speeds) / len(dl_speeds)) if dl_speeds else 0,
        'dl_speed_max': max(dl_speeds) if dl_speeds else 0,
        'ttfb_ms': ttfb, 'e2e_ms': e2e,
    })


def _build_concurrency_events(server_sessions: List[Dict]) -> List[tuple]:
    """构建并发度扫描线事件列表：(ts_ms, +1/-1, device_id)，按 (时间, 增减) 排序。
    同一时刻先处理 -1(关闭) 再处理 +1(建立)，与峰值/错误率统计口径保持一致。"""
    events = []
    for ss in server_sessions:
        events.append((ss['start_ts_ms'], 1, ss['device_id']))
        events.append((ss['end_ts_ms'], -1, ss['device_id']))
    events.sort(key=lambda x: (x[0], x[1]))
    return events


def _add_session_concurrency(sessions: List[Dict], server_sessions: List[Dict]):
    """
    为每个设备端会话计算其起始时刻的并发设备数（基于服务端 WebSocket 连接数据）。
    结果直接写入 session['concurrency'] 字段。
    采用扫描线单次推进：会话按时刻升序遍历，事件指针只前进不回退，
    整体复杂度 O((S+E)logE)，避免每个会话重复扫描全部事件。
    """
    events = _build_concurrency_events(server_sessions)

    # 解析每个会话起始时刻；无法解析的直接置 1
    timed = []
    for s in sessions:
        try:
            dt = datetime.strptime(s['timestamp'], '%Y-%m-%d %H:%M:%S.%f')
            timed.append((int(dt.timestamp() * 1000), s))
        except Exception:
            s['concurrency'] = 1
    timed.sort(key=lambda x: x[0])

    active = defaultdict(int)
    ei, n = 0, len(events)
    for t, s in timed:
        while ei < n and events[ei][0] <= t:
            _, delta, dev = events[ei]
            active[dev] += delta
            if active[dev] <= 0:
                del active[dev]
            ei += 1
        s['concurrency'] = max(len(active), 1)


# ─── 数据准备 (供前端 JS 使用) ────────────────────────────────────────────────

def prepare_frontend_data(entries: List[LogEntry], server_sessions: List[Dict]) -> Dict:
    """将日志条目转为前端可用的 JSON 数据（含原始条目 + 聚合会话）"""
    raw = [{
        'timestamp': e.timestamp, 'date': e.timestamp[:10], 'hour': int(e.timestamp[11:13]),
        'device_id': e.device_id, 'msg_type': e.msg_type, 'feature': e.feature,
        'direction': 'download' if e.msg_type == 'download' else 'upload',
        'segment': e.segment, 'bytes_count': e.bytes_count,
        'duration_ms': e.duration_ms, 'speed_bps': e.speed_bps,
    } for e in entries]
    sessions = build_sessions(entries)
    _add_session_concurrency(sessions, server_sessions)
    return {'entries': raw, 'sessions': sessions}


def prepare_summary(entries: List[LogEntry], server_sessions: List[Dict]) -> Dict:
    """计算摘要卡片数据"""
    devices = sorted(set(e.device_id for e in entries))
    dates = sorted(set(e.timestamp[:10] for e in entries))
    type_counts = defaultdict(int)
    feature_counts = defaultdict(int)
    for e in entries:
        type_counts[e.msg_type] += 1
        if e.msg_type != 'other':
            feature_counts[e.feature] += 1
    total_bytes = sum(e.bytes_count for e in entries)
    concurrency = compute_concurrency_stats(server_sessions)
    return {
        'total_records': len(entries),
        'date_range': f"{dates[0]} ~ {dates[-1]}" if dates else "",
        'device_count': len(devices),
        'devices': devices,
        'dates': dates,
        'features': sorted(feature_counts.keys()),
        'type_counts': dict(type_counts),
        'feature_counts': dict(feature_counts),
        'total_bytes_mb': round(total_bytes / 1024 / 1024, 2),
        'concurrency': concurrency,
    }


def compute_concurrency_stats(server_sessions: List[Dict]) -> Dict:
    """
    并发度统计：基于服务端 WebSocket 连接的起止时间，计算同时活跃的最大设备数。
    使用扫描线算法，同时按小时统计并发度曲线。
    """
    if not server_sessions:
        return {'peak': 0, 'peak_time': '', 'hourly': [0]*24, 'timeline': []}

    # 构建事件列表：(timestamp_ms, +1/-1, device_id)
    events = _build_concurrency_events(server_sessions)

    # 扫描线求峰值并发设备数
    active = defaultdict(int)
    peak = 0
    peak_ts = 0
    for ts, delta, dev in events:
        active[dev] += delta
        if active[dev] <= 0:
            del active[dev]
        n_active = len(active)
        if n_active > peak:
            peak = n_active
            peak_ts = ts

    try:
        peak_time = datetime.fromtimestamp(peak_ts / 1000).strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        peak_time = ''

    # 按小时统计并发度：对每个小时统计在该小时内有活跃连接的不同设备数
    hourly = [0] * 24
    min_ts = events[0][0]
    base_date = datetime.fromtimestamp(min_ts / 1000).replace(hour=0, minute=0, second=0, microsecond=0)
    for h in range(24):
        hour_start = base_date.replace(hour=h)
        hour_start_ts = int(hour_start.timestamp() * 1000)
        hour_end_ts = hour_start_ts + 3600000
        devices_in_hour = set()
        for ss in server_sessions:
            if ss['start_ts_ms'] < hour_end_ts and ss['end_ts_ms'] > hour_start_ts:
                devices_in_hour.add(ss['device_id'])
        hourly[h] = len(devices_in_hour)

    # 时间线数据：在每个事件时间点采样并发设备数，确保能捕获峰值
    timeline = []
    active_devs = defaultdict(int)
    for ts, delta, dev in events:
        active_devs[dev] += delta
        if active_devs[dev] <= 0:
            del active_devs[dev]
        timeline.append({
            'ts': ts,
            'label': datetime.fromtimestamp(ts / 1000).strftime('%m-%d %H:%M'),
            'count': len(active_devs),
        })

    return {
        'peak': peak,
        'peak_time': peak_time,
        'hourly': hourly,
        'timeline': timeline,
    }


# ─── 服务端事件解析 & 跨层分析 ─────────────────────────────────────────────

_TS = r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})'
SVC = {
    'open':    re.compile(_TS + r'.*WebSocket连接建立成功.*SessionId:\s*([^,]+),\s*DeviceId:\s*(\S+)'),
    'close':   re.compile(_TS + r'.*WebSocket连接关闭.*SessionId:\s*([^,]+).*code=(\d+)'),
    'detect':  re.compile(_TS + r'.*收到消息.*SessionId:\s*([^,]+).*"state":"detect"'),
    'start':   re.compile(_TS + r'.*收到消息.*SessionId:\s*([^,]+).*"state":"start"'),
    'stop':    re.compile(_TS + r'.*收到消息.*SessionId:\s*([^,]+).*"state":"stop"'),
    'llm':     re.compile(_TS + r'.*\[LLM\] 开始调用大模型.*SessionId:\s*([^,]+)'),
    'llm_done': re.compile(_TS + r'.*LLM已返回首句.*提交TTS合成.*SessionId:\s*([^,]+)'),
    'stt':     re.compile(_TS + r'.*sendSttMessage发送消息.*SessionId:\s*([^,]+)'),
    'tts':     re.compile(_TS + r'.*sendTtsMessage发送消息.*SessionId:\s*([^,]+).*"state":"(sentence_start|start)"'),
    'timeout': re.compile(_TS + r'.*会话\s+(\S+)\s+已经.*超时'),
}


def _parse_ts_ms(ts_str):
    """解析时间戳字符串为毫秒时间戳"""
    try:
        dt = datetime.strptime(ts_str, '%Y-%m-%d %H:%M:%S.%f')
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


def parse_server_events(log_dir: str) -> List[Dict]:
    """
    解析服务端日志，提取每个 WebSocket 会话的细粒度事件时间线。
    返回 [{session_id, device_id, ws_open_ts, ws_close_ts, close_code,
           listen_detect_ts, listen_start_ts, listen_stop_ts,
           llm_call_ts, stt_result_ts, tts_first_sentence_ts,
           tts_sentence_count, tts_text_len, is_timeout}, ...]
    """
    files = sorted(glob.glob(os.path.join(log_dir, 'xiaozhi-dialogue*.log')))
    files = [f for f in files if '-error' not in os.path.basename(f)]

    sessions = {}  # session_id -> dict

    for fpath in files:
        with open(fpath, 'r', encoding='utf-8') as f:
            for line in f:
                m = SVC['open'].match(line)
                if m:
                    sid = m.group(2).strip()
                    sessions[sid] = {
                        'session_id': sid,
                        'device_id': m.group(3).strip(),
                        'ws_open_ts': _parse_ts_ms(m.group(1)),
                        'ws_close_ts': None, 'close_code': None,
                        'listen_detect_ts': None,
                        'listen_start_ts': None,
                        'listen_stop_ts': None,
                        'llm_call_ts': None,
                        'llm_first_sentence_ts': None,  # LLM已返回首句, 提交TTS合成
                        'stt_result_ts': None,
                        'tts_first_sentence_ts': None,
                        'tts_sentence_count': 0,
                        'tts_text_len': 0,
                        'is_timeout': False,
                    }
                    continue

                m = SVC['close'].match(line)
                if m:
                    sid, ts_str = m.group(2).strip(), m.group(1)
                    if sid in sessions:
                        sessions[sid]['ws_close_ts'] = _parse_ts_ms(ts_str)
                        sessions[sid]['close_code'] = int(m.group(3))
                        # WebSocket 容器层 idle timeout（无 InactiveSessionChecker 前置日志）
                        # 也视为空闲超时回收，避免误判为断线(1006)
                        if 'idle timeout expired' in line:
                            sessions[sid]['is_timeout'] = True
                    continue

                for evt, key in [('detect', 'listen_detect_ts'),
                                  ('start', 'listen_start_ts'),
                                  ('stop', 'listen_stop_ts')]:
                    m = SVC[evt].match(line)
                    if m:
                        sid = m.group(2).strip()
                        if sid in sessions and sessions[sid][key] is None:
                            sessions[sid][key] = _parse_ts_ms(m.group(1))
                        break
                else:
                    m = SVC['llm'].match(line)
                    if m:
                        sid = m.group(2).strip()
                        if sid in sessions and sessions[sid]['llm_call_ts'] is None:
                            sessions[sid]['llm_call_ts'] = _parse_ts_ms(m.group(1))
                        continue

                    m = SVC['llm_done'].match(line)
                    if m:
                        sid = m.group(2).strip()
                        if sid in sessions and sessions[sid]['llm_first_sentence_ts'] is None:
                            sessions[sid]['llm_first_sentence_ts'] = _parse_ts_ms(m.group(1))
                        continue

                    m = SVC['stt'].match(line)
                    if m:
                        sid = m.group(2).strip()
                        if sid in sessions and sessions[sid]['stt_result_ts'] is None:
                            sessions[sid]['stt_result_ts'] = _parse_ts_ms(m.group(1))
                        continue

                    m = SVC['tts'].match(line)
                    if m:
                        sid = m.group(2).strip()
                        state = m.group(3)
                        if sid in sessions:
                            ts_ms = _parse_ts_ms(m.group(1))
                            if state == 'sentence_start':
                                sessions[sid]['tts_sentence_count'] += 1
                                if sessions[sid]['tts_first_sentence_ts'] is None:
                                    sessions[sid]['tts_first_sentence_ts'] = ts_ms
                            elif state == 'start':
                                pass  # TTS stream start
                        continue

                    m = SVC['timeout'].match(line)
                    if m:
                        sid = m.group(2).strip()
                        if sid in sessions:
                            sessions[sid]['is_timeout'] = True

    result = [s for s in sessions.values() if s['ws_open_ts']]
    result.sort(key=lambda s: s['ws_open_ts'])
    return result


def correlate_sessions(client_sessions: List[Dict],
                       server_events: List[Dict]) -> List[Dict]:
    """
    将客户端会话与服务端事件按设备+时间关联。
    对每个客户端会话，找到同一设备上时间最接近的服务端会话。
    """
    by_dev = defaultdict(list)
    for se in server_events:
        by_dev[se['device_id']].append(se)

    correlated = []
    for cs in client_sessions:
        dev = cs['device_id']
        try:
            dt = datetime.strptime(cs['timestamp'], '%Y-%m-%d %H:%M:%S.%f')
            cs_start = int(dt.timestamp() * 1000)
        except Exception:
            continue
        cs_end = cs_start + (cs.get('e2e_ms') or cs.get('dl_duration_ms', 0))
        if cs_end <= cs_start:
            cs_end = cs_start + 60000

        best, best_overlap = None, -1
        for se in by_dev.get(dev, []):
            se_start = se['ws_open_ts']
            se_end = se['ws_close_ts'] or (se_start + 300000)
            overlap = min(cs_end, se_end) - max(cs_start, se_start)
            if overlap > best_overlap:
                best_overlap = overlap
                best = se

        if best and best_overlap > 0:
            merged = dict(cs)
            merged['server'] = best
            correlated.append(merged)

    return correlated


def analyze_latency_breakdown(correlated: List[Dict]) -> Dict:
    """
    分析1: 端到端延迟分解
    将一次请求拆分为: 语音上传 → STT识别 → LLM推理 → TTS合成 → 网络下行
    """
    phases = defaultdict(list)
    for s in correlated:
        sv = s.get('server', {})
        # 语音上传阶段: listen_start → listen_stop
        if sv.get('listen_start_ts') and sv.get('listen_stop_ts'):
            dur = sv['listen_stop_ts'] - sv['listen_start_ts']
            if 0 < dur < 120000:
                phases['语音上传'].append(dur)
        # STT识别: listen_stop → stt_result
        if sv.get('listen_stop_ts') and sv.get('stt_result_ts'):
            dur = sv['stt_result_ts'] - sv['listen_stop_ts']
            if 0 < dur < 60000:
                phases['STT识别'].append(dur)
        # LLM推理: llm_call → llm_first_sentence (LLM已返回首句)
        if sv.get('llm_call_ts') and sv.get('llm_first_sentence_ts'):
            dur = sv['llm_first_sentence_ts'] - sv['llm_call_ts']
            if 0 < dur < 120000:
                phases['LLM推理'].append(dur)
        # TTS合成: llm_first_sentence → tts_first_sentence
        if sv.get('llm_first_sentence_ts') and sv.get('tts_first_sentence_ts'):
            dur = sv['tts_first_sentence_ts'] - sv['llm_first_sentence_ts']
            if 0 < dur < 120000:
                phases['TTS合成'].append(dur)
        # fallback: 如果没有 llm_first_sentence_ts，仍用旧逻辑
        elif sv.get('llm_call_ts') and sv.get('tts_first_sentence_ts'):
            dur = sv['tts_first_sentence_ts'] - sv['llm_call_ts']
            if 0 < dur < 120000:
                phases['LLM推理+TTS'].append(dur)
        # 网络下行
        if s.get('dl_duration_ms') and s['dl_duration_ms'] > 0:
            phases['网络下行'].append(s['dl_duration_ms'])

    result = {}
    for phase, vals in phases.items():
        if not vals:
            continue
        result[phase] = {
            'avg': round(statistics.mean(vals)),
            'median': round(statistics.median(vals)),
            'p95': round(sorted(vals)[int(len(vals) * 0.95)]) if len(vals) >= 5 else round(max(vals)),
            'min': round(min(vals)),
            'max': round(max(vals)),
            'count': len(vals),
        }
    return result


def analyze_connection_stability(server_events: List[Dict],
                                 correlated: List[Dict] = None) -> Dict:
    """
    分析2: 连接稳定性与断线分析
    结合客户端数据判断关闭原因：
    - 客户端汇报速率正常(dl_speed_avg > 0) → 视为正常关闭（客户端主动 close）
    - 即使服务端报 1006 Broken pipe，也归为正常
    - 仅当客户端无数据或速率为0时，才认为真正断线
    """
    # 构建服务端 session_id → 客户端会话 的映射
    client_by_sid = {}
    if correlated:
        for cs in correlated:
            sv = cs.get('server', {})
            if sv.get('session_id'):
                client_by_sid[sv['session_id']] = cs

    total = len(server_events)
    close_reasons = defaultdict(int)
    by_device = defaultdict(lambda: {'total': 0, 'broken': 0, 'timeout': 0, 'normal': 0})

    for se in server_events:
        code = se.get('close_code')
        dev = se['device_id']
        sid = se['session_id']
        by_device[dev]['total'] += 1

        # 检查客户端是否成功完成了数据传输
        client_cs = client_by_sid.get(sid)
        client_ok = (client_cs and client_cs.get('dl_speed_avg', 0) > 0)

        if code == 1000:
            close_reasons['正常关闭(1000)'] += 1
            by_device[dev]['normal'] += 1
        elif client_ok:
            # 客户端速率正常 → 是客户端主动 close，服务端误报为 1006
            close_reasons['客户端主动关闭(正常)'] += 1
            by_device[dev]['normal'] += 1
        elif se.get('is_timeout'):
            close_reasons['超时关闭'] += 1
            by_device[dev]['timeout'] += 1
        elif code == 1006:
            close_reasons['连接断开(1006)'] += 1
            by_device[dev]['broken'] += 1
        elif code is None:
            close_reasons['未关闭'] += 1
        else:
            close_reasons[f'其他({code})'] += 1

    device_stats = []
    for dev, d in sorted(by_device.items()):
        abnormal = d['broken'] + d['timeout']
        device_stats.append({
            'device_id': dev,
            'total': d['total'],
            'normal': d['normal'],
            'broken': d['broken'],
            'timeout': d['timeout'],
            'disconnect_rate': round(abnormal / d['total'] * 100, 1) if d['total'] else 0,
        })
    device_stats.sort(key=lambda x: x['disconnect_rate'], reverse=True)

    return {
        'total': total,
        'close_reasons': dict(close_reasons),
        'device_stats': device_stats,
        'overall_broken': close_reasons.get('连接断开(1006)', 0),
        'overall_timeout': close_reasons.get('超时关闭', 0),
        'overall_normal': close_reasons.get('正常关闭(1000)', 0) + close_reasons.get('客户端主动关闭(正常)', 0),
    }


# 并发度分档：(下界, 上界(None=无上界), 标签)，按定义顺序展示，无数据的档位自动隐藏
CONCURRENCY_BUCKETS = [
    (1, 1, '1台'), (2, 2, '2台'), (3, 3, '3台'), (4, 4, '4台'), (5, 5, '5台'),
    (6, 10, '6-10台'), (11, 15, '11-15台'), (16, 20, '16-20台'),
    (21, 25, '21-25台'), (26, 30, '26-30台'), (31, 35, '31-35台'),
    (36, 40, '36-40台'), (41, None, '40+台'),
]


def _concurrency_bucket(lvl: int):
    """将并发度映射到分档标签；无匹配返回 None。"""
    for lo, hi, label in CONCURRENCY_BUCKETS:
        if lvl >= lo and (hi is None or lvl <= hi):
            return label
    return None


def analyze_error_by_concurrency(server_events: List[Dict],
                                 correlated: List[Dict] = None) -> Dict:
    """
    分析: 不同并发度下的错误率
    并发度 = 会话建立时刻同时活跃的设备数（基于服务端 WebSocket 起止时间扫描线）
    错误   = 连接断开(1006)
             （客户端速率正常但服务端误报 1006 的，与连接稳定性分析一致地修正为正常，不计入错误）
    超时   = InactiveSessionChecker 60s 无活动自动回收，属正常连接回收，单独统计且不计入错误
    """
    # 客户端速率正常的 session_id 集合（服务端误报 1006 需修正为正常）
    client_ok_sids = set()
    for cs in correlated or []:
        sv = cs.get('server', {})
        if sv.get('session_id') and cs.get('dl_speed_avg', 0) > 0:
            client_ok_sids.add(sv['session_id'])

    valid = [se for se in server_events if se.get('ws_open_ts')]
    if not valid:
        return {'breakdown': [], 'overall_error_rate': 0, 'total': 0, 'errors': 0}

    # 扫描线事件：(ts, +1/-1, device_id)
    events = []
    for se in valid:
        start = se['ws_open_ts']
        end = se.get('ws_close_ts') or (start + 300000)
        events.append((start, 1, se['device_id']))
        events.append((end, -1, se['device_id']))
    events.sort(key=lambda x: (x[0], x[1]))

    # 单次移动指针计算每个会话建立时刻的并发度，避免 O(n^2)；并按分档聚合
    by_bucket = defaultdict(lambda: {'total': 0, 'errors': 0, 'broken': 0, 'timeout': 0})
    active = defaultdict(int)
    ei = 0
    for se in sorted(valid, key=lambda s: s['ws_open_ts']):
        t = se['ws_open_ts']
        while ei < len(events) and events[ei][0] <= t:
            _, delta, dev = events[ei]
            active[dev] += delta
            if active[dev] <= 0:
                del active[dev]
            ei += 1
        lvl = max(len(active), 1)
        bucket = _concurrency_bucket(lvl)
        if bucket is None:
            continue

        code = se.get('close_code')
        is_err = False
        if se.get('is_timeout'):
            # 空闲超时（InactiveSessionChecker 60s 无活动自动回收），属正常连接回收，单独统计、不计入错误
            by_bucket[bucket]['timeout'] += 1
        elif code == 1006 and se['session_id'] not in client_ok_sids:
            by_bucket[bucket]['broken'] += 1
            is_err = True
        by_bucket[bucket]['total'] += 1
        if is_err:
            by_bucket[bucket]['errors'] += 1

    # 按分档定义顺序输出，无数据的档位不列出
    breakdown = []
    for _, _, label in CONCURRENCY_BUCKETS:
        if label not in by_bucket:
            continue
        d = by_bucket[label]
        breakdown.append({
            'label': label,
            'total': d['total'],
            'errors': d['errors'],
            'broken': d['broken'],
            'timeout': d['timeout'],
            'error_rate': round(d['errors'] / d['total'] * 100, 1) if d['total'] else 0,
        })

    total_all = sum(d['total'] for d in by_bucket.values())
    errors_all = sum(d['errors'] for d in by_bucket.values())
    timeout_all = sum(d['timeout'] for d in by_bucket.values())
    return {
        'breakdown': breakdown,
        'overall_error_rate': round(errors_all / total_all * 100, 1) if total_all else 0,
        'total': total_all,
        'errors': errors_all,
        'timeout': timeout_all,
    }


def analyze_llm_tts(server_events: List[Dict]) -> Dict:
    """
    分析3: LLM/TTS 服务质量分析
    统计 LLM 调用延迟、TTS 首句延迟、TTS 句数、TTS 总时长
    """
    llm_to_first_tts = []
    tts_durations = []
    sentence_counts = []
    first_sentence_delays = []  # ws_open → first tts sentence

    for se in server_events:
        if se['llm_call_ts'] and se['tts_first_sentence_ts']:
            dur = se['tts_first_sentence_ts'] - se['llm_call_ts']
            if 0 < dur < 120000:
                llm_to_first_tts.append(dur)

        if se['tts_first_sentence_ts'] and se['ws_close_ts']:
            dur = se['ws_close_ts'] - se['tts_first_sentence_ts']
            if 0 < dur < 300000:
                tts_durations.append(dur)

        if se['tts_sentence_count'] > 0:
            sentence_counts.append(se['tts_sentence_count'])

        if se['ws_open_ts'] and se['tts_first_sentence_ts']:
            dur = se['tts_first_sentence_ts'] - se['ws_open_ts']
            if 0 < dur < 120000:
                first_sentence_delays.append(dur)

    def _stats(vals):
        if not vals:
            return {'avg': 0, 'median': 0, 'min': 0, 'max': 0, 'count': 0}
        return {
            'avg': round(statistics.mean(vals)),
            'median': round(statistics.median(vals)),
            'min': round(min(vals)),
            'max': round(max(vals)),
            'count': len(vals),
        }

    return {
        'llm_to_first_tts': _stats(llm_to_first_tts),
        'tts_stream_duration': _stats(tts_durations),
        'sentence_counts': _stats(sentence_counts),
        'first_sentence_delay': _stats(first_sentence_delays),
    }


def analyze_device_behavior(client_sessions: List[Dict],
                            server_events: List[Dict]) -> Dict:
    """
    分析4: 设备行为与使用模式
    每个设备的会话频率、平均时长、功能偏好、使用时间段
    """
    by_dev = defaultdict(lambda: {
        'client_sessions': [], 'server_sessions': [],
    })
    for cs in client_sessions:
        by_dev[cs['device_id']]['client_sessions'].append(cs)
    for se in server_events:
        by_dev[se['device_id']]['server_sessions'].append(se)

    device_stats = []
    for dev, data in sorted(by_dev.items()):
        css = data['client_sessions']
        sss = data['server_sessions']

        # 使用天数
        dates = set()
        hours = []
        for cs in css:
            dates.add(cs['date'])
            hours.append(cs['hour'])

        # 服务端会话时长
        ws_durations = []
        for se in sss:
            if se['ws_close_ts'] and se['ws_open_ts']:
                ws_durations.append(se['ws_close_ts'] - se['ws_open_ts'])

        # 功能偏好
        features = defaultdict(int)
        for cs in css:
            features[cs['feature']] += 1
        top_feature = max(features, key=features.get) if features else '未知'

        # 断线次数
        broken = sum(1 for se in sss if se.get('close_code') == 1006)
        timeout = sum(1 for se in sss if se.get('is_timeout'))

        # 平均速率
        avg_speeds = [cs['dl_speed_avg'] for cs in css if cs.get('dl_speed_avg', 0) > 0]

        # 活跃时段
        hour_dist = [0] * 24
        for h in hours:
            hour_dist[h] += 1

        device_stats.append({
            'device_id': dev,
            'session_count': len(css),
            'server_session_count': len(sss),
            'active_days': len(dates),
            'avg_ws_duration_s': round(statistics.mean(ws_durations) / 1000, 1) if ws_durations else 0,
            'top_feature': top_feature,
            'broken_pipe_count': broken,
            'timeout_count': timeout,
            'avg_dl_speed': round(statistics.mean(avg_speeds)) if avg_speeds else 0,
            'hour_distribution': hour_dist,
        })

    device_stats.sort(key=lambda x: x['session_count'], reverse=True)
    return device_stats


def analyze_input_modes(server_events: List[Dict]) -> Dict:
    """
    分析5: 输入模式分布
    语音提问 vs 文本提问的比例，按时段分布
    """
    voice_count = 0   # listen start
    text_count = 0    # listen detect
    voice_by_hour = [0] * 24
    text_by_hour = [0] * 24

    for se in server_events:
        h = None
        if se['ws_open_ts']:
            try:
                h = datetime.fromtimestamp(se['ws_open_ts'] / 1000).hour
            except Exception:
                pass

        if se.get('listen_start_ts'):
            voice_count += 1
            if h is not None:
                voice_by_hour[h] += 1
        if se.get('listen_detect_ts'):
            text_count += 1
            if h is not None:
                text_by_hour[h] += 1

    total = voice_count + text_count
    return {
        'voice': voice_count,
        'text': text_count,
        'total': total,
        'voice_pct': round(voice_count / total * 100, 1) if total else 0,
        'text_pct': round(text_count / total * 100, 1) if total else 0,
        'voice_by_hour': voice_by_hour,
        'text_by_hour': text_by_hour,
    }


def run_cross_layer_analysis(log_dir: str, client_sessions: List[Dict],
                             server_sessions: List[Dict]) -> Dict:
    """运行所有跨层分析，返回分析结果字典"""
    server_events = parse_server_events(log_dir)
    correlated = correlate_sessions(client_sessions, server_events)
    print(f"  客户端-服务端关联成功: {len(correlated)} 个会话")
    return {
        'correlated_count': len(correlated),
        'server_event_count': len(server_events),
        'latency_breakdown': analyze_latency_breakdown(correlated),
        'connection_stability': analyze_connection_stability(server_events, correlated),
        'error_by_concurrency': analyze_error_by_concurrency(server_events, correlated),
        'llm_tts': analyze_llm_tts(server_events),
        'device_behavior': analyze_device_behavior(client_sessions, server_events),
        'input_modes': analyze_input_modes(server_events),
    }


# ─── HTML 模板 ───────────────────────────────────────────────────────────────

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>设备日志分析仪表盘</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js"></script>
<style>
:root{--primary:#4f46e5;--primary-light:#818cf8;--bg:#f1f5f9;--card:#fff;--text:#1e293b;--text2:#64748b;--border:#e2e8f0;--green:#22c55e;--amber:#f59e0b;--red:#ef4444;--blue:#3b82f6}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:var(--bg);color:var(--text);line-height:1.6}
.header{background:linear-gradient(135deg,#4f46e5,#7c3aed);color:#fff;padding:2rem 2rem 1.5rem;box-shadow:0 4px 12px rgba(0,0,0,.15)}
.header h1{font-size:1.75rem;font-weight:700;margin-bottom:.25rem}
.header p{opacity:.85;font-size:.9rem}
.container{max-width:1400px;margin:0 auto;padding:1.5rem}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:1rem;margin-bottom:1.5rem}
.card{background:var(--card);border-radius:12px;padding:1.25rem;box-shadow:0 1px 3px rgba(0,0,0,.08);border:1px solid var(--border)}
.card .label{font-size:.8rem;color:var(--text2);text-transform:uppercase;letter-spacing:.5px;margin-bottom:.25rem}
.card .value{font-size:1.75rem;font-weight:700;color:var(--primary)}
.card .sub{font-size:.8rem;color:var(--text2);margin-top:.25rem}
.filter-bar{background:var(--card);border-radius:12px;padding:1rem 1.25rem;margin-bottom:1.5rem;box-shadow:0 1px 3px rgba(0,0,0,.08);border:1px solid var(--border);display:flex;flex-wrap:wrap;gap:1rem;align-items:flex-start}
.filter-group{display:flex;flex-direction:column;gap:.35rem}
.filter-group label{font-size:.75rem;font-weight:600;color:var(--text2);text-transform:uppercase;letter-spacing:.5px}
.filter-group select,.filter-group input{padding:.4rem .6rem;border:1px solid var(--border);border-radius:6px;font-size:.85rem;background:#fff}
.btn{padding:.4rem 1rem;border:none;border-radius:6px;font-size:.8rem;cursor:pointer;font-weight:600;transition:all .15s}
.btn-primary{background:var(--primary);color:#fff}.btn-primary:hover{background:#4338ca}
.btn-outline{background:transparent;border:1px solid var(--border);color:var(--text2)}.btn-outline:hover{border-color:var(--primary);color:var(--primary)}
.charts-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:1.5rem;margin-bottom:1.5rem}
@media(max-width:900px){.charts-grid{grid-template-columns:1fr}}
.chart-card{background:var(--card);border-radius:12px;padding:1.25rem;box-shadow:0 1px 3px rgba(0,0,0,.08);border:1px solid var(--border)}
.chart-card h3{font-size:.95rem;font-weight:600;margin-bottom:.75rem;color:var(--text)}
.chart-card canvas{width:100%!important;max-height:320px}
.chart-card.full{grid-column:1/-1}
.table-wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:.82rem}
th{background:#e2e8f0;padding:.6rem .75rem;text-align:left;font-weight:700;color:#334155;border-bottom:2px solid #cbd5e1;cursor:pointer;user-select:none;white-space:nowrap}
th:hover{color:var(--primary)}
th .sort-arrow{margin-left:4px;font-size:.7rem}
td{padding:.5rem .75rem;border-bottom:1px solid var(--border)}
tbody tr:nth-child(even){background:#f1f5f9}
tbody tr:nth-child(odd){background:#fff}
tr:hover{background:#e0e7ff!important}
.dir-down{color:#1d4ed8;font-weight:700}
.dir-up{color:#d97706;font-weight:700}
.badge{display:inline-block;padding:1px 8px;border-radius:10px;font-size:.75rem;font-weight:600}
.badge-blue{background:#dbeafe;color:#1d4ed8}
.badge-green{background:#dcfce7;color:#16a34a}
.badge-amber{background:#fef3c7;color:#d97706}
.badge-red{background:#fee2e2;color:#dc2626}
.footer{text-align:center;padding:1.5rem;color:var(--text2);font-size:.8rem}
.insights{background:linear-gradient(135deg,#f0f9ff,#eff6ff);border:1px solid #bfdbfe;border-radius:12px;padding:1.25rem 1.5rem;margin-bottom:1.5rem}
.insights h2{font-size:1rem;font-weight:700;color:#1e40af;margin-bottom:.75rem;display:flex;align-items:center;gap:.5rem}
.insights-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:1rem}
.insight-item{background:#fff;border-radius:8px;padding:.75rem 1rem;border:1px solid #e0e7ff}
.insight-item h3{font-size:.82rem;font-weight:700;color:#4338ca;margin-bottom:.4rem}
.insight-item table{width:100%;font-size:.78rem;border-collapse:collapse}
.insight-item th{text-align:left;padding:2px 6px;color:#64748b;font-weight:600;border-bottom:1px solid #e2e8f0}
.insight-item td{padding:2px 6px;color:#334155}
.insight-item .highlight{color:#dc2626;font-weight:700}
.insight-item .good{color:#16a34a;font-weight:700}
.insight-item .tag{display:inline-block;padding:1px 6px;border-radius:4px;font-size:.72rem;font-weight:600;margin-right:4px}
</style>
</head>
<body>
<div class="header">
  <h1>📊 设备日志分析仪表盘</h1>
  <p>分析时间: __GENERATED_AT__ | 日志目录: __LOG_DIR__</p>
</div>
<div class="container">
  <div class="cards" id="summary-cards"></div>
  <div class="insights" id="insights-section"></div>
  <div class="filter-bar">
    <div class="filter-group">
      <label>日期范围</label>
      <div style="display:flex;gap:.5rem;align-items:center">
        <select id="filter-date-start"></select>
        <span>~</span>
        <select id="filter-date-end"></select>
      </div>
    </div>
    <div class="filter-group">
      <label>&nbsp;</label>
      <button class="btn btn-primary" onclick="applyFilters()">应用筛选</button>
    </div>
  </div>
  <div class="charts-grid">
    <div class="chart-card"><h3>📈 每日会话趋势</h3><canvas id="chart-daily"></canvas></div>
    <div class="chart-card"><h3>🕐 小时分布</h3><canvas id="chart-hourly"></canvas></div>
    <div class="chart-card"><h3>🎯 功能会话分布</h3><canvas id="chart-feature"></canvas></div>
    <div class="chart-card"><h3>⏱️ 端到端延迟 (TTFB / E2E)</h3><canvas id="chart-duration"></canvas></div>
    <div class="chart-card"><h3>⚡ 会话下行速率分布</h3><canvas id="chart-speed"></canvas></div>
    <div class="chart-card"><h3>📊 按功能会话统计</h3><canvas id="chart-feature-speed"></canvas></div>
    <div class="chart-card"><h3>🔗 速率 vs 访问时间</h3><canvas id="chart-speed-time"></canvas></div>
    <div class="chart-card"><h3>📦 上行 vs 下行流量</h3><canvas id="chart-upload-download"></canvas></div>
    <div class="chart-card full"><h3>📡 设备并发度时间线</h3><canvas id="chart-concurrency"></canvas></div>
    <div class="chart-card"><h3>🔥 并发度 vs TTFB 散点图</h3><canvas id="chart-concurrency-ttfb"></canvas></div>
    <div class="chart-card"><h3>📉 并发度对响应时间影响</h3><canvas id="chart-concurrency-impact"></canvas></div>
    <div class="chart-card full"><h3>🚨 不同并发度下的错误率</h3>
      <canvas id="chart-concurrency-error" style="max-height:300px"></canvas>
      <div id="concurrency-error-detail" style="margin:.75rem 0;font-size:.82rem;color:#334155"></div>
    </div>
    <div class="chart-card full"><h3>⚠️ 零速率异常统计</h3><canvas id="chart-zero-speed" style="max-height:280px"></canvas>
      <div id="zero-speed-summary" style="margin:.75rem 0;font-size:.85rem;color:#334155"></div>
      <div id="zero-speed-breakdown" style="margin-top:.5rem"></div>
    </div>
    <div class="chart-card full"><h3>⏱️ 端到端延迟分解（服务端+客户端）</h3>
      <canvas id="chart-latency-breakdown"></canvas>
      <div id="latency-detail" style="margin:.75rem 0;font-size:.82rem;color:#334155"></div>
    </div>
    <div class="chart-card"><h3>🔌 连接关闭原因分布</h3><canvas id="chart-close-reason"></canvas></div>
    <div class="chart-card"><h3>📊 输入模式分布</h3><canvas id="chart-input-mode"></canvas></div>
    <div class="chart-card full"><h3>🤖 LLM / TTS 服务质量</h3>
      <div id="llm-tts-stats" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:1rem;margin-bottom:1rem"></div>
      <canvas id="chart-llm-tts" style="max-height:280px"></canvas>
    </div>
    <div class="chart-card full"><h3>📡 各设备断线率</h3><canvas id="chart-device-stability" style="max-height:320px"></canvas></div>
    <div class="chart-card full"><h3>📋 设备详情</h3>
      <div class="table-wrap"><table id="device-table"><thead><tr>
        <th onclick="sortTable(0)">设备 <span class="sort-arrow">⇅</span></th>
        <th onclick="sortTable(1)">活跃天数 <span class="sort-arrow">⇅</span></th>
        <th onclick="sortTable(2)">会话数 <span class="sort-arrow">⇅</span></th>
        <th onclick="sortTable(3)">语音提问 <span class="sort-arrow">⇅</span></th>
        <th onclick="sortTable(4)">文本提问 <span class="sort-arrow">⇅</span></th>
        <th onclick="sortTable(5)">平均TTFB(ms) <span class="sort-arrow">⇅</span></th>
        <th onclick="sortTable(6)">平均E2E(ms) <span class="sort-arrow">⇅</span></th>
        <th onclick="sortTable(7)">下行均速(B/s) <span class="sort-arrow">⇅</span></th>
        <th onclick="sortTable(8)">下行最大(B/s) <span class="sort-arrow">⇅</span></th>
        <th onclick="sortTable(9)">总下行(KB) <span class="sort-arrow">⇅</span></th>
        <th onclick="sortTable(10)">总上行(KB) <span class="sort-arrow">⇅</span></th>
      </tr></thead><tbody id="device-tbody"></tbody></table></div>
    </div>
  </div>
</div>
<div class="footer">设备日志分析仪表盘 | 自动生成</div>

<script>
const _DATA = __DATA_JSON__;
const RAW_DATA = _DATA.entries;
const SESSIONS = _DATA.sessions;
const SUMMARY = __SUMMARY_JSON__;
const _CROSS = __CROSS_ANALYSIS__;
const COLORS = ['#4f46e5','#7c3aed','#2563eb','#0891b2','#059669','#d97706','#dc2626','#db2777','#7c2d12','#4338ca','#0d9488','#65a30d'];
let charts = {};
let FS = [...SESSIONS]; // filtered sessions

// ── 初始化 ──
function init() { renderSummaryCards(); renderInsights(); initFilters(); applyFilters(); renderCrossAnalysis(); }

function renderCrossAnalysis() {
  renderLatencyBreakdown(); renderCloseReason(); renderInputMode();
  renderLlmTts(); renderDeviceStability(); renderConcurrencyError();
}

function renderInsights() {
  const lb = _CROSS.latency_breakdown || {};
  const cs = _CROSS.connection_stability || {};
  const lt = _CROSS.llm_tts || {};
  const im = _CROSS.input_modes || {};
  const ec = _CROSS.error_by_concurrency || {};
  const fmt = ms => ms >= 1000 ? (ms/1000).toFixed(1)+'s' : ms+'ms';

  // 1. 延迟分解摘要
  const phases = ['语音上传','STT识别','LLM推理','TTS合成','网络下行'];
  const avail = phases.filter(p => lb[p]);
  // 网络下行包含播放等待时间(by design)，不参与瓶颈判定
  const serverPhases = avail.filter(p => p !== '网络下行');
  const realBottleneck = serverPhases.length ? serverPhases.reduce((a,b) => lb[a].avg > lb[b].avg ? a : b) : null;
  let latencyRows = avail.map(p => {
    const s = lb[p]; const isBottleneck = (p === realBottleneck);
    const note = (p === '网络下行') ? '含播放等待' : (isBottleneck ? '⚠ 服务端瓶颈' : '');
    return `<tr><td>${p}</td><td>${fmt(s.avg)}</td><td>${fmt(s.median)}</td><td>${fmt(s.p95)}</td><td class="${isBottleneck?'highlight':''}">${note}</td></tr>`;
  }).join('');
  const totalAvg = avail.reduce((a,p) => a + lb[p].avg, 0);

  // 2. 连接稳定性摘要
  const normalPct = cs.total ? (cs.overall_normal/cs.total*100).toFixed(1) : 0;
  const brokenPct = cs.total ? (cs.overall_broken/cs.total*100).toFixed(1) : 0;
  const timeoutPct = cs.total ? (cs.overall_timeout/cs.total*100).toFixed(1) : 0;

  // 3. LLM/TTS 摘要
  const llmAvg = lt.llm_to_first_tts ? fmt(lt.llm_to_first_tts.avg) : '-';
  const ttsFirst = lt.first_sentence_delay ? fmt(lt.first_sentence_delay.avg) : '-';
  const sentAvg = lt.sentence_counts ? lt.sentence_counts.avg.toFixed(1) : '-';

  // 4. 输入模式
  const voicePct = im.voice_pct || 0;
  const textPct = im.text_pct || 0;

  document.getElementById('insights-section').innerHTML = `
    <h2>📋 关键发现与分析结论</h2>
    <div class="insights-grid">
      <div class="insight-item">
        <h3>⏱️ 1. 端到端延迟分解</h3>
        <table><tr><th>阶段</th><th>平均</th><th>中位数</th><th>P95</th><th></th></tr>${latencyRows}
        <tr style="border-top:2px solid #e2e8f0;font-weight:700"><td>服务端合计</td><td>${fmt(totalAvg - (lb['网络下行']?.avg||0))}</td><td colspan="3">${realBottleneck ? '服务端瓶颈: <span class="highlight">' + realBottleneck + '</span>' : ''}</td></tr>
        </table>
        <p style="font-size:.72rem;color:#64748b;margin-top:.25rem">💡 网络下行含播放等待时间（按播放进度流式推送，by design）</p>
      </div>
      <div class="insight-item">
        <h3>🔌 2. 连接稳定性</h3>
        <p style="font-size:.82rem;margin-bottom:.4rem">总连接 <b>${cs.total||0}</b> 个</p>
        <table>
          <tr><td><span class="tag" style="background:#dcfce7;color:#16a34a">正常</span>正常关闭 + 客户端主动关闭</td><td class="good">${cs.overall_normal||0} (${normalPct}%)</td></tr>
          <tr><td><span class="tag" style="background:#fee2e2;color:#dc2626">断线</span>连接断开(1006)</td><td class="${brokenPct>15?'highlight':''}">${cs.overall_broken||0} (${brokenPct}%)</td></tr>
          <tr><td><span class="tag" style="background:#fef3c7;color:#d97706">超时</span>超时关闭</td><td class="${timeoutPct>10?'highlight':''}">${cs.overall_timeout||0} (${timeoutPct}%)</td></tr>
        </table>
        <p style="font-size:.75rem;color:#64748b;margin-top:.3rem">💡 客户端主动 close 但服务端报 1006 的已修正为正常</p>
      </div>
      <div class="insight-item">
        <h3>🤖 3. LLM / TTS 服务质量</h3>
        <table>
          <tr><td>LLM → 首句 TTS</td><td><b>${llmAvg}</b></td></tr>
          <tr><td>连接建立 → 首句 TTS</td><td><b>${ttsFirst}</b></td></tr>
          <tr><td>平均 TTS 句数/会话</td><td><b>${sentAvg}</b> 句</td></tr>
        </table>
      </div>
      <div class="insight-item">
        <h3>📊 4. 输入模式分布</h3>
        <p style="font-size:.85rem;margin:.3rem 0">
          <span class="tag" style="background:#e0e7ff;color:#4338ca">语音</span>算卦: <b>${im.voice||0}</b> 次 (<b>${voicePct}%</b>)
        </p>
        <p style="font-size:.85rem;margin:.3rem 0">
          <span class="tag" style="background:#fef3c7;color:#d97706">文本</span>运势/八字: <b>${im.text||0}</b> 次 (<b>${textPct}%</b>)
        </p>
      </div>
      <div class="insight-item">
        <h3>🚨 5. 并发度与错误率</h3>
        ${(() => {
          const bd = ec.breakdown || [];
          if (!bd.length) return '<p style="font-size:.82rem;color:#64748b">无数据</p>';
          const worst = bd.reduce((a,b) => b.error_rate > a.error_rate ? b : a);
          const maxConc = bd[bd.length-1].label;
          return `<p style="font-size:.85rem;margin:.3rem 0">整体错误率: <b class="${ec.overall_error_rate>10?'highlight':'good'}">${ec.overall_error_rate}%</b> (${ec.errors}/${ec.total})</p>
          <p style="font-size:.85rem;margin:.3rem 0">最高并发档: <b>${maxConc}</b>；错误率峰值: <b class="highlight">${worst.error_rate}%</b> @ ${worst.label}</p>
          <p style="font-size:.72rem;color:#64748b;margin-top:.25rem">💡 错误仅计断线(1006)；超时(空闲60s自动回收)单独统计不计入错误</p>`;
        })()}
      </div>
    </div>
  `;
}

function renderSummaryCards() {
  const s = SUMMARY;
  const nSess = SESSIONS.length;
  const voiceCount = SESSIONS.filter(s => s.upload_type === 'voice').length;
  const noUploadCount = SESSIONS.filter(s => s.upload_type === null).length;
  const textCount = SESSIONS.filter(s => s.upload_type === 'text').length;
  const ttfbs = SESSIONS.filter(s => s.ttfb_ms !== null).map(s => s.ttfb_ms);
  const avgTtfb = ttfbs.length ? Math.round(ttfbs.reduce((a,b)=>a+b,0)/ttfbs.length) : 0;
  document.getElementById('summary-cards').innerHTML = `
    <div class="card"><div class="label">总会话数</div><div class="value">${nSess}</div><div class="sub">${s.date_range}</div></div>
    <div class="card"><div class="label">设备数量</div><div class="value">${s.device_count}</div><div class="sub">台活跃设备</div></div>
    <div class="card"><div class="label">提问方式</div><div class="value">${nSess}</div><div class="sub"><span class="dir-up">语音(算卦)${voiceCount}</span> / <span class="dir-up">文本(无上行日志)${noUploadCount + textCount}</span></div></div>
    <div class="card"><div class="label">平均TTFB</div><div class="value">${avgTtfb ? avgTtfb + 'ms' : 'N/A'}</div><div class="sub">仅语音会话可计算</div></div>
    <div class="card"><div class="label">总流量</div><div class="value">${s.total_bytes_mb} MB</div><div class="sub">上行+下行</div></div>
    <div class="card"><div class="label">峰值并发</div><div class="value">${s.concurrency.peak} 台</div><div class="sub">${s.concurrency.peak_time}</div></div>
  `;
}

function initFilters() {
  const dates = SUMMARY.dates;
  const ds = document.getElementById('filter-date-start');
  const de = document.getElementById('filter-date-end');
  dates.forEach(d => { ds.innerHTML += `<option value="${d}">${d}</option>`; de.innerHTML += `<option value="${d}">${d}</option>`; });
  de.value = dates[dates.length - 1];
}

function applyFilters() {
  const ds = document.getElementById('filter-date-start').value;
  const de = document.getElementById('filter-date-end').value;
  FS = SESSIONS.filter(s => s.date >= ds && s.date <= de);
  renderAllCharts(); renderDeviceTable();
}

// ── 图表渲染 ──
function destroyChart(id) { if (charts[id]) { charts[id].destroy(); delete charts[id]; } }
function renderAllCharts() {
  renderDailyTrend(); renderHourlyChart(); renderFeatureChart();
  renderDurationChart(); renderSpeedChart(); renderFeatureSpeedChart();
  renderSpeedTimeChart(); renderUploadDownloadChart(); renderConcurrencyChart();
  renderConcurrencyTtfbChart(); renderConcurrencyImpactChart(); renderZeroSpeedChart();
}

function renderDailyTrend() {
  destroyChart('chart-daily');
  const byDate = {};
  FS.forEach(s => {
    if (!byDate[s.date]) byDate[s.date] = { sessions: 0, devices: new Set(), voice: 0, text: 0 };
    byDate[s.date].sessions++;
    byDate[s.date].devices.add(s.device_id);
    if (s.upload_type === 'voice') byDate[s.date].voice++;
    else byDate[s.date].text++;
  });
  const labels = Object.keys(byDate).sort();
  charts['chart-daily'] = new Chart(document.getElementById('chart-daily'), {
    type: 'line',
    data: { labels, datasets: [
      { label: '会话数', data: labels.map(d => byDate[d].sessions), borderColor: '#4f46e5', backgroundColor: 'rgba(79,70,229,.1)', fill: true, tension: .3, yAxisID: 'y' },
      { label: '语音提问', data: labels.map(d => byDate[d].voice), borderColor: '#f59e0b', borderDash: [5,5], tension: .3, yAxisID: 'y' },
      { label: '活跃设备', data: labels.map(d => byDate[d].devices.size), borderColor: '#059669', fill: false, tension: .3, yAxisID: 'y1' },
    ]},
    options: { responsive: true, interaction: { mode: 'index', intersect: false },
      scales: {
        y: { type: 'linear', position: 'left', title: { display: true, text: '会话数' }, beginAtZero: true },
        y1: { type: 'linear', position: 'right', title: { display: true, text: '设备数' }, beginAtZero: true, grid: { drawOnChartArea: false } }
      }
    }
  });
}

function renderHourlyChart() {
  destroyChart('chart-hourly');
  const hours = new Array(24).fill(0);
  FS.forEach(s => hours[s.hour]++);
  const labels = Array.from({length:24}, (_,i) => `${String(i).padStart(2,'0')}:00`);
  const mx = Math.max(...hours);
  charts['chart-hourly'] = new Chart(document.getElementById('chart-hourly'), {
    type: 'bar',
    data: { labels, datasets: [{ label: '会话数', data: hours, backgroundColor: hours.map(v => v === mx ? '#dc2626' : '#818cf8'), borderRadius: 4 }] },
    options: { responsive: true, plugins: { legend: { display: false } }, scales: { y: { beginAtZero: true } } }
  });
}

function renderFeatureChart() {
  destroyChart('chart-feature');
  const counts = {};
  FS.forEach(s => { counts[s.feature] = (counts[s.feature]||0) + 1; });
  const labels = Object.keys(counts).sort((a,b) => counts[b] - counts[a]);
  charts['chart-feature'] = new Chart(document.getElementById('chart-feature'), {
    type: 'doughnut',
    data: { labels, datasets: [{ data: labels.map(l => counts[l]), backgroundColor: COLORS.slice(0, labels.length), borderWidth: 2, borderColor: '#fff' }] },
    options: { responsive: true, plugins: { legend: { position: 'bottom' } } }
  });
}

function renderDurationChart() {
  destroyChart('chart-duration');
  const ttfbs = FS.filter(s => s.ttfb_ms !== null).map(s => s.ttfb_ms).sort((a,b) => a-b);
  const e2es = FS.filter(s => s.e2e_ms !== null).map(s => s.e2e_ms).sort((a,b) => a-b);
  if (!ttfbs.length) return;
  const pct = (arr, p) => arr[Math.min(Math.floor(arr.length * p), arr.length - 1)];
  const labels = ['Min', 'P25', 'P50', 'P75', 'P90', 'P95', 'Max'];
  const ttfbData = labels.map((_, i) => pct(ttfbs, [0, .25, .5, .75, .9, .95, 1][i]));
  const e2eData = labels.map((_, i) => pct(e2es, [0, .25, .5, .75, .9, .95, 1][i]));
  charts['chart-duration'] = new Chart(document.getElementById('chart-duration'), {
    type: 'bar',
    data: { labels, datasets: [
      { label: 'TTFB (提问→首段)', data: ttfbData, backgroundColor: 'rgba(245,158,11,.7)', borderRadius: 4 },
      { label: 'E2E (提问→末段完成)', data: e2eData, backgroundColor: 'rgba(79,70,229,.7)', borderRadius: 4 },
    ]},
    options: { responsive: true, plugins: { legend: { position: 'top' } },
      scales: { y: { beginAtZero: true, title: { display: true, text: '毫秒(ms)' } } }
    }
  });
}

function renderSpeedChart() {
  destroyChart('chart-speed');
  const speeds = FS.filter(s => s.dl_speed_avg > 0).map(s => s.dl_speed_avg);
  if (!speeds.length) return;
  const buckets = [[0,1000,'<1K'],[1000,3000,'1-3K'],[3000,5000,'3-5K'],[5000,6000,'5-6K'],[6000,8000,'6-8K'],[8000,10000,'8-10K'],[10000,Infinity,'>10K']];
  const labels = buckets.map(b => b[2] + 'B/s');
  const data = buckets.map(([lo,hi]) => speeds.filter(s => s >= lo && s < hi).length);
  charts['chart-speed'] = new Chart(document.getElementById('chart-speed'), {
    type: 'bar',
    data: { labels, datasets: [{ label: '会话数', data, backgroundColor: COLORS.slice(0, labels.length), borderRadius: 4 }] },
    options: { responsive: true, plugins: { legend: { display: false } }, scales: { y: { beginAtZero: true, title: { display: true, text: '会话数' } } } }
  });
}

function renderFeatureSpeedChart() {
  destroyChart('chart-feature-speed');
  const byF = {};
  FS.forEach(s => {
    if (!byF[s.feature]) byF[s.feature] = { ttfbs: [], e2es: [], speeds: [], count: 0 };
    byF[s.feature].count++;
    if (s.ttfb_ms !== null) byF[s.feature].ttfbs.push(s.ttfb_ms);
    if (s.e2e_ms !== null) byF[s.feature].e2es.push(s.e2e_ms);
    if (s.dl_speed_avg > 0) byF[s.feature].speeds.push(s.dl_speed_avg);
  });
  const feats = Object.keys(byF).sort();
  const avg = a => a.length ? Math.round(a.reduce((x,y)=>x+y,0)/a.length) : 0;
  charts['chart-feature-speed'] = new Chart(document.getElementById('chart-feature-speed'), {
    type: 'bar',
    data: { labels: feats, datasets: [
      { label: '会话数', data: feats.map(f => byF[f].count), backgroundColor: 'rgba(79,70,229,.7)', borderRadius: 4, yAxisID: 'y' },
      { label: '平均TTFB(ms)', data: feats.map(f => avg(byF[f].ttfbs)), backgroundColor: 'rgba(245,158,11,.7)', borderRadius: 4, yAxisID: 'y1' },
    ]},
    options: { responsive: true,
      scales: {
        y: { type: 'linear', position: 'left', title: { display: true, text: '会话数' }, beginAtZero: true },
        y1: { type: 'linear', position: 'right', title: { display: true, text: 'TTFB(ms)' }, beginAtZero: true, grid: { drawOnChartArea: false } }
      }
    }
  });
}

function renderSpeedTimeChart() {
  destroyChart('chart-speed-time');
  const voicePts = FS.filter(s => s.dl_speed_avg > 0 && s.upload_type === 'voice').map(s => ({ x: s.hour + parseInt(s.timestamp.substring(14,16))/60, y: s.dl_speed_avg }));
  const textPts = FS.filter(s => s.dl_speed_avg > 0 && s.upload_type === 'text').map(s => ({ x: s.hour + parseInt(s.timestamp.substring(14,16))/60, y: s.dl_speed_avg }));
  charts['chart-speed-time'] = new Chart(document.getElementById('chart-speed-time'), {
    type: 'scatter',
    data: { datasets: [
      { label: '语音提问(算卦)', data: voicePts, backgroundColor: 'rgba(245,158,11,.55)', pointRadius: 5, pointHoverRadius: 8 },
      { label: '文本提问', data: textPts, backgroundColor: 'rgba(37,99,235,.45)', pointRadius: 5, pointHoverRadius: 8 },
    ]},
    options: { responsive: true,
      plugins: { tooltip: { callbacks: { label: ctx => `${ctx.dataset.label}: ${ctx.parsed.y.toLocaleString()} B/s @ ${Math.floor(ctx.parsed.x)}:00` } } },
      scales: {
        x: { title: { display: true, text: '访问时间 (小时)' }, min: 0, max: 24, ticks: { callback: v => v + ':00', stepSize: 2 } },
        y: { title: { display: true, text: '下行速率 (B/s)' }, beginAtZero: true }
      }
    }
  });
}

function renderUploadDownloadChart() {
  destroyChart('chart-upload-download');
  const byF = {};
  FS.forEach(s => {
    if (!byF[s.feature]) byF[s.feature] = { ul: 0, dl: 0 };
    byF[s.feature].ul += s.upload_bytes;
    byF[s.feature].dl += s.dl_bytes;
  });
  const feats = Object.keys(byF).sort();
  charts['chart-upload-download'] = new Chart(document.getElementById('chart-upload-download'), {
    type: 'bar',
    data: { labels: feats, datasets: [
      { label: '上行(提问)', data: feats.map(f => Math.round(byF[f].ul/1024)), backgroundColor: 'rgba(245,158,11,.7)', borderRadius: 4 },
      { label: '下行(响应)', data: feats.map(f => Math.round(byF[f].dl/1024)), backgroundColor: 'rgba(37,99,235,.7)', borderRadius: 4 },
    ]},
    options: { responsive: true, scales: { y: { beginAtZero: true, title: { display: true, text: 'KB' } } } }
  });
}

function renderConcurrencyChart() {
  destroyChart('chart-concurrency');
  const c = SUMMARY.concurrency;
  if (!c || !c.timeline || !c.timeline.length) return;
  const labels = c.timeline.map(p => p.label);
  const data = c.timeline.map(p => p.count);
  const maxVal = Math.max(...data);
  const bgColors = data.map(v => v === maxVal ? 'rgba(239,68,68,.7)' : 'rgba(79,70,229,.5)');
  charts['chart-concurrency'] = new Chart(document.getElementById('chart-concurrency'), {
    type: 'line',
    data: { labels, datasets: [{
      label: '并发设备数', data, borderColor: '#4f46e5',
      backgroundColor: 'rgba(79,70,229,.1)', fill: true, tension: .2, pointRadius: 2, pointHoverRadius: 6,
    }]},
    options: { responsive: true,
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: ctx => `并发设备: ${ctx.parsed.y} 台` } },
        annotation: {}
      },
      scales: {
        x: { ticks: { maxRotation: 45, autoSkip: true, maxTicksLimit: 20 }, title: { display: true, text: '时间' } },
        y: { beginAtZero: true, title: { display: true, text: '并发设备数' }, ticks: { stepSize: 1 } }
      }
    }
  });
}

function renderConcurrencyTtfbChart() {
  destroyChart('chart-concurrency-ttfb');
  const voiceSessions = FS.filter(s => s.ttfb_ms !== null && s.concurrency);
  if (!voiceSessions.length) return;
  const points = voiceSessions.map(s => ({ x: s.concurrency, y: s.ttfb_ms }));
  charts['chart-concurrency-ttfb'] = new Chart(document.getElementById('chart-concurrency-ttfb'), {
    type: 'scatter',
    data: { datasets: [{ label: 'TTFB(ms)', data: points,
      backgroundColor: 'rgba(79,70,229,.45)', pointRadius: 5, pointHoverRadius: 8 }]},
    options: { responsive: true,
      plugins: { legend: { display: false },
        tooltip: { callbacks: { label: ctx => `并发${ctx.parsed.x}台, TTFB=${ctx.parsed.y}ms` } }
      },
      scales: {
        x: { title: { display: true, text: '并发设备数' }, ticks: { stepSize: 1 } },
        y: { title: { display: true, text: 'TTFB(ms)' }, beginAtZero: true }
      }
    }
  });
}

function renderConcurrencyImpactChart() {
  destroyChart('chart-concurrency-impact');
  const sessWithConcurrency = FS.filter(s => s.concurrency);
  if (!sessWithConcurrency.length) return;
  // 按并发度分组
  const byLevel = {};
  sessWithConcurrency.forEach(s => {
    const lvl = s.concurrency;
    if (!byLevel[lvl]) byLevel[lvl] = { ttfbs: [], e2es: [], speeds: [], count: 0 };
    byLevel[lvl].count++;
    if (s.ttfb_ms !== null) byLevel[lvl].ttfbs.push(s.ttfb_ms);
    if (s.e2e_ms !== null) byLevel[lvl].e2es.push(s.e2e_ms);
    if (s.dl_speed_avg > 0) byLevel[lvl].speeds.push(s.dl_speed_avg);
  });
  const levels = Object.keys(byLevel).map(Number).sort((a,b) => a-b);
  const avg = a => a.length ? Math.round(a.reduce((x,y)=>x+y,0)/a.length) : 0;
  const labels = levels.map(l => l + '台');
  charts['chart-concurrency-impact'] = new Chart(document.getElementById('chart-concurrency-impact'), {
    type: 'bar',
    data: { labels, datasets: [
      { label: '平均TTFB(ms)', data: levels.map(l => avg(byLevel[l].ttfbs)), backgroundColor: 'rgba(245,158,11,.7)', borderRadius: 4, yAxisID: 'y' },
      { label: '平均E2E(ms)', data: levels.map(l => avg(byLevel[l].e2es)), backgroundColor: 'rgba(79,70,229,.7)', borderRadius: 4, yAxisID: 'y' },
      { label: '会话数', data: levels.map(l => byLevel[l].count), type: 'line', borderColor: '#dc2626', backgroundColor: 'rgba(220,38,38,.1)', borderDash: [5,5], tension: .3, yAxisID: 'y1', pointRadius: 4 },
    ]},
    options: { responsive: true,
      plugins: { legend: { position: 'top' } },
      scales: {
        y: { type: 'linear', position: 'left', title: { display: true, text: '毫秒(ms)' }, beginAtZero: true },
        y1: { type: 'linear', position: 'right', title: { display: true, text: '会话数' }, beginAtZero: true, grid: { drawOnChartArea: false } }
      }
    }
  });
}

function renderZeroSpeedChart() {
  destroyChart('chart-zero-speed');
  const zeros = RAW_DATA.filter(e => e.speed_bps === 0);
  const byDevDir = {};
  zeros.forEach(e => {
    if (!byDevDir[e.device_id]) byDevDir[e.device_id] = { dl: 0, ul: 0 };
    byDevDir[e.device_id][e.direction === 'download' ? 'dl' : 'ul']++;
  });
  const devices = Object.keys(byDevDir).sort();
  const dlC = devices.map(d => byDevDir[d].dl), ulC = devices.map(d => byDevDir[d].ul);
  const byFeat = {};
  zeros.forEach(e => { byFeat[e.feature] = (byFeat[e.feature]||0) + 1; });
  const tDl = dlC.reduce((a,b)=>a+b,0), tUl = ulC.reduce((a,b)=>a+b,0);
  document.getElementById('zero-speed-summary').innerHTML =
    `<strong>零速率记录总数: ${zeros.length}</strong> &nbsp;|&nbsp; <span class="dir-down">下行 ${tDl}</span> / <span class="dir-up">上行 ${tUl}</span> &nbsp;|&nbsp; 涉及 ${devices.length} 台设备 &nbsp;|&nbsp; 按功能: ${Object.entries(byFeat).sort((a,b)=>b[1]-a[1]).map(([k,v])=>`${k}:${v}`).join(' / ')}`;
  charts['chart-zero-speed'] = new Chart(document.getElementById('chart-zero-speed'), {
    type: 'bar',
    data: { labels: devices, datasets: [
      { label: '下行零速率', data: dlC, backgroundColor: 'rgba(37,99,235,.7)', borderRadius: 4 },
      { label: '上行零速率', data: ulC, backgroundColor: 'rgba(245,158,11,.7)', borderRadius: 4 },
    ]},
    options: { responsive: true, plugins: { legend: { position: 'top' }, title: { display: true, text: '按设备零速率分布', font: { size: 13 } } },
      scales: { y: { beginAtZero: true, title: { display: true, text: '记录数' }, ticks: { stepSize: 1 } } }
    }
  });
  // 按设备汇总表（仅统计数，不列出明细）
  if (devices.length) {
    let html = '<div class="table-wrap"><table style="width:100%;font-size:.78rem"><thead><tr><th>设备</th><th>下行零速率</th><th>上行零速率</th><th>合计</th></tr></thead><tbody>';
    devices.slice().sort((a,b) => (byDevDir[b].dl+byDevDir[b].ul) - (byDevDir[a].dl+byDevDir[a].ul)).forEach(d => {
      const t = byDevDir[d].dl + byDevDir[d].ul;
      html += `<tr><td><span class="badge badge-blue">${d}</span></td><td class="dir-down">${byDevDir[d].dl}</td><td class="dir-up">${byDevDir[d].ul}</td><td><b>${t}</b></td></tr>`;
    });
    html += '</tbody></table></div>';
    document.getElementById('zero-speed-breakdown').innerHTML = html;
  } else {
    document.getElementById('zero-speed-breakdown').innerHTML = '';
  }
}

// ── 设备表格 (会话级) ──
function renderTableRow(r) {
  return `<tr>
    <td><span class="badge badge-blue">${r[0]}</span></td>
    <td>${r[1]}</td><td>${r[2]}</td>
    <td class="dir-up">${r[3]}</td><td class="dir-up">${r[4]}</td>
    <td>${r[5].toLocaleString()}</td><td>${r[6].toLocaleString()}</td>
    <td class="dir-down">${r[7].toLocaleString()}</td><td class="dir-down">${r[8].toLocaleString()}</td>
    <td class="dir-down">${r[9].toLocaleString()}</td><td class="dir-up">${r[10].toLocaleString()}</td>
  </tr>`;
}
function renderDeviceTable() {
  const byDev = {};
  FS.forEach(s => {
    if (!byDev[s.device_id]) byDev[s.device_id] = { dates: new Set(), sessions: 0, voice: 0, text: 0, ttfbs: [], e2es: [], speeds: [], dlBytes: 0, ulBytes: 0 };
    const d = byDev[s.device_id];
    d.dates.add(s.date); d.sessions++;
    if (s.upload_type === 'voice') d.voice++; else d.text++;
    if (s.ttfb_ms !== null) d.ttfbs.push(s.ttfb_ms);
    if (s.e2e_ms !== null) d.e2es.push(s.e2e_ms);
    if (s.dl_speed_avg > 0) d.speeds.push(s.dl_speed_avg);
    d.dlBytes += s.dl_bytes; d.ulBytes += s.upload_bytes;
  });
  const rows = Object.entries(byDev).map(([dev, d]) => {
    const avg = a => a.length ? Math.round(a.reduce((x,y)=>x+y,0)/a.length) : 0;
    const mx = a => a.length ? a[a.length-1] : 0;
    return [dev, d.dates.size, d.sessions, d.voice, d.text, avg(d.ttfbs), avg(d.e2es), avg(d.speeds), mx(d.speeds), Math.round(d.dlBytes/1024), Math.round(d.ulBytes/1024)];
  });
  document.getElementById('device-tbody').innerHTML = rows.map(renderTableRow).join('');
  window._tableRows = rows;
}
let sortDir = {};
function sortTable(col) {
  sortDir[col] = !sortDir[col];
  const rows = window._tableRows || [];
  rows.sort((a,b) => { const va=a[col],vb=b[col]; return sortDir[col] ? (typeof va==='number'?va-vb:String(va).localeCompare(String(vb))) : (typeof va==='number'?vb-va:String(vb).localeCompare(String(va))); });
  document.getElementById('device-tbody').innerHTML = rows.map(renderTableRow).join('');
  window._tableRows = rows;
}

// ── 跨层分析图表 ──
function renderLatencyBreakdown() {
  const lb = _CROSS.latency_breakdown;
  if (!lb || !Object.keys(lb).length) return;
  const phaseOrder = ['语音上传','STT识别','LLM推理','TTS合成','网络下行'];
  const phases = phaseOrder.filter(p => lb[p]);
  const avgs = phases.map(p => lb[p].avg);
  const medians = phases.map(p => lb[p].median);
  const p95s = phases.map(p => lb[p].p95);
  const colors = ['#f59e0b','#06b6d4','#8b5cf6','#ec4899','#3b82f6'];
  destroyChart('chart-latency-breakdown');
  charts['chart-latency-breakdown'] = new Chart(document.getElementById('chart-latency-breakdown'), {
    type: 'bar',
    data: { labels: phases, datasets: [
      { label: '平均值', data: avgs, backgroundColor: colors.map(c=>c+'cc') },
      { label: '中位数', data: medians, backgroundColor: colors.map(c=>c+'66') },
      { label: 'P95', data: p95s, backgroundColor: colors.map(c=>c+'33'), borderColor: colors, borderWidth: 1 },
    ]},
    options: { responsive: true,
      plugins: { tooltip: { callbacks: { label: ctx => `${ctx.dataset.label}: ${(ctx.parsed.y/1000).toFixed(1)}s` } } },
      scales: { y: { title: { display: true, text: '耗时' }, ticks: { callback: v => (v/1000).toFixed(1)+'s' }, beginAtZero: true } }
    }
  });
  // 详细数据
  let html = '<table style="width:100%;font-size:.8rem"><tr><th>阶段</th><th>样本数</th><th>平均</th><th>中位数</th><th>P95</th><th>最小</th><th>最大</th></tr>';
  phases.forEach(p => {
    const s = lb[p];
    html += `<tr><td><b>${p}</b></td><td>${s.count}</td><td>${(s.avg/1000).toFixed(1)}s</td><td>${(s.median/1000).toFixed(1)}s</td><td>${(s.p95/1000).toFixed(1)}s</td><td>${(s.min/1000).toFixed(1)}s</td><td>${(s.max/1000).toFixed(1)}s</td></tr>`;
  });
  const totalAvg = avgs.reduce((a,b)=>a+b,0);
  const serverPhases2 = phases.filter(p => p !== '网络下行');
  const realBn = serverPhases2.length ? serverPhases2.reduce((a,b) => lb[a].avg > lb[b].avg ? a : b) : null;
  html += `<tr style="font-weight:700;border-top:2px solid #cbd5e1"><td>服务端合计</td><td></td><td>${((totalAvg-(lb['网络下行']?.avg||0))/1000).toFixed(1)}s</td><td colspan="4">${realBn ? '服务端瓶颈: '+realBn : ''}</td></tr>`;
  html += `<tr><td colspan="7" style="font-size:.72rem;color:#64748b;padding-top:4px">💡 网络下行含播放等待时间（按播放进度流式推送，by design）</td></tr>`;
  document.getElementById('latency-detail').innerHTML = html + '</table>';
}

function renderCloseReason() {
  const cs = _CROSS.connection_stability;
  if (!cs) return;
  const labels = Object.keys(cs.close_reasons);
  const data = Object.values(cs.close_reasons);
  const colorMap = {'正常关闭(1000)':'#22c55e','客户端主动关闭(正常)':'#06b6d4','连接断开(1006)':'#ef4444','超时关闭':'#f59e0b','未关闭':'#94a3b8'};
  const colors = labels.map(l => colorMap[l] || '#64748b');
  destroyChart('chart-close-reason');
  charts['chart-close-reason'] = new Chart(document.getElementById('chart-close-reason'), {
    type: 'doughnut',
    data: { labels, datasets: [{ data, backgroundColor: colors }] },
    options: { responsive: true,
      plugins: { legend: { position: 'bottom' },
        tooltip: { callbacks: { label: ctx => `${ctx.label}: ${ctx.parsed} 次 (${(ctx.parsed/cs.total*100).toFixed(1)}%)` } }
      }
    }
  });
}

function renderInputMode() {
  const im = _CROSS.input_modes;
  if (!im || !im.total) return;
  destroyChart('chart-input-mode');
  charts['chart-input-mode'] = new Chart(document.getElementById('chart-input-mode'), {
    type: 'doughnut',
    data: { labels: ['语音提问(算卦)','文本提问(运势/八字)'],
      datasets: [{ data: [im.voice, im.text], backgroundColor: ['#4f46e5','#f59e0b'] }] },
    options: { responsive: true,
      plugins: { legend: { position: 'bottom' },
        tooltip: { callbacks: { label: ctx => `${ctx.label}: ${ctx.parsed} 次 (${(ctx.parsed/im.total*100).toFixed(1)}%)` } }
      }
    }
  });
}

function renderLlmTts() {
  const lt = _CROSS.llm_tts;
  if (!lt) return;
  // 统计卡片
  const stats = [
    { label: 'LLM→首句TTS', value: (lt.llm_to_first_tts.avg/1000).toFixed(1)+'s', sub: `中位数 ${(lt.llm_to_first_tts.median/1000).toFixed(1)}s, n=${lt.llm_to_first_tts.count}` },
    { label: 'TTS流时长', value: (lt.tts_stream_duration.avg/1000).toFixed(1)+'s', sub: `中位数 ${(lt.tts_stream_duration.median/1000).toFixed(1)}s` },
    { label: '平均句数', value: lt.sentence_counts.avg.toFixed(1), sub: `最多 ${lt.sentence_counts.max} 句` },
    { label: '首句总延迟', value: (lt.first_sentence_delay.avg/1000).toFixed(1)+'s', sub: `从连接建立到首句TTS` },
  ];
  document.getElementById('llm-tts-stats').innerHTML = stats.map(s =>
    `<div class="card" style="padding:.75rem"><div class="label">${s.label}</div><div class="value" style="font-size:1.3rem">${s.value}</div><div class="sub">${s.sub}</div></div>`
  ).join('');
  // 柱状图: LLM→TTS 延迟分布
  const lb = _CROSS.latency_breakdown;
  const phases = ['语音上传','STT识别','LLM推理','TTS合成','网络下行'];
  const avail = phases.filter(p => lb && lb[p]);
  if (!avail.length) return;
  const medians = avail.map(p => lb[p].median);
  const colors = ['#f59e0b','#06b6d4','#8b5cf6','#ec4899','#3b82f6'];
  destroyChart('chart-llm-tts');
  charts['chart-llm-tts'] = new Chart(document.getElementById('chart-llm-tts'), {
    type: 'bar',
    data: { labels: avail, datasets: [{ label: '中位数耗时', data: medians, backgroundColor: colors.slice(0, avail.length) }] },
    options: { responsive: true, indexAxis: 'y',
      plugins: { legend: { display: false }, tooltip: { callbacks: { label: ctx => (ctx.parsed.x/1000).toFixed(1)+'s' } } },
      scales: { x: { ticks: { callback: v => (v/1000).toFixed(1)+'s' }, beginAtZero: true } }
    }
  });
}

function renderDeviceStability() {
  const cs = _CROSS.connection_stability;
  if (!cs || !cs.device_stats || !cs.device_stats.length) return;
  const ds = cs.device_stats.filter(d => d.total >= 2);
  if (!ds.length) return;
  const labels = ds.map(d => d.device_id);
  destroyChart('chart-device-stability');
  charts['chart-device-stability'] = new Chart(document.getElementById('chart-device-stability'), {
    type: 'bar',
    data: { labels, datasets: [
      { label: '正常关闭', data: ds.map(d=>d.normal), backgroundColor: 'rgba(34,197,94,.6)' },
      { label: '连接断开(1006)', data: ds.map(d=>d.broken), backgroundColor: 'rgba(239,68,68,.6)' },
      { label: '超时关闭', data: ds.map(d=>d.timeout), backgroundColor: 'rgba(245,158,11,.6)' },
    ]},
    options: { responsive: true,
      plugins: { legend: { position: 'top' },
        tooltip: { callbacks: { afterBody: ctx => { const i=ctx[0].dataIndex; return `断线率: ${ds[i].disconnect_rate}%`; } } }
      },
      scales: { x: { stacked: true }, y: { stacked: true, title: { display: true, text: '会话数' } } }
    }
  });
}

function renderConcurrencyError() {
  const ec = _CROSS.error_by_concurrency;
  if (!ec || !ec.breakdown || !ec.breakdown.length) return;
  const labels = ec.breakdown.map(d => d.label);
  const rates = ec.breakdown.map(d => d.error_rate);
  const totals = ec.breakdown.map(d => d.total);
  const errors = ec.breakdown.map(d => d.errors);
  const timeouts = ec.breakdown.map(d => d.timeout);
  destroyChart('chart-concurrency-error');
  charts['chart-concurrency-error'] = new Chart(document.getElementById('chart-concurrency-error'), {
    data: { labels, datasets: [
      { type: 'bar', label: '会话数', data: totals, backgroundColor: 'rgba(79,70,229,.45)', borderRadius: 4, yAxisID: 'y1', order: 4 },
      { type: 'bar', label: '超时回收(正常)', data: timeouts, backgroundColor: 'rgba(245,158,11,.6)', borderRadius: 4, yAxisID: 'y1', order: 3 },
      { type: 'bar', label: '错误数(断线1006)', data: errors, backgroundColor: 'rgba(239,68,68,.7)', borderRadius: 4, yAxisID: 'y1', order: 2 },
      { type: 'line', label: '错误率(%)', data: rates, borderColor: '#dc2626', backgroundColor: 'rgba(220,38,38,.1)', tension: .3, yAxisID: 'y', pointRadius: 4, pointHoverRadius: 7, order: 1 },
    ]},
    options: { responsive: true,
      plugins: { legend: { position: 'top' },
        tooltip: { callbacks: { label: ctx => ctx.dataset.label === '错误率(%)' ? `错误率: ${ctx.parsed.y}%` : `${ctx.dataset.label}: ${ctx.parsed.y}` } }
      },
      scales: {
        y: { type: 'linear', position: 'left', title: { display: true, text: '错误率(%)' }, beginAtZero: true },
        y1: { type: 'linear', position: 'right', title: { display: true, text: '会话数' }, beginAtZero: true, grid: { drawOnChartArea: false }, ticks: { stepSize: 1 } }
      }
    }
  });
  const hl = 'style="color:#dc2626;font-weight:700"';
  let html = `<p style="margin-bottom:.5rem">整体错误率: <b ${ec.overall_error_rate>10?hl:''}>${ec.overall_error_rate}%</b> (${ec.errors}/${ec.total}) &nbsp;|&nbsp; 超时回收: <b style="color:#d97706">${ec.timeout}</b> 次 &nbsp;|&nbsp; <span style="font-size:.78rem;color:#64748b">错误仅计断线(1006)；超时为 InactiveSessionChecker 60s 空闲回收，属正常行为，单独统计不计入错误</span></p>`;
  html += '<div class="table-wrap"><table style="width:100%;font-size:.78rem"><tr><th>并发度</th><th>会话数</th><th>错误数(断线1006)</th><th>超时回收(正常)</th><th>错误率</th></tr>';
  ec.breakdown.forEach(d => {
    html += `<tr><td><b>${d.label}</b></td><td>${d.total}</td><td ${d.errors>0?'style="color:#dc2626;font-weight:600"':''}>${d.errors}</td><td style="color:#d97706">${d.timeout}</td><td ${d.error_rate>10?hl:''}>${d.error_rate}%</td></tr>`;
  });
  html += '</table></div>';
  document.getElementById('concurrency-error-detail').innerHTML = html;
}

document.addEventListener('DOMContentLoaded', init);
</script>
</body>
</html>"""


# ─── 报告生成 ────────────────────────────────────────────────────────────────

def generate_html_report(entries: List[LogEntry], log_dir: str, output_path: str):
    """生成交互式 HTML 报告"""
    server_sessions = parse_server_logs(log_dir)
    summary = prepare_summary(entries, server_sessions)
    frontend_data = prepare_frontend_data(entries, server_sessions)

    print("  运行跨层分析...")
    client_sessions = build_sessions(entries)
    cross_analysis = run_cross_layer_analysis(log_dir, client_sessions, server_sessions)

    html = HTML_TEMPLATE
    html = html.replace('__GENERATED_AT__', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    html = html.replace('__LOG_DIR__', log_dir)
    html = html.replace('__DATA_JSON__', json.dumps(frontend_data, ensure_ascii=False))
    html = html.replace('__SUMMARY_JSON__', json.dumps(summary, ensure_ascii=False))
    html = html.replace('__CROSS_ANALYSIS__', json.dumps(cross_analysis, ensure_ascii=False))

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(html)


# ─── 主函数 ──────────────────────────────────────────────────────────────────

def resolve_log_dirs(base_dir: str) -> Dict[str, str]:
    """
    根据基础日志目录解析各子目录路径。
    目录结构:
      logs/
        dialogue/   ← device-log*.log + xiaozhi-dialogue*.log
        server/     ← xiaozhi-server*.log
    """
    dialogue_dir = os.path.join(base_dir, 'dialogue')
    server_dir = os.path.join(base_dir, 'server')
    return {
        'base': base_dir,
        'dialogue': dialogue_dir,
        'server': server_dir,
    }


def check_dirs(dirs: Dict[str, str]) -> bool:
    """检查关键目录是否存在，打印诊断信息。"""
    ok = True
    dialogue = dirs['dialogue']
    if not os.path.isdir(dialogue):
        print(f"❌ dialogue 日志目录不存在: {dialogue}")
        ok = False
    else:
        device_count = len(glob.glob(os.path.join(dialogue, 'device-log*.log')))
        svc_count = len(glob.glob(os.path.join(dialogue, 'xiaozhi-dialogue*.log')))
        print(f"  dialogue 目录: {dialogue}")
        print(f"    设备日志文件: {device_count} 个")
        print(f"    服务日志文件: {svc_count} 个")
        if device_count == 0 and svc_count == 0:
            print("  ⚠️  dialogue 目录下无日志文件")
    return ok


def parse_args():
    parser = argparse.ArgumentParser(
        description='设备日志分析 - 生成交互式 HTML 仪表盘报告',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例:
  python analyze_logs.py                  # 使用脚本所在目录作为日志根目录
  python analyze_logs.py --log-dir /path  # 指定日志根目录
  python analyze_logs.py -o report.html   # 指定输出文件
""")
    parser.add_argument('--log-dir', '-d', default=None,
                        help='日志根目录 (默认: 脚本所在目录)')
    parser.add_argument('--output', '-o', default=None,
                        help='输出 HTML 文件路径 (默认: <log-dir>/dashboard.html)')
    return parser.parse_args()


def main():
    args = parse_args()

    # 确定日志根目录: 命令行指定 > 脚本所在目录
    if args.log_dir:
        base_dir = os.path.abspath(args.log_dir)
    else:
        base_dir = os.path.dirname(os.path.abspath(__file__))

    dirs = resolve_log_dirs(base_dir)
    dialogue_dir = dirs['dialogue']

    print("=" * 60)
    print("  设备日志分析 - 交互式 HTML 报告")
    print("=" * 60)
    print(f"\n日志根目录: {base_dir}")

    if not check_dirs(dirs):
        sys.exit(1)

    print("\n正在加载设备端日志...")
    entries = load_all_logs(dialogue_dir)
    print(f"  共加载 {len(entries)} 条设备端原始记录")

    if not entries:
        print("\n❌ 未找到有效设备日志记录！")
        print(f"   请确认 {dialogue_dir} 下存在 device-log*.log 文件")
        sys.exit(1)

    sessions = build_sessions(entries)
    print(f"  聚合为 {len(sessions)} 个设备端会话")

    print("\n正在加载服务端日志...")
    server_sessions = parse_server_logs(dialogue_dir)
    print(f"  服务端 WebSocket 会话: {len(server_sessions)} 个")

    # 输出路径
    output_path = args.output or os.path.join(base_dir, 'dashboard.html')
    output_path = os.path.abspath(output_path)

    print(f"\n正在生成交互式 HTML 报告...")
    generate_html_report(entries, dialogue_dir, output_path)
    print(f"\n{'=' * 60}")
    print(f"✅ 报告已生成: {output_path}")
    print(f"   请在浏览器中打开该文件查看交互式仪表盘。")
    print(f"   支持: 日期范围筛选 | 图表交互 | 表格排序")
    print(f"{'=' * 60}")


if __name__ == '__main__':
    main()
