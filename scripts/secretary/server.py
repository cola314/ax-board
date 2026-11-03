"""미나 비서 대시보드 우체통 서버. 내용은 메모리에만 둔다(재시작하면 비워짐).

  GET  /                  대시보드
  GET  /api/items?after=N 카드 목록
  GET  /api/config        대시보드 화면용 설정(config dashboard 절 + 사용자 이름)
  GET  /brief/<이름>      회의 준비자료 HTML(artifacts/secretary/briefs/<이름>.html)
  GET  /api/rules         운영 규칙 마크다운(config rules.file) — 대시보드 📜 규칙 보기
  POST /api/items         감시꾼·미나가 카드 추가 {kind, from, chat, text}
  POST /api/say           대시보드 입력 {text} → 카드 + 미나 수신함
  POST /api/inbox/take    미나 수신함 비우며 가져오기
  POST /api/unsay          {id} 보낸 말 취소(안 가져갔으면 수신함에서 빼고, 가져갔으면 미나에게 취소 알림)
  POST /api/open           {url} GitLab 링크를 사용자 크롬으로 열기(GitLab 호스트만)
  POST /api/notify        다른 세션의 보고 {from, text} → 카드 + 미나 수신함
  POST /api/task          {id, title} 진행 중 등록 / {id, progress, note, eta_min?} 진행률 / {id, done:true} 완료
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
        json.dump({"items": keep, "tasks": list(tasks.values())}, f, ensure_ascii=False)


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
                    return self.send(200, f.read(), "text/html; charset=utf-8")
            except OSError:
                return self.send(404, {"error": "no brief"})
        if u.path == "/api/items":
            after = int(parse_qs(u.query).get("after", ["0"])[0])
            with lock:
                return self.send(200, {"items": [i for i in items[after:] if not i.get("hidden")], "pending": len(inbox),
                                       "listen_age": round(time.time() - heartbeat[0]) if heartbeat[0] else None,
                                       "mgit": view["mgit"], "tasks": list(tasks.values()), "status": status,
                                       "states": {i["id"]: i.get("state", "new") for i in items if i["kind"] in NEWS_KINDS and not i.get("hidden")}})
        self.send(404, {"error": "not found"})

    def do_POST(self):
        b = self.body()
        if self.path == "/api/items":
            if not str(b.get("text") or "").strip():
                return self.send(400, {"error": "empty"})
            return self.send(200, add(b.get("kind", "mina"), **{k: b.get(k) for k in ("from", "chat", "text", "mgit", "doc")}))
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
            elif "progress" in b:
                if tid:  # 서버 재시작으로 등록이 날아갔어도 진행률이 오면 다시 살린다
                    tasks.setdefault(tid, {"id": tid, "title": str(b.get("title") or tid), "started": time.time()})
                    tasks[tid]["progress"] = max(0, min(100, int(b.get("progress") or 0)))
                    tasks[tid]["note"] = str(b.get("note") or "")[:80]
                    if b.get("eta_min") is not None:  # 일하는 쪽이 직접 낸 남은 시간 추정(분) → 끝날 시각으로 저장
                        tasks[tid]["eta_at"] = time.time() + float(b["eta_min"]) * 60
            elif tid:
                tasks[tid] = {"id": tid, "title": str(b.get("title") or tid), "started": time.time()}
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


def status_loop():
    """현황 칸: 한 바퀴 1분쯤(GitLab·git) 걸려 서버에서 따로 돈다. 감시꾼은 자주 깨고 다시 떠서 거기선 끝까지 못 돈다."""
    import status as status_board
    while True:
        try:
            b = status_board.collect()
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
    print("secretary on http://127.0.0.1:%d" % PORT, flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
