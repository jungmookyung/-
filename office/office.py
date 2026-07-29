#!/usr/bin/env python3
"""Agent Office — tmux에서 도는 Claude Code/Codex 워커를 감시하고
픽셀 사무실 대시보드로 보여주는 단일 파일 서버 (MVP).

의존성: Python 3.9+ 표준 라이브러리만. tmux가 있으면 워커 감시, 없으면 빈 사무실.

실행:  python3 office/office.py            # http://localhost:8765
환경:  OFFICE_PORT / OFFICE_POLL / OFFICE_STALL_SECS
"""
import json
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import HTTPServer, SimpleHTTPRequestHandler

ROOT = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(ROOT, "web")
PORT = int(os.environ.get("OFFICE_PORT", "8765"))
POLL_SECS = float(os.environ.get("OFFICE_POLL", "5"))
STALL_SECS = int(os.environ.get("OFFICE_STALL_SECS", "600"))
# busy 판정은 화면 하단 상태바 영역만 본다 — 본문에 "esc to interrupt" 같은
# 문구가 인용돼도 오탐하지 않도록 (원조 시스템의 와처 오탐 버그에서 얻은 교훈).
STATUS_TAIL_LINES = 8

BUSY_MARKERS = ("esc to interrupt", "ctrl+c to interrupt", "running…", "working…")
IDLE_MARKERS = ("? for shortcuts", "@ to mention", "bypass permissions", "plan mode")

_lock = threading.Lock()
_state = {"agents": [], "usage": {}, "quota": [], "tasks": [], "events": [], "updated": None}
_pane_memory = {}  # pane_id -> {"hash": str, "since": float}


def sh(args):
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=10)
        return out.stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def list_panes():
    fmt = "#{pane_id}\t#{session_name}\t#{window_index}.#{pane_index}\t#{pane_current_command}"
    panes = []
    for line in sh(["tmux", "list-panes", "-a", "-F", fmt]).splitlines():
        parts = line.split("\t")
        if len(parts) == 4:
            panes.append({"id": parts[0], "session": parts[1], "pos": parts[2], "cmd": parts[3]})
    return panes


def capture(pane_id):
    # 보이는 화면만 캡처한다 — 스크롤백까지 보면 지나간 busy 마커에 오탐한다.
    return sh(["tmux", "capture-pane", "-p", "-t", pane_id])


def agent_kind(pane, content):
    cmd = pane["cmd"].lower()
    if "claude" in cmd:
        return "claude"
    if "codex" in cmd:
        return "codex"
    low = content.lower()
    if "esc to interrupt" in low or "? for shortcuts" in low:
        return "claude"
    return None  # 에이전트 아님


def classify(pane_id, content):
    """(status, bubble) — busy/idle/stalled/unknown 판정."""
    lines = [ln.rstrip() for ln in content.splitlines()]
    nonempty = [ln for ln in lines if ln.strip()]
    tail = [ln.lower() for ln in nonempty[-STATUS_TAIL_LINES:]]

    busy = any(m in ln for ln in tail for m in BUSY_MARKERS)
    idle = any(m in ln for ln in tail for m in IDLE_MARKERS)

    # 정체 감지: 화면 전체 내용이 STALL_SECS 이상 그대로면 busy라도 stalled
    digest = str(hash(content))
    now = time.time()
    mem = _pane_memory.get(pane_id)
    if mem is None or mem["hash"] != digest:
        _pane_memory[pane_id] = {"hash": digest, "since": now}
        unchanged = 0.0
    else:
        unchanged = now - mem["since"]

    if busy:
        status = "stalled" if unchanged >= STALL_SECS else "busy"
    elif idle:
        status = "idle"
    else:
        status = "unknown"

    bubble = ""
    for ln in reversed(nonempty[-STATUS_TAIL_LINES:]):
        s = ln.strip().strip("│╰╯╭╮─ ")
        if s and not s.startswith(("?", ">")):
            bubble = s[:70]
            break
    return status, bubble, int(unchanged)


def scan_agents():
    agents = []
    for pane in list_panes():
        content = capture(pane["id"])
        kind = agent_kind(pane, content)
        if not kind:
            continue
        status, bubble, unchanged = classify(pane["id"], content)
        agents.append({
            "id": pane["id"],
            "name": f'{pane["session"]}:{pane["pos"]}',
            "kind": kind,
            "status": status,
            "bubble": bubble,
            "unchanged_secs": unchanged,
        })
    return agents


# ---- 오늘 작업량: ~/.claude/projects/**/*.jsonl 에서 오늘치 usage 합산 ----

PRICE = {  # USD per MTok: (input, output, cache_read, cache_write)
    "opus": (15.0, 75.0, 1.5, 18.75),
    "sonnet": (3.0, 15.0, 0.3, 3.75),
    "haiku": (0.8, 4.0, 0.08, 1.0),
}


def price_for(model):
    m = (model or "").lower()
    for key, p in PRICE.items():
        if key in m:
            return p
    return PRICE["sonnet"]


_usage_cache = {}  # path -> ((mtime, size), [(epoch, cost, in, out, cache_r, cache_w)])


def _file_events(path):
    """트랜스크립트 한 파일의 usage 레코드. (mtime, size)가 같으면 캐시 재사용."""
    try:
        st = os.stat(path)
    except OSError:
        return []
    key = (st.st_mtime, st.st_size)
    cached = _usage_cache.get(path)
    if cached and cached[0] == key:
        return cached[1]
    events = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"usage"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = rec.get("message") or {}
                usage = msg.get("usage")
                ts = rec.get("timestamp")
                if not usage or not ts:
                    continue
                try:
                    when = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                except ValueError:
                    continue
                inp = usage.get("input_tokens", 0)
                out = usage.get("output_tokens", 0)
                cr = usage.get("cache_read_input_tokens", 0)
                cw = usage.get("cache_creation_input_tokens", 0)
                p_in, p_out, p_cr, p_cw = price_for(msg.get("model"))
                cost = (inp * p_in + out * p_out + cr * p_cr + cw * p_cw) / 1e6
                events.append((when, cost, inp, out, cr, cw))
    except OSError:
        return []
    _usage_cache[path] = (key, events)
    return events


def usage_events(since_epoch):
    """since 이후의 usage 레코드 전부. mtime이 since보다 오래된 파일은 건너뛴다."""
    base = os.path.expanduser("~/.claude/projects")
    if not os.path.isdir(base):
        return []
    out = []
    for dirpath, _dirs, files in os.walk(base):
        for fname in files:
            if not fname.endswith(".jsonl"):
                continue
            path = os.path.join(dirpath, fname)
            try:
                if os.path.getmtime(path) < since_epoch:
                    continue
            except OSError:
                continue
            out.extend(e for e in _file_events(path) if e[0] >= since_epoch)
    return out


def aggregate(events):
    total = {"calls": 0, "input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "cost": 0.0}
    for _ts, cost, inp, out, cr, cw in events:
        total["calls"] += 1
        total["input"] += inp
        total["output"] += out
        total["cache_read"] += cr
        total["cache_write"] += cw
        total["cost"] += cost
    total["cost"] = round(total["cost"], 2)
    return total


# ---- 쿼터 게이지: office/quota.json (수동 갱신) + 권고 페이스 계산 ----

def load_json(name, default):
    path = os.path.join(ROOT, name)
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return default


def window_secs(w):
    return float(w.get("window_hours", w.get("window_days", 7) * 24)) * 3600


def roll_windows(windows):
    """auto 항목(또는 auto_roll)의 리셋 시각이 지났으면 다음 주기로 넘긴다.
    돌려받은 changed가 True면 quota.json을 다시 써야 한다."""
    now = datetime.now(timezone.utc)
    changed = False
    for w in windows:
        if not (w.get("auto") or w.get("auto_roll")):
            continue
        try:
            resets = datetime.fromisoformat(w["resets_at"])
            step = timedelta(seconds=window_secs(w))
        except (KeyError, ValueError):
            continue
        while resets <= now:
            resets += step
            changed = True
        w["resets_at"] = resets.isoformat(timespec="seconds")
    return windows, changed


def autofill_quota(windows):
    """auto: {metric: cost|output_tokens, budget: N} 항목의 used_pct를
    현재 윈도우 구간의 실제 소비량으로 채운다. (Claude 트랜스크립트 기준)"""
    auto = [w for w in windows if isinstance(w.get("auto"), dict)]
    if not auto:
        return
    starts = {}
    for w in auto:
        try:
            resets = datetime.fromisoformat(w["resets_at"]).timestamp()
        except (KeyError, ValueError):
            continue
        starts[id(w)] = resets - window_secs(w)
    if not starts:
        return
    events = usage_events(min(starts.values()))
    for w in auto:
        start = starts.get(id(w))
        if start is None:
            continue
        metric = w["auto"].get("metric", "cost")
        in_window = [e for e in events if e[0] >= start]
        value = sum(e[1] for e in in_window) if metric == "cost" else sum(e[3] for e in in_window)
        w["used_value"] = round(value, 2)
        budget = float(w["auto"].get("budget", 0))
        if budget > 0:
            w["used_pct"] = round(value / budget * 100, 1)
            w["budget"] = budget


def save_quota(windows):
    tmp = os.path.join(ROOT, "quota.json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(windows, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, os.path.join(ROOT, "quota.json"))


def with_pace(windows):
    """권고 페이스 = 윈도우 경과시간 비율. 초과분은 +%p로."""
    now = datetime.now(timezone.utc)
    out = []
    for w in windows:
        item = dict(w)
        try:
            resets = datetime.fromisoformat(w["resets_at"])
            window = window_secs(w)
            remaining = (resets - now).total_seconds()
            item["expired"] = remaining <= 0  # 리셋 지남 — used_pct 갱신 필요
            remaining = max(0.0, remaining)
            elapsed_pct = max(0.0, min(100.0, (1 - remaining / window) * 100))
            item["pace_pct"] = round(elapsed_pct, 1)
            used = float(w.get("used_pct", 0))
            item["over_pct"] = round(used - elapsed_pct, 1) if used > elapsed_pct else 0
            item["headroom_pct"] = round(elapsed_pct - used, 1) if used <= elapsed_pct else 0
        except (KeyError, ValueError, ZeroDivisionError):
            item["pace_pct"] = None
        out.append(item)
    return out


def load_events(limit=15):
    path = os.path.join(ROOT, "events.jsonl")
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()[-limit:]
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return list(reversed(out))  # 최신이 위로
    except OSError:
        return []


def collector():
    while True:
        agents = scan_agents()
        midnight = datetime.combine(datetime.now().astimezone().date(),
                                    datetime.min.time()).astimezone().timestamp()
        usage = aggregate(usage_events(midnight))
        quota_cfg = load_json("quota.json", [])
        quota_cfg, rolled = roll_windows(quota_cfg)
        if rolled:
            save_quota(quota_cfg)  # 리셋 시각 전진분만 기록 (계산 필드는 저장 안 함)
        autofill_quota(quota_cfg)
        quota = with_pace(quota_cfg)
        tasks = load_json("tasks.json", [])
        events = load_events()
        with _lock:
            _state.update(agents=agents, usage=usage, quota=quota, tasks=tasks,
                          events=events,
                          updated=datetime.now().astimezone().isoformat(timespec="seconds"))
        time.sleep(POLL_SECS)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=WEB_DIR, **kwargs)

    def do_GET(self):
        if self.path == "/api/state":
            with _lock:
                body = json.dumps(_state).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def log_message(self, *args):
        pass


def main():
    threading.Thread(target=collector, daemon=True).start()
    server = HTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Agent Office → http://localhost:{PORT}  (poll {POLL_SECS}s, stall {STALL_SECS}s)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
