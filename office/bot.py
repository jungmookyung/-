#!/usr/bin/env python3
"""Agent Office 양방향 Discord 봇 (Phase 4) — 게이트웨이 수신 + 채팅 명령.

의존성 없이(stdlib) Discord 게이트웨이 WebSocket에 직접 붙어서 채널 메시지를
받고, REST로 답장한다. 명령:

  !사무실                          현황 요약 (워커·태스크)
  !추가 제목 | 프롬프트 | 디렉토리   태스크 큐에 등록 (PM 루프가 자동 출근시킴)
  !완료 t3 / !중단 t3              검수 확정 / 강제 중단
  !리포트                          쿼터 게이지 리포트
  !도움말

설정 (office/discord.json):
  { "bot_token": "...", "channel_id": "명령 받을 채널 ID",
    "allowed_user_ids": ["..."] }        # 비우면 채널 내 전원 허용

봇 생성 시 MESSAGE CONTENT INTENT를 켜야 한다 (개발자 포털 → Bot → 토글).
office.py 서버가 떠 있어야 !사무실/!리포트가 동작한다.
"""
import base64
import hashlib
import json
import os
import socket
import ssl
import struct
import sys
import threading
import time
import urllib.request
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from office import PORT, load_json  # noqa: E402
from notify import fetch_state, report_text  # noqa: E402
from pm import add_task, finish_task  # noqa: E402

GATEWAY_URL = os.environ.get("OFFICE_DISCORD_GATEWAY",
                             "wss://gateway.discord.gg/?v=10&encoding=json")
API_BASE = os.environ.get("OFFICE_DISCORD_API", "https://discord.com/api/v10")
INTENTS = (1 << 9) | (1 << 15) | (1 << 12)  # GUILD_MESSAGES | MESSAGE_CONTENT | DM


# ---------- 최소 WebSocket 클라이언트 (RFC 6455) ----------

class WebSocket:
    def __init__(self, url):
        u = urlparse(url)
        secure = u.scheme == "wss"
        port = u.port or (443 if secure else 80)
        raw = socket.create_connection((u.hostname, port), timeout=30)
        if secure:
            raw = ssl.create_default_context().wrap_socket(raw, server_hostname=u.hostname)
        self.sock = raw
        self.send_lock = threading.Lock()
        self._buf = b""
        key = base64.b64encode(os.urandom(16)).decode()
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        self.sock.sendall((
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {u.hostname}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        ).encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("핸드셰이크 중 연결 끊김")
            head += chunk
        head, _, self._buf = head.partition(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise ConnectionError(f"업그레이드 거부: {head[:120]!r}")

    def _recv_exact(self, n):
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("연결 끊김")
            self._buf += chunk
        data, self._buf = self._buf[:n], self._buf[n:]
        return data

    def send_json(self, obj):
        data = json.dumps(obj).encode()
        header = bytes([0x81])  # FIN + text
        n = len(data)
        if n < 126:
            header += bytes([0x80 | n])
        elif n < 65536:
            header += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            header += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)  # 클라이언트 프레임은 마스킹 필수
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        with self.send_lock:
            self.sock.sendall(header + mask + masked)

    def recv_json(self):
        """다음 텍스트 메시지를 JSON으로. ping/pong은 내부 처리, close는 예외."""
        message = b""
        while True:
            b1, b2 = self._recv_exact(2)
            opcode, masked, length = b1 & 0x0F, b2 & 0x80, b2 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._recv_exact(8))[0]
            mask = self._recv_exact(4) if masked else None
            payload = self._recv_exact(length)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x8:  # close
                raise ConnectionError(f"서버가 연결 종료: {payload[:64]!r}")
            if opcode == 0x9:  # ping → pong
                with self.send_lock:
                    self.sock.sendall(bytes([0x8A, 0x80]) + os.urandom(4))
                continue
            if opcode == 0xA:  # pong
                continue
            message += payload
            if b1 & 0x80:  # FIN
                return json.loads(message.decode())

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


# ---------- Discord REST ----------

def rest_reply(token, channel_id, text):
    req = urllib.request.Request(
        f"{API_BASE}/channels/{channel_id}/messages",
        data=json.dumps({"content": text[:1900]}).encode(),
        headers={"Authorization": f"Bot {token}", "Content-Type": "application/json",
                 "User-Agent": "agent-office (https://localhost, 1.0)"})
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except OSError as exc:
        print(f"답장 실패: {exc}", file=sys.stderr)


# ---------- 명령 처리 ----------

HELP = """명령어:
`!사무실` 현황 요약 · `!리포트` 쿼터 게이지
`!추가 제목 | 프롬프트 | 디렉토리` 태스크 등록
`!완료 t3` 검수 확정 · `!중단 t3` 강제 중단"""

S_ICON = {"busy": "🟢", "idle": "🟡", "stalled": "🔴", "unknown": "⚪"}


def office_summary():
    state = fetch_state()
    if state is None:
        return f"office.py 서버(:{PORT})에 연결할 수 없어요 — 대시보드부터 켜주세요."
    lines = []
    agents = state.get("agents", [])
    if agents:
        lines.append(f"워커 {len(agents)}명:")
        lines += [f'{S_ICON.get(a["status"], "•")} {a["name"]} — {a.get("bubble", "")[:40]}'
                  for a in agents]
    else:
        lines.append("출근한 워커 없음")
    tasks = [t for t in state.get("tasks", []) if t.get("status") not in ("완료", "중단")]
    if tasks:
        lines.append("태스크:")
        lines += [f'· [{t.get("id")}] {t.get("status")} — {t.get("title")}' for t in tasks]
    return "\n".join(lines)


def handle_command(content):
    """명령 문자열 → 응답 텍스트 (없으면 None)."""
    text = content.strip()
    if not text.startswith("!"):
        return None
    cmd, _, rest = text[1:].partition(" ")
    rest = rest.strip()
    if cmd in ("도움말", "help"):
        return HELP
    if cmd in ("사무실", "status"):
        return office_summary()
    if cmd in ("리포트", "report"):
        state = fetch_state()
        return report_text(state) if state else "office.py 서버가 꺼져 있어요."
    if cmd in ("추가", "add"):
        if not rest:
            return "사용법: `!추가 제목 | 프롬프트 | 디렉토리` (프롬프트·디렉토리는 생략 가능)"
        parts = [p.strip() for p in rest.split("|")]
        title = parts[0]
        prompt = parts[1] if len(parts) > 1 and parts[1] else None
        workdir = parts[2] if len(parts) > 2 and parts[2] else "~"
        task = add_task(title, prompt, workdir, owner="discord")
        return f'📥 큐에 추가: [{task["id"]}] {title} — PM이 슬롯 나는 대로 출근시켜요.'
    if cmd in ("완료", "done", "중단", "kill"):
        if not rest:
            return f"사용법: `!{cmd} t3`"
        final = "완료" if cmd in ("완료", "done") else "중단"
        task = finish_task(rest, final)
        return (f'{"✅" if final == "완료" else "🛑"} [{rest}] {task["title"]} — {final} 처리'
                if task else f"태스크를 못 찾았어요: {rest}")
    return None  # 모르는 명령은 조용히 무시


# ---------- 게이트웨이 루프 ----------

def heartbeat_loop(ws, interval_ms, seq_ref, dead):
    while not dead.is_set():
        time.sleep(interval_ms / 1000)
        try:
            ws.send_json({"op": 1, "d": seq_ref[0]})
        except OSError:
            return


def run_gateway(cfg):
    token = cfg["bot_token"]
    channel_id = str(cfg.get("channel_id", "") or "")
    allowed = {str(u) for u in cfg.get("allowed_user_ids", [])}

    ws = WebSocket(GATEWAY_URL)
    dead = threading.Event()
    seq_ref = [None]
    try:
        hello = ws.recv_json()
        if hello.get("op") != 10:
            raise ConnectionError(f"HELLO 대신 op {hello.get('op')}")
        interval = hello["d"]["heartbeat_interval"]
        threading.Thread(target=heartbeat_loop, args=(ws, interval, seq_ref, dead),
                         daemon=True).start()
        ws.send_json({"op": 2, "d": {
            "token": token, "intents": INTENTS,
            "properties": {"os": "linux", "browser": "agent-office", "device": "agent-office"}}})
        while True:
            msg = ws.recv_json()
            if msg.get("s") is not None:
                seq_ref[0] = msg["s"]
            op = msg.get("op")
            if op == 1:  # 서버가 하트비트 요청
                ws.send_json({"op": 1, "d": seq_ref[0]})
            elif op in (7, 9):  # reconnect / invalid session
                raise ConnectionError(f"게이트웨이 재접속 요청 (op {op})")
            elif op == 0 and msg.get("t") == "READY":
                print(f'게이트웨이 연결됨 — 봇: {msg["d"].get("user", {}).get("username", "?")}')
            elif op == 0 and msg.get("t") == "MESSAGE_CREATE":
                d = msg["d"]
                if d.get("author", {}).get("bot"):
                    continue
                if channel_id and str(d.get("channel_id")) != channel_id:
                    continue
                if allowed and str(d.get("author", {}).get("id")) not in allowed:
                    continue
                reply = handle_command(d.get("content", ""))
                if reply:
                    rest_reply(token, d["channel_id"], reply)
    finally:
        dead.set()
        ws.close()


def main():
    cfg = load_json("discord.json", {})
    if os.environ.get("OFFICE_DISCORD_TOKEN"):
        cfg["bot_token"] = os.environ["OFFICE_DISCORD_TOKEN"]
    if not cfg.get("bot_token"):
        print("discord.json에 bot_token이 필요합니다 (discord.json.example 참고)", file=sys.stderr)
        sys.exit(1)
    backoff = 5
    while True:
        try:
            run_gateway(cfg)
        except (OSError, ConnectionError, json.JSONDecodeError) as exc:
            print(f"게이트웨이 끊김: {exc} — {backoff}s 후 재접속", file=sys.stderr)
            time.sleep(backoff)
            backoff = min(backoff * 2, 120)
        else:
            backoff = 5


if __name__ == "__main__":
    main()
