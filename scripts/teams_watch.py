"""Teams 새 메시지 감시꾼. 남이 보낸 새 메시지가 캐시에 쌓이면 출력하고 종료한다.

백그라운드 Bash 로 띄우면 종료 = Claude 재호출이라, 미나가 먼저 말을 거는 계기가 된다.
메시지 내용은 파일로 남기지 않는다(state 에는 본 메시지 id 만).

  python -I scripts/teams_watch.py            # 새 메시지가 올 때까지 대기 후 출력·종료
  python -I scripts/teams_watch.py --once     # 지금 쌓인 새 메시지만 보고 바로 종료
  python -I scripts/teams_watch.py --since 2026-11-02T06:00:00Z --once   # 시각 이후 전부(점검용)
"""
import argparse
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import teams_cache  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "secretary"))
import config  # noqa: E402

STATE = config.state_file()
CHAT_TYPES = ("RichText/Html", "Text")
# threadProperties.isLastMessageFromMe 는 남의 메시지에도 True 로 남아 있어 못 쓴다. 표시 이름으로 거른다.
ME_NAME = config.me_name()  # env TEAMS_ME_NAME > config me.name


def conversations(path):
    out = []
    for _, v in teams_cache.log_puts(path):
        if b"lastMessage" not in v:
            continue
        o = teams_cache.decode_value(v)
        if isinstance(o, dict) and isinstance(o.get("lastMessage"), dict):
            out.append(o)
    return out


def from_me(cv):
    return str(cv["lastMessage"].get("imdisplayname", "")).strip() == ME_NAME


def chat_name(cv):
    title = cv.get("chatTitle")
    if isinstance(title, dict) and title.get("shortTitle"):
        return title["shortTitle"]
    return (cv.get("threadProperties") or {}).get("topic") or str(cv.get("id", ""))[:24]


def text_of(m):
    s = m.get("preview") if isinstance(m.get("preview"), str) else m.get("content") or ""
    s = re.sub(r"<[^>]+>", " ", str(s))
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def new_messages(path, seen, since):
    found = {}
    for cv in conversations(path):
        m = cv["lastMessage"]
        mid = str(m.get("id") or m.get("clientmessageid"))
        if mid in seen or m.get("messagetype") not in CHAT_TYPES:
            continue
        if from_me(cv) or str(cv.get("id", "")).startswith("48:"):
            continue
        if str(m.get("originalarrivaltime", "")) <= since:
            continue
        cid = str(cv.get("id", ""))
        text = text_of(m)
        name = str(m.get("imdisplayname") or "")
        if name and text.startswith(name + ":"):
            text = text[len(name) + 1:].strip()
        found[mid] = {
            "time": m.get("originalarrivaltime"),
            "from": m.get("imdisplayname"),
            "chat": chat_name(cv),
            "text": text[:300],
            "conversation": cid,
            "id": mid,
        }
    return sorted(found.values(), key=lambda x: x["time"])


def load_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(st):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    st["seen"] = st.get("seen", [])[-2000:]
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(st, f)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="기다리지 않고 지금 상태만 본다")
    ap.add_argument("--since", help="이 시각(ISO, UTC) 이후 전부. 지정하면 state 를 갱신하지 않는다")
    ap.add_argument("--interval", type=float, default=3.0)
    ap.add_argument("--timeout", type=float, default=0, help="초. 0 이면 무한 대기")
    a = ap.parse_args()

    st = load_state()
    seen = set(st.get("seen", []))
    since = a.since or st.get("since") or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    if not a.since and "since" not in st:
        st["since"] = since
        save_state(st)

    start, last = time.time(), None
    while True:
        log = teams_cache.latest_log()
        sig = (log, os.path.getsize(log)) if log else None
        if sig and sig != last:
            last = sig
            msgs = new_messages(log, seen, since)
            if msgs:
                for m in msgs:
                    print(json.dumps(m, ensure_ascii=False))
                if not a.since:
                    st["seen"] = st.get("seen", []) + [m["id"] for m in msgs]
                    save_state(st)
                return 0
        if a.once:
            print("새 메시지 없음")
            return 1
        if a.timeout and time.time() - start > a.timeout:
            print("TIMEOUT")
            return 2
        time.sleep(a.interval)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
