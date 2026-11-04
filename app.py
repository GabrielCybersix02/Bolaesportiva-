"""Bola Esportiva - backend Flask + Supabase (Postgres).  Rodar: python app.py   (producao: gunicorn app:app)"""
import os, re, json, time, queue, threading, secrets, unicodedata
from concurrent.futures import ThreadPoolExecutor
import psycopg2, psycopg2.extras
from datetime import datetime, timezone
import requests
from flask import Flask, g, jsonify, has_request_context, request, session, send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix

BASE = os.path.dirname(os.path.abspath(__file__))
DATABASE_URL = os.environ.get("DATABASE_URL", "")   # string de conexao do Supabase
ODDS_KEY = os.environ.get("ODDS_API_KEY", "")        # NUNCA coloque a chave no codigo: defina ODDS_API_KEY no Render
ODDS_BASE = "https://api.the-odds-api.com/v4"
CACHE_MIN = int(os.environ.get("ODDS_CACHE_MINUTES", "15"))
COMISSAO = float(os.environ.get("COMISSAO_PCT", "10")) / 100   # % do cambista sobre apostas dos seus usuarios
GROUPS = {"futebol": "Soccer", "basquete": "Basketball", "tenis": "Tennis", "mma": "Mixed Martial Arts"}

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1)      # Render fica atras de proxy: pega o IP real (limite de tentativas de login)
_kf = os.path.join(BASE, "secret.key")
if not os.path.exists(_kf):
    open(_kf, "w").write(secrets.token_hex(32))
app.secret_key = os.environ.get("SECRET_KEY") or open(_kf).read().strip()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=os.environ.get("HTTPS", "0") == "1", PERMANENT_SESSION_LIFETIME=86400 * 7)

# ---------------------------------------------------------------- banco
SCHEMA = [
"""CREATE TABLE IF NOT EXISTS users(id SERIAL PRIMARY KEY, nome TEXT NOT NULL, login TEXT UNIQUE NOT NULL,
  pw TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN('admin','cambista','usuario')), saldo BIGINT NOT NULL DEFAULT 0,
  ativo INTEGER NOT NULL DEFAULT 1, criado_por INTEGER REFERENCES users(id), senha_padrao INTEGER DEFAULT 0, criado_em TEXT)""",
"""CREATE TABLE IF NOT EXISTS transactions(id SERIAL PRIMARY KEY, data TEXT, tipo TEXT, de INTEGER, para INTEGER, valor BIGINT, descr TEXT)""",
"""CREATE TABLE IF NOT EXISTS tickets(id SERIAL PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), data TEXT,
  selecoes TEXT, odd DOUBLE PRECISION, valor BIGINT, retorno BIGINT, status TEXT DEFAULT 'aberto')""",
"CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, ts DOUBLE PRECISION, v TEXT)",
"CREATE TABLE IF NOT EXISTS logos(nome TEXT PRIMARY KEY, url TEXT)"]
class Conn:
    """Embrulho fino do psycopg2: usa '?' nos SQLs e devolve o cursor. As conexoes ficam num pool e sao reaproveitadas."""
    def __init__(self):
        self.c, self.tx = None, False
        self._open()
    def _open(self):
        self.c = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.DictCursor, connect_timeout=15,
                                  keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=3,
                                  **({} if "sslmode" in DATABASE_URL else {"sslmode": "require"}))
        self.c.autocommit = True
    def execute(self, sql, p=()):
        head = sql.lstrip()[:8].upper()
        for attempt in (0, 1):
            try:
                cur = self.c.cursor(); cur.execute(sql.replace("?", "%s"), tuple(p))
                if head.startswith("BEGIN"): self.tx = True
                elif head.startswith(("COMMIT", "ROLLBACK")): self.tx = False
                return cur
            except (psycopg2.OperationalError, psycopg2.InterfaceError):
                if attempt or self.tx: raise          # conexao morta (o pooler do Supabase derruba as ociosas): reconecta 1 vez
                try: self.c.close()
                except Exception: pass
                self._open()
    def close(self): POOL.put(self)                   # devolve ao pool em vez de fechar
class Pool:
    def __init__(self, size):
        self.free, self.sem = queue.LifoQueue(), threading.BoundedSemaphore(size)
    def get(self):
        if not self.sem.acquire(timeout=25): raise RuntimeError("Banco ocupado. Tente de novo em instantes.")
        try:
            try: return self.free.get_nowait()
            except queue.Empty: return Conn()
        except Exception:
            self.sem.release(); raise
    def put(self, c):
        try:
            if c.tx or c.c.get_transaction_status() != psycopg2.extensions.TRANSACTION_STATUS_IDLE:
                c.c.rollback(); c.tx = False
            if c.c.closed: raise psycopg2.InterfaceError("fechada")
            self.free.put(c)
        except Exception:
            try: c.c.close()
            except Exception: pass
        finally: self.sem.release()
POOL = Pool(int(os.environ.get("DB_POOL", "6")))
def db():
    if "db" not in g: g.db = POOL.get()
    return g.db
@app.teardown_appcontext
def _close(e):
    c = g.pop("db", None)
    if c: POOL.put(c)
def raw(): return POOL.get()                          # fora de requisicao (threads de fundo): .close() devolve ao pool
def now(): return datetime.now(timezone.utc).isoformat()
def init_db():
    if not DATABASE_URL: raise SystemExit("Defina a variavel DATABASE_URL com a string de conexao do Supabase.")
    c = raw()
    for q in SCHEMA: c.execute(q)
    try: c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS telefone TEXT")
    except Exception: pass
    c.execute("ALTER TABLE logos ADD COLUMN IF NOT EXISTS ts DOUBLE PRECISION DEFAULT 0")
    c.execute("INSERT INTO users(nome,login,pw,role,senha_padrao,criado_em) SELECT 'Administrador','admin',?,'admin',1,? "
              "WHERE NOT EXISTS (SELECT 1 FROM users WHERE role='admin') ON CONFLICT (login) DO NOTHING",
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
              criadoPor=str(r["criado_por"]) if r["criado_por"] else None, senhaPadrao=bool(r["senha_padrao"]), telefone=r["telefone"] or "")
          for r in c.execute(*q("SELECT * FROM users", "id"))]
    if ids is not None:
        tq = f"SELECT * FROM transactions WHERE de IN ({','.join('?'*len(ids))}) OR para IN ({','.join('?'*len(ids))})"; ta = tuple(ids) * 2
    else: tq, ta = "SELECT * FROM transactions", ()
    txs = [dict(id=str(r["id"]), data=r["data"], tipo=r["tipo"], de=str(r["de"]) if r["de"] else None, para=str(r["para"]) if r["para"] else None,
                valor=r["valor"] / 100, desc=r["descr"]) for r in c.execute(tq + " ORDER BY id DESC LIMIT 500", ta)]
    tsql, targs = q("SELECT * FROM tickets", "user_id")
    tks = [dict(id=str(r["id"]), userId=str(r["user_id"]), data=r["data"], selecoes=json.loads(r["selecoes"]), odd=r["odd"],
                valor=r["valor"] / 100, retorno=r["retorno"] / 100, status=r["status"])
           for r in c.execute(tsql + " ORDER BY id DESC LIMIT 300", targs)]
    com = 0
    if u["role"] == "cambista":
        com = float(c.execute("SELECT COALESCE(SUM(t.valor),0) FROM tickets t JOIN users x ON x.id=t.user_id WHERE x.criado_por=? AND t.status!='cancelado'", (u["id"],)).fetchone()[0]) * COMISSAO / 100
    cam = None
    if u["role"] == "usuario" and u["criado_por"]:
        r = c.execute("SELECT nome, telefone FROM users WHERE id=?", (u["criado_por"],)).fetchone()
        if r: cam = dict(nome=r["nome"], telefone=r["telefone"] or "")
    return jsonify(ok=True, me=str(u["id"]), users=us, transactions=txs, tickets=tks, comissao=round(com, 2), cambista=cam)

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
    c.execute("BEGIN")
    try:
        t = c.execute("SELECT * FROM users WHERE id=? AND ativo=1 FOR UPDATE", (d.get("alvo"),)).fetchone()
        alvo_role = "cambista" if u["role"] == "admin" else "usuario"
        if not t or t["role"] != alvo_role or (u["role"] == "cambista" and t["criado_por"] != u["id"]):
            c.execute("ROLLBACK"); return err("Selecione uma conta válida.")
        if u["role"] == "cambista":   # ADM tem saldo ilimitado; cambista usa o proprio saldo
            cur = c.execute("SELECT saldo FROM users WHERE id=? FOR UPDATE", (u["id"],)).fetchone()[0]
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
@app.post("/api/telefone")
@need("cambista")
def telefone(u):
    d = "".join(ch for ch in str((request.get_json(silent=True) or {}).get("telefone", "")) if ch.isdigit())
    if len(d) in (10, 11): d = "55" + d
    if d and not 12 <= len(d) <= 15: return err("Número inválido. Use DDD + número, ex: 11 91234-5678.")
    db().execute("UPDATE users SET telefone=? WHERE id=?", (d, u["id"])); return jsonify(ok=True)
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
FORCE_MIN = int(os.environ.get("ODDS_FORCE_MIN_SECONDS", "60"))   # botao "Atualizar" nao gasta creditos se o cache tem menos que isso
IDLE_H = float(os.environ.get("ODDS_IDLE_HOURS", "1"))            # renova sozinho so enquanto alguem usou esse esporte nesta janela
_odds, _seen, _btry = {}, {}, {}                                   # cache em memoria: esporte -> (ts, eventos)
_glocks = {g_: threading.Lock() for g_ in GROUPS}

def run_db(fn):
    """Roda fn(conexao) usando a conexao da requisicao, ou uma do pool quando estamos numa thread de fundo."""
    if has_request_context(): return fn(db())
    c = raw()
    try: return fn(c)
    finally: c.close()
def kv_get(k):
    def f(c):
        r = c.execute("SELECT ts,v FROM kv WHERE k=?", (k,)).fetchone(); return (r["ts"], json.loads(r["v"])) if r else (0, None)
    return run_db(f)
def kv_set(k, v):
    run_db(lambda c: c.execute("INSERT INTO kv VALUES(?,?,?) ON CONFLICT (k) DO UPDATE SET ts=EXCLUDED.ts, v=EXCLUDED.v", (k, time.time(), json.dumps(v))))

def parse_events(data, key, grupo):
    out = []
    for ev in data:
        bm = next((b for b in ev.get("bookmakers", []) if any(m["key"] == "h2h" for m in b["markets"])), None)
        if not bm: continue
        mk = next(m for m in bm["markets"] if m["key"] == "h2h")
        o = {x["name"]: x["price"] for x in mk["outcomes"]}
        if ev["home_team"] not in o or ev["away_team"] not in o: continue
        out.append(dict(id=ev["id"], home=ev["home_team"], away=ev["away_team"], commence=ev["commence_time"],
                        league=ev["sport_title"], key=ev.get("sport_key", key), grupo=grupo,
                        odds={"1": o[ev["home_team"]], "X": o.get("Draw"), "2": o[ev["away_team"]]}))
    return out
def fetch_group(grupo):
    if not ODDS_KEY: raise RuntimeError("Chave da API de odds não configurada (defina ODDS_API_KEY no Render).")
    sports = requests.get(f"{ODDS_BASE}/sports/", params={"apiKey": ODDS_KEY}, timeout=20)
    if sports.status_code == 401: raise RuntimeError("Chave da API de odds inválida.")
    if sports.status_code == 429: raise RuntimeError("Limite de requisições da API excedido.")
    sports.raise_for_status()
    lst = [s for s in sports.json() if s["group"] == GROUPS[grupo] and s["active"] and not s.get("has_outrights")]
    def one(s):                                    # antes: um campeonato por vez (30+ chamadas em fila). Agora em paralelo.
        try:
            r = requests.get(f"{ODDS_BASE}/sports/{s['key']}/odds/", timeout=20,
                             params={"apiKey": ODDS_KEY, "regions": "eu", "markets": "h2h", "oddsFormat": "decimal", "dateFormat": "iso"})
            return parse_events(r.json(), s["key"], grupo) if r.ok else None
        except Exception:
            return None
    with ThreadPoolExecutor(max_workers=8) as ex: parts = list(ex.map(one, lst))
    out = [e for p in parts if p for e in p]
    if lst and all(p is None for p in parts): raise RuntimeError("API de odds indisponível no momento.")   # mantem o cache antigo
    if not out and not lst: raise RuntimeError("Nenhum campeonato ativo no momento.")
    return out

def cached(grupo):
    m = _odds.get(grupo)
    if m is None:
        ts, v = kv_get("odds:" + grupo); m = _odds[grupo] = (ts, v)
    return m
def refresh(grupo):
    v = fetch_group(grupo); ts = time.time(); _odds[grupo] = (ts, v)
    try: kv_set("odds:" + grupo, v)
    except Exception as e: print("kv_set odds falhou:", e, flush=True)
    return v, ts
def bg_refresh(grupo):
    lk = _glocks[grupo]
    if not lk.acquire(blocking=False): return
    try: refresh(grupo)
    except Exception as e: print("refresh odds falhou:", grupo, e, flush=True)
    finally: lk.release()
def get_group(grupo, force=False):
    """Responde na hora com o cache (mesmo velho) e renova por tras. So espera a API quando nunca houve cache."""
    _seen[grupo] = time.time()
    ts, v = cached(grupo); age = time.time() - ts
    if v is not None and (not force or age < FORCE_MIN):
        if age >= CACHE_MIN * 60 and time.time() - _btry.get(grupo, 0) > 60:
            _btry[grupo] = time.time(); threading.Thread(target=bg_refresh, args=(grupo,), daemon=True).start()
        return v, ts
    with _glocks[grupo]:
        ts2, v2 = cached(grupo)
        if v2 is not None and time.time() - ts2 < FORCE_MIN: return v2, ts2      # outra requisicao acabou de renovar
        try: return refresh(grupo)
        except Exception:
            if v2 is not None: return v2, ts2
            raise
def odds_loop():
    time.sleep(3)
    try:                                           # aquece o futebol assim que o servidor sobe (antes do 1o login)
        ts, v = cached("futebol")
        if v is None or time.time() - ts >= CACHE_MIN * 60: _seen["futebol"] = time.time(); bg_refresh("futebol")
    except Exception as e: print("aquecimento falhou:", e, flush=True)
    while True:
        time.sleep(60)
        for g_ in GROUPS:
            try:
                if time.time() - _seen.get(g_, 0) > IDLE_H * 3600: continue
                if time.time() - cached(g_)[0] >= CACHE_MIN * 60: bg_refresh(g_)
            except Exception: pass
def find_event(eid):
    for g_ in GROUPS:
        for e in cached(g_)[1] or []:
            if e["id"] == eid: return e
@app.get("/api/odds")
@need()
def odds(u):
    grupo = request.args.get("grupo", "futebol"); force = request.args.get("force") == "1"
    if grupo != "aovivo" and grupo not in GROUPS: return err("Esporte inválido.")
    base = "futebol" if grupo == "aovivo" else grupo
    try: ev, ts = get_group(base, force)
    except Exception as e: return jsonify(ok=False, msg=str(e)), 502
    t = time.time(); out = []
    for e in ev:
        st = _ts(e["commence"])
        if grupo == "aovivo" and not (-3600 < t - st < 3 * 3600): continue      # comecou ha <3h ou comeca em <1h
        out.append({**e, "started": st <= t})                                    # copia: nao suja o cache
    return jsonify(ok=True, events=out, updated=ts, logos=logo_map({n for e in out for n in (e["home"], e["away"])}, base)[0])

# ---- logos dos times
_pending, _plock, _tries, _inflight = {}, threading.Lock(), {}, set()
LOGO, LOGO_TS = {}, {}                             # memoria: nome -> url ('' = nao achou) / quando tentou
LOGO_RETRY = 6 * 3600                              # quem nao foi achado tenta de novo depois de 6h
SPORTSDB_KEY = os.environ.get("SPORTSDB_KEY", "3")             # "3" = chave gratis (30 req/min). Chave paga libera 100 req/min.
SPORTSDB_DELAY = float(os.environ.get("SPORTSDB_DELAY", "2.2"))
UA = {"User-Agent": "BolaEsportiva/1.0 (logos de times)"}

# ---- football-data.org (escudos oficiais). A chave vem SEMPRE da variavel FOOTBALL_DATA_KEY (nunca no codigo).
FD_KEY = os.environ.get("FOOTBALL_DATA_KEY", "").strip().strip("\"'").strip()   # tira espacos/aspas/quebra de linha colados por engano
_fd_dbg = {"estado": "aguardando", "codigos": {}, "erro": ""}   # diagnostico (aparece em /api/admin/fd)
FD_BASE = "https://api.football-data.org/v4"
FD_COMPS = ["PL", "PD", "SA", "BL1", "FL1", "PPL", "DED", "ELC", "BSA", "CL", "EC", "WC"]   # campeonatos do plano gratis
FD_REFRESH = 24 * 3600                                         # recarrega a tabela de escudos 1x por dia (12 chamadas)
_fd = {}                                                       # nome normalizado -> url do escudo
_STOP = {"fc", "cf", "afc", "sc", "ac", "as", "cd", "rc", "rcd", "sk", "fk", "ssc", "ud", "sd", "ca", "us", "ss", "club", "clube", "ec", "cr", "de", "do", "da", "dos", "das", "calcio", "the"}
def norm(s):
    """'Club Atlético de Madrid' e 'Atlético Madrid' viram a mesma chave."""
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode().lower().replace("&", " and ").replace("athletico", "atletico")
    return "".join(t for t in re.findall(r"[a-z0-9]+", s) if t not in _STOP)
def fd_lookup(n):
    k = norm(n); u = _fd.get(k)
    if u or len(k) < 8: return u or None
    hit = {v for key, v in _fd.items() if len(key) >= 8 and (k in key or key in k)}      # nome parecido: so vale se der 1 unico escudo
    return hit.pop() if len(hit) == 1 else None
def _hnum(r, h, default):
    try: return int(float(r.headers.get(h, default)))
    except Exception: return default
def fd_refresh():
    """Baixa os times de cada campeonato respeitando o limite (10 req/min no plano gratis) lendo os headers de resposta."""
    out = {}; _fd_dbg.update(estado="carregando", codigos={}, erro="")
    for code in FD_COMPS:
        for _ in range(3):
            try: r = requests.get(f"{FD_BASE}/competitions/{code}/teams", headers={**UA, "X-Auth-Token": FD_KEY}, timeout=20)
            except Exception as e: _fd_dbg["erro"] = repr(e)[:200]; time.sleep(10); continue
            _fd_dbg["codigos"][code] = r.status_code
            if r.status_code == 429: time.sleep(min(70, _hnum(r, "X-RequestCounter-Reset", 60) + 1)); continue
            if r.status_code == 401:
                _fd_dbg.update(estado="chave invalida", erro=r.text[:200]); print("football-data: chave invalida (FOOTBALL_DATA_KEY).", flush=True); return 0
            if not r.ok: _fd_dbg["erro"] = f"{code}: {r.status_code} {r.text[:150]}"
            if r.ok:
                for t in r.json().get("teams") or []:
                    if t.get("crest"):
                        for nm in (t.get("name"), t.get("shortName")):
                            if nm and norm(nm): out.setdefault(norm(nm), t["crest"])
                if _hnum(r, "X-Requests-Available-Minute", 10) <= 1: time.sleep(_hnum(r, "X-RequestCounter-Reset", 60) + 1)
            break                                              # 403 = campeonato fora do plano: pula
        time.sleep(6.5)                                        # folga extra: 12 campeonatos em ~80s, sem estourar o limite
    _fd_dbg["estado"] = "ok" if out else "falhou"
    if out:
        _fd.update(out)
        try: kv_set("fd:crests", out)
        except Exception as e: print("kv_set fd falhou:", e, flush=True)
        print(f"football-data: {len(out)} escudos carregados.", flush=True)
    else: print("football-data: nenhum escudo carregado (veja o status da chave/limite).", flush=True)
    return len(out)
def fd_loop():
    if not FD_KEY: print("football-data: FOOTBALL_DATA_KEY nao definida (usando TheSportsDB/Wikipedia).", flush=True); return
    try:
        _fd_dbg["estado"] = "lendo banco"; ts, v = kv_get("fd:crests")                            # sobe rapido: usa o que ja estava salvo no banco
        if v: _fd.update(v)
    except Exception as e: ts = 0; _fd_dbg["erro"] = "banco: " + repr(e)[:150]
    _fd_dbg["estado"] = "iniciando"
    while True:
        wait = 3600
        try:
            if time.time() - (ts or 0) >= FD_REFRESH or not _fd:
                if fd_refresh(): ts = time.time()
                else: wait = 600                               # falhou: tenta de novo em 10 min (antes esperava 24h)
        except Exception as e: print("fd_refresh falhou:", e, flush=True); _fd_dbg.update(estado="excecao", erro=repr(e)[:200]); wait = 600
        time.sleep(wait)
_fd_thread = None
def fd_ensure():
    """Garante que a rotina de escudos esta rodando (sobe de novo se a thread nao existir ou tiver morrido)."""
    global _fd_thread
    if FD_KEY and (_fd_thread is None or not _fd_thread.is_alive()):
        _fd_thread = threading.Thread(target=fd_loop, daemon=True); _fd_thread.start()
def load_logos():
    try:
        c = raw()
        try:
            for r in c.execute("SELECT nome,url,ts FROM logos"): LOGO[r["nome"]] = r["url"] or ""; LOGO_TS[r["nome"]] = r["ts"] or 0
        finally: c.close()
    except Exception as e: print("load_logos falhou:", e, flush=True)
load_logos()
def logo_map(names, grupo="futebol"):
    """Tudo em memoria (antes: 1 consulta ao banco por time a cada pedido de odds)."""
    fd_ensure()
    res, pend, t = {}, 0, time.time()
    for n in names:
        f_ = fd_lookup(n) if grupo == "futebol" else None
        if f_: LOGO[n] = res[n] = f_; continue                  # escudo oficial vence qualquer cache antigo (inclusive "nao achou")
        u_ = LOGO.get(n)
        if u_: res[n] = u_; continue
        if grupo in ("tenis", "mma"): continue                  # sao atletas, nao times
        with _plock:
            if n in _inflight: pend += 1; continue
        if u_ is not None and t - LOGO_TS.get(n, 0) < LOGO_RETRY: continue      # ja tentou e nao existe
        pend += 1
        with _plock: _pending.setdefault(n, grupo)
    return res, pend
def _sportsdb(q, soccer):
    r = requests.get(f"https://www.thesportsdb.com/api/v1/json/{SPORTSDB_KEY}/searchteams.php", params={"t": q}, timeout=10, headers=UA)
    if not r.ok: return None, True                              # 429 (limite) ou erro do servidor: tentar depois
    for t in ((r.json() or {}).get("teams") or []):
        if soccer and t.get("strSport") not in (None, "Soccer"): continue
        b = t.get("strBadge") or t.get("strTeamBadge")
        if b: return b, False
    return None, False
SM_KEY = os.environ.get("SPORTMONKS_KEY", "").strip().strip("\"'").strip()   # chave SO na variavel de ambiente (nunca no codigo)
_sm_dbg = {"ultimo_status": None, "erro": ""}
def _sportmonks(n):
    """Busca o escudo no Sportmonks. Devolve (url|None, transitorio). Token vai no header, nao na URL (nao aparece em logs)."""
    r = requests.get("https://api.sportmonks.com/v3/football/teams/search/" + requests.utils.quote(n, safe=""), timeout=10,
                     headers={**UA, "Authorization": SM_KEY}, params={"per_page": 10})
    _sm_dbg["ultimo_status"] = r.status_code
    if r.status_code in (401, 403): _sm_dbg["erro"] = r.text[:150]; return None, False      # chave/plano sem acesso: nao adianta insistir
    if r.status_code == 429 or r.status_code >= 500: return None, True
    if not r.ok: return None, False
    k = norm(n); teams = [t for t in ((r.json() or {}).get("data") or []) if t.get("image_path") and not t.get("placeholder")]
    for t in teams:
        if norm(t.get("name", "")) == k: return t["image_path"], False                    # nome igual: certeza
    if len(teams) == 1 and len(k) >= 5 and (k in norm(teams[0].get("name", "")) or norm(teams[0].get("name", "")) in k):
        return teams[0]["image_path"], False                                              # unico resultado parecido
    return None, False
def find_logo(n, grupo):
    """Devolve a url, '' (nao existe de verdade) ou None (falha temporaria: tentar de novo)."""
    transient, soccer = False, grupo == "futebol"
    if SM_KEY and soccer:
        try:
            b, tr = _sportmonks(n)
            if b: return b
            transient = transient or tr
        except Exception as e: _sm_dbg["erro"] = repr(e)[:150]; transient = True
    short = re.sub(r"^(FC|AFC|CF|SC|AC|AS|CD|RC|RCD|SK|FK)\s+|\s+(FC|AFC|CF|SC|AC)$", "", n).strip()
    for q in dict.fromkeys([n, short]):
        if not q: continue
        try:
            b, tr = _sportsdb(q, soccer)
            if b: return b
            if tr: transient = True; break                      # limite da API: nao insiste
        except Exception: transient = True; break
        time.sleep(SPORTSDB_DELAY)
    try:
        r = requests.get("https://en.wikipedia.org/w/api.php", timeout=10, headers=UA, params=dict(action="query", format="json", generator="search",
                         gsrsearch=n + (" football club" if soccer else ""), gsrlimit=1, prop="pageimages", piprop="thumbnail", pithumbsize=96, redirects=1))
        if r.ok:
            for p in ((r.json().get("query") or {}).get("pages") or {}).values():
                src = (p.get("thumbnail") or {}).get("source")
                if src and re.search(r"\.(png|svg)", src, re.I): return src      # escudo e png/svg; foto (jpg) de jogador/estadio e ignorada
        else: transient = True
    except Exception: transient = True
    return None if transient else ""
def logo_worker():
    while True:
        item = None
        try:
            with _plock:
                if _pending: item = _pending.popitem(); _inflight.add(item[0])
            if not item: time.sleep(2); continue
            n, grupo = item; url = fd_lookup(n) if grupo in ("futebol", "aovivo") else None
            fast = bool(url)                                   # achou no football-data: sem espera
            if not url: url = find_logo(n, grupo)
            if url is None:
                _tries[n] = _tries.get(n, 0) + 1
                if _tries[n] < 6:
                    with _plock: _pending[n] = grupo; _inflight.discard(n)
                    time.sleep(20); continue
                url = ""
            LOGO[n] = url; LOGO_TS[n] = time.time()
            c = raw()
            try: c.execute("INSERT INTO logos(nome,url,ts) VALUES(?,?,?) ON CONFLICT (nome) DO UPDATE SET url=EXCLUDED.url, ts=EXCLUDED.ts", (n, url, time.time()))
            finally: c.close()
            with _plock: _inflight.discard(n)
            if not fast: time.sleep(SPORTSDB_DELAY)
        except Exception:
            if item:
                with _plock: _inflight.discard(item[0])
            time.sleep(5)
@app.post("/api/logos")
@need()
def logos():
    d = request.get_json(silent=True) or {}
    res, pend = logo_map({str(n) for n in (d.get("names") or [])[:300]}, str(d.get("grupo", "futebol")))
    return jsonify(ok=True, logos=res, pendentes=pend)
AF_KEY = os.environ.get("APIFOOTBALL_KEY", "").strip().strip("\"'").strip()   # chave SO na variavel de ambiente
AF_BASE = "https://v3.football.api-sports.io"
def af(path, **params):
    r = requests.get(AF_BASE + path, headers={**UA, "x-apisports-key": AF_KEY}, params=params, timeout=20)
    try: return r.json()
    except Exception: return {"errors": f"resposta invalida ({r.status_code})"}
@app.get("/api/admin/apifootball")
@need("admin")
def af_diag(u):
    """Diagnostico da API-Football: /api/admin/apifootball?q=saudi  e depois  ?liga=307&ano=2025  (testa se o plano libera odds)."""
    if not AF_KEY: return err("Defina APIFOOTBALL_KEY no Render.")
    out = {}
    try:
        st = (af("/status").get("response") or {})
        out["plano"] = (st.get("subscription") or {}).get("plan"); out["chamadas"] = st.get("requests")
        lg = af("/leagues", search=request.args.get("q", "saudi"))
        out["erros_ligas"] = lg.get("errors")
        out["ligas"] = [dict(id=x["league"]["id"], nome=x["league"]["name"], pais=x["country"]["name"],
                             temporadas=[dict(ano=y["year"], odds=y["coverage"]["odds"], atual=y["current"]) for y in x["seasons"][-2:]])
                        for x in (lg.get("response") or [])[:8]]
        if request.args.get("liga"):
            od = af("/odds", league=request.args["liga"], season=request.args.get("ano"))
            out["odds"] = dict(erros=od.get("errors"), jogos_com_odds=od.get("results"),
                               amostra=[dict(jogo=x["fixture"]["id"], casas=len(x.get("bookmakers") or [])) for x in (od.get("response") or [])[:3]])
    except Exception as e: out["excecao"] = repr(e)[:200]
    return jsonify(ok=True, **out)
@app.get("/api/admin/ligas")
@need("admin")
def ligas(u):                                                  # lista as ligas de futebol que o The Odds API oferece (nao gasta creditos)
    r = requests.get(f"{ODDS_BASE}/sports/", params={"apiKey": ODDS_KEY, "all": "true"}, timeout=20)
    if not r.ok: return err(f"The Odds API respondeu {r.status_code}.", 502)
    q = request.args.get("q", "").lower().strip()
    l = [dict(key=s["key"], nome=s["title"], ativo=s["active"]) for s in r.json() if s["group"] == "Soccer" and not s.get("has_outrights")]
    if q: l = [x for x in l if q in x["key"].lower() or q in x["nome"].lower()]
    return jsonify(ok=True, total=len(l), ativas=sum(x["ativo"] for x in l), ligas=l)
@app.get("/api/admin/fd")
@need("admin")
def fd_status(u):                                              # diagnostico: abra /api/admin/fd logado como admin
    fd_ensure()
    if request.args.get("run") == "1" and _fd_dbg["estado"] != "carregando":      # /api/admin/fd?run=1 forca a carga agora
        threading.Thread(target=fd_refresh, daemon=True).start()
    return jsonify(ok=True, thread_viva=bool(_fd_thread and _fd_thread.is_alive()), chave_definida=bool(FD_KEY), tamanho_chave=len(FD_KEY), escudos=len(_fd), exemplo=list(_fd.items())[:3], diagnostico=_fd_dbg,
                   sportmonks=dict(chave_definida=bool(SM_KEY), **_sm_dbg))
fd_ensure()
threading.Thread(target=logo_worker, daemon=True).start()
threading.Thread(target=odds_loop, daemon=True).start()

# ---------------------------------------------------------------- apostas
@app.post("/api/bets")
@need("usuario", "cambista")
def bet(u):
    d = request.get_json(silent=True) or {}; sel = d.get("selecoes") or []; stake = cents(d.get("valor"))
    por, feito_por = "", None
    if u["role"] == "cambista":                    # cambista faz o bilhete em nome de um cliente dele (debita o saldo do cliente)
        try: cid = int(d.get("clienteId"))
        except (TypeError, ValueError): cid = None
        cli = db().execute("SELECT * FROM users WHERE id=? AND role='usuario' AND criado_por=? AND ativo=1", (cid, u["id"])).fetchone() if cid else None
        if not cli: return err("Selecione um cliente válido da sua lista.")
        por = f" • feito por {u['nome']}"; feito_por, u = u, cli
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
        mk = str(s.get("market")); o = ev["odds"].get(mk)
        if not o: return err("Mercado indisponível.")
        odd *= o; final.append(dict(id=ev["id"], key=ev["key"], commence=ev["commence"], home=ev["home"], away=ev["away"], jogo=f"{ev['home']} x {ev['away']}",
                          mercado=mk, palpite=ev["home"] if mk == "1" else ev["away"] if mk == "2" else "Empate", odd=o))   # odd vem do servidor
    odd = round(odd, 2); c = db(); c.execute("BEGIN")
    try:
        if stake > c.execute("SELECT saldo FROM users WHERE id=? FOR UPDATE", (u["id"],)).fetchone()[0]:
            c.execute("ROLLBACK"); return err("Saldo insuficiente do cliente. Faça uma recarga para ele no painel." if feito_por else "Saldo insuficiente. Peça uma recarga ao seu cambista.")
        c.execute("UPDATE users SET saldo=saldo-? WHERE id=?", (stake, u["id"]))
        tid = c.execute("INSERT INTO tickets(user_id,data,selecoes,odd,valor,retorno) VALUES(?,?,?,?,?,?) RETURNING id",
                        (u["id"], now(), json.dumps(final), odd, stake, int(round(stake * odd)))).fetchone()[0]
        tx(c, "aposta", u["id"], None, stake, f"Bilhete #{tid} ({len(final)} seleç{'ões' if len(final) > 1 else 'ão'}) odd {odd:.2f}{por}")
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK"); raise
    cr = c.execute("SELECT nome, telefone FROM users WHERE id=?", (u["criado_por"],)).fetchone() if u["criado_por"] else None
    return jsonify(ok=True, id=tid, odd=odd, valor=stake / 100, retorno=round(stake * odd) / 100, selecoes=final, usuario=u["nome"],
                   cambista=dict(nome=cr["nome"], telefone=cr["telefone"] or "") if cr else None)
@app.post("/api/tickets/<int:tid>/settle")
@need("admin", "cambista")
def settle(u, tid):
    res = (request.get_json(silent=True) or {}).get("resultado"); c = db()
    if res not in ("ganhou", "perdeu", "cancelado"): return err("Resultado inválido.")
    c.execute("BEGIN")
    try:
        t = c.execute("SELECT t.*, x.criado_por, x.nome FROM tickets t JOIN users x ON x.id=t.user_id WHERE t.id=? FOR UPDATE OF t", (tid,)).fetchone()
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

# ---------------------------------------------------------------- placar ao vivo
LIVE_TTL = int(os.environ.get("LIVE_POLL_SECONDS", "30"))
_live = {}
def _ts(iso): return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
def live_for_key(key):
    ts, data = _live.get(key, (0, {}))
    if time.time() - ts < LIVE_TTL: return data
    def num(v):
        try: return int(float(v))
        except Exception: return None
    try:
        r = requests.get(f"{ODDS_BASE}/sports/{key}/scores/", params={"apiKey": ODDS_KEY, "daysFrom": 1}, timeout=15); r.raise_for_status()
        out = {}
        for e in r.json():
            sc = {x["name"]: x["score"] for x in (e.get("scores") or [])}
            out[e["id"]] = dict(home=num(sc.get(e["home_team"])), away=num(sc.get(e["away_team"])), completed=bool(e.get("completed")), started=e.get("scores") is not None or bool(e.get("completed")))
        _live[key] = (time.time(), out); return out
    except Exception:
        _live[key] = (time.time(), data); return data      # mantem o ultimo placar e nao insiste a cada segundo
@app.get("/api/live")
@need()
def live(u):
    c = db(); ids = scope_ids(c, u); keys, want = set(), set(); t = time.time()
    sql = "SELECT selecoes FROM tickets WHERE status='aberto'" + ("" if ids is None else f" AND user_id IN ({','.join('?'*len(ids))})")
    for r in c.execute(sql, tuple(ids or ())):
        for s_ in json.loads(r["selecoes"]):
            if s_.get("key") and s_.get("commence") and 0 <= t - _ts(s_["commence"]) < 6 * 3600: keys.add(s_["key"]); want.add(s_["id"])
    out = {}
    for k in keys: out.update({i: v for i, v in live_for_key(k).items() if i in want})
    return jsonify(ok=True, live=out)

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
    c = db(); c.execute("BEGIN")
    for t in ("transactions", "tickets"): c.execute(f"DELETE FROM {t}")
    c.execute("DELETE FROM users WHERE role!='admin'"); c.execute("UPDATE users SET pw=?, senha_padrao=1 WHERE id=?", (generate_password_hash("admin123"), u["id"])); c.execute("COMMIT")
    session.clear(); return jsonify(ok=True)
@app.get("/api/relatorios")
@need("admin", "cambista")
def relatorios(u):
    c = db(); ids = [r[0] for r in c.execute("SELECT id FROM users WHERE role='usuario'" + ("" if u["role"] == "admin" else " AND criado_por=?"), () if u["role"] == "admin" else (u["id"],))]
    ph = ",".join("?" * len(ids)) or "NULL"; day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row = lambda sql, extra=(): float(c.execute(sql.format(ph), list(ids) + list(extra)).fetchone()[0] or 0)
    tot = row("SELECT SUM(valor) FROM tickets WHERE user_id IN ({}) AND status!='cancelado'")
    return jsonify(ok=True, hoje=int(row("SELECT COUNT(*) FROM tickets WHERE user_id IN ({}) AND data >= ?", (day,))),
                   bruto=tot / 100, premios=row("SELECT SUM(retorno) FROM tickets WHERE user_id IN ({}) AND status='ganhou'") / 100,
                   abertos=int(row("SELECT COUNT(*) FROM tickets WHERE user_id IN ({}) AND status='aberto'")),
                   comissao=round(tot * COMISSAO / 100, 2) if u["role"] == "cambista" else None)

@app.get("/health")
def health():
    try: db().execute("SELECT 1").fetchone(); return "ok"
    except Exception: return "erro", 500
@app.get("/")
def index(): return send_from_directory(os.path.join(BASE, "static"), "index.html")
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
