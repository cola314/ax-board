"""정각 문서용 지난 N분 소식 통계(메일·GitLab·Teams 1:1·그룹방 건수). 소식판 저장본(chat.json)만 읽는다.
  python -I scripts/secretary/hourly_stats.py          # 지난 60분
  python -I scripts/secretary/hourly_stats.py 120      # 지난 120분
"""
import json
import os
import re
import sys
from datetime import datetime, timedelta

CHAT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "artifacts", "secretary", "chat.json")  # server.py 와 같은 파일
GITLAB_MAIL = re.compile(r"^(Re: )?[\w./-]+ \| ")


def recent(items, minutes, now):
    """items 는 시간순, ts 는 'HH:MM:SS'(날짜 없음). 끝에서부터 거꾸로 보며 창 밖이거나 날이 바뀌면(ts 가 커짐) 멈춘다."""
    start, prev, out = (now - timedelta(minutes=minutes)).strftime("%H:%M:%S"), "99", []
    for it in reversed(items):
        ts = it.get("ts") or ""
        if ts > prev or ts < start:
            break
        prev = ts
        out.append(it)
    return out


def stats(items):
    c = {"mail": 0, "gitlab_mail": 0, "gitlab": 0, "dm": 0, "dm_talk": 0, "group": 0}
    for it in items:
        if it.get("kind") != "teams":
            continue
        frm, chat = it.get("from") or "", it.get("chat") or ""
        if frm.startswith("✉️"):
            c["gitlab_mail" if GITLAB_MAIL.match(it.get("text") or "") else "mail"] += 1
        elif frm.startswith("🦊"):
            c["gitlab"] += 1
        elif chat == frm:  # 1:1 방은 방 이름이 상대 이름. 내가 직접 대화 중이라 무시(skip)된 건 따로
            c["dm_talk" if it.get("state") == "skip" else "dm"] += 1
        else:
            c["group"] += 1
    return c


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    minutes = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    with open(CHAT, encoding="utf-8") as f:
        items = json.load(f).get("items", [])
    c = stats(recent(items, minutes, datetime.now()))
    print("메일 %(mail)d · GitLab %(gitlab)d(알림 메일 %(gitlab_mail)d 제외) · Teams 1:1 %(dm)d(직접 대화 %(dm_talk)d 제외) · 그룹방 %(group)d" % c)
