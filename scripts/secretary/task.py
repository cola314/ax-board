"""비서 대시보드 '진행 중' 표시. 미나가 뒤에서 일을 돌릴 때 등록하고 끝나면 지운다.
  python -I scripts/secretary/task.py start <id> "<제목>"
  python -I scripts/secretary/task.py progress <id> <0-100> "<지금 하는 단계>" [--eta <남은 분>]
  python -I scripts/secretary/task.py done <id>
"""
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402

BASE = config.base()

if __name__ == "__main__":
    cmd, tid = sys.argv[1], sys.argv[2]
    if cmd == "start":
        body = {"id": tid, "title": " ".join(sys.argv[3:])}
    elif cmd == "progress":
        rest = sys.argv[4:]
        eta = None
        if "--eta" in rest:  # 남은 시간을 아는 쪽(테스트 몇 분 등)이 직접 알려 주면 그걸 쓴다
            i = rest.index("--eta")
            eta, rest = float(rest[i + 1]), rest[:i] + rest[i + 2:]
        body = {"id": tid, "progress": int(sys.argv[3]), "note": " ".join(rest), "eta_min": eta}
    else:
        body = {"id": tid, "done": True}
    req = urllib.request.Request(BASE + "/api/task", data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(req, timeout=5).read()
