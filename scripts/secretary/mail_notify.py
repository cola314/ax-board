"""Gmail 새 메일을 윈도우 알림 DB 에서 읽는다(토큰·권한 불필요).

크롬 Gmail 데스크톱 알림이 켜져 있어야 한다(config gmail.chrome_profile 의 전용 프로필, Gmail 창 열어 둠).
알림 한 건 = Chrome 토스트, launch 속성에 mail.google.com, <text> 가 [보낸 사람, 제목·요약, 출처].
"""
import os
import re
import sqlite3
import time

import config

DB = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Windows\Notifications\wpndatabase.db")
TEXT = re.compile(r"<text(?![^>]*placement=)[^>]*>(.*?)</text>", re.S)


GMAIL_TITLE = re.compile(config.get("gmail.window_title_regex") or r"(?!)")  # 비어 있으면 아무 창도 안 맞음
CHROME = config.get("gmail.chrome_path")
PROFILE = config.get("gmail.chrome_profile")
GMAIL_URL = config.get("gmail.url")


def gmail_open():
    """Gmail 창(제목이 config gmail.window_title_regex 에 맞는)이 하나라도 떠 있나."""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    found = []

    def cb(hwnd, _):
        n = user32.GetWindowTextLengthW(hwnd)
        if n and user32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if GMAIL_TITLE.search(buf.value):
                found.append(buf.value)
        return True

    user32.EnumWindows(ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(cb), 0)
    return bool(found)


def ensure_gmail():
    """Gmail 창이 없으면 전용 프로필 앱 창으로 띄운다. 띄웠으면 True."""
    if gmail_open():
        return False
    import subprocess
    subprocess.Popen([CHROME, "--profile-directory=" + PROFILE, "--app=" + GMAIL_URL],
                     creationflags=0x00000008 | 0x00000200)  # DETACHED_PROCESS | NEW_PROCESS_GROUP
    return True


def now_filetime():
    return int((time.time() + 11644473600) * 10_000_000)


def signature():
    """DB·WAL 수정 시각. 바뀌었을 때만 조회한다."""
    sig = []
    for p in (DB, DB + "-wal"):
        try:
            sig.append(os.path.getmtime(p))
        except OSError:
            sig.append(0)
    return tuple(sig)


def new_mails(since):
    """since(FILETIME) 이후 Gmail 알림 목록과 가장 늦은 시각."""
    con = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True, timeout=2)
    try:
        rows = con.execute(
            "select n.Id, n.ArrivalTime, n.Payload from Notification n "
            "join NotificationHandler h on n.HandlerId = h.RecordId "
            "where h.PrimaryId = 'Chrome' and n.ArrivalTime > ? order by n.ArrivalTime", (since,)).fetchall()
    finally:
        con.close()
    out, latest = [], since
    for nid, t, p in rows:
        s = p.decode("utf-8", "ignore") if isinstance(p, bytes) else str(p)
        latest = max(latest, t)
        if "mail.google.com" not in s:
            continue
        texts = [re.sub(r"\s+", " ", x).strip() for x in TEXT.findall(s)]
        out.append({"id": "mail-%s" % nid, "from": texts[0] if texts else "",
                    "text": " / ".join(texts[1:])[:300]})
    return out, latest
