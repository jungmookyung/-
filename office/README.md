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

## 환경변수

| 변수 | 기본 | 설명 |
|------|------|------|
| `OFFICE_PORT` | 8765 | 대시보드 포트 |
| `OFFICE_POLL` | 5 | tmux 폴링 주기(초) |
| `OFFICE_STALL_SECS` | 600 | 정체 판정 기준(초) |

## 로드맵

- **Phase 1 (지금)**: 와처 + 픽셀 대시보드 — 보이는 것부터
- **Phase 2**: PM 봇 — 태스크 큐를 읽어 `spawn.sh`로 워커를 띄우고, 완료 감지 시
  검수 대기 전환·결과 수집. 정체/컨텍스트 80% 시점 조기 경보
- **Phase 3**: 채널 연동 — Discord 봇으로 보고/명령, 정기 쿼터 리포트(cbot-quota)와
  소모 알림(quota-alert) 포팅, quota.json 자동 갱신
