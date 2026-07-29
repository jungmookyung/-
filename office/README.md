# Agent Office — 나만의 픽셀 에이전트 사무실 (MVP)

tmux에서 돌아가는 Claude Code / Codex 워커들을 감시해서 픽셀 사무실 대시보드로
보여주는 시스템. 스크린샷으로 본 "Agent Office"를 역설계해 최소 구성으로 만든 1단계.

## 실행

```bash
python3 office/office.py
# → http://localhost:8765
```

의존성 없음 (Python 3.9+ 표준 라이브러리만). tmux가 없거나 워커가 없으면 빈 사무실이 뜬다.

## 워커 출근시키기

tmux 세션에서 claude를 띄우면 자동으로 감지되어 사무실에 나타난다:

```bash
./office/spawn.sh 워커이름 ~/작업디렉토리 "시킬 일"
# 예: ./office/spawn.sh quota-fix ~/proj/bot "cbot-quota에 권고 페이스 게이지 추가해줘"
```

또는 그냥 아무 tmux 창에서 `claude`를 직접 실행해도 된다.

## 상태 판정 (와처)

| 상태 | 조건 |
|------|------|
| 🟢 작업중 | 화면 **하단 8줄**에 "esc to interrupt" 등 busy 마커 |
| 🟡 대기 | 하단 8줄에 입력 프롬프트 마커 (검수 대기 홀드 등) |
| 🔴 정체 | busy인데 화면이 10분(기본) 이상 그대로 |
| ⚪ 불명 | 어느 쪽도 아님 |

busy 판정을 하단 상태바 영역으로 한정한 이유: 워커가 보고문 **본문에**
"esc to interrupt" 같은 문구를 인용하면 화면 전체 매칭은 오탐한다
(원조 시스템에서 실제로 있었던 버그).

## 오늘 작업량

`~/.claude/projects/**/*.jsonl` 트랜스크립트에서 오늘치 usage를 합산해
호출 수·입출력/캐시 토큰·추정 비용을 보여준다. 비용은 모델별 단가표 기반 추정치.

## 쿼터 권고 페이스 게이지

`office/quota.json`을 만들면 게이지가 뜬다 (예시: `quota.json.example`):

```json
[
  { "name": "Claude 주간", "used_pct": 39, "resets_at": "2026-07-15T23:00:00+09:00", "window_days": 7 },
  { "name": "Codex 주간",  "used_pct": 12, "resets_at": "2026-07-18T15:02:00+09:00", "window_days": 7 }
]
```

권고 페이스 = 윈도우 경과시간 비율. 바에 `|` 마커로 권고 위치를 표시하고,
아래에 `권고 페이스 50% ｜ 잔여권고 12.0%` 또는 초과 시 `초과 +X.X%p`를 보여준다.
`used_pct`는 당분간 수동 갱신 (자동화는 로드맵 참고). 5시간 창은 `window_hours: 5`로.

## 태스크보드

`office/tasks.json` (예시: `tasks.json.example`):

```json
[
  { "title": "오너 포트폴리오 사이트 P3 구현", "status": "대기", "owner": "SubPM j20iyr" }
]
```

## PM 봇 (Phase 2)

태스크 큐를 읽어 워커를 자동으로 출근시키고 상태를 관리한다:

```bash
python3 office/pm.py add "제목" -p "워커에게 줄 프롬프트" -d ~/작업디렉토리
python3 office/pm.py            # PM 루프 시작 (office.py와 별도 프로세스)
python3 office/pm.py status     # 큐 현황
python3 office/pm.py done t1    # 검수 통과 → 완료 처리 + 세션 정리
python3 office/pm.py kill t1    # 강제 중단
```

상태 전이: `대기 → 작업중 → 검수대기 → 완료/중단`

- 빈 슬롯(기본 3)이 나면 대기 태스크를 `tmux` 세션으로 출근시킨다
- busy였던 워커가 idle이 되면 **검수대기**로 전환하고 알림 — 검수는 오너 몫
- **조기 경보**: 화면이 오래 그대로면 정체 알림, 상태바에서 `Context ... N%`를
  읽어 잔여 20% 이하면 "마무리 지시 권장" 알림 (1bav34처럼 100% 소진 후에야
  아는 상황 방지)
- 이벤트는 `events.jsonl`에 쌓이고 대시보드 알림 패널에 뜬다

## 환경변수

| 변수 | 기본 | 설명 |
|------|------|------|
| `OFFICE_PORT` | 8765 | 대시보드 포트 |
| `OFFICE_POLL` | 5 | tmux 폴링 주기(초) |
| `OFFICE_STALL_SECS` | 600 | 정체 판정 기준(초) |
| `OFFICE_MAX_WORKERS` | 3 | PM 봇 동시 워커 수 |
| `OFFICE_CLAUDE_BIN` | claude | 워커 실행 바이너리 (테스트 더미 대체용) |
| `OFFICE_CONTEXT_WARN_PCT` | 20 | 컨텍스트 잔여 경보 기준(%) |

## 로드맵

- **Phase 1 (완료)**: 와처 + 픽셀 대시보드
- **Phase 2 (완료)**: PM 봇 — 태스크 큐 → 워커 자동 출근, 검수대기 전환,
  정체·컨텍스트 잔여 20% 조기 경보
- **Phase 3**: 채널 연동 — Discord 봇으로 보고/명령, 정기 쿼터 리포트(cbot-quota)와
  소모 알림(quota-alert) 포팅, quota.json 자동 갱신
