"""미나 답을 대시보드에 카드로 올린다. 인자가 없으면 stdin 을 읽는다.
  python -I scripts/secretary/post.py "답장 초안: ..."
  python -I scripts/secretary/post.py --mgit "http://127.0.0.1:41172/?c=abc123" "리뷰 준비 끝났습니다"   # 카드에 [mgit 으로 리뷰] 버튼
  python -I scripts/secretary/post.py --mgit <mgit 주소> --doc "http://localhost:8790/brief/<번호>-change" "..."  # + [📝 변경 설명] 버튼
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
    if args[:1] in (["--help"], ["-h"]):  # 사용법 확인용 호출이 대시보드에 '--help' 로 올라가던 것 방지
        sys.stdout.reconfigure(encoding="utf-8")
        print(__doc__)
        sys.exit(0)
    body = {"kind": "mina"}
    while args[:1] in (["--mgit"], ["--doc"]):
        body[args[0][2:]], args = args[1], args[2:]
    body["text"] = (" ".join(args) or sys.stdin.read()).strip()
    req = urllib.request.Request(BASE + "/api/items", data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(req, timeout=5).read()
