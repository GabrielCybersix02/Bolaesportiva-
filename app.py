"""FORT.BET - backend Flask + SQLite.  Rodar: python app.py   (producao: gunicorn app:app)"""
import os, json, time, sqlite3, threading, secrets
from datetime import datetime, timezone
import requests
from flask import Flask, g, jsonify, request, session, send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("FORTBET_DB", os.path.join(BASE, "fortbet.db"))
ODDS_KEY = os.environ.get("ODDS_API_KEY", "22afaffa1da56210d15163abfe249280")
ODDS_BASE = "https://api.the-odds-api.com/v4"
CACHE_MIN = int(os.environ.get("ODDS_CACHE_MINUTES", "15"))
COMISSAO = float(os.environ.get("COMISSAO_PCT", "10")) / 100   # % do cambista sobre apostas dos seus usuarios
GROUPS = {"futebol": "Soccer", "basquete": "Basketball", "tenis": "Tennis", "mma": "Mixed Martial Arts"}

app = Flask(__name__, static_folder="static", static_url_path="/static")
_kf = os.path.join(BASE, "secret.key")
if not os.path.exists(_kf):
    open(_kf, "w").write(secrets.token_hex(32))
app.secret_key = os.environ.get("SECRET_KEY") or open(_kf).read().strip()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=os.environ.get("HTTPS", "0") == "1", PERMANENT_SESSION_LIFETIME=86400 * 7)

# ---------------------------------------------------------------- banco
SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT, nome TEXT NOT NULL, login TEXT UNIQUE NOT NULL,
  pw TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN('admin','cambista','usuario')), saldo INTEGER NOT NULL DEFAULT 0,
  ativo INTEGER NOT NULL DEFAULT 1, criado_por INTEGER REFERENCES users(id), senha_padrao INTEGER DEFAULT 0, criado_em TEXT);
CREATE TABLE IF NOT EXISTS transactions(id INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT, tipo TEXT, de INTEGER, para INTEGER, valor INTEGER, descr TEXT);
CREATE TABLE IF NOT EXISTS tickets(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id), data TEXT,
  selecoes TEXT, odd REAL, valor INTEGER, retorno INTEGER, status TEXT DEFAULT 'aberto');
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, ts REAL, v TEXT);
CREATE TABLE IF NOT EXISTS logos(nome TEXT PRIMARY KEY, url TEXT);
"""
def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, isolation_level=None, timeout=15)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db
@app.teardown_appcontext
def _close(e):
    c = g.pop("db", None)
    if c: c.close()
def raw():
    c = sqlite3.connect(DB_PATH, isolation_level=None, timeout=15); c.row_factory = sqlite3.Row; return c
def now(): return datetime.now(timezone.utc).isoformat()
def init_db():
    c = raw(); c.executescript(SCHEMA)
    if not c.execute("SELECT 1 FROM users WHERE role='admin'").fetchone():
        c.execute("INSERT INTO users(nome,login,pw,role,senha_padrao,criado_em) VALUES('Administrador','admin',?, 'admin',1,?)",
                  (generate_password_hash("admin123"), now()))
    c.close()
init_db()

def tx(c, tipo, de, para, valor, descr):
    c.execute("INSERT INTO transactions(data,tipo,de,para,valor,descr) VALUES(?,?,?,?,?,?)", (now(), tipo, de, para, valor, descr))
def cents(v):
    try: return int(round(float(str(v).replace(",", ".")) * 100))
    except Exception: return 0

# ---------------------------------------------------------------- auth
def me():
    uid = session.get("uid")
    if not uid: return None
    u = db().execute("SELECT * FROM users WHERE id=? AND ativo=1", (uid,)).fetchone()
    return u
def need(*roles):
    def deco(f):
        def w(*a, **k):
            u = me()
            if not u: return jsonify(ok=False, msg="Sessão expirada. Faça login novamente."), 401
            if roles and u["role"] not in roles: return jsonify(ok=False, msg="Sem permissão."), 403
            return f(u, *a, **k)
        w.__name__ = f.__name__; return w
    return deco
def err(msg, code=400): return jsonify(ok=False, msg=msg), code
FAILS = {}
@app.post("/api/login")
def login():
    d = request.get_json(silent=True) or {}
    login_, senha = str(d.get("login", "")).strip().lower(), str(d.get("senha", ""))
    key = (request.remote_addr, login_); f = FAILS.get(key, (0, 0))
    if f[1] > time.time(): return err("Muitas tentativas. Aguarde 30 segundos.", 429)
    u = db().execute("SELECT * FROM users WHERE login=?", (login_,)).fetchone()
    if not u or not check_password_hash(u["pw"], senha):
        n = f[0] + 1; FAILS[key] = (0, time.time() + 30) if n >= 5 else (n, 0)
        return err("Login ou senha incorretos.", 401)
    if not u["ativo"]: return err("Conta bloqueada. Fale com quem cadastrou você.", 403)
    FAILS.pop(key, None); session.clear(); session.permanent = True; session["uid"] = u["id"]
    return jsonify(ok=True, id=u["id"], senhaPadrao=bool(u["senha_padrao"]))
@app.get("/api/whoami")
@need()
def whoami(u): return jsonify(ok=True, id=u["id"])
@app.post("/api/logout")
def logout(): session.clear(); return jsonify(ok=True)
@app.post("/api/change-password")
@need()
def change_pw(u):
    d = request.get_json(silent=True) or {}
    if not check_password_hash(u["pw"], str(d.get("atual", ""))): return err("Senha atual incorreta.")
    nova = str(d.get("nova", ""))
    if len(nova) < 4: return err("A nova senha precisa ter pelo menos 4 caracteres.")
    db().execute("UPDATE users SET pw=?, senha_padrao=0 WHERE id=?", (generate_password_hash(nova), u["id"]))
    return jsonify(ok=True)

# ---------------------------------------------------------------- estado (mesmo formato que o front usa)
def scope_ids(c, u):
    if u["role"] == "admin": return None
    if u["role"] == "cambista": return [u["id"]] + [r[0] for r in c.execute("SELECT id FROM users WHERE criado_por=?", (u["id"],))]
    return [u["id"]]
@app.get("/api/state")
@need()
def state(u):
    c = db(); ids = scope_ids(c, u)
    q = lambda base, col: (base, ()) if ids is None else (base + f" WHERE {col} IN ({','.join('?'*len(ids))})", tuple(ids))
    us = [dict(id=str(r["id"]), nome=r["nome"], login=r["login"], role=r["role"], saldo=r["saldo"] / 100, ativo=bool(r["ativo"]),
              criadoPor=str(r["criado_por"]) if r["criado_por"] else None, senhaPadrao=bool(r["senha_padrao"]))
          for r in c.execute(*q("SELECT * FROM users", "id"))]
    if ids is not None:
        tq = f"SELECT * FROM transactions WHERE de IN ({','.join('?'*len(ids))}) OR para IN ({','.join('?'*len(ids))})"; ta = tuple(ids) * 2
    else: tq, ta = "SELECT * FROM transactions", ()
    txs = [dict(id=str(r["id"]), data=r["data"], tipo=r["tipo"], de=str(r["de"]) if r["de"] else None, para=str(r["para"]) if r["para"] else None,
                valor=r["valor"] / 100, desc=r["descr"]) for r in c.execute(tq + " ORDER BY id DESC LIMIT 500", ta)]
    tks = [dict(id=str(r["id"]), userId=str(r["user_id"]), data=r["data"], selecoes=json.loads(r["selecoes"]), odd=r["odd"],
                valor=r["valor"] / 100, retorno=r["retorno"] / 100, status=r["status"])
           for r in c.execute(*q("SELECT * FROM tickets", "user_id"))][::-1][:300]
    com = 0
    if u["role"] == "cambista":
        com = c.execute("SELECT COALESCE(SUM(t.valor),0) FROM tickets t JOIN users x ON x.id=t.user_id WHERE x.criado_por=? AND t.status!='cancelado'", (u["id"],)).fetchone()[0] * COMISSAO / 100
    return jsonify(ok=True, users=us, transactions=txs, tickets=tks, comissao=round(com, 2))

# ---------------------------------------------------------------- contas / saldo
def make_user(c, role, nome, login_, senha, por):
    nome, login_ = nome.strip(), login_.strip().lower()
    if len(nome) < 2: return "Informe o nome."
    import re
    if not re.fullmatch(r"[a-z0-9._-]{3,20}", login_): return "Login: 3 a 20 caracteres (letras, números, . - _)."
    if len(senha) < 4: return "A senha precisa ter pelo menos 4 caracteres."
    if c.execute("SELECT 1 FROM users WHERE login=?", (login_,)).fetchone(): return "Esse login já existe."
    c.execute("INSERT INTO users(nome,login,pw,role,criado_por,criado_em) VALUES(?,?,?,?,?,?)", (nome, login_, generate_password_hash(senha), role, por, now()))
@app.post("/api/cambistas")
@need("admin")
def new_cambista(u):
    d = request.get_json(silent=True) or {}
    m = make_user(db(), "cambista", str(d.get("nome", "")), str(d.get("login", "")), str(d.get("senha", "")), u["id"])
    return err(m) if m else jsonify(ok=True)
@app.post("/api/usuarios")
@need("cambista")
def new_usuario(u):
    d = request.get_json(silent=True) or {}
    m = make_user(db(), "usuario", str(d.get("nome", "")), str(d.get("login", "")), str(d.get("senha", "")), u["id"])
    return err(m) if m else jsonify(ok=True)
@app.post("/api/saldo")
@need("admin", "cambista")
def add_saldo(u):
    d = request.get_json(silent=True) or {}; c = db(); v = cents(d.get("valor"))
    if v <= 0: return err("Informe um valor válido.")
    c.execute("BEGIN IMMEDIATE")
    try:
        t = c.execute("SELECT * FROM users WHERE id=? AND ativo=1", (d.get("alvo"),)).fetchone()
        alvo_role = "cambista" if u["role"] == "admin" else "usuario"
        if not t or t["role"] != alvo_role or (u["role"] == "cambista" and t["criado_por"] != u["id"]):
            c.execute("ROLLBACK"); return err("Selecione uma conta válida.")
        if u["role"] == "cambista":   # ADM tem saldo ilimitado; cambista usa o proprio saldo
            cur = c.execute("SELECT saldo FROM users WHERE id=?", (u["id"],)).fetchone()[0]
            if v > cur: c.execute("ROLLBACK"); return err("Seu saldo é insuficiente. Peça recarga ao ADM.")
            c.execute("UPDATE users SET saldo=saldo-? WHERE id=?", (v, u["id"]))
        c.execute("UPDATE users SET saldo=saldo+? WHERE id=?", (v, t["id"]))
        tx(c, "credito", u["id"], t["id"], v, ("Saldo adicionado a " if u["role"] == "admin" else "Recarga para ") + t["nome"])
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK"); raise
    return jsonify(ok=True, nome=t["nome"])
def can_manage(u, t):
    return t and ((u["role"] == "admin" and t["role"] == "cambista") or (u["role"] == "cambista" and t["role"] == "usuario" and t["criado_por"] == u["id"]))
@app.post("/api/toggle/<int:uid>")
@need("admin", "cambista")
def toggle(u, uid):
    t = db().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not can_manage(u, t): return err("Sem permissão.", 403)
    db().execute("UPDATE users SET ativo=1-ativo WHERE id=?", (uid,)); return jsonify(ok=True)
@app.post("/api/reset-senha")
@need("admin", "cambista")
def reset_senha(u):
    d = request.get_json(silent=True) or {}; t = db().execute("SELECT * FROM users WHERE id=?", (d.get("alvo"),)).fetchone()
    if not can_manage(u, t): return err("Selecione uma conta válida.")
    n = str(d.get("nova", ""))
    if len(n) < 4: return err("A senha precisa ter pelo menos 4 caracteres.")
    db().execute("UPDATE users SET pw=? WHERE id=?", (generate_password_hash(n), t["id"])); return jsonify(ok=True)

# ---------------------------------------------------------------- odds (API protegida no servidor) + logos
def kv_get(k):
    r = raw().execute("SELECT ts,v FROM kv WHERE k=?", (k,)).fetchone(); return (r["ts"], json.loads(r["v"])) if r else (0, None)
def kv_set(k, v):
    c = raw(); c.execute("REPLACE INTO kv VALUES(?,?,?)", (k, time.time(), json.dumps(v))); c.close()
def fetch_group(grupo):
    sports = requests.get(f"{ODDS_BASE}/sports/", params={"apiKey": ODDS_KEY}, timeout=20)
    if sports.status_code == 401: raise RuntimeError("Chave da API de odds inválida.")
    if sports.status_code == 429: raise RuntimeError("Limite de requisições da API excedido.")
    sports.raise_for_status()
    lst = [s for s in sports.json() if s["group"] == GROUPS[grupo] and s["active"] and not s.get("has_outrights")]
    out = []
    for s in lst:
        r = requests.get(f"{ODDS_BASE}/sports/{s['key']}/odds/", timeout=20,
                         params={"apiKey": ODDS_KEY, "regions": "eu", "markets": "h2h", "oddsFormat": "decimal", "dateFormat": "iso"})
        if not r.ok: continue
        for ev in r.json():
            bm = next((b for b in ev.get("bookmakers", []) if any(m["key"] == "h2h" for m in b["markets"])), None)
            if not bm: continue
            mk = next(m for m in bm["markets"] if m["key"] == "h2h")
            o = {x["name"]: x["price"] for x in mk["outcomes"]}
            if ev["home_team"] not in o or ev["away_team"] not in o: continue
            out.append(dict(id=ev["id"], home=ev["home_team"], away=ev["away_team"], commence=ev["commence_time"],
                            league=ev["sport_title"], key=ev["sport_key"], grupo=grupo,
                            odds={"1": o[ev["home_team"]], "X": o.get("Draw"), "2": o[ev["away_team"]]}))
    if not out and not lst: raise RuntimeError("Nenhum campeonato ativo no momento.")
    return out
def get_group(grupo, force=False):
    ts, v = kv_get("odds:" + grupo)
    if v is not None and not force and time.time() - ts < CACHE_MIN * 60: return v, ts
    try:
        v = fetch_group(grupo); kv_set("odds:" + grupo, v); return v, time.time()
    except Exception:
        if v is not None: return v, ts      # usa cache antigo se a API falhar
        raise
def find_event(eid):
    for g_ in GROUPS:
        _, v = kv_get("odds:" + g_)
        for e in v or []:
            if e["id"] == eid: return e
@app.get("/api/odds")
@need()
def odds(u):
    grupo = request.args.get("grupo", "futebol"); force = request.args.get("force") == "1"
    try:
        ev, ts = get_group("futebol" if grupo == "aovivo" else grupo, force)
    except Exception as e:
        return jsonify(ok=False, msg=str(e)), 502
    t = time.time()
    def started(e): return datetime.fromisoformat(e["commence"].replace("Z", "+00:00")).timestamp() <= t
    if grupo == "aovivo":
        ev = [e for e in ev if started(e) and t - datetime.fromisoformat(e["commence"].replace("Z", "+00:00")).timestamp() < 3 * 3600
              or 0 < datetime.fromisoformat(e["commence"].replace("Z", "+00:00")).timestamp() - t < 3600]
    for e in ev: e["started"] = started(e)
    return jsonify(ok=True, events=ev, updated=ts, logos=logo_map({n for e in ev for n in (e["home"], e["away"])}))
_pending, _plock = set(), threading.Lock()
def logo_map(names):
    c = raw(); res = {}
    for n in names:
        r = c.execute("SELECT url FROM logos WHERE nome=?", (n,)).fetchone()
        if r is None:
            with _plock: _pending.add(n)
        elif r["url"]: res[n] = r["url"]
    c.close(); return res
def logo_worker():
    while True:
        with _plock: n = _pending.pop() if _pending else None
        if not n: time.sleep(2); continue
        url = ""
        try:
            r = requests.get("https://www.thesportsdb.com/api/v1/json/3/searchteams.php", params={"t": n}, timeout=10)
            if r.status_code == 429: 
                with _plock: _pending.add(n)
                time.sleep(30); continue
            teams = (r.json() or {}).get("teams") or []
            url = (teams[0].get("strBadge") or teams[0].get("strTeamBadge") or "") if teams else ""
        except Exception: pass
        c = raw(); c.execute("REPLACE INTO logos VALUES(?,?)", (n, url)); c.close(); time.sleep(2.2)
@app.post("/api/logos")
@need()
def logos():
    names = (request.get_json(silent=True) or {}).get("names", [])[:300]
    return jsonify(ok=True, logos=logo_map({str(n) for n in names}))
threading.Thread(target=logo_worker, daemon=True).start()

# ---------------------------------------------------------------- apostas
@app.post("/api/bets")
@need("usuario")
def bet(u):
    d = request.get_json(silent=True) or {}; sel = d.get("selecoes") or []; stake = cents(d.get("valor"))
    if not sel: return err("Adicione pelo menos uma seleção.")
    if stake < 100: return err("Aposta mínima: R$ 1,00.")
    odd, final, seen = 1.0, [], set()
    for s in sel:
        ev = find_event(str(s.get("matchId")))
        if not ev: return err("Jogo não encontrado. Atualize a lista de jogos.")
        if ev["id"] in seen: return err("Somente uma seleção por jogo.")
        seen.add(ev["id"])
        if datetime.fromisoformat(ev["commence"].replace("Z", "+00:00")).timestamp() <= time.time():
            return err(f"Apostas encerradas: {ev['home']} x {ev['away']}.")
        o = ev["odds"].get(str(s.get("market")))
        if not o: return err("Mercado indisponível.")
        odd *= o; final.append(dict(jogo=f"{ev['home']} x {ev['away']}", mercado=s["market"], odd=o))   # odd vem do servidor
    odd = round(odd, 2); c = db(); c.execute("BEGIN IMMEDIATE")
    try:
        if stake > c.execute("SELECT saldo FROM users WHERE id=?", (u["id"],)).fetchone()[0]:
            c.execute("ROLLBACK"); return err("Saldo insuficiente. Peça uma recarga ao seu cambista.")
        c.execute("UPDATE users SET saldo=saldo-? WHERE id=?", (stake, u["id"]))
        c.execute("INSERT INTO tickets(user_id,data,selecoes,odd,valor,retorno) VALUES(?,?,?,?,?,?)",
                  (u["id"], now(), json.dumps(final), odd, stake, int(round(stake * odd))))
        tx(c, "aposta", u["id"], None, stake, f"Aposta ({len(final)} seleç{'ões' if len(final) > 1 else 'ão'}) odd {odd:.2f}")
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK"); raise
    return jsonify(ok=True, retorno=stake * odd / 100)
@app.post("/api/tickets/<int:tid>/settle")
@need("admin", "cambista")
def settle(u, tid):
    res = (request.get_json(silent=True) or {}).get("resultado"); c = db()
    if res not in ("ganhou", "perdeu", "cancelado"): return err("Resultado inválido.")
    c.execute("BEGIN IMMEDIATE")
    try:
        t = c.execute("SELECT t.*, x.criado_por, x.nome FROM tickets t JOIN users x ON x.id=t.user_id WHERE t.id=?", (tid,)).fetchone()
        if not t or t["status"] != "aberto" or (u["role"] == "cambista" and t["criado_por"] != u["id"]):
            c.execute("ROLLBACK"); return err("Bilhete não encontrado ou já encerrado.")
        c.execute("UPDATE tickets SET status=? WHERE id=?", (res, tid))
        if res == "ganhou":
            c.execute("UPDATE users SET saldo=saldo+? WHERE id=?", (t["retorno"], t["user_id"])); tx(c, "premio", None, t["user_id"], t["retorno"], f"Prêmio bilhete #{tid}")
        elif res == "cancelado":
            c.execute("UPDATE users SET saldo=saldo+? WHERE id=?", (t["valor"], t["user_id"])); tx(c, "estorno", None, t["user_id"], t["valor"], f"Estorno bilhete #{tid}")
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK"); raise
    return jsonify(ok=True)

# ---------------------------------------------------------------- extras
@app.post("/api/admin/seed")
@need("admin")
def seed(u):
    c = db(); n = 0
    for nome, lg, sal, us in [("Cambista Teste 1", "cambista1", 500, [("Usuário Teste 1", "teste1", 100), ("Usuário Teste 2", "teste2", 50)]), ("Cambista Teste 2", "cambista2", 200, [("Usuário Teste 3", "teste3", 0)])]:
        cam = c.execute("SELECT * FROM users WHERE login=?", (lg,)).fetchone()
        if not cam:
            make_user(c, "cambista", nome, lg, "1234", u["id"]); cam = c.execute("SELECT * FROM users WHERE login=?", (lg,)).fetchone(); n += 1
            c.execute("UPDATE users SET saldo=? WHERE id=?", (sal * 100, cam["id"])); tx(c, "credito", u["id"], cam["id"], sal * 100, "Saldo adicionado a " + nome)
        for un, ul, us_ in us:
            if c.execute("SELECT 1 FROM users WHERE login=?", (ul,)).fetchone(): continue
            make_user(c, "usuario", un, ul, "1234", cam["id"]); n += 1; r = c.execute("SELECT id FROM users WHERE login=?", (ul,)).fetchone()[0]
            if us_:
                c.execute("UPDATE users SET saldo=saldo-? WHERE id=?", (us_ * 100, cam["id"])); c.execute("UPDATE users SET saldo=? WHERE id=?", (us_ * 100, r)); tx(c, "credito", cam["id"], r, us_ * 100, "Recarga para " + un)
    return jsonify(ok=True, criados=n)
@app.post("/api/admin/reset")
@need("admin")
def reset(u):
    c = db(); c.execute("BEGIN IMMEDIATE")
    for t in ("transactions", "tickets"): c.execute(f"DELETE FROM {t}")
    c.execute("DELETE FROM users WHERE role!='admin'"); c.execute("UPDATE users SET pw=?, senha_padrao=1 WHERE id=?", (generate_password_hash("admin123"), u["id"])); c.execute("COMMIT")
    session.clear(); return jsonify(ok=True)
@app.get("/api/relatorios")
@need("admin", "cambista")
def relatorios(u):
    c = db(); ids = [r[0] for r in c.execute("SELECT id FROM users WHERE role='usuario'" + ("" if u["role"] == "admin" else " AND criado_por=?"), () if u["role"] == "admin" else (u["id"],))]
    ph = ",".join("?" * len(ids)) or "NULL"; day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row = lambda sql: c.execute(sql.format(ph), ids).fetchone()[0] or 0
    tot = row("SELECT SUM(valor) FROM tickets WHERE user_id IN ({}) AND status!='cancelado'")
    return jsonify(ok=True, hoje=row("SELECT COUNT(*) FROM tickets WHERE user_id IN ({}) AND data LIKE '" + day + "%'"),
                   bruto=tot / 100, premios=row("SELECT SUM(retorno) FROM tickets WHERE user_id IN ({}) AND status='ganhou'") / 100,
                   abertos=row("SELECT COUNT(*) FROM tickets WHERE user_id IN ({}) AND status='aberto'"),
                   comissao=round(tot * COMISSAO / 100, 2) if u["role"] == "cambista" else None)

@app.get("/")
def index(): return send_from_directory(os.path.join(BASE, "static"), "index.html")
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
