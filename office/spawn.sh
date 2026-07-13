#!/usr/bin/env bash
# 워커 출근: tmux 세션을 만들어 claude를 띄운다.
# 사용법: ./office/spawn.sh <이름> <작업디렉토리> ["첫 프롬프트"]
set -euo pipefail

NAME="${1:?이름을 주세요 (예: quota-fix)}"
DIR="${2:?작업 디렉토리를 주세요}"
PROMPT="${3:-}"

SESSION="agent-${NAME}"
if tmux has-session -t "$SESSION" 2>/dev/null; then
  echo "이미 출근해 있음: $SESSION" >&2
  exit 1
fi

if [ -n "$PROMPT" ]; then
  tmux new-session -d -s "$SESSION" -c "$DIR" claude "$PROMPT"
else
  tmux new-session -d -s "$SESSION" -c "$DIR" claude
fi
echo "출근 완료: $SESSION ($DIR)"
echo "화면 보기: tmux attach -t $SESSION"
