"""GitLab 새 소식: 나한테 온 To-Do(멘션·리뷰 요청·할당 등)와 내 MR 의 승인 완료·머지·닫힘.

읽기 전용 glab 토큰으로 조회만 한다. 상태는 wait.py 의 state 에 함께 둔다.
"""
import json
import subprocess

import config

HOST = config.get("gitlab.host")
ME = config.get("gitlab.username")
CLI = config.get("gitlab.cli")
ACTION = {"mentioned": "멘션", "directly_addressed": "나를 지목", "review_requested": "리뷰 요청",
          "assigned": "할당", "approval_required": "승인 요청", "build_failed": "빌드 실패",
          "unmergeable": "머지 불가", "marked": "할 일 추가"}


def api(path):
    r = subprocess.run([CLI, "api", path, "--hostname", HOST], capture_output=True, timeout=30,
                       creationflags=0x08000000)  # CREATE_NO_WINDOW
    if r.returncode:
        raise RuntimeError(r.stderr.decode("utf-8", "ignore")[:200])
    return json.loads(r.stdout.decode("utf-8"))


def poll(st):
    """새 소식 목록. st 를 갱신한다(todo_last, my_mrs)."""
    out = []
    todos = api("todos?state=pending&per_page=50")
    last = st.get("todo_last")
    if last is not None:
        for t in sorted(todos, key=lambda x: x["id"]):
            if t["id"] > last and (t.get("author") or {}).get("username") != ME:  # 내가 나에게 단 리뷰 요청 등은 제외
                tgt = t.get("target") or {}
                out.append({"id": "todo-%d" % t["id"], "from": (t.get("author") or {}).get("name", ""),
                            "chat": (t.get("project") or {}).get("path_with_namespace", ""),
                            "text": "%s · %s %s\n%s" % (ACTION.get(t["action_name"], t["action_name"]),
                                                        tgt.get("title", ""), t.get("body", "")[:150],
                                                        t.get("target_url", ""))})
    st["todo_last"] = max([t["id"] for t in todos] + [last or 0])

    prev = st.get("my_mrs")  # {"pid:iid": {"title", "url", "approved"}}
    now = {}
    for mr in api("merge_requests?author_username=%s&state=opened&scope=all&per_page=50" % ME):
        key = "%d:%d" % (mr["project_id"], mr["iid"])
        ap = api("projects/%d/merge_requests/%d/approvals" % (mr["project_id"], mr["iid"]))
        now[key] = {"title": mr["title"], "url": mr["web_url"],
                    "approved": bool(ap.get("approved")) and ap.get("approvals_left", 1) == 0}
        if prev is not None and now[key]["approved"] and not (prev.get(key) or {}).get("approved"):
            out.append({"id": "mr-ok-" + key, "from": "내 MR", "chat": mr["references"]["full"],
                        "text": "승인 다 참 → 머지 가능 · %s\n%s" % (mr["title"], mr["web_url"])})
    for key, old in (prev or {}).items():
        if key not in now:
            pid, iid = key.split(":")
            state = api("projects/%s/merge_requests/%s" % (pid, iid)).get("state")
            label = {"merged": "머지됨", "closed": "닫힘"}.get(state, state)
            out.append({"id": "mr-%s-%s" % (state, key), "from": "내 MR", "chat": "",
                        "text": "%s · %s\n%s" % (label, old["title"], old["url"])})
    st["my_mrs"] = now
    return out
