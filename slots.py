"""Area de Slots da Bola Esportiva: lobby + slot de frutas (6x5, jackpots) + Aviator (crash).
O sorteio e o dinheiro ficam SO no servidor; o navegador apenas anima.
Ligacao no app.py (ja existe, antes de @app.get("/health")):  import slots; slots.register(app, db, raw, need, tx, err, cents, MAX_PREMIO, BASE)
Ajustes por variavel de ambiente no Render:  AVIATOR_RTP (padrao 97 = casa fica com 3%; use 94 para a casa ficar com 6%)  |  AVIATOR_MAX_X (padrao 1000)"""
import json, math, os, secrets, time
from math import comb
from flask import jsonify, request, send_from_directory, session, redirect
from datetime import datetime, timezone

RNG = secrets.SystemRandom()
iso = lambda: datetime.now(timezone.utc).isoformat()

# ------------------------------------------------------------------ catalogo do lobby (quadradinhos)
CATALOGO = [
    dict(id="jackpot", nome="Fruity Jackpot", cat="slots", emoji="🍒", c1="#7B2FF7", c2="#F107A3", tag="HOT"),
    dict(id="aviator", nome="Aviator", cat="crash", emoji="✈️", c1="#E50539", c2="#1a1a2e", tag="NOVO"),
    dict(id="breve1", nome="Fortune Ox", cat="slots", emoji="🐂", c1="#F59E0B", c2="#B91C1C", breve=True),
    dict(id="breve2", nome="Tigre da Sorte", cat="slots", emoji="🐯", c1="#EA580C", c2="#7F1D1D", breve=True),
    dict(id="breve3", nome="Coelho Ninja", cat="slots", emoji="🐰", c1="#06B6D4", c2="#7C3AED", breve=True),
    dict(id="breve4", nome="Mines", cat="crash", emoji="💣", c1="#10B981", c2="#064E3B", breve=True),
]

# ------------------------------------------------------------------ SLOT 6x5 "paga em qualquer lugar" (8+ iguais na tela)
G = dict(nome="Fruity Jackpot", min=100, max=10000, cols=6, rows=5,
    peso={"limao": 22, "melancia": 18, "laranja": 16, "uva": 14, "sino": 11, "sete": 7, "bar": 6, "coroa": 2.0},
    pay={  # multiplicador da APOSTA TOTAL por faixa de quantidade: 8-9 | 10-11 | 12-14 | 15+
        "limao": (.45, 1.4, 4.6, 18.5), "melancia": (.65, 1.85, 6.5, 28), "laranja": (.9, 2.8, 9.25, 37), "uva": (1.1, 3.7, 13, 55),
        "sino": (1.85, 5.5, 18.5, 90), "sete": (3.7, 11, 46, 275), "bar": (5.5, 16.5, 74, 370)},
    jackpots=[("MINI", 4, 10), ("MINOR", 5, 25), ("MAJOR", 6, 100), ("GRAND", 7, 500), ("SUPER GRAND", 8, 1000)])   # (nome, coroas necessarias, x aposta)
def faixa(n): return 0 if n < 10 else 1 if n < 12 else 2 if n < 15 else 3
def jackpot_de(n):
    r = None
    for nome, q, m in G["jackpots"]:
        if n >= q: r = (nome, m)
    return r
def spin_grid():
    k, w = list(G["peso"]), list(G["peso"].values())
    flat = RNG.choices(k, weights=w, k=G["cols"] * G["rows"])
    return [flat[c * G["rows"]:(c + 1) * G["rows"]] for c in range(G["cols"])]        # grid[coluna][linha]
def evaluate(grid, stake):
    cont = {}
    for col in grid:
        for s in col: cont[s] = cont.get(s, 0) + 1
    win, vit, jp = 0.0, [], None
    for s, n in cont.items():
        if s == "coroa":
            j = jackpot_de(n)
            if j: jp = dict(nome=j[0], mult=j[1]); win += j[1] * stake
        elif n >= 8:
            m = G["pay"][s][faixa(n)]; win += m * stake; vit.append(dict(simbolo=s, qtd=n, mult=m))
    return int(win), vit, jp
def rtp_slot():                                       # RTP exato: o premio de cada simbolo so depende da contagem dele (binomial)
    N = G["cols"] * G["rows"]; T = sum(G["peso"].values()); tot = 0.0
    for s, w in G["peso"].items():
        p = w / T
        for n in range(N + 1):
            pr = comb(N, n) * p ** n * (1 - p) ** (N - n)
            if s == "coroa": j = jackpot_de(n); m = j[1] if j else 0
            else: m = G["pay"][s][faixa(n)] if n >= 8 else 0
            tot += pr * m
    return tot

# ------------------------------------------------------------------ AVIATOR
AV_RTP = min(max(float(os.environ.get("AVIATOR_RTP", "97")), 80), 99.5) / 100     # retorno ao jogador; (1-RTP) = lucro da casa
AV_MAX = int(float(os.environ.get("AVIATOR_MAX_X", "1000")) * 100)
AV_MIN, AV_MAXBET = 100, 50000                         # R$ 1,00 a R$ 500,00 por aposta
K, ESPERA, PAUSA = 0.07, 6.0, 3.5                      # multiplicador = e^(K*t) | segundos de espera p/ apostar | pausa depois da queda
def sortear_crash():
    """Ponto de queda x100. P(crash >= m) = RTP/m  ->  qualquer estrategia de saque devolve em media RTP; ~(1-RTP) das rodadas caem em 1.00x."""
    r = RNG.random()
    return max(100, min(AV_MAX, int(100 * AV_RTP / (1 - r))))
def mult_em(start, t): return 100 if t <= start else int(100 * math.exp(K * (t - start)))
def fim_de(start, crash): return start + math.log(crash / 100) / K

def register(app, db, raw, need, tx, err, cents, MAX_PREMIO, BASE):
    c0 = raw()
    try:
        c0.execute("CREATE TABLE IF NOT EXISTS slot_spins(id SERIAL PRIMARY KEY, data TEXT, user_id INTEGER NOT NULL REFERENCES users(id), jogo TEXT, aposta BIGINT, premio BIGINT, grid TEXT)")
        c0.execute("CREATE TABLE IF NOT EXISTS aviator_rounds(id SERIAL PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), crash INTEGER NOT NULL, start_ts DOUBLE PRECISION NOT NULL, end_ts DOUBLE PRECISION NOT NULL)")
        c0.execute("CREATE TABLE IF NOT EXISTS aviator_bets(id SERIAL PRIMARY KEY, round_id INTEGER NOT NULL REFERENCES aviator_rounds(id), user_id INTEGER NOT NULL REFERENCES users(id), slot INTEGER NOT NULL, stake BIGINT NOT NULL, auto INTEGER, mult INTEGER, premio BIGINT DEFAULT 0, status TEXT DEFAULT 'aberta', data TEXT)")
    finally: c0.close()

    def entrar():                                      # so cliente logado joga; visitante volta para o site
        uid = session.get("uid")
        row = db().execute("SELECT role FROM users WHERE id=? AND ativo=1", (uid,)).fetchone() if uid else None
        return bool(row and row["role"] == "usuario")
    def pagina(nome):
        r = send_from_directory(os.path.join(BASE, "static"), nome); r.headers["Cache-Control"] = "no-cache"; return r

    @app.get("/slots")
    def slots_page(): return pagina("slots.html") if entrar() else redirect("/")
    @app.get("/slots/jogo/<jogo>")
    def slots_jogo(jogo):
        if not entrar(): return redirect("/")
        return pagina({"jackpot": "slot_game.html", "aviator": "aviator.html"}.get(jogo, "slots.html"))
    @app.get("/api/slots")
    @need("usuario")
    def slots_lobby(u): return jsonify(ok=True, saldo=u["saldo"] / 100, jogos=CATALOGO)
    @app.get("/api/slots/jackpot/info")
    @need("usuario")
    def slot_info(u):
        return jsonify(ok=True, saldo=u["saldo"] / 100, nome=G["nome"], min=G["min"] / 100, max=G["max"] / 100, cols=G["cols"], rows=G["rows"],
                       pay=G["pay"], jackpots=[dict(nome=n, coroas=q, mult=m) for n, q, m in G["jackpots"]], rtp=round(rtp_slot() * 100, 2))

    @app.post("/api/slots/jackpot/spin")
    @need("usuario")
    def slot_spin(u):
        stake = cents((request.get_json(silent=True) or {}).get("valor"))
        if not G["min"] <= stake <= G["max"]: return err("Aposta de R$ %.2f a R$ %.2f." % (G["min"] / 100, G["max"] / 100))
        c = db(); c.execute("BEGIN")
        try:
            saldo = c.execute("SELECT saldo FROM users WHERE id=? FOR UPDATE", (u["id"],)).fetchone()[0]
            if stake > saldo: c.execute("ROLLBACK"); return err("Saldo insuficiente. Peça uma recarga ao ADM.")
            grid = spin_grid(); win, vit, jp = evaluate(grid, stake); win = min(win, MAX_PREMIO)
            c.execute("UPDATE users SET saldo=saldo-?+? WHERE id=?", (stake, win, u["id"]))
            c.execute("INSERT INTO slot_spins(data,user_id,jogo,aposta,premio,grid) VALUES(?,?,?,?,?,?)", (iso(), u["id"], "jackpot", stake, win, json.dumps(grid)))
            tx(c, "slot", u["id"], None, stake, "Slot %s: aposta R$ %.2f, prêmio R$ %.2f" % (G["nome"], stake / 100, win / 100))
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK"); raise
        return jsonify(ok=True, grid=grid, vitorias=vit, jackpot=jp, premio=win / 100, saldo=(saldo - stake + win) / 100)

    # ---------------------------------------------------------- Aviator
    def creditar(c, uid, valor, desc):
        c.execute("UPDATE users SET saldo=saldo+? WHERE id=?", (valor, uid)); tx(c, "slot", None, uid, valor, desc)
    def liquidar(c, b, rnd, agora, forcar=None):
        """Fecha uma aposta aberta de forma atomica (so paga uma vez). forcar=multiplicador de saque manual. Devolve (status, mult, premio)."""
        crash, start, fim = rnd["crash"], rnd["start_ts"], rnd["end_ts"]; mult = None
        if forcar is not None and agora < fim: mult = forcar
        elif b["auto"] and b["auto"] <= crash and (agora >= fim or mult_em(start, agora) >= b["auto"]): mult = b["auto"]
        elif agora < fim: return None
        status, premio = ("ganhou", min(int(b["stake"] * mult / 100), MAX_PREMIO)) if mult else ("perdeu", 0)
        c.execute("BEGIN")
        try:
            if c.execute("UPDATE aviator_bets SET status=?, mult=?, premio=? WHERE id=? AND status='aberta'", (status, mult, premio, b["id"])).rowcount == 1 and premio:
                creditar(c, b["user_id"], premio, "Aviator: saque %.2fx, prêmio R$ %.2f" % (mult / 100, premio / 100))
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK"); raise
        return status, mult, premio
    def liquidar_tudo(c, uid, agora):
        for b in c.execute("SELECT b.*, r.crash, r.start_ts, r.end_ts FROM aviator_bets b JOIN aviator_rounds r ON r.id=b.round_id WHERE b.user_id=? AND b.status='aberta'", (uid,)).fetchall():
            liquidar(c, b, b, agora)
    def rodada_atual(c, uid, agora, criar=True):
        r = c.execute("SELECT * FROM aviator_rounds WHERE user_id=? ORDER BY id DESC LIMIT 1", (uid,)).fetchone()
        if (not r or agora > r["end_ts"] + PAUSA) and criar:
            crash = sortear_crash(); start = agora + ESPERA
            c.execute("INSERT INTO aviator_rounds(user_id,crash,start_ts,end_ts) VALUES(?,?,?,?)", (uid, crash, start, fim_de(start, crash)))
            r = c.execute("SELECT * FROM aviator_rounds WHERE user_id=? ORDER BY id DESC LIMIT 1", (uid,)).fetchone()
        return r
    def estado(c, u):
        agora = time.time(); liquidar_tudo(c, u["id"], agora); r = rodada_atual(c, u["id"], agora)
        fase = "espera" if agora < r["start_ts"] else "voo" if agora < r["end_ts"] else "fim"
        bets = [dict(slot=b["slot"], stake=b["stake"] / 100, auto=b["auto"] and b["auto"] / 100, status=b["status"], mult=b["mult"] and b["mult"] / 100, premio=b["premio"] / 100)
                for b in c.execute("SELECT * FROM aviator_bets WHERE round_id=? ORDER BY slot", (r["id"],)).fetchall()]
        hist = [x[0] / 100 for x in c.execute("SELECT crash FROM aviator_rounds WHERE end_ts<? ORDER BY id DESC LIMIT 14", (agora,)).fetchall()]
        saldo = c.execute("SELECT saldo FROM users WHERE id=?", (u["id"],)).fetchone()[0]
        return dict(ok=True, agora=agora, saldo=saldo / 100, k=K, min=AV_MIN / 100, max=AV_MAXBET / 100, historico=hist,
                    rodada=dict(id=r["id"], fase=fase, start=r["start_ts"], crash=r["crash"] / 100 if fase == "fim" else None, bets=bets))
    @app.get("/api/aviator")
    @need("usuario")
    def av_estado(u): return jsonify(estado(db(), u))

    @app.post("/api/aviator/bet")
    @need("usuario")
    def av_bet(u):
        d = request.get_json(silent=True) or {}; slot = 2 if d.get("slot") == 2 else 1; stake = cents(d.get("valor"))
        try: auto = int(round(float(d.get("auto")) * 100)) if d.get("auto") else None
        except Exception: auto = None
        if auto is not None and auto < 101: return err("Saque automático deve ser maior que 1.00x.")
        if not AV_MIN <= stake <= AV_MAXBET: return err("Aposta de R$ %.2f a R$ %.2f." % (AV_MIN / 100, AV_MAXBET / 100))
        c = db(); agora = time.time(); r = rodada_atual(c, u["id"], agora)
        if agora >= r["start_ts"]: return err("Rodada em andamento. Aguarde a próxima para apostar.")
        c.execute("BEGIN")
        try:
            saldo = c.execute("SELECT saldo FROM users WHERE id=? FOR UPDATE", (u["id"],)).fetchone()[0]
            if c.execute("SELECT 1 FROM aviator_bets WHERE round_id=? AND slot=?", (r["id"], slot)).fetchone(): c.execute("ROLLBACK"); return err("Aposta já feita.")
            if stake > saldo: c.execute("ROLLBACK"); return err("Saldo insuficiente. Peça uma recarga ao ADM.")
            c.execute("UPDATE users SET saldo=saldo-? WHERE id=?", (stake, u["id"]))
            c.execute("INSERT INTO aviator_bets(round_id,user_id,slot,stake,auto) VALUES(?,?,?,?,?)", (r["id"], u["id"], slot, stake, auto))
            tx(c, "slot", u["id"], None, stake, "Aviator: aposta %d de R$ %.2f" % (slot, stake / 100)); c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK"); raise
        return jsonify(estado(c, u))
    @app.post("/api/aviator/cancel")
    @need("usuario")
    def av_cancel(u):
        slot = 2 if (request.get_json(silent=True) or {}).get("slot") == 2 else 1; c = db(); agora = time.time(); r = rodada_atual(c, u["id"], agora, False)
        if not r or agora >= r["start_ts"]: return err("Não é mais possível cancelar.")
        c.execute("BEGIN")
        try:
            b = c.execute("SELECT * FROM aviator_bets WHERE round_id=? AND slot=? AND status='aberta' FOR UPDATE", (r["id"], slot)).fetchone()
            if b and c.execute("DELETE FROM aviator_bets WHERE id=?", (b["id"],)).rowcount == 1: creditar(c, u["id"], b["stake"], "Aviator: aposta cancelada")
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK"); raise
        return jsonify(estado(c, u))
    @app.post("/api/aviator/cashout")
    @need("usuario")
    def av_cashout(u):
        slot = 2 if (request.get_json(silent=True) or {}).get("slot") == 2 else 1; c = db(); agora = time.time(); r = rodada_atual(c, u["id"], agora, False)
        b = r and c.execute("SELECT * FROM aviator_bets WHERE round_id=? AND slot=? AND status='aberta'", (r["id"], slot)).fetchone()
        if b and agora >= r["start_ts"]:
            liquidar(c, dict(b, crash=r["crash"], start_ts=r["start_ts"], end_ts=r["end_ts"]), r, agora, forcar=mult_em(r["start_ts"], agora))
        return jsonify(estado(c, u))
    @app.get("/api/aviator/listas")
    @need("usuario")
    def av_listas(u):
        c = db(); nome = lambda n: (n or "?")[:1].upper() + "***"
        meus = [dict(stake=x["stake"] / 100, mult=x["mult"] and x["mult"] / 100, premio=x["premio"] / 100) for x in c.execute("SELECT * FROM aviator_bets WHERE user_id=? AND status<>'aberta' ORDER BY id DESC LIMIT 15", (u["id"],)).fetchall()]
        topo = [dict(nome=nome(x["nome"]), mult=x["mult"] / 100, premio=x["premio"] / 100) for x in c.execute("SELECT b.mult, b.premio, us.nome FROM aviator_bets b JOIN users us ON us.id=b.user_id WHERE b.status='ganhou' ORDER BY b.premio DESC LIMIT 10").fetchall()]
        return jsonify(ok=True, anterior=meus, topo=topo)
