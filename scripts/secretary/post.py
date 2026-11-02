"""미나 답을 대시보드에 카드로 올린다. 인자가 없으면 stdin 을 읽는다.
  python -I scripts/secretary/post.py "답장 초안: ..."
  python -I scripts/secretary/post.py --mgit "http://127.0.0.1:41172/?c=abc123" "리뷰 준비 끝났습니다"   # 카드에 [mgit 으로 리뷰] 버튼
"""
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402

BASE = config.base()

if __name__ == "__main__":
    sys.stdin.reconfigure(encoding="utf-8")
    args = sys.argv[1:]
    body = {"kind": "mina"}
    if args[:1] == ["--mgit"]:
        body["mgit"], args = args[1], args[2:]
    body["text"] = (" ".join(args) or sys.stdin.read()).strip()
    req = urllib.request.Request(BASE + "/api/items", data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(req, timeout=5).read()
