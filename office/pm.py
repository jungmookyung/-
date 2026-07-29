#!/usr/bin/env python3
"""Agent Office PM 봇 (Phase 2) — 태스크 큐를 읽어 워커를 출근시키고 상태를 관리한다.

상태 전이:  대기 → 작업중 → 검수대기 → 완료/중단
  - 대기:     큐에 들어온 상태. 빈 슬롯이 나면 PM이 tmux 워커를 띄운다
  - 작업중:   워커가 busy. 정체·컨텍스트 부족을 감시한다
  - 검수대기: busy였던 워커가 idle로 전환 = 일 끝내고 홀드 중. 오너 검수 필요
  - 완료/중단: 오너가 `pm.py done/kill <id>` 로 확정. 세션 정리

이벤트(spawn/done/stalled/context_low/lost)는 events.jsonl 에 기록되어
대시보드 알림 패널에 뜬다.

실행:
  python3 office/pm.py                          # PM 루프
  python3 office/pm.py add "제목" -p "프롬프트" -d ~/작업디렉토리
  python3 office/pm.py status                   # 큐 현황
  python3 office/pm.py done <id> | kill <id>    # 검수 확정 / 강제 중단
"""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from office import ROOT, capture, classify, load_json, sh  # noqa: E402

TASKS = os.path.join(ROOT, "tasks.json")
EVENTS = os.path.join(ROOT, "events.jsonl")
CLAUDE_BIN = os.environ.get("OFFICE_CLAUDE_BIN", "claude")
MAX_WORKERS = int(os.environ.get("OFFICE_MAX_WORKERS", "3"))
POLL_SECS = float(os.environ.get("OFFICE_PM_POLL", "5"))
CONTEXT_WARN_PCT = int(os.environ.get("OFFICE_CONTEXT_WARN_PCT", "20"))  # 잔여 20% = 소진 80%

import re  # noqa: E402

CONTEXT_RE = re.compile(r"context[^%\d]*?(\d{1,3})\s*%", re.IGNORECASE)


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def save_tasks(tasks):
    tmp = TASKS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(tasks, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, TASKS)  # 원자적 교체 — 대시보드가 찢어진 파일을 읽지 않게


def emit(task, etype, msg):
    event = {"ts": now_iso(), "task": task.get("id"), "worker": task.get("worker"),
             "type": etype, "msg": msg}
    with open(EVENTS, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    print(f'[{event["ts"]}] {etype}: {msg}')


def session_alive(name):
    return subprocess.run(["tmux", "has-session", "-t", name],
                          capture_output=True).returncode == 0


def spawn(task):
    session = f'agent-{task["id"]}'
    workdir = os.path.expanduser(task.get("dir", "~"))
    prompt = task.get("prompt") or task.get("title", "")
    sh(["tmux", "new-session", "-d", "-s", session, "-c", workdir, CLAUDE_BIN, prompt])
    if not session_alive(session):
        return None
    return session


def context_remaining(content):
    """하단 상태 영역에서 'Context ... N%' 를 찾아 잔여 %를 돌려준다. 없으면 None."""
    tail = [ln for ln in content.splitlines() if ln.strip()][-8:]
    for ln in reversed(tail):
        m = CONTEXT_RE.search(ln)
        if m:
            pct = int(m.group(1))
            if 0 <= pct <= 100:
                return pct
    return None


def tick(warned):
    tasks = load_json("tasks.json", [])
    changed = False

    for task in tasks:
        if task.get("status") != "작업중":
            continue
        session = task.get("worker")
        if not session or not session_alive(session):
            task["status"] = "중단"
            emit(task, "lost", f'{task["title"]} — 워커 세션이 사라짐')
            changed = True
            continue
        content = capture(session)
        status, _bubble, unchanged = classify(session, content)
        if status == "busy":
            if not task.get("worked"):
                task["worked"] = True
                changed = True  # 매 틱 디스크에서 다시 읽으므로 즉시 저장해야 한다
        elif status == "stalled" and warned.get((session, "stalled")) is None:
            warned[(session, "stalled")] = True
            emit(task, "stalled", f'{task["title"]} — 화면이 {unchanged // 60}분째 그대로')
        elif status == "idle" and task.get("worked"):
            task["status"] = "검수대기"
            emit(task, "done", f'{task["title"]} — 작업 끝, 검수 대기 (tmux attach -t {session})')
            changed = True
        remain = context_remaining(content)
        if remain is not None and remain <= CONTEXT_WARN_PCT and warned.get((session, "ctx")) is None:
            warned[(session, "ctx")] = True
            emit(task, "context_low", f'{task["title"]} — 컨텍스트 잔여 {remain}%, 마무리 지시 권장')

    running = sum(1 for t in tasks if t.get("status") == "작업중")
    for task in tasks:
        if running >= MAX_WORKERS:
            break
        if task.get("status") != "대기":
            continue
        session = spawn(task)
        if session:
            task.update(status="작업중", worker=session, worked=False)
            emit(task, "spawn", f'{task["title"]} — 출근 ({session})')
            running += 1
            changed = True
        else:
            emit(task, "lost", f'{task["title"]} — 워커 기동 실패 ({CLAUDE_BIN} 확인)')
            task["status"] = "중단"
            changed = True

    if changed:
        save_tasks(tasks)


def run_loop():
    print(f"PM 봇 시작 — 최대 워커 {MAX_WORKERS}, 폴링 {POLL_SECS}s, "
          f"컨텍스트 경보 잔여 {CONTEXT_WARN_PCT}%")
    warned = {}
    while True:
        try:
            tick(warned)
        except Exception as exc:  # 루프는 죽지 않는다
            print(f"tick 오류: {exc}", file=sys.stderr)
        time.sleep(POLL_SECS)


def next_id(tasks):
    nums = [int(t["id"][1:]) for t in tasks if re.fullmatch(r"t\d+", str(t.get("id", "")))]
    return f"t{max(nums, default=0) + 1}"


def cmd_add(args):
    tasks = load_json("tasks.json", [])
    task = {"id": next_id(tasks), "title": args.title, "prompt": args.prompt or args.title,
            "dir": args.dir, "owner": args.owner, "status": "대기", "worker": None}
    tasks.append(task)
    save_tasks(tasks)
    print(f'큐에 추가: {task["id"]} — {task["title"]}')


def cmd_finish(args, final_status):
    tasks = load_json("tasks.json", [])
    for task in tasks:
        if task.get("id") == args.id:
            session = task.get("worker")
            if session and session_alive(session):
                sh(["tmux", "kill-session", "-t", session])
            task["status"] = final_status
            emit(task, "done" if final_status == "완료" else "lost",
                 f'{task["title"]} — {final_status} 처리, 세션 정리')
            save_tasks(tasks)
            return
    print(f"태스크 없음: {args.id}", file=sys.stderr)
    sys.exit(1)


def cmd_status(_args):
    for t in load_json("tasks.json", []):
        print(f'{t.get("id", "?"):>4}  {t.get("status", "?"):　<4}  {t.get("title", "")}'
              + (f'  [{t["worker"]}]' if t.get("worker") else ""))


def main():
    ap = argparse.ArgumentParser(description="Agent Office PM 봇")
    sub = ap.add_subparsers(dest="cmd")
    p_add = sub.add_parser("add", help="태스크 추가")
    p_add.add_argument("title")
    p_add.add_argument("-p", "--prompt", default=None)
    p_add.add_argument("-d", "--dir", default="~")
    p_add.add_argument("-o", "--owner", default="나")
    for name in ("done", "kill"):
        p = sub.add_parser(name, help=f"태스크 {name}")
        p.add_argument("id")
    sub.add_parser("status", help="큐 현황")
    args = ap.parse_args()

    if args.cmd == "add":
        cmd_add(args)
    elif args.cmd == "done":
        cmd_finish(args, "완료")
    elif args.cmd == "kill":
        cmd_finish(args, "중단")
    elif args.cmd == "status":
        cmd_status(args)
    else:
        run_loop()


if __name__ == "__main__":
    main()
