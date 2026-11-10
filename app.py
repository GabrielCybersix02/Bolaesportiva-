"""Bola Esportiva - backend Flask + Supabase (Postgres).  Rodar: python app.py   (producao: gunicorn app:app)"""
import hashlib, os, re, json, time, queue, threading, secrets, unicodedata
from concurrent.futures import ThreadPoolExecutor
import psycopg2, psycopg2.extras
from datetime import datetime, timezone, timedelta
import requests
from flask import Flask, g, jsonify, has_request_context, request, session, send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix

BASE = os.path.dirname(os.path.abspath(__file__))
DATABASE_URL = os.environ.get("DATABASE_URL", "")   # string de conexao do Supabase
ODDS_KEY = os.environ.get("ODDS_API_KEY", "")        # NUNCA coloque a chave no codigo: defina ODDS_API_KEY no Render
ODDS_BASE = "https://api.the-odds-api.com/v4"
CACHE_MIN = int(os.environ.get("ODDS_CACHE_MINUTES", "15"))
COMISSAO_PCT = float(os.environ.get("COMISSAO_PCT", "10"))      # % do cambista sobre TODO valor apostado pelos clientes dele
COMISSAO = COMISSAO_PCT / 100
MAX_PREMIO = int(float(os.environ.get("MAX_PREMIO_REAIS", "20000")) * 100)   # teto do premio por bilhete, em centavos (padrao R$ 20.000)
def premio(valor, odd): return min(int(round(valor * odd)), MAX_PREMIO)       # premio = aposta x odd, nunca acima do teto
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
"""CREATE TABLE IF NOT EXISTS pins(code TEXT PRIMARY KEY, criado DOUBLE PRECISION NOT NULL, selecoes TEXT NOT NULL, valor BIGINT NOT NULL, usado INTEGER NOT NULL DEFAULT 0, usado_por INTEGER, ticket_id INTEGER)""",
"""CREATE TABLE IF NOT EXISTS acertos(id SERIAL PRIMARY KEY, data TEXT, user_id INTEGER NOT NULL REFERENCES users(id), cambista_id INTEGER, valor BIGINT NOT NULL, por INTEGER)""",
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
    c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS credito BIGINT NOT NULL DEFAULT 0")   # fiado: total de credito dado ao cliente e ainda nao acertado
    c.execute("ALTER TABLE tickets ADD COLUMN IF NOT EXISTS feito_por INTEGER"); c.execute("ALTER TABLE tickets ADD COLUMN IF NOT EXISTS cliente TEXT")                    # cambista que vendeu o bilhete (o valor sai do saldo DELE)
    c.execute("ALTER TABLE pins ADD COLUMN IF NOT EXISTS token TEXT")                              # link publico para o cliente acompanhar o bilhete
    c.execute("ALTER TABLE tickets ADD COLUMN IF NOT EXISTS token TEXT")
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS tickets_token ON tickets(token)")
    if not c.execute("SELECT 1 FROM kv WHERE k='mig:caixa1'").fetchone():                         # 1x: "Limite (fiado)" antigo nunca saiu do saldo do cambista; some das saidas dele
        c.execute("UPDATE transactions SET de=NULL WHERE LEFT(descr, 14)='Limite (fiado)'"); c.execute("INSERT INTO kv(k,ts,v) VALUES('mig:caixa1',0,'1')")
    if not c.execute("SELECT 1 FROM kv WHERE k='mig:credito1'").fetchone():                       # 1x: o saldo que os clientes ja tem vira credito (fiado) deles
        c.execute("UPDATE users SET credito=saldo WHERE role='usuario'"); c.execute("INSERT INTO kv(k,ts,v) VALUES('mig:credito1',0,'1')")
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
def pub(f):                       # rota aberta: visitante (sem login) tambem acessa; u = usuario logado ou None
    def w(*a, **k): return f(me(), *a, **k)
    w.__name__ = f.__name__; return w
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
    us = [dict(id=str(r["id"]), nome=r["nome"], login=r["login"], role=r["role"], saldo=r["saldo"] / 100, credito=(r["credito"] or 0) / 100, ativo=bool(r["ativo"]),
              criadoPor=str(r["criado_por"]) if r["criado_por"] else None, senhaPadrao=bool(r["senha_padrao"]), telefone=r["telefone"] or "")
          for r in c.execute(*q("SELECT * FROM users", "id"))]
    if ids is not None:
        tq = f"SELECT * FROM transactions WHERE de IN ({','.join('?'*len(ids))}) OR para IN ({','.join('?'*len(ids))})"; ta = tuple(ids) * 2
    else: tq, ta = "SELECT * FROM transactions", ()
    txs = [dict(id=str(r["id"]), data=r["data"], tipo=r["tipo"], de=str(r["de"]) if r["de"] else None, para=str(r["para"]) if r["para"] else None,
                valor=r["valor"] / 100, desc=r["descr"]) for r in c.execute(tq + " ORDER BY id DESC LIMIT 500", ta)]
    tsql, targs = q("SELECT * FROM tickets", "user_id")
    tks = [dict(id=str(r["id"]), userId=str(r["user_id"]), data=r["data"], selecoes=json.loads(r["selecoes"]), odd=r["odd"],
                valor=r["valor"] / 100, retorno=r["retorno"] / 100, status=r["status"], feitoPor=str(r["feito_por"]) if r["feito_por"] else None, cliente=r["cliente"] or "")
           for r in c.execute(tsql + " ORDER BY id DESC LIMIT 300", targs)]
    cam = None
    if u["role"] == "usuario" and u["criado_por"]:
        r = c.execute("SELECT nome, telefone FROM users WHERE id=? AND role='cambista'", (u["criado_por"],)).fetchone()   # usuario criado pelo ADM nao tem cambista
        if r: cam = dict(nome=r["nome"], telefone=r["telefone"] or "")
    return jsonify(ok=True, me=str(u["id"]), users=us, transactions=txs, tickets=tks, cambista=cam)

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
@need("admin")          # so o ADM cria usuario (cliente); cambista nao cria mais
def new_usuario(u):
    d = request.get_json(silent=True) or {}
    m = make_user(db(), "usuario", str(d.get("nome", "")), str(d.get("login", "")), str(d.get("senha", "")), u["id"])
    return err(m) if m else jsonify(ok=True)
@app.post("/api/saldo")
@need("admin")
def add_saldo(u):
    d = request.get_json(silent=True) or {}; c = db(); v = cents(d.get("valor"))
    if v <= 0: return err("Informe um valor válido.")
    c.execute("BEGIN")
    try:
        t = c.execute("SELECT * FROM users WHERE id=? AND ativo=1 FOR UPDATE", (d.get("alvo"),)).fetchone()
        if not t or t["role"] not in ("cambista", "usuario"):          # so o ADM coloca saldo (para cambista ou usuario)
            c.execute("ROLLBACK"); return err("Selecione uma conta válida.")
        # FIADO: o cambista libera limite ao cliente sem pagar nada na hora; o valor vira "credito" e entra na conta do acerto de segunda
        if t["role"] == "usuario": c.execute("UPDATE users SET saldo=saldo+?, credito=credito+? WHERE id=?", (v, v, t["id"]))
        else: c.execute("UPDATE users SET saldo=saldo+? WHERE id=?", (v, t["id"]))
        tx(c, "credito", u["id"], t["id"], v, ("Saldo adicionado a " if t["role"] == "cambista" else "Saldo (fiado) para ") + t["nome"])
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK"); raise
    return jsonify(ok=True, nome=t["nome"])
def can_manage(u, t):
    return t and ((u["role"] == "admin" and t["role"] in ("cambista", "usuario")) or (u["role"] == "cambista" and t["role"] == "usuario" and t["criado_por"] == u["id"]))
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
# ---------------------------------------------------------------- apagar conta (so ADM): tira o cambista/usuario e o historico dele.
# A caixa dos cambistas e calculada na hora a partir dos bilhetes e acertos, entao apagar aqui ja a atualiza sozinha.
def _ids_apagar(c, t):
    ids = [t["id"]]
    if t["role"] == "cambista": ids += [r[0] for r in c.execute("SELECT id FROM users WHERE criado_por=? AND role='usuario'", (t["id"],)).fetchall()]   # clientes do cambista vao junto
    return ids
def _resumo_apagar(c, t, ids):
    ph = ",".join("?" * len(ids))
    r = c.execute(f"SELECT COUNT(CASE WHEN status!='cancelado' THEN 1 END) AS n, COALESCE(SUM(CASE WHEN status!='cancelado' THEN valor END),0) AS apostado, "
                  f"COALESCE(SUM(CASE WHEN status='ganhou' THEN retorno END),0) AS premios, COUNT(CASE WHEN status='aberto' THEN 1 END) AS abertos "
                  f"FROM tickets WHERE user_id IN ({ph}) OR feito_por IN ({ph})", tuple(ids) * 2).fetchone()
    out = dict(nome=t["nome"], role=t["role"], bilhetes=r["n"], apostado=int(r["apostado"]) / 100, premios=int(r["premios"]) / 100, abertos=r["abertos"], clientes=len(ids) - 1)
    if t["role"] == "cambista": out["caixa"] = _caixa_cam(c, t["id"])["resultado"]        # >0 deve ao ADM | <0 ADM deve a ele | 0 em dia
    return out
@app.post("/api/contas/<int:uid>/apagar")
@need("admin")
def apagar_conta(u, uid):
    """Sem confirmar=true so mostra o que vai ser apagado (e o efeito na caixa). Com confirmar=true apaga de verdade."""
    d = request.get_json(silent=True) or {}; c = db()
    def checa(t, ids):
        if not t or t["role"] not in ("cambista", "usuario"): return "Conta não encontrada."
        if t["id"] == u["id"]: return "Você não pode apagar a própria conta."
        r = _resumo_apagar(c, t, ids)
        if r["abertos"]: return f"{t['nome']} tem {r['abertos']} bilhete(s) em aberto. Espere terminar (ou anule os bilhetes) antes de apagar."
        return r
    t = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    r = checa(t, _ids_apagar(c, t) if t else [])
    if isinstance(r, str): return err(r)
    if not d.get("confirmar"): return jsonify(ok=True, preview=True, **r)
    c.execute("BEGIN")
    try:
        t = c.execute("SELECT * FROM users WHERE id=? FOR UPDATE", (uid,)).fetchone()          # trava: dois cliques nao apagam duas vezes
        ids = _ids_apagar(c, t) if t else []
        r = checa(t, ids)                                                                       # confere de novo dentro da transacao (ninguem apostou no meio)
        if isinstance(r, str): c.execute("ROLLBACK"); return err(r)
        ph = ",".join("?" * len(ids)); a1, a2 = tuple(ids), tuple(ids) * 2
        c.execute(f"DELETE FROM tickets WHERE user_id IN ({ph}) OR feito_por IN ({ph})", a2)
        c.execute(f"DELETE FROM acertos WHERE user_id IN ({ph}) OR cambista_id IN ({ph})", a2)
        c.execute(f"DELETE FROM transactions WHERE de IN ({ph}) OR para IN ({ph})", a2)
        c.execute(f"DELETE FROM pins WHERE usado_por IN ({ph})", a1)
        c.execute(f"DELETE FROM users WHERE id IN ({ph})", a1)
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK"); raise
    return jsonify(ok=True, apagado=True, **r)
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
    def one(s):                                    # em paralelo; tenta ate 3x (429/5xx/timeout) para nenhuma liga sumir por falha momentanea
        for i in range(3):
            try:
                r = requests.get(f"{ODDS_BASE}/sports/{s['key']}/odds/", timeout=20,
                                 params={"apiKey": ODDS_KEY, "regions": "eu", "markets": "h2h", "oddsFormat": "decimal", "dateFormat": "iso"})
                if r.ok: return parse_events(r.json(), s["key"], grupo)
                if r.status_code in (404, 422): return []           # liga sem odds agora: nao e erro
                if r.status_code not in (429, 500, 502, 503, 504): return None
            except Exception: pass
            time.sleep(1.2 * (i + 1))
        return None
    with ThreadPoolExecutor(max_workers=6) as ex: parts = list(ex.map(one, lst))
    out = [e for p in parts if p for e in p]
    falhou = [s["key"] for s, p in zip(lst, parts) if p is None]
    if falhou:                                     # liga que falhou agora: reaproveita os jogos dela do cache anterior (ainda futuros)
        antigo = (cached(grupo)[1] or []); t0 = time.time() - 3 * 3600; ja = {e["id"] for e in out}
        out += [e for e in antigo if e.get("key") in falhou and e["id"] not in ja and _ts(e["commence"]) > t0]
        print("odds: ligas com falha, mantidas do cache:", falhou, flush=True)
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
# ---------------------------------------------------------------- OddsPapi (fonte extra de jogos de futebol)
# A chave vem SEMPRE da variavel ODDSPAPI_KEY (nunca no codigo). Plano gratis = 250 requisicoes/mes: o codigo conta cada chamada,
# guarda o total no banco e se recusa a passar do limite. Os jogos ficam em cache e sao renovados no maximo a cada ODDSPAPI_REFRESH_HOURS.
OP_KEY = os.environ.get("ODDSPAPI_KEY", "").strip().strip("\"'").strip()
OP_BASE = "https://api.oddspapi.io/v4"
OP_BOOKS = [b.strip() for b in os.environ.get("ODDSPAPI_BOOKMAKERS", "pinnacle").split(",") if b.strip()][:1]   # mais casas = mais jogos com odds
OP_HOURS = float(os.environ.get("ODDSPAPI_REFRESH_HOURS", "24"))
OP_DAYS = max(1, min(9, int(os.environ.get("ODDSPAPI_DAYS", "3"))))
OP_LIMIT = int(os.environ.get("ODDSPAPI_MONTHLY_LIMIT", "250"))
OP_RESERVE = int(os.environ.get("ODDSPAPI_RESERVE", "60"))         # sobra para liquidar bilhetes (settlements)
_op = {"ts": 0, "ev": None, "err": "", "fail": 0, "busy": False, "n": None, "month": "", "merged": (None, None, []), "cost": 9, "info": {}, "books": None}
_op_lock, _op_last, _op_start = threading.Lock(), [0.0], threading.Lock()
def _op_month(): return time.strftime("%Y-%m", time.gmtime())
def op_used():
    m = _op_month()
    if _op["n"] is None or _op["month"] != m:
        try: _op["n"] = int(kv_get("op:usage:" + m)[1] or 0)
        except Exception: _op["n"] = 0
        _op["month"] = m
        try:                                                       # chave nova da OddsPapi = conta nova com cota cheia: zera o contador local sozinho
            fp = hashlib.sha256(OP_KEY.encode()).hexdigest()[:12] if OP_KEY else ""
            if fp and kv_get("op:keyfp")[1] != fp:
                _op["n"] = 0; kv_set("op:usage:" + m, 0); kv_set("op:keyfp", fp); _op["err"] = ""; _op["fail"] = 0
                print("oddspapi: chave nova detectada, contador de cota zerado.", flush=True)
        except Exception as e: print("oddspapi: nao deu para checar a chave:", e, flush=True)
    return _op["n"]
def _op_bump():
    op_used(); _op["n"] += 1
    try: kv_set("op:usage:" + _op["month"], _op["n"])
    except Exception: pass
def op_get(path, reserve=0, **params):
    if not OP_KEY: raise RuntimeError("ODDSPAPI_KEY não definida.")
    with _op_lock:
        if op_used() >= OP_LIMIT - reserve: raise RuntimeError("Cota mensal da OddsPapi no limite (proteção ativa).")
        w = 2.1 - (time.time() - _op_last[0])                       # a API exige ~2s entre chamadas do mesmo endpoint
        if w > 0: time.sleep(w)
        _op_bump()
        try: r = requests.get(OP_BASE + path, params={**params, "apiKey": OP_KEY}, headers=UA, timeout=90)
        except Exception as e: raise RuntimeError(("OddsPapi fora do ar: " + str(e))[:150].replace(OP_KEY, "***"))
        finally: _op_last[0] = time.time()
    if not r.ok: raise RuntimeError(f"OddsPapi {r.status_code}: {r.text[:400]}".replace(OP_KEY, "***"))
    return r.json()
def _op_prices(fx, books=None):
    for b in (books or OP_BOOKS):
        mk = (((fx.get("bookmakerOdds") or {}).get(b) or {}).get("markets") or {}).get("101")      # mercado 101 = 1X2 tempo normal
        if not mk: continue
        o = mk.get("outcomes") or {}
        def pr(i):
            p = ((o.get(i) or {}).get("players") or {}).get("0") or {}
            v = p.get("price")
            return v if p.get("active", True) and isinstance(v, (int, float)) and v > 1 else None
        h, d, a = pr("101"), pr("102"), pr("103")
        if h and a: return {"1": h, "X": d, "2": a}
    return None
def _as_list(d):
    if isinstance(d, list): return d
    if isinstance(d, dict):
        for v in d.values():
            if isinstance(v, list): return v
    return []
def op_refresh():
    t0 = datetime.now(timezone.utc); iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ"); used0 = op_used()
    if _op["books"] is None:
        try: v = kv_get("op:books")[1]
        except Exception: v = None
        _op["books"] = v if isinstance(v, list) and v else list(OP_BOOKS)
    def fixtures(books):
        return op_get("/fixtures", reserve=OP_RESERVE, sportId=10, statusId=0, hasOdds="true", bookmaker=books[0],
                      **{"from": iso(t0), "to": iso(t0 + timedelta(days=OP_DAYS))})
    try: fx = fixtures(_op["books"])
    except RuntimeError as e:
        if len(_op["books"]) > 1 and re.search(r"OddsPapi 4(00|22)", str(e)):      # alguma casa nao existe com esse nome: fica so na primeira
            _op["books"] = _op["books"][:1]
            try: kv_set("op:books", _op["books"])
            except Exception: pass
            fx = fixtures(_op["books"])
        else: raise
    bks = _op["books"]
    fx = [f for f in _as_list(fx) if isinstance(f, dict) and f.get("fixtureId") and f.get("participant1Name") and f.get("participant2Name") and f.get("startTime")]
    cnt = {}
    for f in fx: cnt[f.get("tournamentId")] = cnt.get(f.get("tournamentId"), 0) + 1
    tids = sorted(cnt, key=lambda k: -cnt[k]); batches = [tids[i:i + 5] for i in range(0, len(tids), 5)]
    batches = batches[:max(0, OP_LIMIT - OP_RESERVE - op_used())]    # nunca estoura a cota: o que nao couber espera o proximo ciclo
    prices = {}; sem_odds = 0
    for b in batches:
        try: data = op_get("/odds-by-tournaments", reserve=OP_RESERVE, tournamentIds=",".join(map(str, b)), bookmaker=bks[0])
        except RuntimeError as e:
            if "OddsPapi 404" in str(e): sem_odds += 1; continue       # lote sem odds dessa casa: pula e segue com os outros, em vez de perder a atualizacao toda
            raise
        for f in _as_list(data):
            pz = _op_prices(f, bks) if isinstance(f, dict) else None
            if pz: prices[f.get("fixtureId")] = pz
    out = [dict(id="op:" + f["fixtureId"], home=f["participant1Name"], away=f["participant2Name"], commence=f["startTime"],
                league=f.get("tournamentName") or "Outros", key="op_%s_%s" % (f.get("categorySlug", ""), f.get("tournamentSlug", "")),
                grupo="futebol", odds=prices[f["fixtureId"]]) for f in fx if f["fixtureId"] in prices]
    _op["info"] = dict(fixtures_com_odds=len(fx), campeonatos=len(tids), lotes=len(batches), jogos_com_preco_1x2=len(out), casas=bks, lotes_sem_odds=sem_odds, chamadas=op_used() - used0)
    _op["cost"] = max(2, op_used() - used0)
    if not out: raise RuntimeError(f"OddsPapi devolveu {len(fx)} jogos mas nenhum com odds 1X2 (casas: {','.join(bks)}).")
    _op.update(ts=time.time(), ev=out, err="")
    try: kv_set("odds:op", out)
    except Exception as e: print("kv_set odds:op falhou:", e, flush=True)
    print(f"oddspapi: {len(out)} jogos de {len(batches)} lotes (uso do mes: {op_used()}/{OP_LIMIT}).", flush=True)
def _op_bg():
    try: op_refresh()
    except Exception as e: _op["err"] = str(e)[:500]; _op["fail"] = time.time(); print("oddspapi falhou:", e, flush=True)
    finally: _op["busy"] = False; _op_start.release()
def op_kick(force=False):
    """Carrega o cache salvo e, se estiver velho, renova por tras (no maximo 1x a cada ODDSPAPI_REFRESH_HOURS)."""
    if not OP_KEY: return
    if _op["ev"] is None:
        ts, v = kv_get("odds:op"); _op["ts"], _op["ev"] = ts or 0, v or []
    gap = max(OP_HOURS, 720.0 * _op["cost"] / max(1, OP_LIMIT - OP_RESERVE))     # espaca as atualizacoes para a cota durar o mes todo
    if (force or time.time() - _op["ts"] >= gap * 3600) and (force or time.time() - _op["fail"] > 1800) and _op_start.acquire(blocking=False):
        _op["busy"] = True; threading.Thread(target=_op_bg, daemon=True).start()
def _same(a, b): return bool(a) and bool(b) and (a == b or (min(len(a), len(b)) >= 5 and (a in b or b in a)))
def merge_op(ev):
    """Junta os jogos da OddsPapi aos da The Odds API sem duplicar (mesmos times e horario parecido: vale o da The Odds API)."""
    op = _op["ev"] or []
    if not op: return ev
    sig = (id(ev), _op["ts"])
    if _op["merged"][0] == sig[0] and _op["merged"][1] == sig[1]: return _op["merged"][2]
    buckets = {}
    for e in ev:
        t = _ts(e["commence"]); buckets.setdefault(int(t // 14400), []).append((norm(e["home"]), norm(e["away"]), t))
    extra = []
    for e in op:
        t = _ts(e["commence"]); h, a = norm(e["home"]), norm(e["away"]); b = int(t // 14400)
        if any(abs(t - t2) <= 14400 and _same(h, h2) and _same(a, a2) for bb in (b - 1, b, b + 1) for h2, a2, t2 in buckets.get(bb, [])): continue
        extra.append(e)
    out = ev + extra; _op["merged"] = (sig[0], sig[1], out); return out
def find_event(eid):
    for g_ in GROUPS:
        for e in cached(g_)[1] or []:
            if e["id"] == eid: return e
    if str(eid).startswith("op:"):
        if _op["ev"] is None: op_kick()
        for e in _op["ev"] or []:
            if e["id"] == eid: return e
MAXDUR = {"futebol": 2.5 * 3600, "basquete": 3.5 * 3600, "tenis": 5 * 3600, "mma": 6 * 3600}   # depois disso o jogo ja acabou: sai da lista
@app.get("/api/odds")
@pub
def odds(u):
    grupo = request.args.get("grupo", "futebol"); force = request.args.get("force") == "1"
    if grupo != "aovivo" and grupo not in GROUPS: return err("Esporte inválido.")
    base = "futebol" if grupo == "aovivo" else grupo
    try: ev, ts = get_group(base, force)
    except Exception as e:
        if base == "futebol" and OP_KEY:                                       # The Odds API falhou/sem credito: a OddsPapi segura o site
            op_kick(); ev, ts = [], _op["ts"] or time.time()
            if not _op["ev"]: return jsonify(ok=False, msg=str(e)), 502
        else: return jsonify(ok=False, msg=str(e)), 502
    if base == "futebol" and OP_KEY: op_kick(); ev = merge_op(ev)
    t = time.time(); out = []; dur = MAXDUR.get(base, 3 * 3600)
    for e in ev:
        st = _ts(e["commence"])
        if t - st > dur: continue                                              # jogo ja terminou (a API de odds as vezes demora a tirar)
        lv = (_live.get(e.get("key")) or (0, {}))[1].get(e["id"])
        if lv and lv.get("completed"): continue                                # placar diz que acabou
        if grupo == "aovivo" and not (-3600 < t - st < 3 * 3600): continue      # comecou ha <3h ou comeca em <1h
        out.append({**e, "started": st <= t})                                    # copia: nao suja o cache
    return jsonify(ok=True, events=out, updated=ts, op=dict(busy=bool(_op["busy"]), jogos=len(_op["ev"] or [])) if OP_KEY and base == "futebol" else None, logos=logo_map({n for e in out for n in (e["home"], e["away"])}, base)[0])

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
            if not r.ok: _fd_dbg["erro"] = f"{code}: {r.status_code} {r.text[:400]}"
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
@pub
def logos(u):
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
def montar(sel):
    """Valida as selecoes e devolve (final, odd). O preco SEMPRE vem do servidor. Levanta ValueError com a mensagem para o usuario."""
    if not sel: raise ValueError("Adicione pelo menos uma seleção.")
    odd, final, seen = 1.0, [], set()
    for s in sel:
        ev = find_event(str(s.get("matchId")))
        if not ev: raise ValueError("Jogo não encontrado. Atualize a lista de jogos.")
        if ev["id"] in seen: raise ValueError("Somente uma seleção por jogo.")
        seen.add(ev["id"])
        if datetime.fromisoformat(ev["commence"].replace("Z", "+00:00")).timestamp() <= time.time():
            raise ValueError(f"Apostas encerradas: {ev['home']} x {ev['away']}.")
        mk = str(s.get("market"))
        if mk.startswith("x:"):                       # mercado extra: o preco vem do servidor (OddsPapi), nunca do navegador
            try: xr = extra_price(ev, mk)
            except Exception as e: raise ValueError("Mercado extra indisponível agora: " + str(e)[:120])
            if not xr: raise ValueError("Esse mercado mudou ou saiu do ar. Reabra o jogo e escolha de novo.")
            odd *= xr["odd"]; final.append(dict(id=ev["id"], key=ev["key"], commence=ev["commence"], home=ev["home"], away=ev["away"], jogo=f"{ev['home']} x {ev['away']}",
                          mercado=mk, mercadoNome=xr["titulo"], palpite=xr["label"], fam=xr["fam"], hc=xr["hc"], fid=xr["fid"], odd=xr["odd"])); continue
        o = ev["odds"].get(mk)
        if not o: raise ValueError("Mercado indisponível.")
        odd *= o; final.append(dict(id=ev["id"], key=ev["key"], commence=ev["commence"], home=ev["home"], away=ev["away"], jogo=f"{ev['home']} x {ev['away']}",
                          mercado=mk, palpite=ev["home"] if mk == "1" else ev["away"] if mk == "2" else "Empate", odd=o))   # odd vem do servidor
    return final, round(odd, 2)
@app.post("/api/bets")
@need("usuario", "cambista")
def bet(u):
    d = request.get_json(silent=True) or {}; sel = d.get("selecoes") or []; stake = cents(d.get("valor"))
    por, feito_por = "", None
    if u["role"] == "cambista":                    # cambista faz o bilhete em nome de um cliente dele (debita o saldo do cliente)
        try: cid = int(d.get("clienteId"))
        except (TypeError, ValueError): cid = None
        if cid:               # (legado) cliente com conta; o fluxo normal agora e sem conta: bilhete no nome do cambista
            cli = db().execute("SELECT * FROM users WHERE id=? AND role='usuario' AND criado_por=? AND ativo=1", (cid, u["id"])).fetchone()
            if not cli: return err("Cliente inválido.")
            por = f" • feito por {u['nome']}"; feito_por, u = u, cli
    if not sel: return err("Adicione pelo menos uma seleção.")
    if stake < 100: return err("Aposta mínima: R$ 1,00.")
    try: final, odd = montar(sel)
    except ValueError as e: return err(str(e))
    c = db(); c.execute("BEGIN")
    try:
        if feito_por:        # cambista vendendo: o valor sai do saldo DO CAMBISTA; para o cliente vira fiado (credito), entra no acerto de segunda
            if stake > c.execute("SELECT saldo FROM users WHERE id=? FOR UPDATE", (feito_por["id"],)).fetchone()[0]:
                c.execute("ROLLBACK"); return err("Seu saldo é insuficiente para este bilhete. Peça recarga ao ADM.")
            c.execute("UPDATE users SET saldo=saldo-? WHERE id=?", (stake, feito_por["id"]))
            c.execute("UPDATE users SET credito=credito+? WHERE id=?", (stake, u["id"]))
        else:
            if stake > c.execute("SELECT saldo FROM users WHERE id=? FOR UPDATE", (u["id"],)).fetchone()[0]:
                c.execute("ROLLBACK"); return err("Saldo insuficiente. Peça uma recarga ao ADM.")
            c.execute("UPDATE users SET saldo=saldo-? WHERE id=?", (stake, u["id"]))
        token = secrets.token_urlsafe(9)
        tid = c.execute("INSERT INTO tickets(user_id,data,selecoes,odd,valor,retorno,feito_por,token) VALUES(?,?,?,?,?,?,?,?) RETURNING id",
                        (u["id"], now(), json.dumps(final), odd, stake, premio(stake, odd), feito_por["id"] if feito_por else None, token)).fetchone()[0]
        tx(c, "aposta", feito_por["id"] if feito_por else u["id"], None, stake,
           f"Bilhete #{tid} de {u['nome']} ({len(final)} seleç{'ões' if len(final) > 1 else 'ão'}) odd {odd:.2f}" if feito_por else f"Bilhete #{tid} ({len(final)} seleç{'ões' if len(final) > 1 else 'ão'}) odd {odd:.2f}")
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK"); raise
    cr = c.execute("SELECT nome, telefone FROM users WHERE id=? AND role='cambista'", (u["criado_por"],)).fetchone() if u["criado_por"] else None
    return jsonify(ok=True, id=tid, token=token, odd=odd, valor=stake / 100, retorno=premio(stake, odd) / 100, selecoes=final, usuario=u["nome"],
                   cambista=dict(nome=cr["nome"], telefone=cr["telefone"] or "") if cr else None)
def _estorno(c, t, tid, suf):
    if t["feito_por"]:       # bilhete vendido por cambista: devolve ao saldo dele e tira o fiado do cliente
        c.execute("UPDATE users SET saldo=saldo+? WHERE id=?", (t["valor"], t["feito_por"])); c.execute("UPDATE users SET credito=credito-? WHERE id=?", (t["valor"], t["user_id"]))
        tx(c, "estorno", None, t["feito_por"], t["valor"], f"Estorno bilhete #{tid}{suf}")
    else:
        c.execute("UPDATE users SET saldo=saldo+? WHERE id=?", (t["valor"], t["user_id"])); tx(c, "estorno", None, t["user_id"], t["valor"], f"Estorno bilhete #{tid}{suf}")
@app.post("/api/tickets/<int:tid>/settle")
@need("admin")      # cambista nao decide resultado: a liquidacao e automatica (ADM so como excecao)
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
            _estorno(c, t, tid, "")
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK"); raise
    return jsonify(ok=True)

# ---------------------------------------------------------------- placar ao vivo
LIVE_TTL = int(os.environ.get("LIVE_POLL_SECONDS", "30"))
_live = {}
def _ts(iso): return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
def live_for_key(key, ttl=None):
    ts, data = _live.get(key, (0, {}))
    if key.startswith("op_"): return {}                  # liga da OddsPapi: sem placar na The Odds API
    if time.time() - ts < (LIVE_TTL if ttl is None else ttl): return data
    def num(v):
        try: return int(float(v))
        except Exception: return None
    try:
        r = requests.get(f"{ODDS_BASE}/sports/{key}/scores/", params={"apiKey": ODDS_KEY, "daysFrom": 3}, timeout=15); r.raise_for_status()
        out = {}
        for e in r.json():
            sc = {x["name"]: x["score"] for x in (e.get("scores") or [])}
            out[e["id"]] = dict(home=num(sc.get(e["home_team"])), away=num(sc.get(e["away_team"])), completed=bool(e.get("completed")), started=e.get("scores") is not None or bool(e.get("completed")))
        _live[key] = (time.time(), out); return out
    except Exception:
        _live[key] = (time.time(), data); return data      # mantem o ultimo placar e nao insiste a cada segundo
LIST_TTL = int(os.environ.get("LIVE_LIST_SECONDS", "90"))     # placar na lista de jogos: cada liga com jogo rolando gasta 1-2 creditos da The Odds API a cada atualizacao
@app.get("/api/placares")
@pub
def placares(u):
    t = time.time(); evs = {}
    for g_ in GROUPS:
        for e in cached(g_)[1] or []:
            if e.get("key") and not e["key"].startswith("op_") and 0 <= t - _ts(e["commence"]) < 3 * 3600: evs.setdefault(e["key"], set()).add(e["id"])
    out = {}
    for k, ids in evs.items(): out.update({i: v for i, v in live_for_key(k, LIST_TTL).items() if i in ids and v.get("started")})
    return jsonify(ok=True, placares=out)
@app.get("/api/live")
@need()
def live(u):
    c = db(); ids = scope_ids(c, u); keys, want = set(), set(); t = time.time()
    sql = "SELECT selecoes FROM tickets WHERE status='aberto'" + ("" if ids is None else f" AND user_id IN ({','.join('?'*len(ids))})")
    for r in c.execute(sql, tuple(ids or ())):
        for s_ in json.loads(r["selecoes"]):
            if s_.get("key") and not s_["key"].startswith("op_") and s_.get("commence") and 0 <= t - _ts(s_["commence"]) < 6 * 3600: keys.add(s_["key"]); want.add(s_["id"])
    out = {}
    for k in keys: out.update({i: v for i, v in live_for_key(k).items() if i in want})
    return jsonify(ok=True, live=out)

# ---------------------------------------------------------------- mercados extras (gols, ambas marcam, dupla chance, escanteios, cartoes, faltas)
# Vem da OddsPapi: 1 chamada /odds ao abrir o jogo (guardada 10 min) + catalogo de mercados 1x por semana. Corners/cartoes dependem da casa e do jogo ter esses mercados.
XB = [b.strip() for b in os.environ.get("ODDSPAPI_EXTRA_BOOKS", "pinnacle,bet365").split(",") if b.strip()][:4]
XTTL = 600
_cat, _xm = {"ts": 0, "d": None}, {}
FAM_ORD = ["gols", "btts", "dc", "escanteios", "cartoes", "faltas"]
FAM_TIT = {"gols": "Total de Gols", "btts": "Ambas Marcam", "dc": "Dupla Chance", "escanteios": "Escanteios", "cartoes": "Cartões", "faltas": "Faltas"}
FAM_SLIP = {"gols": "Total de Gols", "btts": "Ambas as Equipes Marcam", "dc": "Dupla Chance", "escanteios": "Total de Escanteios", "cartoes": "Total de Cartões", "faltas": "Total de Faltas"}
FAM_NOUN = {"gols": "gols", "escanteios": "escanteios", "cartoes": "cartões", "faltas": "faltas"}
def _famil(name, per):
    n = (name or "").lower().strip()
    if per != "fulltime": return None
    if n == "both teams to score": return "btts"
    if "double chance" in n and not any(w in n for w in ("half", "1st", "2nd", "&", " and ")): return "dc"
    if any(w in n for w in ("1st", "2nd", "half", "team", "player", "home", "away", "asian", "handicap", "exact", "odd even", "range", "first", "last", "race", "&", " and ", "combo")): return None
    if n == "over under full time": return "gols"
    if "over under" not in n: return None
    if n.startswith("corners"): return "escanteios"
    if "booking" in n or "card" in n: return "cartoes"
    if "foul" in n: return "faltas"
    return None
def op_catalog():
    if _cat["d"] is not None and time.time() - _cat["ts"] < 7 * 86400: return _cat["d"]
    ts, v = kv_get("op:cat:soccer1")
    if v and time.time() - ts < 7 * 86400: _cat.update(ts=ts, d=v); return v
    lst = op_get("/markets", reserve=OP_RESERVE, sportId=10); d = {}
    for m in lst if isinstance(lst, list) else []:
        if m.get("playerProp"): continue
        hc = m.get("handicap"); f = _famil(m.get("marketName"), m.get("period"))
        if not f: continue
        if f in FAM_NOUN and not (isinstance(hc, (int, float)) and abs(hc % 1 - 0.5) < 1e-9): continue     # so linhas .5 (sem devolucao)
        d[str(m["marketId"])] = [f, hc, m.get("marketName"), {str(o["outcomeId"]): o.get("outcomeName") for o in m.get("outcomes", [])}]
    if not d: raise RuntimeError("Catálogo de mercados da OddsPapi veio vazio.")
    kv_set("op:cat:soccer1", d); _cat.update(ts=time.time(), d=d); return d
def fixture_for(ev):
    eid = str(ev["id"])
    if eid.startswith("op:"): return eid[3:]
    if _op["ev"] is None: op_kick()
    t = _ts(ev["commence"]); h, a = norm(ev["home"]), norm(ev["away"])
    for e in _op["ev"] or []:
        if abs(_ts(e["commence"]) - t) <= 14400 and _same(h, norm(e["home"])) and _same(a, norm(e["away"])): return str(e["id"])[3:]
def _lab(f, on, hc, ev):
    on = str(on or "")
    if f == "btts": return {"yes": "Sim", "no": "Não"}.get(on.lower(), on)
    if f == "dc": return {"1X": ev["home"] + " ou Empate", "12": ev["home"] + " ou " + ev["away"], "X2": "Empate ou " + ev["away"]}.get(on.upper(), on)
    return {"over": "Mais de ", "under": "Menos de "}.get(on.lower(), on + " ") + str(hc).replace(".", ",")
def _build_groups(ev, cat, boards):
    got = {}
    for b in XB:                                                    # cada mercado inteiro vem de UMA casa (nao mistura precos)
        for mid, m in ((boards.get(b) or {}).get("markets") or {}).items():
            ci = cat.get(str(mid))
            if not ci or str(mid) in got or m.get("marketActive") is False: continue
            pr = {}
            for oid in ci[3]:
                p = (((m.get("outcomes") or {}).get(oid) or {}).get("players") or {}).get("0") or {}
                v = p.get("price")
                if p.get("active", True) and isinstance(v, (int, float)) and v > 1: pr[oid] = v
            if len(pr) == len(ci[3]): got[str(mid)] = pr
    gr, flat = {f: [] for f in FAM_ORD}, {}
    for mid, pr in got.items():
        f, hc, name, outs = cat[mid]; opts = []
        for oid in sorted(outs, key=int):
            k = f"x:{mid}:{oid}"; lab = _lab(f, outs[oid], hc, ev); sl = lab + (" " + FAM_NOUN[f] if f in FAM_NOUN else "")
            opts.append(dict(k=k, l=lab, o=pr[oid], s=sl))
            flat[k] = dict(odd=pr[oid], label=sl, titulo=FAM_SLIP[f], fam=f, hc=hc)
        gr[f].append((hc or 0, dict(t=("Mais/Menos " + str(hc).replace(".", ",")) if f in FAM_NOUN else FAM_TIT[f], o=opts)))
    groups = [dict(id=f, t=FAM_TIT[f], m=[m for _, m in sorted(gr[f], key=lambda x: x[0])]) for f in FAM_ORD if gr[f]]
    return groups, flat
def op_markets(ev):
    fid = fixture_for(ev)
    if not fid: raise RuntimeError("Este jogo ainda não está ligado à OddsPapi (sem mercados extras).")
    c = _xm.get(fid)
    if c and time.time() - c[0] < XTTL: return c[1], c[2], fid
    cat = op_catalog()
    d = op_get("/odds", reserve=OP_RESERVE, fixtureId=fid, bookmakers=",".join(XB))
    groups, flat = _build_groups(ev, cat, (d or {}).get("bookmakerOdds") or {})
    if len(_xm) > 80: _xm.pop(min(_xm, key=lambda k: _xm[k][0]), None)
    _xm[fid] = (time.time(), groups, flat); return groups, flat, fid
def extra_price(ev, key):
    groups, flat, fid = op_markets(ev); x = flat.get(key)
    return dict(x, fid=fid) if x else None
@app.get("/api/jogo")
@pub
def jogo(u):
    ev = find_event(str(request.args.get("id", "")))
    if not ev: return err("Jogo não encontrado. Atualize a lista.", 404)
    if ev.get("grupo", "futebol") != "futebol": return jsonify(ok=True, grupos=[], motivo="Mercados extras só estão disponíveis para futebol.")
    if _ts(ev["commence"]) <= time.time(): return jsonify(ok=True, grupos=[], motivo="Jogo em andamento: apostas encerradas.")
    try: groups, flat, fid = op_markets(ev)
    except Exception as e: return jsonify(ok=True, grupos=[], motivo=str(e)[:220])
    return jsonify(ok=True, grupos=groups, motivo="" if groups else "Esta casa ainda não abriu mercados extras para este jogo (escanteios e cartões costumam abrir uns 3 dias antes).")

# ---------------------------------------------------------------- liquidacao automatica dos bilhetes
AUTO_SETTLE = os.environ.get("AUTO_SETTLE", "1") == "1"            # AUTO_SETTLE=0 desliga (volta a ser so manual)
_op_res, _op_try = {}, {}
OUT_ID = {"1": "101", "X": "102", "2": "103"}
_op_raw = {}
def op_settle_raw(fid):
    """Todos os mercados liquidados do jogo (1 chamada /settlements, guardada 25 min e com pausa de 20 min entre tentativas)."""
    c = _op_raw.get(fid)
    if c and time.time() - c[0] < 1500: return c[1]
    if time.time() - _op_try.get(fid, 0) < 1200: return None
    _op_try[fid] = time.time()
    try: d = op_get("/settlements", fixtureId=fid)
    except Exception as e: print("settlements falhou:", e, flush=True); return None
    mk = d.get("markets") if isinstance(d, dict) else None
    if mk:
        if len(_op_raw) > 60: _op_raw.pop(min(_op_raw, key=lambda k: _op_raw[k][0]), None)
        _op_raw[fid] = (time.time(), mk)
    return mk or None
def op_outcome_result(fid, mid, oid):
    k = f"op:xres:{fid}:{mid}:{oid}"
    if k in _op_res: return _op_res[k]
    v = kv_get(k)[1]
    if v: _op_res[k] = v; return v
    raw = op_settle_raw(fid)
    if not raw: return None
    r = ((((raw.get(mid) or {}).get("outcomes") or {}).get(oid) or {}).get("players") or {}).get("0") or {}
    r = r.get("result")
    if r in ("WIN", "LOSE"):
        _op_res[k] = r
        try: kv_set(k, r)
        except Exception: pass
        return r
    return None
def op_result(fid):
    """Resultado do jogo da OddsPapi (1 chamada /settlements por jogo; guardado no banco, nao repete)."""
    if fid in _op_res: return _op_res[fid]
    v = kv_get("op:res:" + fid)[1]
    if v: _op_res[fid] = v; return v
    mkts = op_settle_raw(fid)
    if not mkts: return None
    m = (mkts.get("101") or {}).get("outcomes") or {}
    res = {k: ((((m.get(o) or {}).get("players") or {}).get("0")) or {}).get("result") for k, o in OUT_ID.items()}
    if "WIN" in res.values() and all(r in ("WIN", "LOSE") for r in res.values() if r):
        _op_res[fid] = res
        try: kv_set("op:res:" + fid, res)
        except Exception: pass
        return res
    return None
# ---- SportAPI: resultado dos jogos de futebol (plano gratis: 2.000 chamadas/dia). Chave SO na variavel de ambiente SPORTAPI_KEY.
SA_KEY = os.environ.get("SPORTAPI_KEY", "").strip().strip("\"'").strip()
SA_BASE = os.environ.get("SPORTAPI_BASE", "https://sportapi.ai/api").rstrip("/")
SA_HEADER = os.environ.get("SPORTAPI_HEADER", "").strip()            # nome do cabecalho da chave; vazio = tenta X-API-Key e depois Authorization: Bearer
SA_DAILY = int(os.environ.get("SPORTAPI_DAILY_MAX", "1500"))         # trava de seguranca (o plano gratis permite 2.000/dia)
SA_TTL = 240
_sa = {"cache": {}, "hdr": None, "err": "", "n": 0, "day": ""}
_sa_lock = threading.Lock()
def sa_day(date, ttl=SA_TTL):
    """Jogos de futebol de uma data (AAAA-MM-DD, UTC) segundo a SportAPI; a lista fica guardada 4 min (1 min no placar ao vivo)."""
    c = _sa["cache"].get(date)
    if c and time.time() - c[0] < ttl: return c[1]
    if not SA_KEY: raise RuntimeError("SPORTAPI_KEY não definida.")
    with _sa_lock:
        d0 = time.strftime("%Y-%m-%d", time.gmtime())
        if _sa["day"] != d0: _sa["day"], _sa["n"] = d0, 0
        names = [SA_HEADER] if SA_HEADER else [_sa["hdr"]] if _sa["hdr"] else ["X-API-Key", "Authorization"]
        last = None
        for name in names:
            if _sa["n"] >= SA_DAILY: raise RuntimeError("Limite diário de segurança da SportAPI atingido.")
            hdr = {**UA, name: ("Bearer " + SA_KEY) if name.lower() == "authorization" else SA_KEY}
            _sa["n"] += 1
            try: r = requests.get(f"{SA_BASE}/fixtures/date/{date}", headers=hdr, timeout=25)
            except Exception as e: raise RuntimeError(("SportAPI fora do ar: " + str(e))[:150].replace(SA_KEY, "***"))
            if r.status_code in (401, 403): last = r.status_code; continue
            if not r.ok: raise RuntimeError(f"SportAPI {r.status_code}: {r.text[:200]}".replace(SA_KEY, "***"))
            d = r.json(); lst = d.get("data") if isinstance(d, dict) else None
            if not isinstance(lst, list): raise RuntimeError("SportAPI: resposta sem a lista 'data'.")
            _sa["hdr"], _sa["err"] = name, ""
            if len(_sa["cache"]) > 12: _sa["cache"].pop(min(_sa["cache"], key=lambda k: _sa["cache"][k][0]), None)
            _sa["cache"][date] = (time.time(), lst); return lst
        raise RuntimeError(f"SportAPI recusou a chave ({last}). Veja o nome do cabeçalho na documentação e defina SPORTAPI_HEADER no Render.")
def sa_find(s_, ttl=SA_TTL):
    """Acha o jogo da selecao na SportAPI: os DOIS times precisam bater e o horario ficar a menos de 3h."""
    t = _ts(s_["commence"]); hn, an = norm(s_.get("home", "")), norm(s_.get("away", ""))
    if not hn or not an: return None
    d = datetime.fromtimestamp(t, timezone.utc).date()
    for off in (0, -1, 1):
        lst = sa_day((d + timedelta(days=off)).isoformat(), ttl)
        for f in lst:
            try: k = _ts(f.get("kickoff_utc") or "")
            except Exception: continue
            if abs(k - t) > 3 * 3600: continue
            if any(_same(hn, norm(f.get(x) or "")) for x in ("home_team", "home_short")) and any(_same(an, norm(f.get(x) or "")) for x in ("away_team", "away_short")): return f
    return None
def sa_verdict(s_):
    """'won' | 'lost' | None. So decide resultado final (1/X/2) de jogo com status FT e dados coerentes; o resto fica para os outros metodos ou para o ADM."""
    try:
        mk = s_.get("mercado")
        if not SA_KEY or mk not in ("1", "X", "2") or not s_.get("commence"): return None
        f = sa_find(s_)
        if not f or f.get("status") != "FT" or f.get("penalty_home") is not None or f.get("penalty_away") is not None: return None
        h, a = f.get("home_score"), f.get("away_score")
        if not all(isinstance(v, int) and not isinstance(v, bool) for v in (h, a)): return None
        out = "1" if h > a else "2" if a > h else "X"
        w = {"home": "1", "away": "2", "draw": "X"}.get(f.get("winner"))
        if w and w != out: return None                  # a propria API se contradiz: nao arrisca, fica para o ADM
        return "won" if out == mk else "lost"
    except Exception as e:
        _sa["err"] = str(e)[:300]; print("sportapi falhou:", e, flush=True); return None
def sa_live(s_):
    """Placar atual do jogo da selecao, no mesmo formato do live_for_key (home, away, started, completed). None = sem dado confiavel."""
    try:
        f = sa_find(s_, 60)
        if not f: return None
        h, a, st = f.get("home_score"), f.get("away_score"), f.get("status")
        if st == "Scheduled" or not all(isinstance(v, int) and not isinstance(v, bool) for v in (h, a)): return None
        return dict(home=h, away=a, started=True, completed=(st == "FT"), minute=f.get("minute") if st == "LIVE" else None)
    except Exception as e:
        _sa["err"] = str(e)[:300]; return None
@app.get("/api/admin/sportapi")
@need("admin")
def sa_status(u):                                         # diagnostico: abra /api/admin/sportapi?testar=1 logado como admin
    out = dict(ok=True, chave_definida=bool(SA_KEY), tamanho_chave=len(SA_KEY), chamadas_hoje=_sa["n"], limite_dia=SA_DAILY, cabecalho=_sa["hdr"], ultimo_erro=_sa["err"])
    if request.args.get("testar") == "1":
        try:
            lst = sa_day(time.strftime("%Y-%m-%d", time.gmtime()))
            out.update(jogos_hoje=len(lst), encerrados=sum(1 for f in lst if f.get("status") == "FT"), ao_vivo=sum(1 for f in lst if f.get("status") == "LIVE"), cabecalho=_sa["hdr"])
        except Exception as e: out.update(ok=False, erro=str(e)[:300])
    return jsonify(out)
def sel_verdict(s_):
    """'won' | 'lost' | None (ainda sem resultado confiavel). So decide com jogo encerrado de verdade."""
    if not s_.get("commence") or time.time() - _ts(s_["commence"]) < 100 * 60: return None
    mk, sid, key = s_.get("mercado"), str(s_.get("id", "")), str(s_.get("key", ""))
    if str(mk).startswith("x:"):                          # mercado extra: resultado vem da OddsPapi (WIN/LOSE); sem resultado claro fica aberto para o ADM
        try: _, mid, oid = str(mk).split(":")
        except ValueError: return None
        return {"WIN": "won", "LOSE": "lost"}.get(op_outcome_result(str(s_.get("fid") or ""), mid, oid)) if s_.get("fid") else None
    if SA_KEY and mk in ("1", "X", "2") and (sid.startswith("op:") or key.startswith("soccer_")):      # resultado pela SportAPI (gratis): poupa a cota da OddsPapi
        v = sa_verdict(s_)
        if v: return v
        if sid.startswith("op:") and time.time() - _ts(s_["commence"]) < 8 * 3600: return None          # ainda sem resultado la: espera, sem gastar chamada da OddsPapi
    if sid.startswith("op:"):
        r = (op_result(sid[3:]) or {}).get(mk)
        return {"WIN": "won", "LOSE": "lost"}.get(r)
    if not key.startswith(("soccer_", "basketball_")): return None      # tenis/MMA continuam manuais
    lv = live_for_key(key).get(sid)
    if not lv or not lv.get("completed") or lv.get("home") is None or lv.get("away") is None: return None
    out = "1" if lv["home"] > lv["away"] else "2" if lv["away"] > lv["home"] else "X"
    return "won" if out == mk else "lost"
def settle_tx(c, tid, res, auto=False):
    c.execute("BEGIN")
    try:
        t = c.execute("SELECT * FROM tickets WHERE id=? AND status='aberto' FOR UPDATE", (tid,)).fetchone()
        if not t: c.execute("ROLLBACK"); return False
        c.execute("UPDATE tickets SET status=? WHERE id=?", (res, tid)); suf = " (automático)" if auto else ""
        if res == "ganhou":
            c.execute("UPDATE users SET saldo=saldo+? WHERE id=?", (t["retorno"], t["user_id"])); tx(c, "premio", None, t["user_id"], t["retorno"], f"Prêmio bilhete #{tid}{suf}")
        elif res == "cancelado":
            _estorno(c, t, tid, suf)
        c.execute("COMMIT"); return True
    except Exception:
        c.execute("ROLLBACK"); raise
def auto_settle_once():
    c = raw()
    try:
        for r in c.execute("SELECT id, selecoes FROM tickets WHERE status='aberto' ORDER BY id").fetchall():
            lost, allwon = False, True
            for s_ in json.loads(r["selecoes"]):
                v = sel_verdict(s_)
                if v == "lost": lost = True; break              # uma selecao perdida ja decide o bilhete
                if v != "won": allwon = False
            res = "perdeu" if lost else "ganhou" if allwon else None
            if res and settle_tx(c, r["id"], res, True): print(f"bilhete #{r['id']} liquidado automaticamente: {res}", flush=True)
    finally: c.close()
def settle_loop():
    time.sleep(25)
    while True:
        try: auto_settle_once()
        except Exception as e: print("auto_settle falhou:", e, flush=True)
        time.sleep(120)
if AUTO_SETTLE: threading.Thread(target=settle_loop, daemon=True).start()

@app.get("/api/admin/oddspapi")
@need("admin")
def op_diag(u):
    """Diagnostico: /api/admin/oddspapi  (status)  •  ?run=1 (forca atualizar agora)  •  ?conta=1 (consulta a conta; nao gasta cota)."""
    if not OP_KEY: return err("Defina ODDSPAPI_KEY no Render.")
    if request.args.get("run") == "1": op_kick(force=True)
    elif _op["ev"] is None: op_kick()
    out = dict(chave_definida=True, jogos=len(_op["ev"] or []), atualizado_ha_min=round((time.time() - _op["ts"]) / 60) if _op["ts"] else None, atualizando=_op["busy"],
               info_ultima=_op["info"], odds_api_jogos=len(cached("futebol")[1] or []), uso_no_mes=op_used(), limite=OP_LIMIT, reserva=OP_RESERVE, bookmakers=OP_BOOKS, dias=OP_DAYS, renova_a_cada_horas=OP_HOURS, ultimo_erro=_op["err"], auto_liquidacao=AUTO_SETTLE)
    if request.args.get("conta") == "1":
        def sc(x): return {k: sc(v) for k, v in x.items() if isinstance(v, (dict, int, float, bool)) or k in ("plan", "planName")} if isinstance(x, dict) else x
        try: out["conta"] = sc(requests.get(OP_BASE + "/account", params={"apiKey": OP_KEY}, headers=UA, timeout=20).json())
        except Exception as e: out["conta"] = "falhou: " + str(e).replace(OP_KEY, "***")[:120]
    return jsonify(ok=True, **out)

# ---------------------------------------------------------------- 5DollarFootballAPI (diagnostico)
# A chave vem SEMPRE da variavel FIVEDOLLAR_KEY no Render (nunca no codigo).
FD5_KEY = os.environ.get("FIVEDOLLAR_KEY", "").strip().strip("\"'").strip()
FD5_BASE = "https://api.5dollarfootballapi.com/v1"
@app.get("/api/admin/5dollar")
@need("admin")
def fd5_diag(u):
    """Teste: /api/admin/5dollar?path=/fixtures  •  ?path=/fixtures&status=scheduled  •  ?path=/fixtures/ID/odds  •  ?path=/bookmakers
    Mostra a resposta CRUA (cortada) para eu ver o formato real. Qualquer outro parametro da URL vai junto para a API."""
    if not FD5_KEY: return err("Defina FIVEDOLLAR_KEY no Render.")
    path = request.args.get("path", "/fixtures")
    if not path.startswith("/") or "://" in path or ".." in path: return err("path inválido.")
    params = {k: v for k, v in request.args.items() if k != "path"}
    try:
        r = requests.get(FD5_BASE + path, params=params, headers={**UA, "Authorization": "Bearer " + FD5_KEY}, timeout=25)
        txt = r.text.replace(FD5_KEY, "***")
        return jsonify(ok=r.ok, status=r.status_code, limite_minuto=r.headers.get("X-RateLimit-Limit"), restante=r.headers.get("X-RateLimit-Remaining"), tamanho=len(txt), resposta=txt[:5000])
    except Exception as e: return jsonify(ok=False, erro=str(e).replace(FD5_KEY, "***")[:200]), 502

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
                c.execute("UPDATE users SET saldo=?, credito=? WHERE id=?", (us_ * 100, us_ * 100, r)); tx(c, "credito", cam["id"], r, us_ * 100, "Limite (fiado) para " + un)
    return jsonify(ok=True, criados=n)
@app.post("/api/admin/reset")
@need("admin")
def reset(u):
    c = db(); c.execute("BEGIN")
    for t in ("acertos", "pins", "transactions", "tickets"): c.execute(f"DELETE FROM {t}")     # acertos/pins antes de apagar os usuarios (chave estrangeira)
    c.execute("DELETE FROM users WHERE role!='admin'"); c.execute("UPDATE users SET pw=?, senha_padrao=1 WHERE id=?", (generate_password_hash("admin123"), u["id"])); c.execute("COMMIT")
    session.clear(); return jsonify(ok=True)
# ---------------------------------------------------------------- comissoes (menu proprio: cambista ve a dele, ADM ve todas)
def _com(cents): return round(float(cents or 0) * COMISSAO / 100, 2)
_COM_BASE = ("FROM tickets t JOIN users x ON x.id=t.user_id JOIN users cam ON cam.id=(CASE WHEN x.role='cambista' THEN x.id ELSE x.criado_por END) "
             "WHERE cam.role='cambista' AND t.status!='cancelado'")      # bilhete anulado nao conta
def _com_cambista(c, cid):
    rows = c.execute("SELECT x.id, (CASE WHEN x.role='cambista' THEN 'Clientes (PIN)' ELSE x.nome END) AS nome, COUNT(*) AS n, COALESCE(SUM(t.valor),0) AS tot " + _COM_BASE + " AND cam.id=? GROUP BY x.id, x.role, x.nome ORDER BY tot DESC", (cid,)).fetchall()
    rec = c.execute("SELECT t.id, t.data, (CASE WHEN x.role='cambista' THEN 'Cliente (PIN)' ELSE x.nome END) AS nome, t.valor, t.status " + _COM_BASE + " AND cam.id=? ORDER BY t.id DESC LIMIT 60", (cid,)).fetchall()
    tot = sum(float(r["tot"]) for r in rows)
    return dict(pct=COMISSAO_PCT, apostado=tot / 100, comissao=_com(tot), bilhetes=sum(r["n"] for r in rows),
                clientes=[dict(nome=r["nome"], bilhetes=r["n"], apostado=float(r["tot"]) / 100, comissao=_com(r["tot"])) for r in rows],
                recentes=[dict(id=str(r["id"]), data=r["data"], cliente=r["nome"], valor=r["valor"] / 100, comissao=_com(r["valor"]), status=r["status"]) for r in rec])
# ---------------------------------------------------------------- PIN: o cliente (sem conta) monta o bilhete e recebe um codigo; o cambista digita e confirma
PIN_TTL = 12 * 3600
_pin_hits = {}
@app.post("/api/pin")
def pin_criar():
    d = request.get_json(silent=True) or {}; sel = [dict(matchId=str(x.get("matchId")), market=str(x.get("market"))) for x in (d.get("selecoes") or [])[:20] if isinstance(x, dict)]
    stake = cents(d.get("valor"))
    ip = (request.headers.get("X-Forwarded-For") or request.remote_addr or "?").split(",")[0].strip(); t = time.time()
    hits = [x for x in _pin_hits.get(ip, []) if t - x < 3600]
    if len(hits) >= 30: return err("Muitos bilhetes seguidos. Aguarde um pouco e tente de novo.", 429)
    if stake < 100: return err("Aposta mínima: R$ 1,00.")
    if stake > 5000000: return err("Valor muito alto.")
    try: final, odd = montar(sel)
    except ValueError as e: return err(str(e))
    c = db(); code = None
    for _ in range(12):
        cand = str(secrets.randbelow(900000) + 100000)
        if not c.execute("SELECT 1 FROM pins WHERE code=?", (cand,)).fetchone(): code = cand; break
    if not code: return err("Tente de novo.", 503)
    token = secrets.token_urlsafe(9)
    c.execute("INSERT INTO pins(code,criado,selecoes,valor,token) VALUES(?,?,?,?,?)", (code, t, json.dumps(sel), stake, token))
    hits.append(t); _pin_hits[ip] = hits
    return jsonify(ok=True, pin=code, token=token, odd=odd, valor=stake / 100, retorno=premio(stake, odd) / 100, selecoes=final)
def _pin_load(c, code):
    p = c.execute("SELECT * FROM pins WHERE code=?", (str(code).strip(),)).fetchone()
    if not p or time.time() - p["criado"] > PIN_TTL: return None, "PIN não encontrado ou expirado. Peça ao cliente para gerar outro."
    if p["usado"]: return None, "Este PIN já foi usado em um bilhete."
    return p, ""
@app.get("/api/pin/<code>")
@need("cambista")
def pin_ver(u, code):
    c = db(); p, m = _pin_load(c, code)
    if not p: return err(m, 404)
    try: final, odd = montar(json.loads(p["selecoes"]))
    except ValueError as e: return err(str(e))
    return jsonify(ok=True, pin=p["code"], odd=odd, valor=p["valor"] / 100, retorno=premio(p["valor"], odd) / 100, selecoes=final, saldo=u["saldo"] / 100)
@app.post("/api/pin/<code>/confirmar")
@need("cambista")
def pin_confirmar(u, code):
    d = request.get_json(silent=True) or {}; c = db(); p, m = _pin_load(c, code)
    if not p: return err(m, 404)
    try: final, odd = montar(json.loads(p["selecoes"]))
    except ValueError as e: return err(str(e))
    stake = cents(d["valor"]) if d.get("valor") not in (None, "") else int(p["valor"])
    if stake < 100: return err("Aposta mínima: R$ 1,00.")
    nome = " ".join(str(d.get("nome") or "").split())[:40]          # nome do cliente (opcional) para sair no bilhete
    c.execute("BEGIN")
    try:
        pp = c.execute("SELECT usado FROM pins WHERE code=? FOR UPDATE", (p["code"],)).fetchone()
        if not pp or pp["usado"]: c.execute("ROLLBACK"); return err("Este PIN já foi usado em um bilhete.")
        if stake > c.execute("SELECT saldo FROM users WHERE id=? FOR UPDATE", (u["id"],)).fetchone()[0]:
            c.execute("ROLLBACK"); return err("Seu saldo é insuficiente para este bilhete. Peça recarga ao ADM.")
        c.execute("UPDATE users SET saldo=saldo-? WHERE id=?", (stake, u["id"]))
        tid = c.execute("INSERT INTO tickets(user_id,data,selecoes,odd,valor,retorno,cliente,token) VALUES(?,?,?,?,?,?,?,?) RETURNING id",
                        (u["id"], now(), json.dumps(final), odd, stake, premio(stake, odd), nome, p["token"] or secrets.token_urlsafe(9))).fetchone()[0]
        tx(c, "aposta", u["id"], None, stake, f"Bilhete #{tid} {nome + ' ' if nome else ''}PIN {p['code']} ({len(final)} seleç{'ões' if len(final) > 1 else 'ão'}) odd {odd:.2f}")
        c.execute("UPDATE pins SET usado=1, usado_por=?, ticket_id=? WHERE code=?", (u["id"], tid, p["code"]))
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK"); raise
    tk = c.execute("SELECT token FROM tickets WHERE id=?", (tid,)).fetchone()[0]
    return jsonify(ok=True, id=tid, token=tk, odd=odd, valor=stake / 100, retorno=premio(stake, odd) / 100, selecoes=final, usuario=nome or ("Cliente (PIN " + p["code"] + ")"))

# ---------------------------------------------------------------- bilhete publico: o cliente acompanha pelo link /b/<token> (sem login)
def _sel_estado(s, lv):
    if not lv or not lv.get("started") or s.get("mercado") not in ("1", "X", "2") or lv.get("home") is None or lv.get("away") is None: return "aguardando"
    h, a = lv["home"], lv["away"]; atual = "1" if h > a else "2" if a > h else "X"
    if lv.get("completed"): return "ganhou" if atual == s["mercado"] else "perdeu"
    return "ganhando" if atual == s["mercado"] else "perdendo"
@app.get("/api/bilhete/<token>")
def bilhete_publico(token):
    c = db(); t = c.execute("SELECT * FROM tickets WHERE token=?", (token,)).fetchone()
    if not t:
        p = c.execute("SELECT * FROM pins WHERE token=?", (token,)).fetchone()
        if not p or time.time() - p["criado"] > PIN_TTL: return err("Bilhete não encontrado ou expirado.", 404)
        if p["usado"]: return err("Bilhete já confirmado. Atualize a página.", 404)
        try: final, odd = montar(json.loads(p["selecoes"]))
        except ValueError as e: return err(str(e), 410)
        return jsonify(ok=True, pendente=True, pin=p["code"], odd=odd, valor=p["valor"] / 100, retorno=premio(p["valor"], odd) / 100,
                       selecoes=[dict(jogo=s["jogo"], palpite=s["palpite"], odd=s["odd"], home=s["home"], away=s["away"], placar=None, estado="aguardando") for s in final])
    sels = json.loads(t["selecoes"]); tt = time.time(); live = {}; sa_lv = {}
    if SA_KEY:                                          # placar pela SportAPI (gratis; lista da data guardada 1 min, varios clientes olhando gastam 1 chamada so)
        for s in sels:
            if s.get("id") and s.get("commence") and str(s.get("key", "")).startswith(("soccer_", "op_")) and 0 <= tt - _ts(s["commence"]) < 6 * 3600:
                v = sa_live(s)
                if v: sa_lv[s["id"]] = v
    for k in {s["key"] for s in sels if s.get("key") and not s["key"].startswith("op_") and s["id"] not in sa_lv and s.get("commence") and 0 <= tt - _ts(s["commence"]) < 6 * 3600}:
        live.update(live_for_key(k))                    # cache de 30s: varios clientes olhando nao gastam creditos a mais
    live.update(sa_lv)
    out = []
    for s in sels:
        lv = live.get(s["id"]); pl = {"h": lv.get("home"), "a": lv.get("away"), "m": lv.get("minute"), "fim": bool(lv.get("completed"))} if lv and lv.get("started") else None
        est = _sel_estado(s, lv)
        if t["status"] != "aberto" and est in ("ganhando", "perdendo", "aguardando"): est = "ganhou" if t["status"] == "ganhou" else est
        out.append(dict(jogo=s["jogo"], palpite=s.get("palpite") or s.get("mercado"), mercado=s.get("mercadoNome") or "", odd=s["odd"], home=s.get("home", ""), away=s.get("away", ""), placar=pl, estado=est))
    return jsonify(ok=True, pendente=False, id=t["id"], cliente=t["cliente"] or "", status=t["status"], odd=t["odd"],
                   valor=t["valor"] / 100, retorno=t["retorno"] / 100, selecoes=out)
@app.get("/sw.js")
def service_worker():                                  # na raiz para o escopo do PWA cobrir o site todo
    r = send_from_directory(os.path.join(BASE, "static"), "sw.js", mimetype="application/javascript")
    r.headers["Cache-Control"] = "no-cache"; return r
@app.get("/b/<token>")
def pagina_bilhete(token):
    r = send_from_directory(os.path.join(BASE, "static"), "bilhete.html")
    r.headers["Cache-Control"] = "no-cache, must-revalidate"; return r

# ---------------------------------------------------------------- caixa dos cambistas (so ADM): quem deve a quem
# Conta do cambista (tudo que os clientes dele apostaram, PIN ou conta; bilhete anulado nao conta):
#   deve ao ADM   = apostado - comissao            (dinheiro que ele recebeu dos clientes e repassa ao ADM)
#   ADM paga      = premios dos bilhetes que bateram (o cambista paga o cliente e o ADM cobre)
#   resultado     = deve ao ADM - premios - acertos ja registrados
#       > 0  o cambista esta devendo ao ADM (vermelho)   |   < 0  o ADM deve pagar o cambista (verde)
# acertos: guarda em `acertos` com user_id = id do cambista e cambista_id NULL (valor > 0 = cambista pagou o ADM; < 0 = ADM pagou o cambista)
def _caixa_cam(c, cid):
    r = c.execute("SELECT COUNT(*) AS n, COALESCE(SUM(t.valor),0) AS apostado, "
                  "COALESCE(SUM(CASE WHEN t.status='aberto' THEN t.valor END),0) AS ab_valor, "
                  "COALESCE(SUM(CASE WHEN t.status='aberto' THEN t.retorno END),0) AS ab_ret, "
                  "COALESCE(SUM(CASE WHEN t.status='ganhou' THEN t.retorno END),0) AS premios, "
                  "COUNT(CASE WHEN t.status='aberto' THEN 1 END) AS n_aberto, COUNT(CASE WHEN t.status='ganhou' THEN 1 END) AS n_ganhou " + _COM_BASE + " AND cam.id=?", (cid,)).fetchone()
    apostado, premios, ab_ret = int(r["apostado"]), int(r["premios"]), int(r["ab_ret"])
    comissao = int(round(apostado * COMISSAO)); deve = apostado - comissao
    pago = int(c.execute("SELECT COALESCE(SUM(valor),0) FROM acertos WHERE user_id=? AND cambista_id IS NULL", (cid,)).fetchone()[0])
    resultado = deve - premios - pago                         # centavos
    saldo = int(c.execute("SELECT saldo FROM users WHERE id=?", (cid,)).fetchone()[0])
    return dict(pct=COMISSAO_PCT, bilhetes=r["n"], abertos=r["n_aberto"], ganhos=r["n_ganhou"], apostado=apostado / 100, comissao=comissao / 100, deve=deve / 100,
                premios=premios / 100, emAberto=int(r["ab_valor"]) / 100, seBater=ab_ret / 100, acertos=pago / 100, resultado=resultado / 100, saldo=saldo / 100)
@app.get("/api/caixa-cambistas")
@need("admin")
def caixa_cambistas(u):
    c = db()
    try: cid = int(request.args.get("cambista") or 0)
    except ValueError: cid = 0
    if cid:
        t = c.execute("SELECT nome FROM users WHERE id=? AND role='cambista'", (cid,)).fetchone()
        if not t: return err("Cambista não encontrado.", 404)
        rec = c.execute("SELECT t.id, t.data, (CASE WHEN x.role='cambista' THEN COALESCE(NULLIF(t.cliente,''),'Cliente (PIN)') ELSE x.nome END) AS nome, t.valor, t.retorno, t.status "
                        + _COM_BASE + " AND cam.id=? ORDER BY t.id DESC LIMIT 40", (cid,)).fetchall()
        return jsonify(ok=True, nome=t["nome"], **_caixa_cam(c, cid),
                       recentes=[dict(id=str(r["id"]), data=r["data"], cliente=r["nome"], valor=r["valor"] / 100, retorno=r["retorno"] / 100, status=r["status"]) for r in rec])
    lst = [dict(id=str(r["id"]), nome=r["nome"], ativo=bool(r["ativo"]), resultado=_caixa_cam(c, r["id"])["resultado"])
           for r in c.execute("SELECT id, nome, ativo FROM users WHERE role='cambista' ORDER BY nome").fetchall()]
    return jsonify(ok=True, cambistas=lst)
@app.post("/api/caixa-cambistas/acerto")
@need("admin")
def caixa_acerto(u):
    """Registra que o cambista pagou o que devia ao ADM (ou que o ADM pagou o cambista). valor opcional = pagamento parcial."""
    d = request.get_json(silent=True) or {}; c = db()
    try: cid = int(d.get("cambista"))
    except (TypeError, ValueError): return err("Selecione um cambista.")
    c.execute("BEGIN")
    try:
        t = c.execute("SELECT * FROM users WHERE id=? AND role='cambista' FOR UPDATE", (cid,)).fetchone()    # trava: dois cliques nao registram duas vezes
        if not t: c.execute("ROLLBACK"); return err("Cambista não encontrado.")
        net = int(round(_caixa_cam(c, cid)["resultado"] * 100))
        if net == 0: c.execute("ROLLBACK"); return err("Este cambista não tem nada a acertar.")
        v = cents(d["valor"]) if d.get("valor") not in (None, "") else abs(net)
        if v <= 0 or v > abs(net): c.execute("ROLLBACK"); return err("Valor inválido. O acerto é de R$ %.2f." % (abs(net) / 100))
        sinal = v if net > 0 else -v
        c.execute("INSERT INTO acertos(data,user_id,cambista_id,valor,por) VALUES(?,?,NULL,?,?)", (now(), cid, sinal, u["id"]))
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK"); raise
    return jsonify(ok=True, nome=t["nome"], valor=v / 100, sentido="recebido" if sinal > 0 else "pago")

@app.get("/api/comissoes")
@need("admin", "cambista")
def comissoes(u):
    c = db()
    if u["role"] == "cambista": return jsonify(ok=True, **_com_cambista(c, u["id"]))
    try: cid = int(request.args.get("cambista") or 0)
    except ValueError: cid = 0
    if cid:                                                     # ADM abrindo um cambista especifico
        t = c.execute("SELECT nome FROM users WHERE id=? AND role='cambista'", (cid,)).fetchone()
        if not t: return err("Cambista não encontrado.", 404)
        return jsonify(ok=True, nome=t["nome"], **_com_cambista(c, cid))
    vol = {r["cid"]: (r["n"], float(r["tot"])) for r in c.execute("SELECT cam.id AS cid, COUNT(*) AS n, COALESCE(SUM(t.valor),0) AS tot " + _COM_BASE + " GROUP BY cam.id")}
    cli = {r[0]: r[1] for r in c.execute("SELECT criado_por, COUNT(*) FROM users WHERE role='usuario' AND criado_por IS NOT NULL GROUP BY criado_por")}
    lst = []
    for r in c.execute("SELECT id, nome, login, ativo FROM users WHERE role='cambista' ORDER BY nome").fetchall():
        n, tot = vol.get(r["id"], (0, 0.0))
        lst.append(dict(id=str(r["id"]), nome=r["nome"], login=r["login"], ativo=bool(r["ativo"]), clientes=cli.get(r["id"], 0), bilhetes=n, apostado=tot / 100, comissao=_com(tot)))
    lst.sort(key=lambda x: -x["comissao"])
    return jsonify(ok=True, pct=COMISSAO_PCT, apostado=sum(x["apostado"] for x in lst), comissao=round(sum(x["comissao"] for x in lst), 2), cambistas=lst)

@app.get("/api/relatorios")
@need("admin", "cambista")
def relatorios(u):
    c = db(); ids = [r[0] for r in c.execute("SELECT id FROM users WHERE role IN ('usuario','cambista')" if u["role"] == "admin" else "SELECT id FROM users WHERE role='usuario' AND criado_por=?", () if u["role"] == "admin" else (u["id"],))]
    if u["role"] == "cambista": ids.append(u["id"])          # bilhetes do proprio cambista (clientes sem conta, via PIN)
    ph = ",".join("?" * len(ids)) or "NULL"; day = datetime.now(timezone(timedelta(hours=-3))).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat()   # "hoje" no horario do Brasil
    row = lambda sql, extra=(): float(c.execute(sql.format(ph), list(ids) + list(extra)).fetchone()[0] or 0)
    tot = row("SELECT SUM(valor) FROM tickets WHERE user_id IN ({}) AND status!='cancelado'")
    return jsonify(ok=True, hoje=int(row("SELECT COUNT(*) FROM tickets WHERE user_id IN ({}) AND data >= ?", (day,))),
                   bruto=tot / 100, premios=row("SELECT SUM(retorno) FROM tickets WHERE user_id IN ({}) AND status='ganhou'") / 100,
                   abertos=int(row("SELECT COUNT(*) FROM tickets WHERE user_id IN ({}) AND status='aberto'")),
                   )

# ---------------------------------------------------------------- acertos (fiado): quem deve a quem, toda segunda
# conta do cliente:  acerto = saldo + apostas em aberto - credito (limite que o cambista deu)
#   negativo -> o cliente PERDEU e deve ao cambista   |   positivo -> o cliente GANHOU e o cambista deve a ele
BR = timezone(timedelta(hours=-3))
def prox_segunda():
    h = datetime.now(BR).date(); d = (7 - h.weekday()) % 7          # segunda = 0; se hoje e segunda, vence hoje
    return (h + timedelta(days=d)).isoformat(), d
_ACERTO_SQL = ("SELECT x.id, x.nome, x.login, x.ativo, x.saldo, COALESCE(x.credito,0) AS credito, x.criado_por AS cid, cam.nome AS cnome, "
    "COALESCE(SUM(CASE WHEN t.status='aberto' THEN t.valor END),0) AS aberto, "
    "COALESCE(SUM(CASE WHEN t.status!='cancelado' THEN t.valor END),0) AS apostado, "
    "COALESCE(SUM(CASE WHEN t.status='ganhou' THEN t.retorno END),0) AS premios, COUNT(t.id) AS bilhetes "
    "FROM users x LEFT JOIN tickets t ON t.user_id=x.id LEFT JOIN users cam ON cam.id=x.criado_por WHERE x.role='usuario'")
def _acerto_row(r):
    net = int(r["saldo"]) + int(r["aberto"]) - int(r["credito"])
    return dict(id=str(r["id"]), nome=r["nome"], login=r["login"], ativo=bool(r["ativo"]), cambista=r["cnome"], cambistaId=str(r["cid"]) if r["cid"] else None,
                limite=int(r["credito"]) / 100, saldo=int(r["saldo"]) / 100, emJogo=int(r["aberto"]) / 100, apostado=int(r["apostado"]) / 100,
                premios=int(r["premios"]) / 100, bilhetes=int(r["bilhetes"]), acerto=net / 100)
def _acerto_rows(c, where="", args=()):
    return [_acerto_row(r) for r in c.execute(_ACERTO_SQL + where + " GROUP BY x.id, x.nome, x.login, x.ativo, x.saldo, x.credito, x.criado_por, cam.nome ORDER BY x.nome", args)]
def _tot(rows):
    return dict(receber=round(sum(-r["acerto"] for r in rows if r["acerto"] < 0), 2), pagar=round(sum(r["acerto"] for r in rows if r["acerto"] > 0), 2))
@app.get("/api/acertos")
@need()
def acertos(u):
    c = db(); venc, dias = prox_segunda()
    if u["role"] == "usuario":
        rows = _acerto_rows(c, " AND x.id=?", (u["id"],))
        hist = [dict(data=r["data"], valor=r["valor"] / 100) for r in c.execute("SELECT data, valor FROM acertos WHERE user_id=? ORDER BY id DESC LIMIT 30", (u["id"],))]
        return jsonify(ok=True, vencimento=venc, dias=dias, eu=rows[0] if rows else None, historico=hist)
    if u["role"] == "cambista":
        rows = _acerto_rows(c, " AND x.criado_por=?", (u["id"],))
        return jsonify(ok=True, vencimento=venc, dias=dias, clientes=rows, **_tot(rows))
    rows = _acerto_rows(c); grp = {}
    for r in rows: grp.setdefault(r["cambistaId"], dict(id=r["cambistaId"], nome=r["cambista"] or "—", clientes=[]))["clientes"].append(r)
    cams = [dict(g, **_tot(g["clientes"])) for g in grp.values()]; cams.sort(key=lambda g: g["nome"].lower())
    return jsonify(ok=True, vencimento=venc, dias=dias, cambistas=cams, **_tot(rows))
@app.post("/api/acertos/pagar")
@need("admin", "cambista")
def acerto_pagar(u):
    """Registra que o cliente pagou o que devia (ou que o cambista pagou o que o cliente ganhou). valor opcional = pagamento parcial."""
    d = request.get_json(silent=True) or {}; c = db()
    try: cid = int(d.get("cliente"))
    except (TypeError, ValueError): return err("Selecione um cliente.")
    c.execute("BEGIN")
    try:
        t = c.execute("SELECT * FROM users WHERE id=? AND role='usuario' FOR UPDATE", (cid,)).fetchone()
        if not t or (u["role"] == "cambista" and t["criado_por"] != u["id"]): c.execute("ROLLBACK"); return err("Cliente não encontrado.")
        ab = int(c.execute("SELECT COALESCE(SUM(valor),0) FROM tickets WHERE user_id=? AND status='aberto'", (cid,)).fetchone()[0])
        net = int(t["saldo"]) + ab - int(t["credito"] or 0)
        if net == 0: c.execute("ROLLBACK"); return err("Este cliente não tem nada a acertar.")
        v = cents(d["valor"]) if d.get("valor") not in (None, "") else abs(net)
        if v <= 0 or v > abs(net): c.execute("ROLLBACK"); return err("Valor inválido. O acerto é de R$ %.2f." % (abs(net) / 100))
        if net < 0:                                                     # cliente pagou ao cambista: abate a divida
            c.execute("UPDATE users SET credito=credito-? WHERE id=?", (v, cid)); sinal = -v
        else:                                                           # cambista pagou o premio: sai do saldo do cliente
            v = min(v, int(t["saldo"]))
            if v <= 0: c.execute("ROLLBACK"); return err("Cliente sem saldo para pagar agora (bilhetes ainda em aberto).")
            c.execute("UPDATE users SET saldo=saldo-? WHERE id=?", (v, cid)); sinal = v
        c.execute("INSERT INTO acertos(data,user_id,cambista_id,valor,por) VALUES(?,?,?,?,?)", (now(), cid, t["criado_por"], sinal, u["id"]))
        tx(c, "acerto", cid if sinal < 0 else None, None if sinal < 0 else cid, v, ("Acerto: cliente pagou " if sinal < 0 else "Acerto: pago ao cliente ") + t["nome"])
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK"); raise
    return jsonify(ok=True, nome=t["nome"], valor=v / 100, sentido="recebido" if sinal < 0 else "pago")

import slots; slots.register(app, db, raw, need, tx, err, cents, MAX_PREMIO, BASE)    # area de Slots
@app.get("/health")
def health():
    try: db().execute("SELECT 1").fetchone(); return "ok"
    except Exception: return "erro", 500
@app.get("/")
def index():
    r = send_from_directory(os.path.join(BASE, "static"), "index.html")
    r.headers["Cache-Control"] = "no-cache, must-revalidate"           # o navegador sempre confere se ha versao nova (antes podia mostrar o site antigo)
    return r
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
