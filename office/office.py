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
from datetime import datetime, timezone
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


def scan_usage():
    base = os.path.expanduser("~/.claude/projects")
    today = datetime.now().astimezone().date()
    total = {"calls": 0, "input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "cost": 0.0}
    if not os.path.isdir(base):
        return total
    midnight = datetime.combine(today, datetime.min.time()).astimezone().timestamp()
    for dirpath, _dirs, files in os.walk(base):
        for fname in files:
            if not fname.endswith(".jsonl"):
                continue
            path = os.path.join(dirpath, fname)
            try:
                if os.path.getmtime(path) < midnight:
                    continue
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
                            when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        except ValueError:
                            continue
                        if when.astimezone().date() != today:
                            continue
                        inp = usage.get("input_tokens", 0)
                        out = usage.get("output_tokens", 0)
                        cr = usage.get("cache_read_input_tokens", 0)
                        cw = usage.get("cache_creation_input_tokens", 0)
                        p_in, p_out, p_cr, p_cw = price_for(msg.get("model"))
                        total["calls"] += 1
                        total["input"] += inp
                        total["output"] += out
                        total["cache_read"] += cr
                        total["cache_write"] += cw
                        total["cost"] += (inp * p_in + out * p_out + cr * p_cr + cw * p_cw) / 1e6
            except OSError:
                continue
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


def with_pace(windows):
    """권고 페이스 = 윈도우 경과시간 비율. 초과분은 +%p로."""
    now = datetime.now(timezone.utc)
    out = []
    for w in windows:
        item = dict(w)
        try:
            resets = datetime.fromisoformat(w["resets_at"])
            window = float(w.get("window_hours", w.get("window_days", 7) * 24)) * 3600
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
        usage = scan_usage()
        quota = with_pace(load_json("quota.json", []))
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
