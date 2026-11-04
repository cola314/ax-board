"""대시보드 '현황' 칸 데이터: 내 MR, push 안 한 로컬 브랜치, 진행 중 일, 다음 회의.

server.py 의 status_loop 스레드가 5분마다 collect() 를 불러 현황 칸을 채운다. 칸마다 실패해도 나머지는 채운다.
"""
import os
import re
import subprocess
import time
from datetime import datetime

import config

ROOT = config.ROOT
SOURCES = config.path(config.get("repos.sources_dir"))
BRANCH_PREFIX = config.get("gitlab.branch_prefix") or "feature/%s" % config.get("gitlab.username")
RECENT_DAYS = 30


def git(repo, *args):
    r = subprocess.run(["git", "-C", repo, *args], capture_output=True, timeout=20, creationflags=0x08000000)
    return r.stdout.decode("utf-8", "ignore").strip() if r.returncode == 0 else ""


def my_mrs():
    import gitlab_watch
    out = []
    for mr in gitlab_watch.api("merge_requests?author_username=%s&state=opened&scope=all&per_page=50" % gitlab_watch.ME):
        pid, iid = mr["project_id"], mr["iid"]
        ap = gitlab_watch.api("projects/%d/merge_requests/%d/approvals" % (pid, iid))
        pipe = mr.get("head_pipeline") or gitlab_watch.api("projects/%d/merge_requests/%d" % (pid, iid)).get("head_pipeline") or {}
        need = ap.get("approvals_required", 0)
        got = len(ap.get("approved_by") or [])
        out.append({"title": mr["title"], "ref": mr["references"]["full"], "url": mr["web_url"],
                    "approvals": "%d/%d" % (got, need), "pipeline": pipe.get("status", "-"),
                    "mergeable": bool(ap.get("approved")) and mr.get("detailed_merge_status") == "mergeable",
                    "merge_status": mr.get("detailed_merge_status", "")})
    return out


def brief_names(repo, iid):
    """리뷰 초안 파일 이름 후보(briefs/<이름>.html, 첫 번째가 정식). 기본 프로젝트(news.default_project)는 옛 이름 mr-<iid>-review 도."""
    names = ["mr-%s-%s-review" % (repo, iid)]
    return (["mr-%s-review" % iid] + names) if repo == config.get("news.default_project") else names


def review_mrs():
    """나에게 리뷰 요청 온 열린 MR(머지·닫힘은 state=opened 라 자동으로 빠짐). 요청 시각은 reviewers API 의 내 created_at.
    내가 이미 승인한 MR, review_tab.max_age_days(기본 30일) 넘게 갱신 없는 MR 은 뺀다. 승인 다 찬 MR 은 approved_all 로 표시."""
    import gitlab_watch
    out = []
    cutoff = datetime.now().astimezone().timestamp() - (config.get("review_tab.max_age_days") or 30) * 86400
    for mr in gitlab_watch.api("merge_requests?reviewer_username=%s&state=opened&scope=all&per_page=50" % gitlab_watch.ME):
        pid, iid = mr["project_id"], mr["iid"]
        if datetime.fromisoformat(mr["updated_at"].replace("Z", "+00:00")).timestamp() < cutoff:
            continue
        appr = {}
        try:
            appr = gitlab_watch.api("projects/%d/merge_requests/%d/approvals" % (pid, iid))
            if any((a.get("user") or {}).get("username") == gitlab_watch.ME for a in appr.get("approved_by") or []):
                continue
        except Exception:
            pass
        n_by, need = len(appr.get("approved_by") or []), appr.get("approvals_required") or 0
        ref = mr["references"]["full"]
        repo = ref.split("!")[0].rsplit("/", 1)[-1]
        try:
            me = next((r for r in gitlab_watch.api("projects/%d/merge_requests/%d/reviewers" % (pid, iid))
                       if (r.get("user") or {}).get("username") == gitlab_watch.ME), {})
        except Exception:
            me = {}
        names = brief_names(repo, iid)
        legacy = repo == config.get("news.default_project")  # 리뷰 초안 작업 id(task.py): 기본 프로젝트 mr-<iid>, 그 밖 mr-<repo>-<iid>
        out.append({"title": mr["title"], "ref": ref, "url": mr["web_url"], "repo": repo, "iid": iid,
                    "author": (mr.get("author") or {}).get("name", ""), "requested_at": me.get("created_at") or mr["created_at"],
                    "briefs": names, "task": "mr-%s" % iid if legacy else "mr-%s-%s" % (repo, iid),
                    "approved_all": bool(appr.get("approved") or (need and n_by >= need)),  # 필요한 승인 다 참 → 대시보드에서 회색으로 아래 접음
                    "approvals": "%d/%d" % (n_by, need) if appr else ""})
    return sorted(out, key=lambda x: x["requested_at"], reverse=True)


def unpushed_branches():
    """sources/* 레포(워크트리는 본 레포로 묶음)에서 원격에 없는 커밋이 있는 내 최근 feature 브랜치."""
    seen, out = set(), []
    since = time.time() - RECENT_DAYS * 86400
    for name in sorted(os.listdir(SOURCES)):
        repo = os.path.join(SOURCES, name)
        if not os.path.exists(os.path.join(repo, ".git")):
            continue
        common = os.path.normcase(os.path.abspath(os.path.join(repo, git(repo, "rev-parse", "--git-common-dir") or ".git")))
        if common in seen:
            continue
        seen.add(common)
        refs = git(repo, "for-each-ref", "refs/heads/" + BRANCH_PREFIX,
                   "--format=%(refname:short)|%(committerdate:unix)|%(contents:subject)")
        for line in refs.splitlines():
            br, ts, subj = (line.split("|", 2) + ["", ""])[:3]
            if not ts or int(ts) < since:
                continue
            ahead = git(repo, "rev-list", "--count", br, "--not", "--remotes")
            if ahead and int(ahead) > 0:
                out.append({"repo": name, "branch": br, "ahead": int(ahead), "subject": subj,
                            "date": datetime.fromtimestamp(int(ts)).strftime("%m-%d")})
    return sorted(out, key=lambda x: x["date"], reverse=True)


def in_progress():
    """TODO 파일(config todo.file)의 todo.section 절에서 아직 안 끝난([ ]) 최상위 항목 제목."""
    with open(config.path(config.get("todo.file")), encoding="utf-8") as f:
        text = f.read()
    m = re.search(r"^## %s\n(.*?)(?=^## )" % re.escape(config.get("todo.section")), text, re.S | re.M)
    out = []
    for line in (m.group(1) if m else "").splitlines():
        hit = re.match(r"^- \[ \] (.*)", line)
        if hit:
            title = re.search(r"\*\*(.+?)\*\*", hit.group(1))
            out.append(re.sub(r"\s+", " ", (title.group(1) if title else hit.group(1)))[:90])
    return out


def next_meetings(n=3):
    import calendar_watch
    now = time.time()
    evs = []
    for e in calendar_watch.load().get("events", []):
        try:
            ts = datetime.fromisoformat(e["start"].replace("Z", "+00:00")).timestamp()
        except (KeyError, ValueError):
            continue
        if ts > now - 600:
            evs.append(dict(e, when=datetime.fromtimestamp(ts).strftime("%m/%d(%a) %H:%M"), _ts=ts))
    evs.sort(key=lambda x: x["_ts"])
    return [{k: e.get(k) for k in ("when", "title", "location", "link")} for e in evs[:n]]


def collect(prev=None):
    status = {}
    cells = (("mrs", my_mrs, config.on("gitlab")), ("todo", in_progress, bool(config.get("todo.file"))),
             ("meetings", next_meetings, config.on("calendar")),
             ("reviews", review_mrs, config.on("gitlab") and config.on("review_tab")))
    for key, fn, enabled in cells:
        if not enabled:  # 꺼진 기능 칸은 아예 안 보낸다
            continue
        try:
            status[key] = {"items": fn(), "at": time.strftime("%H:%M")}
        except Exception as e:  # 칸 하나 실패해도 나머지는 채운다. 이전 값이 있으면 그대로 두고 실패만 표시(GitLab i/o timeout 등)
            old = (prev or {}).get(key) or {}
            status[key] = (dict(old, failed="조회 실패 %s" % time.strftime("%H:%M")) if "items" in old
                           else {"error": "조회 실패: %s" % str(e)[:120], "at": time.strftime("%H:%M")})
    return status
