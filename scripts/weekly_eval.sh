#!/bin/bash
# Cerno 주간 골든셋 회귀 (13_파일럿_운영계획.md §5 — 주 1회 N≥10).
# HIS 서버 크론에서 실행: 실패·개인자료 누출 시 exit 1 + ALERT.log 기록.
# 설치: crontab -e →  0 21 * * 0  /home/deploy/cerno/scripts/weekly_eval.sh
#      (UTC 일 21:00 = KST 월 06:00, 주간 회고 전)
set -u
OUT=/home/deploy/cerno/eval-weekly
mkdir -p "$OUT"
STAMP=$(date +%Y%m%d-%H%M)

docker cp /home/deploy/cerno/eval cerno-cerno-api-1:/tmp/eval >/dev/null 2>&1
# docker cp 는 root 소유로 떨어진다 — 비루트 컨테이너가 out/ 을 만들 수 있게
docker exec -u root cerno-cerno-api-1 chown -R cerno /tmp/eval >/dev/null 2>&1
docker exec -w /app -e PYTHONPATH=/app cerno-cerno-api-1 \
  python /tmp/eval/harness.py --n 10 > "$OUT/$STAMP.log" 2>&1
RC=$?
docker cp cerno-cerno-api-1:/tmp/eval/out/last_run.json "$OUT/$STAMP.json" >/dev/null 2>&1

echo "$STAMP rc=$RC" >> "$OUT/summary.log"
if [ "$RC" -ne 0 ]; then
  # 회고 때 가장 먼저 보는 파일 — 존재 자체가 경보다
  echo "$STAMP 골든셋 회귀 실패(rc=$RC) — $OUT/$STAMP.log 확인" >> "$OUT/ALERT.log"
fi
exit "$RC"
