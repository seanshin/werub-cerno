#!/usr/bin/env bash
# 파일럿 데이터 볼륨 백업 (17 기획 P0-7) — HIS 서버 호스트 크론용.
#
# ask_log·review_log(SQLite)·metrics-daily.jsonl 이 named volume 의 단일 사본이다.
# 볼륨 유실 = 회고 원자료 전멸이므로 일 1회 tar 로 떠서 7일 롤링 보관한다.
# PHI 경계: 백업 대상은 이미 저장 승인된 데이터뿐이고 서버 밖으로 나가지 않는다.
#
# 설치(주간 평가 크론과 같은 crontab):
#   30 20 * * * /home/deploy/cerno/scripts/backup_data.sh >> /home/deploy/cerno/backup/cron.log 2>&1
# 복원 검증(분기 1회 권장):
#   tar -tzf backup/data-YYYYMMDD.tar.gz && sqlite3 <추출한 cerno.db> "SELECT COUNT(*) FROM review_log"
set -euo pipefail

BACKUP_DIR="${CERNO_BACKUP_DIR:-$HOME/cerno/backup}"
KEEP_DAYS=7
STAMP="$(date +%Y%m%d)"
OUT="$BACKUP_DIR/data-$STAMP.tar.gz"

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

# 컨테이너 안에서 tar — sqlite 는 WAL 이라 온라인 복사로도 일관 스냅샷에 충분히 가깝고,
# 야간(사용 없음) 실행으로 위험을 더 줄인다. 볼륨 직접 접근보다 권한 문제가 없다.
docker exec cerno-cerno-api-1 tar -czf - -C /app data > "$OUT.tmp"
mv "$OUT.tmp" "$OUT"
chmod 600 "$OUT"

# 7일 롤링
find "$BACKUP_DIR" -name 'data-*.tar.gz' -mtime +"$KEEP_DAYS" -delete

echo "$(date -u '+%F %T') backup ok: $OUT ($(du -h "$OUT" | cut -f1))"
