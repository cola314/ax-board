"""Teams(new) 로컬 IndexedDB(LevelDB) .log 에서 메시지를 읽는다. 외부 의존성 없음.

LevelDB 로그 → put 값 → Chromium IDB 값(blink 헤더) → V8 직렬화 객체 순으로 푼다.
"""
import glob
import os
import struct

DEFAULT_DIR = os.path.expandvars(
    r"%LOCALAPPDATA%\Packages\MSTeams_8wekyb3d8bbwe\LocalCache\Microsoft\MSTeams"
    r"\EBWebView\WV2Profile_tfw\IndexedDB\https_teams.microsoft.com_0.indexeddb.leveldb")
BLOCK = 32768


def _varint(b, p):
    r = s = 0
    while True:
        x = b[p]
        p += 1
        r |= (x & 0x7F) << s
        s += 7
        if x < 0x80:
            return r, p


def latest_log(d=DEFAULT_DIR):
    logs = sorted(glob.glob(os.path.join(d, "*.log")))
    return logs[-1] if logs else None


def log_puts(path):
    """LevelDB .log 파일의 (key, value) put 목록. 파일은 공유 읽기로 통째로 읽는다."""
    with open(path, "rb") as f:
        data = f.read()
    recs, buf = [], b""
    for bs in range(0, len(data), BLOCK):
        blk = data[bs:bs + BLOCK]
        p = 0
        while p + 7 <= len(blk):
            ln, ty = struct.unpack_from("<HB", blk, p + 4)
            if ty == 0 and ln == 0:
                break
            frag = blk[p + 7:p + 7 + ln]
            p += 7 + ln
            if ty == 1:
                recs.append(frag)
            elif ty == 2:
                buf = frag
            elif ty == 3:
                buf += frag
            elif ty == 4:
                recs.append(buf + frag)
                buf = b""
    puts = []
    for r in recs:
        if len(r) < 12:
            continue
        cnt = struct.unpack_from("<I", r, 8)[0]
        p = 12
        try:
            for _ in range(cnt):
                t = r[p]
                p += 1
                kl, p = _varint(r, p)
                k = r[p:p + kl]
                p += kl
                if t == 1:
                    vl, p = _varint(r, p)
                    puts.append((k, r[p:p + vl]))
                    p += vl
        except IndexError:
            continue
    return puts


class _V8:
    def __init__(self, b, p):
        self.b, self.p = b, p
        self.refs = []

    def vint(self):
        v, self.p = _varint(self.b, self.p)
        return v

    def read(self):
        b = self.b
        while b[self.p] == 0:
            self.p += 1
        t = chr(b[self.p])
        self.p += 1
        if t == '"':
            n = self.vint()
            s = b[self.p:self.p + n].decode("latin-1")
            self.p += n
            return s
        if t == "c":
            n = self.vint()
            s = b[self.p:self.p + n].decode("utf-16-le", "replace")
            self.p += n
            return s
        if t == "S":
            n = self.vint()
            s = b[self.p:self.p + n].decode("utf-8", "replace")
            self.p += n
            return s
        if t == "D":
            v = struct.unpack_from("<d", b, self.p)[0]
            self.p += 8
            self.refs.append(v)
            return v
        if t == "N":
            v = struct.unpack_from("<d", b, self.p)[0]
            self.p += 8
            return v
        if t == "I":
            v = self.vint()
            return (v >> 1) ^ -(v & 1)
        if t == "U":
            return self.vint()
        if t == "Z":
            n = self.vint() >> 1
            v = int.from_bytes(b[self.p:self.p + n], "little")
            self.p += n
            return v
        if t == "T":
            return True
        if t == "F":
            return False
        if t in "_0-":
            return None
        if t == "^":
            return self.refs[self.vint()]
        if t == "o":
            obj = {}
            self.refs.append(obj)
            while chr(b[self.p]) != "{":
                k = self.read()
                obj[k] = self.read()
            self.p += 1
            self.vint()
            return obj
        if t == "A":
            n = self.vint()
            arr = []
            self.refs.append(arr)
            arr.extend(self.read() for _ in range(n))
            while chr(b[self.p]) != "$":
                self.read()
                self.read()
            self.p += 1
            self.vint()
            self.vint()
            return arr
        if t == "a":
            self.vint()
            arr = {}
            self.refs.append(arr)
            while chr(b[self.p]) != "@":
                k = self.read()
                arr[k] = self.read()
            self.p += 1
            self.vint()
            self.vint()
            return list(arr.values())
        raise ValueError("unsupported v8 tag %r at %d" % (t, self.p - 1))


def decode_value(v):
    """IDB 값에서 V8 객체를 꺼낸다. 압축·외부 blob 값 등 모르는 형식이면 None."""
    for ver in (b"\xff\x10o", b"\xff\x0fo", b"\xff\x0eo"):
        i = v.find(ver, 0, 256)
        if i >= 0:
            try:
                return _V8(v, i + 2).read()
            except (ValueError, IndexError, struct.error):
                return None
    return None


def messages(path):
    """.log 안의 메시지 객체(originalarrivaltime 가 있는 dict)를 순서대로."""
    out = []
    for _, v in log_puts(path):
        if b"originalarrivaltime" not in v:
            continue
        obj = decode_value(v)
        if isinstance(obj, dict) and "originalarrivaltime" in obj:
            out.append(obj)
    return out
