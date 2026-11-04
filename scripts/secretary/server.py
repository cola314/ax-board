"""미나 비서 대시보드 우체통 서버. 내용은 메모리에만 둔다(재시작하면 비워짐).

  GET  /                  대시보드
  GET  /api/items?after=N 카드 목록(+ attendance: 이번 주 근무시간 요약, features.attendance / status: 현황·리뷰·일감 칸)
  GET  /api/config        대시보드 화면용 설정(config dashboard 절 + 사용자 이름)
  GET  /brief/<이름>      회의 준비자료 HTML(artifacts/secretary/briefs/<이름>.html)
  GET  /api/rules         운영 규칙 마크다운(config rules.file) — 대시보드 📜 규칙 보기
  POST /api/items         감시꾼·미나가 카드 추가 {kind, from, chat, text}
  POST /api/say           대시보드 입력 {text} → 카드 + 미나 수신함
  POST /api/inbox/take    미나 수신함 비우며 가져오기
  POST /api/unsay          {id} 보낸 말 취소(안 가져갔으면 수신함에서 빼고, 가져갔으면 미나에게 취소 알림)
  POST /api/open           {url} GitLab 링크를 사용자 크롬으로 열기(GitLab 호스트만)
  POST /api/items {.., tier}  tier: now(기본)·hour(정시 묶음)·soon(+conv,at: 1:1 10분 뒤, 이미 답했으면 무시)·none(바로 무시)
  POST /api/my_last         {대화방 id: 내가 마지막으로 보낸 시각} — soon 판정용
  POST /api/notify        다른 세션의 보고 {from, text} → 카드 + 미나 수신함
  POST /api/task          {id, title} 진행 중 등록 / {id, progress, note, eta_min?} 진행률 / {id, failed:true, note} 실패 / {id, done:true} 완료
  POST /api/status        현황 칸 데이터(서버가 5분마다 직접 모음, 수동 갱신용)
  POST /api/view          {mgit: url} → 대시보드 mgit 탭
  POST /api/news/hide     {id} 소식 하나 지우기(숨김)
  POST /api/news/clear    받은 소식(Teams 카드) 모두 숨기기
  POST /api/news/state    {id|key|chat|before, state} 소식 상태(new/doing/review/done/skip). key 면 같은 일감, chat 이면 같은 대화방 새 소식 전부
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from html import unescape as html_unescape
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402

PORT = config.port()  # env SECRETARY_PORT > config port
HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")

# 대화·소식·수신함을 파일에 남긴다(서버 재시작에도 유지). 이 PC 의 artifacts 만, 커밋 안 됨. "모두 지우기"는 hidden 표시.
CHAT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "artifacts", "secretary", "chat.json")
BRIEFS = os.path.join(os.path.dirname(CHAT), "briefs")

lock = threading.Lock()
items, inbox = [], []
view = {"mgit": ""}  # 대시보드 mgit 탭에 띄울 주소
tasks = {}  # 미나가 뒤에서 돌리는 일 {id: {title, started, progress?, note?}} — 대시보드 "진행 중" 표시
status = {}  # 대시보드 '현황' 칸(status_loop 가 5분마다 채움)
STATUS_EVERY = 300
status_kick = threading.Event()  # GitLab 소식(승인·머지·리뷰 요청 등)이 오면 5분 안 기다리고 현황(내 MR)을 바로 다시 모은다
heartbeat = [0.0]  # 감시꾼이 마지막으로 수신함을 확인한 시각(2초마다)
# 소식 표시 등급(tier): now=바로, hour=정시에 묶어서, soon=1:1 은 10분 기다렸다가 내가 이미 답했으면 무시로, none=바로 무시(skip)
held = []  # 아직 소식판에 안 올린 보류 소식 [{release_at, conv?, at?, kind, kw}]
my_last = {}  # 대화방별 내가 마지막으로 보낸 시각(ISO) — 감시꾼이 넘겨 줌
SOON = 600
TASK_STALE = 1800  # 뒤에서 도는 일이 이만큼(초) 등록·진행 보고 없이 조용하면 미나 카드로 한 번 알린다


STATES = ("new", "doing", "review", "done", "skip")
NEWS_KINDS = ("teams", "session")
DEFAULT_PROJECT = config.get("news.default_project")  # "#249 ..." 처럼 번호만 오면(세션 보고 등) 이 프로젝트로. 비면 안 묶음
_KEY_PATTERNS = tuple(re.compile(p) for p in config.get("news.key_patterns", []))  # 그룹 1=프로젝트, 2=번호


def news_key(item):
    """같은 일감의 소식을 묶는 키 'backend#249'. 못 찾으면 None(개별)."""
    text = " ".join(str(item.get(k) or "") for k in ("text", "from", "chat"))
    for pat in _KEY_PATTERNS:
        m = pat.search(text)
        if m:
            return "%s#%s" % (m.group(1), m.group(2))
    m = re.match(r"^\W*#(\d+)", str(item.get("from") or ""))
    return "%s#%s" % (DEFAULT_PROJECT, m.group(1)) if m and DEFAULT_PROJECT else None


def tag_news(item):
    if item["kind"] in NEWS_KINDS:
        item.setdefault("state", "new")
        if "key" not in item:
            key = news_key(item)
            if key:
                item["key"] = key


def set_state(state, iid=None, key=None):
    """key 가 있으면 같은 키 소식 전부, 아니면 id 하나. 바뀐 개수."""
    n = 0
    for i in items:
        if i["kind"] in NEWS_KINDS and ((key and i.get("key") == key) or (iid is not None and i["id"] == iid)):
            i["state"] = state
            n += 1
    return n


def load():
    try:
        with open(CHAT, encoding="utf-8") as f:
            saved = json.load(f)
    except (OSError, ValueError):
        return
    held.extend(saved.get("held", []))
    my_last.update(saved.get("my_last", {}))
    for t in saved.get("tasks", []):  # 뒤에서 도는 일은 서버를 다시 띄워도 '진행 중'에 남긴다
        tasks[t["id"]] = t
    for i, it in enumerate(saved.get("items", []), 1):
        it["id"] = i
        tag_news(it)
        items.append(it)
        if it.get("queued"):
            inbox.append(it)


def save():
    os.makedirs(os.path.dirname(CHAT), exist_ok=True)
    keep = [dict(i, queued=i in inbox) for i in items][-500:]
    with open(CHAT, "w", encoding="utf-8") as f:
        json.dump({"items": keep, "tasks": list(tasks.values()), "held": held, "my_last": my_last}, f, ensure_ascii=False)


def add(kind, **kw):
    with lock:
        item = {"id": len(items) + 1, "ts": time.strftime("%H:%M:%S"), "kind": kind}
        item.update({k: v for k, v in kw.items() if v})
        if kind == "teams" and (item.get("from") == "내 MR" or "/" in str(item.get("chat") or "")
                                or (item.get("chat") == "메일" and " | " in str(item.get("text") or ""))):
            status_kick.set()
        tag_news(item)
        if item.get("key"):  # 이미 진행 중·완료로 둔 일감에 새 소식이 오면 그 상태를 따른다(완료 건에 새 소식은 다시 new)
            prev = [i.get("state") for i in items if i.get("key") == item["key"] and i["kind"] in NEWS_KINDS]
            if prev and prev[-1] in ("doing", "review"):
                item["state"] = prev[-1]
        items.append(item)
        save()
        return item


# 준비자료 HTML 에 주입하는 스크롤바 스타일. Chromium 121+ 은 표준 scrollbar-width/color 가 있으면
# ::-webkit-scrollbar 를 무시하고 OS 기본(화살표 버튼)을 그리므로 표준 속성은 무력화하고 webkit 만 쓴다.
SCROLLBAR_CSS = b"""<style>
*{scrollbar-width:auto!important;scrollbar-color:auto!important}
::-webkit-scrollbar{width:8px;height:8px}
::-webkit-scrollbar-track,::-webkit-scrollbar-corner{background:transparent}
::-webkit-scrollbar-thumb{background:color-mix(in srgb,var(--muted,var(--mute,#8b93a3)) 35%,transparent);border-radius:999px;border:2px solid transparent;background-clip:padding-box}
::-webkit-scrollbar-thumb:hover{background:color-mix(in srgb,var(--muted,var(--mute,#8b93a3)) 65%,transparent);background-clip:padding-box}
::-webkit-scrollbar-button{display:none;width:0;height:0}
</style>"""
# 준비자료 코드 블록 구문 강조(highlight.js, 라이트 github / 다크 github-dark). 대상: pre>code, 언어 클래스 없는 pre(자동 감지),
# +/- 줄만 있으면 diff. 이미 span 등으로 꾸민 블록은 그대로. 오프라인이면 스크립트만 실패하고 문서는 원래대로.
HLJS = "https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/"
HIGHLIGHT = (r"""<link rel="stylesheet" media="(prefers-color-scheme: light)" href="%(u)sstyles/github.min.css">
<link rel="stylesheet" media="(prefers-color-scheme: dark)" href="%(u)sstyles/github-dark.min.css">
<style>.hljs{background:transparent!important}pre code.hljs{padding:0}</style>
<script src="%(u)shighlight.min.js" defer></script>
<script>addEventListener("DOMContentLoaded",function(){var h=window.hljs;if(!h)return;
h.configure({languages:["java","kotlin","sql","bash","yaml","json","xml","javascript","typescript","python","ini","diff"]});  // 자동 감지 후보(로그가 swift·go 로 잡히는 것 방지)
document.querySelectorAll("pre").forEach(function(p){var e=p.querySelector(":scope>code")||p;if(e.children.length||/\bnohighlight\b/.test(e.className))return;
var l=e.textContent.split("\n").filter(function(x){return x.trim()});
if(!/\blang(uage)?-/.test(e.className)&&l.length&&l.every(function(x){return /^([+\- ]|@@)/.test(x)})&&l.some(function(x){return /^[+-]/.test(x)}))e.classList.add("language-diff");
try{h.highlightElement(e)}catch(_){}});});</script>""" % {"u": HLJS}).encode()


# ── 근무시간(현황판 맨 위 한 줄) ── attendance.json(attendance_set.py 가 저장) + 지금 시각.
# 규칙은 /attendance 스킬 '핵심 상수': 주 40h, 점심 12-13 1h, 퇴근 19시 넘으면 저녁 19-20 1h, 휴일·연차 8h
ATT_FILE = os.path.join(os.path.dirname(CHAT), "attendance.json")
ATT_OVERRIDES = os.path.join(os.path.dirname(ATT_FILE), "attendance_overrides.json")
LUNCH, DINNER, OFF_DAY = (720, 780), (1140, 1200), 480


def _hm(s):
    h, m = str(s).split(":")
    return int(h) * 60 + int(m)


def _fmt(m):
    return "%02d:%02d" % divmod(int(m), 60)


def work_min(start, end, dinner=True):
    """출근~퇴근(분) 사이 근무 분: 점심 겹침 빼고, 퇴근이 19시를 넘으면 저녁 겹침도 뺀다(dinner=False 면 저녁 안 쉰 날)."""
    if end <= start:
        return 0
    w = end - start - max(0, min(end, LUNCH[1]) - max(start, LUNCH[0]))
    if dinner and end > DINNER[0]:
        w -= max(0, min(end, DINNER[1]) - max(start, DINNER[0]))
    return w


def leave_at(start, need):
    """need 분을 채우는 가장 이른 퇴근(분). 보통 start+need+60(점심), 19시를 넘으면 +60(저녁) — 스킬 계산식과 같음."""
    end = start + max(0, need)
    while work_min(start, end) < need:
        end += 1
    return end


def attendance(now=None, data=None, week_hours=None):
    """이번 주 근무 요약. 오늘은 지금 시각까지, 남은 시간은 남은 근무일(오늘 포함, 휴일·연차·퇴근한 날 제외)로 균등 분배."""
    now = now or time.localtime()
    if data is None:
        try:
            with open(ATT_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return None
    goal = int(float(week_hours or config.get("attendance.week_hours") or 40) * 60)
    try:  # 사용자가 알려 준 예외(예: 저녁 휴게 안 쓰고 일함) {날짜: {"no_dinner": true}} — flex 갱신에 덮이지 않게 따로 둠
        with open(ATT_OVERRIDES, encoding="utf-8") as f:
            overrides = json.load(f)
    except (OSError, ValueError):
        overrides = {}
    today, now_m = time.strftime("%Y-%m-%d", now), now.tm_hour * 60 + now.tm_min
    rows, before, today_min, days_left, today_in, today_out = [], 0, 0, 0, None, None
    for d in data.get("days") or []:
        date, i, o, off = d.get("date") or "", d.get("in"), d.get("out"), d.get("off") or []
        if off:
            m, kind = OFF_DAY, "off"
        elif date < today:
            m, kind = (work_min(_hm(i), _hm(o), not (overrides.get(date) or {}).get("no_dinner")) if i and o else 0), "past"
        elif date == today:
            kind, today_in, today_out = "today", i, o
            m = work_min(_hm(i), _hm(o) if o else max(now_m, _hm(i))) if i else 0
            if not o:
                days_left += 1
        else:
            m, kind = 0, "future"
            days_left += 1
        if kind == "today":
            today_min = m
        else:
            before += m
        rows.append({"date": date, "in": i, "out": o, "off": off, "min": m, "kind": kind})
    done = before + today_min
    per_day = (goal - before) / days_left if days_left else None  # 오늘 포함 남은 날 하루 몫
    leave = None
    if per_day is not None and today_in and not today_out:
        leave = leave_at(_hm(today_in), max(0, round(per_day)))
    return {"week_hours": goal // 60, "goal": goal, "done": done, "left": max(0, goal - done), "days_left": days_left,
            "per_day": round(max(0, per_day)) if per_day is not None else None, "today": today, "in": today_in,
            "out": today_out, "leave": _fmt(leave) if leave is not None else None,
            "leave_late": leave is not None and leave > 22 * 60,  # 퇴근 가능 범위(~22:00)를 넘으면 하루로 못 채움
            "fetched": data.get("fetched") or "",
            "stale": not str(data.get("fetched") or "").startswith(today), "rows": rows}


_att_cache = [None, None]


def attendance_cached():
    """대시보드가 0.5초마다 묻는다 — 파일 수정 시각·분이 같으면 다시 계산하지 않는다."""
    if not config.on("attendance"):
        return None
    try:
        sig = (os.path.getmtime(ATT_FILE), time.strftime("%Y-%m-%d %H:%M"))
    except OSError:
        return None
    if _att_cache[0] != sig:
        _att_cache[:] = [sig, attendance()]
    return _att_cache[1]


def with_briefs(st):
    """👀 리뷰 칸: MR 마다 이미 있는 리뷰 초안(briefs/<이름>.html)을 그때그때 확인해 brief 로 붙인다(수집은 5분마다라)."""
    rv = st.get("reviews")
    if not rv or not rv.get("items"):
        return st
    try:  # 여러 MR 을 한 장에 묶은 옛 초안 mr-<a>-<b>-review(기본 프로젝트 번호 범위)
        spans = [(int(x.group(1)), int(x.group(2)), x.group(0)[:-5]) for x in
                 (re.fullmatch(r"mr-(\d+)-(\d+)-review\.html", f) for f in os.listdir(BRIEFS)) if x]
    except OSError:
        spans = []

    def brief(m):
        names = m.get("briefs", [])
        hit = next((n for n in names if os.path.exists(os.path.join(BRIEFS, n + ".html"))), None)
        legacy = names and names[0] == "mr-%s-review" % m.get("iid")
        return hit or (next((n for a, b, n in spans if a <= int(m["iid"]) <= b), None) if legacy else None)

    def with_meta(m, name):  # 초안 결론: <meta name="review-verdict[-<iid>]" content="action|ok"> · review-summary[-<iid>]
        meta = brief_meta(name) if name else {}
        sfx = "-%s" % m.get("iid")
        return dict(m, brief=name, verdict=meta.get("review-verdict" + sfx) or meta.get("review-verdict"),
                    summary=meta.get("review-summary" + sfx) or meta.get("review-summary"))
    return dict(st, reviews=dict(rv, items=[with_meta(m, brief(m)) for m in rv["items"]]))


ISSUE_ORDER = {"decision": 0, "review": 1, "doing": 2, "new": 3}  # 📌 일감 탭 정렬(그 밖은 4), 같은 그룹은 최근 할당순


def issue_docs(repo, iid, files):
    """일감 문서: 기본 프로젝트는 briefs/<번호>-*.html, 그 밖(또는 기본 프로젝트도)은 <repo>-<번호>-*.html."""
    pre = ["%s-%s-" % (repo, iid)] + (["%s-" % iid] if repo == DEFAULT_PROJECT else [])
    return sorted(f[:-5] for f in files if f.endswith(".html") and any(f.startswith(p) for p in pre))


def with_issues(st):
    """📌 일감 칸: 일감마다 소식 상태(같은 키 최신 소식), 문서(briefs), 결정 대기(문서 meta issue-status=decision), mgit 주소를 붙이고 정렬.
    호출하는 쪽이 lock 을 잡고 있다(items 를 읽음)."""
    iss = st.get("issues")
    if not iss or not iss.get("items"):
        return st
    try:
        files = os.listdir(BRIEFS)
    except OSError:
        files = []
    state, mgit_key, mgit_num = {}, {}, {}
    for i in items:  # 뒤 것이 최신
        if i["kind"] in NEWS_KINDS and i.get("key"):
            state[i["key"]] = i.get("state", "new")
        mg = i.get("mgit") or ""
        if mg and "/brief/" not in mg:  # 소식 카드에 붙은 mgit 주소(대시보드 rememberMgit 과 같은 규칙: 키 또는 본문의 #번호)
            if i.get("key"):
                mgit_key[i["key"]] = mg
            for n in re.findall(r"#(\d+)\b", str(i.get("text") or "")):
                mgit_num[n] = mg
    out = []
    for m in iss["items"]:
        key, num = "%s#%s" % (m["repo"], m["iid"]), str(m["iid"])
        docs = issue_docs(m["repo"], m["iid"], files)
        decision = next((meta.get("issue-decision") or "결정 필요" for meta in map(brief_meta, docs)
                         if meta.get("issue-status") == "decision"), "")
        st_ = "decision" if decision else state.get(key, "")
        out.append(dict(m, key=key, state=st_, decision=decision, docs=docs,
                        mgit=mgit_key.get(key) or (mgit_num.get(num) if m["repo"] == DEFAULT_PROJECT else "") or ""))
    out.sort(key=lambda x: x.get("assigned_at") or "", reverse=True)
    out.sort(key=lambda x: ISSUE_ORDER.get(x["state"], 4))
    return dict(st, issues=dict(iss, items=out))


_meta_cache = {}  # 초안 이름 → (mtime, {meta 이름: 값}) — 바뀐 파일만 다시 읽는다


def brief_meta(name):
    path = os.path.join(BRIEFS, name + ".html")
    try:
        mt = os.path.getmtime(path)
        if _meta_cache.get(name, (None,))[0] != mt:
            with open(path, encoding="utf-8", errors="replace") as f:
                head = f.read(20000)
            _meta_cache[name] = (mt, {k: html_unescape(v) for k, v in
                                      re.findall(r'<meta\s+name="((?:review|issue)-[\w-]+)"\s+content="([^"]*)"', head)})
        return _meta_cache[name][1]
    except OSError:
        return {}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return {}

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            with open(HTML, "rb") as f:
                return self.send(200, f.read(), "text/html; charset=utf-8")
        if u.path == "/api/config":  # 화면용 공개 값만(비서·사용자 이름, 축약 규칙, 단축 버튼)
            return self.send(200, dict(config.get("dashboard") or {}, user=config.me_name()))
        if u.path == "/api/rules":  # 대시보드 '📜 규칙' 보기: 운영 규칙 마크다운 원문
            rel = config.get("rules.file") or ""
            if not rel:
                return self.send(404, {"error": "rules.file not set"})
            try:
                with open(config.path(rel), encoding="utf-8") as f:
                    return self.send(200, {"file": rel, "markdown": f.read()})
            except OSError as e:
                return self.send(404, {"error": str(e)})
        if u.path.startswith("/brief/"):  # 회의 준비자료 HTML(artifacts/secretary/briefs/<이름>.html) — 메인 패널 iframe 으로 연다
            name = os.path.basename(u.path[len("/brief/"):])
            try:
                with open(os.path.join(BRIEFS, name + ".html"), "rb") as f:
                    html = f.read()
                i = html.lower().rfind(b"</head>")  # 스크롤바 공통 스타일 주입 — 문서마다 넣지 않아도 대시보드와 같게
                html = html[:i] + SCROLLBAR_CSS + HIGHLIGHT + html[i:] if i >= 0 else SCROLLBAR_CSS + HIGHLIGHT + html
                return self.send(200, html, "text/html; charset=utf-8")
            except OSError:
                return self.send(404, {"error": "no brief"})
        if u.path == "/api/items":
            after = int(parse_qs(u.query).get("after", ["0"])[0])
            with lock:
                return self.send(200, {"items": [i for i in items[after:] if not i.get("hidden")], "pending": len(inbox),
                                       "listen_age": round(time.time() - heartbeat[0]) if heartbeat[0] else None,
                                       "mgit": view["mgit"], "tasks": list(tasks.values()), "status": with_issues(with_briefs(status)), "held": len(held),
                                       "attendance": attendance_cached(),
                                       "states": {i["id"]: i.get("state", "new") for i in items if i["kind"] in NEWS_KINDS and not i.get("hidden")}})
        self.send(404, {"error": "not found"})

    def do_POST(self):
        b = self.body()
        if self.path == "/api/items":
            if not str(b.get("text") or "").strip():
                return self.send(400, {"error": "empty"})
            kw = {k: b.get(k) for k in ("from", "chat", "text", "mgit", "doc")}
            tier = b.get("tier") or "now"
            if tier in ("hour", "soon"):
                with lock:
                    held.append({"release_at": next_hour() if tier == "hour" else time.time() + SOON, "conv": b.get("conv"),
                                 "at": b.get("at"), "ts": time.strftime("%H:%M:%S"), "kind": b.get("kind", "mina"), "kw": kw})
                    save()
                return self.send(200, {"held": True})
            if tier == "none":
                kw["state"] = "skip"
            return self.send(200, add(b.get("kind", "mina"), **kw))
        if self.path == "/api/my_last":  # 감시꾼: {대화방 id: 내가 마지막으로 보낸 시각}
            with lock:
                my_last.update({str(k): str(v) for k, v in b.items()})
            return self.send(200, {"ok": True})
        if self.path == "/api/say":
            text = str(b.get("text", "")).strip()
            if not text:
                return self.send(400, {"error": "empty"})
            item = add("user", text=text)
            with lock:
                inbox.append(item)
                save()
            return self.send(200, item)
        if self.path == "/api/unsay":  # 대시보드에서 보낸 말 취소 {id}: 아직 미나가 안 가져갔으면 수신함에서 빼고, 가져갔으면 취소 알림을 넣는다
            with lock:
                it = next((i for i in items if i["id"] == int(b.get("id") or 0) and i["kind"] == "user"), None)
                if not it or it.get("cancelled"):
                    return self.send(404, {"error": "no such message"})
                it["cancelled"] = True
                pending = it in inbox
                if pending:
                    inbox.remove(it)
                else:
                    inbox.append({"kind": "cancel", "id": it["id"], "text": it["text"]})
                save()
            return self.send(200, {"ok": True, "delivered": not pending})
        if self.path == "/api/notify":  # 다른 Claude 세션이 비서에게 보내는 보고(/tell-mina)
            text = str(b.get("text", "")).strip()
            if not text:
                return self.send(400, {"error": "empty"})
            item = add("session", text=text, **{"from": str(b.get("from") or "다른 세션")[:60]})
            with lock:
                inbox.append(item)
                save()
            return self.send(200, item)
        if self.path == "/api/task":  # {id, title} 등록 / {id, progress, note} 진행률 / {id, done} 완료
            tid = str(b.get("id") or "")
            if b.get("done"):
                tasks.pop(tid, None)
            elif "progress" in b or b.get("failed"):
                if tid:  # 서버 재시작으로 등록이 날아갔어도 진행률이 오면 다시 살린다
                    tasks.setdefault(tid, {"id": tid, "title": str(b.get("title") or tid), "started": time.time()})
                    tasks[tid]["last_change"] = time.time()
                    if "progress" in b:
                        tasks[tid]["progress"] = max(0, min(100, int(b.get("progress") or 0)))
                    tasks[tid]["note"] = str(b.get("note") or "")[:80]
                    if b.get("failed"):
                        tasks[tid]["failed"] = True
                    if b.get("eta_min") is not None:  # 일하는 쪽이 직접 낸 남은 시간 추정(분) → 끝날 시각으로 저장
                        tasks[tid]["eta_at"] = time.time() + float(b["eta_min"]) * 60
            elif tid:
                tasks[tid] = {"id": tid, "title": str(b.get("title") or tid), "started": time.time(), "last_change": time.time()}
            with lock:
                save()
            return self.send(200, {"tasks": list(tasks.values())})
        if self.path == "/api/status":  # 현황 칸 데이터 통째로 교체
            status.clear()
            status.update(b)
            return self.send(200, {"ok": True})
        if self.path == "/api/open":  # 소식의 GitLab 링크를 사용자 크롬으로 연다 {url} — GitLab 주소만 허용
            url = str(b.get("url") or "")
            host = config.get("gitlab.host") or ""
            if not host or not re.match(r"https?://%s/" % re.escape(host), url):
                return self.send(400, {"error": "gitlab url only"})
            subprocess.Popen([config.get("gmail.chrome_path") or "chrome", url], creationflags=0x08000000)
            return self.send(200, {"ok": True})
        if self.path == "/api/view":  # 미나가 mgit 주소를 넘기면 대시보드가 mgit 탭으로 전환
            view["mgit"] = str(b.get("mgit") or "")
            return self.send(200, view)
        if self.path == "/api/news/state":
            state = str(b.get("state") or "")
            if state not in STATES:
                return self.send(400, {"error": "state must be one of %s" % ", ".join(STATES)})
            with lock:
                if b.get("before") is not None:  # 이 소식 id 이하(이전)인 '새 소식'을 전부 이 상태로(예: 옛 소식 일괄 무시)
                    n = 0
                    for i in items:
                        if i["kind"] in ("teams", "session") and i["id"] <= int(b["before"]) and i.get("state", "new") == "new":
                            i["state"] = state
                            n += 1
                    save()
                    return self.send(200, {"changed": n})
                if b.get("chat"):  # 같은 대화방의 '새 소식'을 전부 이 상태로(예: 시끄러운 단톡 무시)
                    n = 0
                    for i in items:
                        if i["kind"] in NEWS_KINDS and i.get("chat") == b["chat"] and i.get("state", "new") == "new":
                            i["state"] = state
                            n += 1
                    save()
                    return self.send(200, {"changed": n})
                n = set_state(state, iid=b.get("id"), key=b.get("key"))
                if not n and b.get("id") is not None:
                    return self.send(404, {"error": "no such news"})
                # id 로 바꿨는데 그 소식에 key 가 있으면 같은 일감 전부 함께
                if b.get("id") is not None and not b.get("key"):
                    k = next((i.get("key") for i in items if i["id"] == b.get("id")), None)
                    if k:
                        n = set_state(state, key=k)
                save()
            return self.send(200, {"changed": n})
        if self.path == "/api/news/hide":  # 소식 하나 지우기(숨김) {id}
            with lock:
                for i in items:
                    if i["id"] == int(b.get("id") or 0):
                        i["hidden"] = True
                save()
            return self.send(200, {"ok": True})
        if self.path == "/api/news/clear":
            with lock:
                for i in items:
                    if i["kind"] in ("teams", "session"):
                        i["hidden"] = True
                save()
            return self.send(200, {"ok": True})
        if self.path == "/api/inbox/take":
            with lock:
                heartbeat[0] = time.time()
                taken = inbox[:]
                inbox.clear()
                if taken:
                    save()
            return self.send(200, {"items": [{k: v for k, v in t.items() if k != "queued"} for t in taken]})
        self.send(404, {"error": "not found"})


def next_hour():
    t = time.localtime()
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour + 1, 0, 0, 0, 0, -1))


def replied(h):
    """1:1 보류 소식: 그 뒤에 내가 그 방에 답했으면 True."""
    mine, at = my_last.get(str(h.get("conv") or "")), h.get("at")
    return bool(mine and at and str(mine) > str(at))


def task_alert(t, now):
    """뒤에서 도는 일 하나의 예외 알림 (종류, 문구). 없으면 None. 같은 종류 경고는 작업당 한 번(t['alerted'] 로 거름)."""
    done = t.get("alerted") or []
    if t.get("failed"):
        return None if "failed" in done else ("failed", "❌ %s 실패: %s" % (t["title"], t.get("note") or "사유 없음"))
    if "stale" not in done and now - float(t.get("last_change", t.get("started", now))) > TASK_STALE:
        return "stale", "⚠️ %s — %d분째 소식 없음" % (t["title"], TASK_STALE // 60)
    return None


def check_tasks():
    """평소엔 배지만 두고, 30분 무소식·실패일 때만 미나 카드로 알린다. 실패한 일은 목록에서 뺀다."""
    now, out = time.time(), []
    with lock:
        for tid, t in list(tasks.items()):
            a = task_alert(t, now)
            if a:
                t["alerted"] = (t.get("alerted") or []) + [a[0]]
                out.append(a[1])
            if t.get("failed"):
                tasks.pop(tid, None)
        if out:
            save()
    for text in out:
        add("mina", text=text, alert="task")


def release_loop():
    """보류 소식을 때가 되면 소식판에 올린다(정시 묶음은 한꺼번에, 1:1 은 내가 이미 답했으면 무시로)."""
    while True:
        time.sleep(20)
        try:
            check_tasks()
        except Exception as e:
            print("TASK_CHECK_SKIP", e, file=sys.stderr, flush=True)
        now = time.time()
        with lock:
            due = [h for h in held if h["release_at"] <= now]
            for h in due:
                held.remove(h)
        shown = []
        for h in due:
            kw = dict(h["kw"])
            if h.get("conv") and replied(h):
                kw["state"] = "skip"
            it = add(h["kind"], **kw)
            if it.get("state") != "skip":
                shown.append({k: it.get(k) for k in ("id", "from", "chat", "text")})
        if len(shown) >= 2:  # 정시 묶음: 미나를 깨워 '이번 시간 묶음' 요약 카드를 올리게(한 줄씩 누가·무엇·내 몫)
            with lock:
                inbox.append({"kind": "digest", "items": shown})


def status_loop():
    """현황 칸: 한 바퀴 1분쯤(GitLab·git) 걸려 서버에서 따로 돈다. 감시꾼은 자주 깨고 다시 떠서 거기선 끝까지 못 돈다."""
    import status as status_board
    while True:
        try:
            b = status_board.collect(status)
            with lock:
                status.clear()
                status.update(b)
        except Exception as e:
            print("STATUS_SKIP", e, file=sys.stderr, flush=True)
        status_kick.wait(STATUS_EVERY)
        status_kick.clear()


if __name__ == "__main__":
    load()
    if config.on("status"):
        threading.Thread(target=status_loop, daemon=True).start()
    threading.Thread(target=release_loop, daemon=True).start()
    print("secretary on http://127.0.0.1:%d" % PORT, flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
