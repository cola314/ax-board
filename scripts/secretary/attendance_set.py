"""flex_week.js 결과(JSON)를 stdin 으로 받아 이번 주 출퇴근 기록으로 저장한다.

"몇 시간 남았어?" 에 이 파일 + 지금 시각으로 바로 답한다(계산 규칙은 /attendance 스킬).
"""
import json
import os
import sys
import time

FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "artifacts", "secretary", "attendance.json")

if __name__ == "__main__":
    sys.stdin.reconfigure(encoding="utf-8")
    days = json.loads(sys.stdin.read())
    os.makedirs(os.path.dirname(FILE), exist_ok=True)
    with open(FILE, "w", encoding="utf-8") as f:
        json.dump({"fetched": time.strftime("%Y-%m-%d %H:%M"), "days": days}, f, ensure_ascii=False)
    print("saved", len(days))
