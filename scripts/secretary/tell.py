"""다른 Claude 세션에서 미나 비서에게 보고를 보낸다(/tell-mina 스킬).
  python -I scripts/secretary/tell.py "<보내는 세션 이름>" "<내용>"
"""
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402

BASE = config.base()

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    if len(sys.argv) < 3:
        sys.exit("usage: tell.py <from> <text>")
    body = json.dumps({"from": sys.argv[1], "text": " ".join(sys.argv[2:])}).encode("utf-8")
    req = urllib.request.Request(BASE + "/api/notify", data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        urllib.request.urlopen(req, timeout=5).read()
        print("비서에게 전달함")
    except OSError:
        print("비서 모드가 꺼져 있음(localhost:%d 응답 없음) — 전달 못 함" % config.port())
        sys.exit(1)
