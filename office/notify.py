#!/usr/bin/env python3
"""Agent Office Discord 알림 봇 (Phase 3) — 웹훅으로 발신 전용.

세 가지를 보낸다:
  1. 이벤트 전달: events.jsonl 에 새로 쌓이는 spawn/done/stalled/context_low/lost
  2. 정기 쿼터 리포트: 텍스트 게이지 + 권고 페이스 (기본 6시간마다, 시작 시 1회)
  3. ⚡소모 알림: 윈도우 사용량이 임계(50/80/95%)를 돌파하는 순간 1회

설정: 환경변수 OFFICE_DISCORD_WEBHOOK 또는 office/discord.json {"webhook_url": ...}
웹훅이 없으면 dry-run — 보낼 내용을 stdout에 출력한다 (형식 확인용).

실행:  python3 office/notify.py          # office.py 서버가 떠 있어야 한다
       python3 office/notify.py report   # 리포트 1회만 보내고 종료
"""
import json
import os
import sys
import time
import urllib.request
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from office import PORT, ROOT, load_json  # noqa: E402

EVENTS = os.path.join(ROOT, "events.jsonl")
POLL_SECS = float(os.environ.get("OFFICE_NOTIFY_POLL", "10"))
REPORT_HOURS = float(os.environ.get("OFFICE_REPORT_HOURS", "6"))
THRESHOLDS = sorted(int(x) for x in os.environ.get("OFFICE_ALERT_THRESHOLDS", "50,80,95").split(","))

E_ICON = {"spawn": "🟢", "done": "✅", "stalled": "🔴", "context_low": "🟡", "lost": "⚠️"}


def webhook_url():
    return (os.environ.get("OFFICE_DISCORD_WEBHOOK")
            or load_json("discord.json", {}).get("webhook_url"))


def post(text):
    url = webhook_url()
    if not url:
        print(f"[dry-run]\n{text}\n")
        return True
    data = json.dumps({"content": text[:1900], "username": "agent-office"}).encode()
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json", "User-Agent": "agent-office"})
    try:
        urllib.request.urlopen(req, timeout=10).read()
        return True
    except OSError as exc:
        print(f"웹훅 전송 실패: {exc}", file=sys.stderr)
        return False


def fetch_state():
    try:
        with urllib.request.urlopen(f"http://localhost:{PORT}/api/state", timeout=5) as res:
            return json.load(res)
    except OSError:
        return None


def gauge(used_pct, pace_pct, width=20):
    """서브PM 바 컨셉의 텍스트 게이지 — | 마커가 권고 위치."""
    used = max(0.0, min(100.0, used_pct or 0))
    cells = list("▓" * round(used / 100 * width) + "░" * (width - round(used / 100 * width)))
    if pace_pct is not None:
        cells.insert(min(width, max(0, round(pace_pct / 100 * width))), "|")
    return "".join(cells)


def quota_lines(state):
    lines = []
    for q in state.get("quota", []):
        used = q.get("used_pct", 0)
        pace = q.get("pace_pct")
        body = f'{q.get("name", "?"):<10} {used:>5.1f}%  {gauge(used, pace)}'
        if q.get("expired"):
            tail = "리셋 지남 — quota.json 갱신 필요"
        elif pace is None:
            tail = ""
        elif q.get("over_pct", 0) > 0:
            tail = f'권고 {pace}% ｜ 초과 +{q["over_pct"]}%p ⚠️'
        else:
            tail = f'권고 {pace}% ｜ 잔여권고 {q.get("headroom_pct", 0)}%'
        extra = ""
        if q.get("budget"):
            if (q.get("auto") or {}).get("metric", "cost") == "cost":
                extra = f' (${q.get("used_value", 0):.2f}/${q["budget"]:g})'
            else:
                extra = f' ({q.get("used_value", 0):.0f}/{q["budget"]:.0f} tok)'
        lines.append(body + "  " + tail + extra)
    return lines


def report(state):
    now = datetime.now().astimezone().strftime("%m-%d %H:%M")
    u = state.get("usage", {})
    parts = [f"📊 쿼터 리포트 ({now} 기준)"]
    ql = quota_lines(state)
    if ql:
        parts.append("```\n" + "\n".join(ql) + "\n```")
    else:
        parts.append("(quota.json 없음)")
    parts.append(f'오늘: ${u.get("cost", 0):.2f} · {u.get("calls", 0)}회 · '
                 f'출력 {u.get("output", 0) / 1e3:.0f}K tok')
    agents = state.get("agents", [])
    if agents:
        busy = sum(1 for a in agents if a["status"] == "busy")
        stalled = sum(1 for a in agents if a["status"] == "stalled")
        parts.append(f'워커 {len(agents)} (작업중 {busy}, 정체 {stalled})')
    return post("\n".join(parts))


def check_thresholds(state, alerted):
    for q in state.get("quota", []):
        name, used = q.get("name", "?"), q.get("used_pct", 0)
        prev = alerted.get(name, 0)
        crossed = [t for t in THRESHOLDS if used >= t]
        level = max(crossed, default=0)
        if level > prev:
            alerted[name] = level
            pace = q.get("pace_pct")
            tail = ""
            if pace is not None:
                tail = (f' — 권고 페이스 {pace}%, 초과 +{q["over_pct"]}%p' if q.get("over_pct", 0) > 0
                        else f' — 권고 페이스 {pace}% 이내')
            post(f"⚡ {name} 사용량 {level}% 돌파 (현재 {used}%){tail}")
        elif used < prev - 5:  # 윈도우가 리셋되어 내려가면 다시 알릴 수 있게
            alerted[name] = max([t for t in THRESHOLDS if used >= t], default=0)


def tail_events(offset):
    """events.jsonl 의 offset 이후 새 이벤트를 돌려준다. 파일이 줄면 처음부터."""
    try:
        size = os.path.getsize(EVENTS)
    except OSError:
        return [], 0
    if size < offset:
        offset = 0
    if size == offset:
        return [], offset
    with open(EVENTS, encoding="utf-8") as fh:
        fh.seek(offset)
        chunk = fh.read()
        new_offset = fh.tell()
    events = []
    for line in chunk.splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events, new_offset


def run_loop():
    mode = "웹훅" if webhook_url() else "dry-run"
    print(f"알림 봇 시작 ({mode}) — 리포트 {REPORT_HOURS}h 주기, 임계 {THRESHOLDS}")
    # 시작 시점 이전의 밀린 이벤트는 보내지 않는다
    offset = os.path.getsize(EVENTS) if os.path.exists(EVENTS) else 0
    alerted = {}
    last_report = 0.0
    while True:
        state = fetch_state()
        if state is None:
            print("office.py 서버에 연결 못 함 — 재시도", file=sys.stderr)
            time.sleep(POLL_SECS)
            continue
        events, offset = tail_events(offset)
        if events:
            post("\n".join(f'{E_ICON.get(e.get("type"), "•")} {e.get("msg", "")}' for e in events))
        check_thresholds(state, alerted)
        if time.time() - last_report >= REPORT_HOURS * 3600:
            if report(state):
                last_report = time.time()
        time.sleep(POLL_SECS)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "report":
        state = fetch_state()
        if state is None:
            print("office.py 서버가 떠 있어야 합니다", file=sys.stderr)
            sys.exit(1)
        sys.exit(0 if report(state) else 1)
    run_loop()


if __name__ == "__main__":
    main()
