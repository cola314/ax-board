"""비서 설정 로더. 사람·회사마다 다른 값(이름, GitLab 호스트, Gmail 창 제목 등)은 전부 여기로.

설정 파일: 환경변수 SECRETARY_CONFIG(경로) > 이 폴더의 secretary.config.json. 없으면 기본값(기능 전부 꺼짐).
기본값 위에 파일 내용을 덮어쓴다(빠진 항목은 기본값). 형식은 secretary.config.example.json 참고.

  python -I scripts/secretary/config.py get gitlab.host      # 값 하나 출력(미나가 규칙 수행 중 확인용)
  python -I scripts/secretary/config.py flex-js              # flex_week.js 에 user_key 를 넣어 출력(javascript_tool 로 실행)
"""
import copy
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.environ.get("SECRETARY_CONFIG") or os.path.join(HERE, "secretary.config.json")

DEFAULTS = {
    "me": {"name": "", "aliases": []},             # Teams 표시 이름(내 메시지 거르기) + 깨울 호칭
    "port": 8790,
    "root": "",                                    # 상대 경로 기준. 비우면 scripts/ 의 부모(레포 루트)
    "features": {"teams": False, "gmail": False, "gitlab": False, "calendar": False, "flex": False, "status": False,
                 "review_check": True},                    # 정기 점검(근무일 review_check.times 에 미나를 깨움)
    "gitlab": {"host": "gitlab.com", "username": "", "cli": "glab", "branch_prefix": ""},
    "gmail": {"window_title_regex": "", "chrome_path": r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              "chrome_profile": "Default", "url": "https://mail.google.com/mail/u/0/#inbox"},
    "repos": {"sources_dir": "sources"},
    "todo": {"file": "TODO.md", "section": "🔥 진행 중"},
    "flex": {"user_key": ""},
    "review_check": {"times": ["11:30", "17:00"]},  # 정기 점검 시각(로컬, HH:MM). 늦게 떠도 마지막 +2시간까지는 한 번
    # 같은 일감 소식 묶음 키 '프로젝트#번호'. 정규식 그룹 1=프로젝트, 2=번호. 순서대로 먼저 맞는 것
    "news": {"key_patterns": [r"/([\w.-]+)/-/(?:work_items|issues)/(\d+)",   # GitLab 일감 URL
                              r"^(?:Re: )?([\w.-]+) \|.*\(#(\d+)\)"],         # GitLab 알림 메일 "proj | 제목 (#249)"
             "default_project": ""},  # "#249 ..." 처럼 번호만 오면 이 프로젝트로. 비우면 묶지 않음
    # 대시보드 화면용(공개해도 되는 값만, GET /api/config). 빠진 값은 dashboard.html 기본값
    "dashboard": {},
    # 대시보드 '📜 규칙' 보기가 읽을 운영 규칙 마크다운(ROOT 기준 상대 경로). 비우면 규칙 보기 없음
    "rules": {"file": ""},
}


def _merge(base, over):
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def load(path=PATH):
    cfg = copy.deepcopy(DEFAULTS)
    try:
        with open(path, encoding="utf-8") as f:
            _merge(cfg, json.load(f))
    except OSError:
        pass
    return cfg


CFG = load()
ROOT = os.path.normpath(os.path.join(os.path.dirname(PATH), CFG["root"]) if CFG["root"]
                        else os.path.join(HERE, "..", ".."))


def get(dotted, default=None):
    cur = CFG
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def on(feature):
    """features 에서 켜진 기능인가."""
    return bool(CFG["features"].get(feature))


def path(rel):
    """config 의 상대 경로를 ROOT 기준 절대 경로로."""
    return os.path.normpath(os.path.join(ROOT, rel))


def port():
    return int(os.environ.get("SECRETARY_PORT") or CFG["port"])


def base():
    return "http://127.0.0.1:%d" % port()


def me_name():
    return os.environ.get("TEAMS_ME_NAME") or CFG["me"]["name"]


def wake_names():
    """Teams 그룹방 본문에 이 이름이 있으면 바로 깨운다."""
    return tuple(n for n in [me_name()] + list(CFG["me"].get("aliases") or []) if n)


def state_file():
    """감시꾼 state(Teams 본 메시지 id·메일·GitLab 기준점). teams_watch.py 와 wait.py 가 같이 쓴다."""
    return os.path.join(ROOT, "artifacts", "teams-watch", "state.json")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    if sys.argv[1:2] == ["get"]:
        v = get(sys.argv[2])
        print(v if isinstance(v, str) else json.dumps(v, ensure_ascii=False))
    elif sys.argv[1:] == ["flex-js"]:
        with open(os.path.join(HERE, "flex_week.js"), encoding="utf-8") as f:
            print(f.read().replace("__FLEX_USER_KEY__", CFG["flex"]["user_key"]))
    else:
        print(json.dumps(CFG, ensure_ascii=False, indent=2))
