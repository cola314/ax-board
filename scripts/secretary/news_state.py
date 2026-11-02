"""소식 상태 바꾸기(같은 일감 소식은 함께). 미나가 일하면서 자동으로 쓴다.
  python -I scripts/secretary/news_state.py backend#249 done          # 일감 키
  python -I scripts/secretary/news_state.py 152 review                # 소식 id
상태: new(새 소식) doing(진행 중) review(내 확인 대기 — 미나가 고친 것을 사용자가 mgit 으로 확인할 차례) done(완료) skip(무시)
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
    target, state = sys.argv[1], sys.argv[2]
    body = {"id": int(target), "state": state} if target.isdigit() else {"key": target, "state": state}
    req = urllib.request.Request(BASE + "/api/news/state", data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        print("changed", json.load(r)["changed"])
