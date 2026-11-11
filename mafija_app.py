import hashlib, json, os, random, secrets, socket, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
_env = os.path.join(BASE, ".env.local")
if os.path.exists(_env):
    for _l in open(_env, encoding="utf-8"):
        _k, _s, _v = _l.strip().partition("=")
        if _s and not _k.startswith("#"):
            os.environ.setdefault(_k.strip(), _v.strip().strip("\"'"))

PASSWORD = os.environ.get("MAFIJA_PASSWORD", "MafijaMostar12")
PORT = int(os.environ.get("PORT", 8000))
DATABASE_URL = os.environ.get("DATABASE_URL")
DATA = os.path.join(BASE, "data.json")
LOCK = threading.Lock()
TOKEN = hashlib.sha256(("mafija-token:" + PASSWORD).encode()).hexdigest()  # stateless, preÅ¾ivi restart


def load(key, default):
    if DATABASE_URL:
        import psycopg2
        with psycopg2.connect(DATABASE_URL) as c, c.cursor() as cur:
            cur.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT)")
            cur.execute("SELECT value FROM kv WHERE key=%s", (key,))
            row = cur.fetchone()
            return json.loads(row[0]) if row else default
    if os.path.exists(DATA):
        with open(DATA, encoding="utf-8") as f:
            return json.load(f).get(key, default)
    return default


def save(key, val):
    if DATABASE_URL:
        import psycopg2
        with psycopg2.connect(DATABASE_URL) as c, c.cursor() as cur:
            cur.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT)")
            cur.execute("INSERT INTO kv VALUES (%s,%s) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
                        (key, json.dumps(val, ensure_ascii=False)))
        return
    d = {}
    if os.path.exists(DATA):
        with open(DATA, encoding="utf-8") as f:
            d = json.load(f)
    d[key] = val
    with open(DATA, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)


def save_players(p):
    save("players", p)


PLAYERS = load("players", [])
GAME = load("game", None)  # trenutna igra


def is_mafia(role):
    return role in ("mafia", "dama")


def public_game():
    if not GAME:
        return None
    g = {k: v for k, v in GAME.items() if k != "players"}
    hide = GAME["phase"] == "reveal"
    g["players"] = [
        {"id": p["id"], "name": p["name"], "alive": p["alive"], **({} if hide else {"role": p["role"]})}
        for p in GAME["players"]
    ]
    return g


def check_win():
    alive = [p for p in GAME["players"] if p["alive"]]
    m = sum(1 for p in alive if is_mafia(p["role"]))
    c = len(alive) - m
    winner = "mafia" if m >= c else ("city" if m == 0 else None)
    if not winner:
        return
    GAME["phase"] = "over"
    GAME["winner"] = winner
    for gp in GAME["players"]:
        pr = next((x for x in PLAYERS if x["id"] == gp["id"]), None)
        if not pr:
            continue
        pr["games"] = pr.get("games", 0) + 1
        if winner == "city" and not is_mafia(gp["role"]):
            pr["city_wins"] = pr.get("city_wins", 0) + 1
        if winner == "mafia" and is_mafia(gp["role"]):
            pr["mafia_wins"] = pr.get("mafia_wins", 0) + 1
    save_players(PLAYERS)


def find(pid):
    return next((p for p in GAME["players"] if p["id"] == pid), None)


def handle(path, body):
    global GAME
    if path == "/api/state":
        return {"players": PLAYERS, "game": public_game()}

    if path == "/api/add":
        name = (body.get("name") or "").strip()
        if not name:
            raise ValueError("Unesi ime.")
        if any(p["name"].lower() == name.lower() for p in PLAYERS):
            raise ValueError("IgraÄ veÄ‡ postoji.")
        PLAYERS.append({"id": secrets.token_hex(4), "name": name, "city_wins": 0, "mafia_wins": 0, "games": 0})
        save_players(PLAYERS)
        return {}

    if path == "/api/delete":
        PLAYERS[:] = [p for p in PLAYERS if p["id"] != body.get("id")]
        save_players(PLAYERS)
        return {}

    if path == "/api/start":
        ids = body.get("ids") or []
        mafia_n = int(body.get("mafia") or 1)
        chosen = [p for p in PLAYERS if p["id"] in ids]
        if mafia_n < 1:
            raise ValueError("Minimalno 1 obiÄna mafija.")
        if len(chosen) < mafia_n + 4:
            raise ValueError("Premalo igraÄa: treba bar %d (mafija + dama + doktor + policajac + 1 graÄ‘anin)." % (mafia_n + 4))
        roles = ["mafia"] * mafia_n + ["dama", "doctor", "police"]
        roles += ["citizen"] * (len(chosen) - len(roles))
        random.shuffle(roles)
        order = chosen[:]
        random.shuffle(order)
        GAME = {
            "phase": "reveal", "round": 1, "reveal_idx": 0, "winner": None,
            "silenced": None, "log": [],
            "players": [{"id": p["id"], "name": p["name"], "role": r, "alive": True} for p, r in zip(order, roles)],
        }
        return {}

    if not GAME:
        raise ValueError("Nema aktivne igre.")

    if path == "/api/reveal":
        p = GAME["players"][GAME["reveal_idx"]]
        return {"role": p["role"]}

    if path == "/api/reveal_next":
        GAME["reveal_idx"] += 1
        if GAME["reveal_idx"] >= len(GAME["players"]):
            GAME["phase"] = "leader"
        return {}

    if path == "/api/begin":
        GAME["phase"] = "night"
        return {}

    if path == "/api/check":
        t = find(body.get("target"))
        return {"mafia": bool(t and t["role"] == "mafia")}  # dama se prikazuje kao nije mafija

    if path == "/api/resolve":
        kill, silence, protect = body.get("kill"), body.get("silence"), body.get("protect")
        k = find(kill) if kill else None
        msg = []
        if k and kill != protect:
            k["alive"] = False
            msg.append("%s je ubijen/a tokom noÄ‡i." % k["name"])
        elif k:
            msg.append("Mafija je pokuÅ¡ala ubiti %s, ali ga je doktor spasio!" % k["name"])
        else:
            msg.append("Niko nije ubijen.")
        s = find(silence) if silence else None
        GAME["silenced"] = s["id"] if s and s["alive"] else None
        if GAME["silenced"]:
            msg.append("%s je uÅ¡utkan/a i ne moÅ¾e glasati danas." % s["name"])
        GAME["log"] = msg
        GAME["phase"] = "day"
        check_win()
        return {}

    if path == "/api/vote":
        t = find(body.get("target")) if body.get("target") else None
        if t:
            t["alive"] = False
            if t["role"] == "mafia":
                for p in GAME["players"]:
                    if p["role"] == "dama" and p["alive"]:
                        p["role"] = "mafia"
                        GAME["log"] = ["ObiÄna mafija je izglasana â€“ dama postaje obiÄna mafija!"]
                        break
                else:
                    GAME["log"] = []
            else:
                GAME["log"] = []
            GAME["log"].append("%s je izglasan/a i ispada (%s)." % (t["name"], {"mafia": "mafija", "dama": "dama", "doctor": "doktor", "police": "policajac", "citizen": "graÄ‘anin"}[t["role"]]))
        else:
            GAME["log"] = ["Niko nije izglasan."]
        GAME["silenced"] = None
        GAME["round"] += 1
        GAME["phase"] = "night"
        check_win()
        return {}

    if path == "/api/end":
        GAME = None
        return {}

    raise ValueError("Nepoznato.")


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def authed(self):
        c = self.headers.get("Cookie", "")
        return any(x.strip() == "t=" + TOKEN for x in c.split(";"))

    def send(self, code, data, ctype="application/json", extra=None):
        b = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/api/state":
            return self.api()
        if not p.startswith("/api/"):
            with open(os.path.join(BASE, "public", "index.html"), "rb") as f:
                return self.send(200, f.read(), "text/html")
        self.send(404, {"error": "404", "path": self.path})

    def do_POST(self):
        self.api()

    def api(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            body = {}
        if self.path == "/api/login":
            if secrets.compare_digest(str(body.get("password", "")), PASSWORD):
                return self.send(200, {}, extra={"Set-Cookie": "t=%s; HttpOnly; Path=/; SameSite=Strict; Max-Age=2592000" % TOKEN})
            return self.send(401, {"error": "PogreÅ¡na Å¡ifra."})
        if not self.authed():
            return self.send(401, {"error": "auth"})
        with LOCK:
            global GAME
            if DATABASE_URL:  # serverless: svaki zahtjev Äita svjeÅ¾e stanje
                PLAYERS[:] = load("players", [])
                GAME = load("game", None)
            try:
                res = handle(self.path, body)
                if self.path != "/api/state":
                    save("game", GAME)
                self.send(200, res)
            except Exception as e:
                self.send(400, {"error": str(e)})


if __name__ == "__main__":
    try:
        ip = socket.gethostbyname(socket.gethostname())
    except Exception:
        ip = "localhost"
    print("Mafija: http://localhost:%d  |  na telefonu: http://%s:%d" % (PORT, ip, PORT))
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
