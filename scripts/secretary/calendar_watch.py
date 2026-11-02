"""캘린더: 미나가 커넥터로 받은 일정을 파일로 두고, 감시꾼이 회의 1시간 전에 자료 조사로 깨우고 15분 전에 알린다.

  python -I scripts/secretary/calendar_watch.py set < events.json   # 미나가 일정 저장
  python -I scripts/secretary/calendar_watch.py brief <id> < brief.txt   # 미리 만든 브리핑 저장
  events.json = [{"id", "title", "start": ISO(시간대 포함), "link"?, "attendees"?}]
"""
import json
import os
import sys
import time
from datetime import datetime

FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "artifacts", "secretary", "calendar.json")
REFRESH_EVERY = 2 * 3600
PREP = 60 * 60  # 자료 조사 시작
LEAD = 15 * 60  # 알림
WORK_HOURS = range(8, 20)


def load():
    try:
        with open(FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"refreshed": 0, "events": []}


def need_refresh(cal):
    now = time.time()
    return datetime.now().hour in WORK_HOURS and now - cal.get("refreshed", 0) > REFRESH_EVERY


def due(cal, reminded, lead=LEAD):
    """lead 초 안에 시작하는(이미 1분 넘게 지난 건 제외) 아직 안 알린 일정."""
    now = time.time()
    out = []
    for e in cal.get("events", []):
        try:
            start = datetime.fromisoformat(e["start"].replace("Z", "+00:00")).timestamp()
        except (KeyError, ValueError):
            continue
        if -60 < start - now <= lead and e["id"] not in reminded:
            out.append(dict(e, minutes=max(0, round((start - now) / 60))))
    return out


def save(events):
    os.makedirs(os.path.dirname(FILE), exist_ok=True)
    with open(FILE, "w", encoding="utf-8") as f:
        json.dump({"refreshed": time.time(), "events": events}, f, ensure_ascii=False)


def set_brief(eid, text):
    cal = load()
    for e in cal["events"]:
        if e["id"] == eid:
            e["brief"] = text
    with open(FILE, "w", encoding="utf-8") as f:
        json.dump(cal, f, ensure_ascii=False)


if __name__ == "__main__":
    sys.stdin.reconfigure(encoding="utf-8")
    if sys.argv[1:] == ["set"]:
        old = {e["id"]: e.get("brief") for e in load()["events"]}
        events = json.load(sys.stdin)
        for e in events:  # 새로고침해도 미리 만든 브리핑은 유지
            if old.get(e["id"]) and not e.get("brief"):
                e["brief"] = old[e["id"]]
        save(events)
        print("saved", len(events))
    elif sys.argv[1:2] == ["brief"]:  # python calendar_watch.py brief <id> < brief.txt
        set_brief(sys.argv[2], sys.stdin.read().strip())
        print("brief saved")
