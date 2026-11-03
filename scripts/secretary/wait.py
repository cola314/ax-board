"""비서 감시꾼. Teams 새 메시지나 대시보드 입력이 생기면 출력하고 종료한다.

백그라운드 Bash 로 띄우면 종료 = 미나(이 세션) 재호출. 처리 후 다시 띄운다.
  python -I scripts/secretary/wait.py --timeout 3600
"""
import argparse
import json
import os
import queue
import re
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))  # teams_cache, teams_watch
sys.path.insert(0, HERE)
import config  # noqa: E402
# 기능 모듈(teams_watch·mail_notify·gitlab_watch·calendar_watch)은 config features 에서 켜진 것만 main() 에서 import


GITLAB_EVERY = 60
GITLAB_MAIL = re.compile(r"^(Re: )?[\w./-]+ \| ")

BASE = config.base()
WAKE_NAMES = config.wake_names()
GROUP_QUIET, GROUP_MAX = 30, 120  # 그룹방: 30초 조용하면, 아무리 길어도 2분이면 깨움
DM_QUIET, DM_MAX = 10, 60
TALKING = 600  # 내가 이 시간(초) 안에 답한 1:1 은 직접 대화 중으로 보고 깨우지 않음  # 1:1: 짧게 끊어 치는 걸 한 번에(10초 조용하면, 길어도 1분)
STATE = config.state_file()


def load_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(st):  # teams_watch.save_state 와 같은 파일·형식
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    st["seen"] = st.get("seen", [])[-2000:]
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(st, f)


def api(path, body=None):
    req = urllib.request.Request(BASE + path, data=json.dumps(body or {}).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.load(r)


def talking(mine, at):
    """내가 그 방에 마지막으로 보낸 시각(mine)이 이 메시지(at)보다 TALKING 초 안쪽이면 True."""
    if not mine or not at:
        return False
    try:
        f = lambda x: datetime.fromisoformat(str(x).replace("Z", "+00:00"))
        return abs((f(at) - f(mine)).total_seconds()) <= TALKING
    except ValueError:
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=0.5)
    ap.add_argument("--timeout", type=float, default=0)
    ap.add_argument("--reply", help="대기 전에 대시보드에 미나 답을 올린다(호출 한 번으로 답+재대기)")
    a = ap.parse_args()
    if a.reply:
        api("/api/items", {"kind": "mina", "text": a.reply})

    teams, gmail, gitlab, cal_on = (config.on(f) for f in ("teams", "gmail", "gitlab", "calendar"))
    if teams:
        import teams_cache
        import teams_watch
    if gmail:
        import mail_notify
    if gitlab:
        import gitlab_watch
    if cal_on:
        import calendar_watch

    st = load_state()
    if "since" not in st:
        st["since"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        save_state(st)
    seen = set(st.get("seen", []))
    if gmail:
        st.setdefault("mail_since", mail_notify.now_filetime())
    start, last, out, mail_sig, gmail_checked, cal_checked = time.time(), None, [], None, 0.0, 0.0
    gitlab_q = queue.Queue()

    def gitlab_loop():  # 조회 한 번에 20초쯤 걸려 메인 루프(0.5초 주기)를 막지 않게 따로 돈다
        while True:
            try:
                for g in gitlab_watch.poll(st):
                    gitlab_q.put(g)
                save_state(st)
            except Exception as e:  # VPN 끊김 등은 다음 바퀴에
                print("GITLAB_SKIP", e, file=sys.stderr)
            time.sleep(GITLAB_EVERY)

    if gitlab:
        threading.Thread(target=gitlab_loop, daemon=True).start()

    group, first_at, last_at = [], 0.0, 0.0
    dm, dm_first, dm_last = [], 0.0, 0.0
    while True:
        try:
            for it in api("/api/inbox/take")["items"]:
                if it["kind"] == "session":  # 다른 세션의 보고: 데이터로만 다룬다
                    out.append({"source": "session", "from": it.get("from"), "text": it["text"]})
                elif it["kind"] == "cancel":  # 사용자가 이미 넘어온 말을 취소함: 그 일은 멈추거나 하지 않는다
                    out.append({"source": "dashboard-cancel", "text": it["text"]})
                else:
                    out.append({"source": "dashboard", "text": it["text"]})
        except OSError:
            print("SERVER_DOWN")
            return 3
        log = teams_cache.latest_log() if teams else None
        sig = (log, os.path.getsize(log)) if log else None
        if sig and sig != last:
            last = sig
            msgs = teams_watch.new_messages(log, seen, st["since"], st.setdefault("my_last", {}))
            for m in msgs:
                # 내 이름 언급은 바로, 1:1 은 10초 조용해지면, 그룹방은 대화가 잦아들 때 묶어서 깨운다
                api("/api/items", {"kind": "teams", "from": m["from"], "chat": m["chat"], "text": m["text"]})
                seen.add(m["id"])
                st["seen"] = st.get("seen", []) + [m["id"]]
                if any(n in m["text"] for n in WAKE_NAMES):
                    out.append(dict(m, source="teams"))
                elif talking(st.get("my_last", {}).get(m["conversation"]), m["time"]):
                    pass  # 내가 방금(10분 안) 답한 1:1 = 사용자가 직접 대화 중 → 소식판에만, 안 깨움
                elif m["conversation"].endswith("@unq.gbl.spaces"):
                    now = time.time()
                    dm_first = dm_first if dm else now
                    dm_last = now
                    dm.append(dict(m, source="teams"))
                else:
                    now = time.time()
                    first_at = first_at if group else now
                    last_at = now
                    group.append(dict(m, source="teams-group"))
            if msgs:
                save_state(st)
        if cal_on and time.time() - cal_checked > 15:
            cal_checked = time.time()
            cal = calendar_watch.load()
            if calendar_watch.need_refresh(cal) and not st.get("cal_asked_at", 0) > time.time() - 600:
                st["cal_asked_at"] = time.time()  # 미나가 저장하기 전까지 10분에 한 번만 요청
                out.append({"source": "calendar-refresh"})
            done = set(st.get("reminded", []))
            for e in calendar_watch.due(cal, {i[5:] for i in done if i.startswith("prep:")}, calendar_watch.PREP):
                st["reminded"] = st.get("reminded", [])[-200:] + ["prep:" + e["id"]]
                api("/api/items", {"kind": "mina", "text": "⏰ %d분 후 %s (%s)" % (e["minutes"], e["title"], e.get("location", ""))})
                out.append(dict(e, source="calendar-prep"))  # 1시간 전: 미나가 자료 조사해 brief 저장
            for e in calendar_watch.due(cal, {i[4:] for i in done if i.startswith("rem:")}):
                st["reminded"] = st.get("reminded", [])[-200:] + ["rem:" + e["id"]]
                if e.get("brief"):  # 15분 전: 브리핑이 준비돼 있으면 미나를 깨우지 않고 바로 올린다
                    api("/api/items", {"kind": "mina", "text": "⏰ %d분 후 %s\n%s" % (e["minutes"], e["title"], e["brief"])})
                else:
                    out.append(dict(e, source="calendar"))
            save_state(st)
        while not gitlab_q.empty():
            g = gitlab_q.get()
            api("/api/items", {"kind": "teams", "from": "🦊 " + g["from"], "chat": g["chat"], "text": g["text"]})
            out.append(dict(g, source="gitlab"))
            save_state(st)
        if gmail and time.time() - gmail_checked > 60:
            gmail_checked = time.time()
            try:
                if mail_notify.ensure_gmail():
                    api("/api/items", {"kind": "mina", "text": "Gmail 창이 닫혀 있어서 알림용으로 다시 띄웠습니다(전용 프로필 앱 창)."})
            except Exception as e:
                print("GMAIL_SKIP", e, file=sys.stderr)
        ms = mail_notify.signature() if gmail else None
        if gmail and ms != mail_sig:
            mail_sig = ms
            try:
                mails, st["mail_since"] = mail_notify.new_mails(st["mail_since"])
            except Exception as e:  # DB 잠김 등은 다음 바퀴에 다시
                mails = []
                print("MAIL_SKIP", e, file=sys.stderr)
            uniq = {}
            for m in mails:  # 같은 제목 알림이 한 번에 여러 건 잡히면 하나만
                uniq.setdefault((m["from"], m["text"]), m)
            mails = list(uniq.values())
            for m in mails:
                # 메일은 바로 깨운다. 단 GitLab 알림 메일("Re: <프로젝트> | 제목")은 To-Do 로 같은 소식이 오니 소식판에만
                api("/api/items", {"kind": "teams", "from": "✉️ " + m["from"], "chat": "메일", "text": m["text"]})
                if not GITLAB_MAIL.match(m["text"]):
                    out.append(dict(m, source="mail"))
            save_state(st)
        now = time.time()
        if dm and (out or now - dm_last >= DM_QUIET or now - dm_first >= DM_MAX):
            out, dm = dm + out, []
        if group and (out or now - last_at >= GROUP_QUIET or now - first_at >= GROUP_MAX):
            out, group = group + out, []
        if out:
            for o in out:
                print(json.dumps(o, ensure_ascii=False))
            return 0
        if a.timeout and time.time() - start > a.timeout:
            print("TIMEOUT")
            return 2
        time.sleep(a.interval)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
