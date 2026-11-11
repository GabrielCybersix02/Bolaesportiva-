"""Area de Slots da Bola Esportiva: lobby + slot de frutas (6x5, jackpots) + Aviator (crash).
O sorteio e o dinheiro ficam SO no servidor; o navegador apenas anima.
Ligacao no app.py (ja existe, antes de @app.get("/health")):  import slots; slots.register(app, db, raw, need, tx, err, cents, MAX_PREMIO, BASE)
Ajustes por variavel de ambiente no Render:  AVIATOR_RTP (padrao 97 = casa fica com 3%; use 94 para a casa ficar com 6%)  |  AVIATOR_MAX_X (padrao 1000)"""
import json, math, os, secrets, time
from math import comb
from flask import jsonify, request, send_from_directory, session, redirect
from datetime import datetime, timezone, timedelta
VERSAO = "2026.11.12"        # sobe a cada pacote de arquivos; o lobby mostra para voce conferir se o servidor esta atualizado

RNG = secrets.SystemRandom()
iso = lambda: datetime.now(timezone.utc).isoformat()

# ------------------------------------------------------------------ catalogo do lobby (quadradinhos)
CATALOGO = [
    dict(id="jackpot", nome="Fruity Jackpot", cat="slots", emoji="🍒", c1="#7B2FF7", c2="#F107A3", tag="HOT"),
    dict(id="aviator", nome="Aviator", cat="crash", emoji="✈️", c1="#E50539", c2="#1a1a2e", tag="NOVO"),
    dict(id="tigrinho", nome="Tigrinho 777", cat="slots", emoji="🐯", c1="#FF8A00", c2="#E0360B", tag="NOVO"),
    dict(id="tigre", nome="Tigre Dourado", cat="slots", emoji="🐯", c1="#EF4444", c2="#7F1D1D", tag="NOVO"),
    dict(id="coelho", nome="Coelho Sortudo", cat="slots", emoji="🐰", c1="#38BDF8", c2="#7C3AED", tag="NOVO"),
    dict(id="dragao", nome="Dragão Dourado", cat="slots", emoji="🐲", c1="#E11D8F", c2="#5B21B6", tag="NOVO"),
    dict(id="boi", nome="Boi da Fortuna", cat="slots", emoji="🐂", c1="#2563EB", c2="#0EA5E9", tag="NOVO"),
    dict(id="cobra", nome="Cobra Esmeralda", cat="slots", emoji="🐍", c1="#15803D", c2="#064E3B", tag="NOVO"),
    dict(id="panda", nome="Panda Zen", cat="slots", emoji="🐼", c1="#16A34A", c2="#EC4899", tag="NOVO"),
    dict(id="cavalo", nome="Cavalo Imperial", cat="slots", emoji="🐴", c1="#B45309", c2="#451A03", tag="NOVO"),
    dict(id="olimpo", nome="Trovão Olímpico", cat="slots", emoji="⚡", c1="#1E3A8A", c2="#7C3AED", tag="NOVO"),
    dict(id="doce", nome="Doce Fortuna", cat="slots", emoji="🍭", c1="#F472B6", c2="#8B5CF6", tag="NOVO"),
    dict(id="gemas", nome="Gemas Reais", cat="slots", emoji="💎", c1="#06B6D4", c2="#1E1B4B", tag="NOVO"),
    dict(id="moedas", nome="Moedas Mágicas", cat="slots", emoji="🪙", c1="#F59E0B", c2="#7C2D12", tag="NOVO"),
    dict(id="rato", nome="Ratinho da Sorte", cat="slots", emoji="🐭", c1="#FBBF24", c2="#0F766E", tag="NOVO"),
    dict(id="ovo", nome="Ovo do Dragão", cat="slots", emoji="🥚", c1="#F97316", c2="#7F1D1D", tag="NOVO"),
    dict(id="frutas", nome="Festa das Frutas", cat="slots", emoji="🍉", c1="#FB923C", c2="#BE185D", tag="NOVO"),
    dict(id="pesca", nome="Pesca Premiada", cat="slots", emoji="🐟", c1="#0EA5E9", c2="#0C4A6E", tag="NOVO"),
    dict(id="oeste", nome="Ouro do Oeste", cat="slots", emoji="🤠", c1="#F59E0B", c2="#7C2D12", tag="NOVO"),
    dict(id="livro", nome="Livro Dourado", cat="slots", emoji="📖", c1="#D97706", c2="#1E1B4B", tag="NOVO"),
    dict(id="mines", nome="Mines", cat="crash", emoji="💎", c1="#2563EB", c2="#0B2A6B", tag="NOVO"),
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

# ------------------------------------------------------------------ SLOT 3x3 "Tigre Dourado" (5 linhas, tigre = curinga)
TG = dict(nome="Tigre Dourado", min=100, max=10000, linhas=[(0, 0, 0), (1, 1, 1), (2, 2, 2), (0, 1, 2), (2, 1, 0)],
    simbolos=["moeda", "tangerina", "lanterna", "bombinha", "envelope", "lingote", "tigre"], peso=[22, 20, 16, 14, 10, 6, 6],
    pay=[4.4, 6.8, 10.7, 17.5, 33, 88, 220])          # x da aposta de LINHA (aposta total / 5), 3 iguais na linha; tigre substitui qualquer simbolo
def tg_spin():
    return [RNG.choices(range(7), weights=TG["peso"], k=3) for _ in range(3)]                  # grid[coluna][linha]
def tg_eval(grid, stake):
    win, hits = 0.0, []
    for i, ln in enumerate(TG["linhas"]):
        sy = [grid[c][ln[c]] for c in range(3)]; nw = {x for x in sy if x != 6}
        if len(nw) <= 1:
            k = nw.pop() if nw else 6; m = TG["pay"][k]; win += m * stake / 5; hits.append(dict(linha=i, simbolo=TG["simbolos"][k], mult=m))
    return int(win), hits

# ------------------------------------------------------------------ "Tigrinho 777" (regras do app Java: 3 rolos, 5 simbolos, cada rolo sorteia 1 de 5)
TJ = dict(nome="Tigrinho 777", min=100, max=50000, simbolos=["sete", "coracao", "cereja", "diamante", "espadilha"], pay=[17, 13, 10, 8, 6])   # 3 iguais: devolve aposta + aposta x mult
def tj_eval(r, stake):
    """3 iguais = aposta + aposta x mult | 2 iguais = perde metade (devolve 50%) | todos diferentes = perde tudo. Devolve (total_devolvido, tipo, mult)"""
    if r[0] == r[1] == r[2]: m = TJ["pay"][r[0]]; return stake * (m + 1), "tres", m
    if len(set(r)) == 3: return 0, "nada", 0
    return stake // 2, "par", 0
def rtp_tigrinho():                                   # exato: 125 combinacoes igualmente provaveis
    return sum(tj_eval([a, b, c], 1000)[0] / 1000 for a in range(5) for b in range(5) for c in range(5)) / 125

# ------------------------------------------------------------------ JOGOS TEMATICOS (mesma matematica dos motores acima, so muda tema/arte no navegador)
NOVOS = {"tigre": "linhas", "jackpot": "cluster", "gemas": "linhas", "moedas": "linhas", "rato": "linhas", "ovo": "linhas", "frutas": "cluster", "pesca": "cluster", "oeste": "cluster", "livro": "cluster", "coelho": "linhas", "dragao": "linhas", "boi": "linhas", "cobra": "linhas", "panda": "linhas", "cavalo": "linhas", "olimpo": "cluster", "doce": "cluster"}   # linhas = 3x3 como o Tigre Dourado | cluster = 6x5 como o Fruity Jackpot
NOMES = {c["id"]: c["nome"] for c in CATALOGO}

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
    def pagina(nome):                                  # entrega a pagina com o arte.js "carimbado" pela data do arquivo: o navegador nunca usa versao velha
        st = os.path.join(BASE, "static")
        try:
            v = int(os.path.getmtime(os.path.join(st, "arte.js")))
            r = app.response_class(open(os.path.join(st, nome), encoding="utf-8").read().replace('/static/arte.js"', '/static/arte.js?v=%d"' % v), mimetype="text/html")
        except Exception:
            r = send_from_directory(st, nome)
        r.headers["Cache-Control"] = "no-store"; return r
    @app.get("/api/slots/versao")                      # diagnostico: abra /api/slots/versao para conferir se todos os arquivos novos subiram
    def slots_versao():
        st = os.path.join(BASE, "static")
        def m(n):
            try: return int(os.path.getmtime(os.path.join(st, n)))
            except Exception: return None
        return jsonify(jogos=len(CATALOGO), arquivos={n: m(n) for n in ("arte.js", "slots.html", "jogo_linhas.html", "jogo_cluster.html", "aviator.html", "mines.html", "tigrinho.html", "carteira.html")})
    @app.get("/slots")
    def slots_page(): return pagina("slots.html") if entrar() else redirect("/")
    @app.get("/slots/jogo/<jogo>")
    def slots_jogo(jogo):
        if not entrar(): return redirect("/")
        if jogo in NOVOS: return pagina("jogo_%s.html" % NOVOS[jogo])
        return pagina({"jackpot": "slot_game.html", "aviator": "aviator.html", "tigre": "tigre.html", "mines": "mines.html", "tigrinho": "tigrinho.html", "icones": "icones.html"}.get(jogo, "slots.html"))
    @app.get("/api/slots")
    @need("usuario")
    def slots_lobby(u): return jsonify(ok=True, saldo=u["saldo"] / 100, jogos=CATALOGO, versao=VERSAO, nome=u["nome"])
    @app.after_request
    def _sem_cache_estatico(r):                      # arquivos .js/.css/.json do jogo nunca ficam velhos no celular
        if request.path.startswith("/static/") and request.path.rsplit(".", 1)[-1] in ("js", "css", "json", "html"): r.headers["Cache-Control"] = "no-cache"
        return r
    @app.get("/api/skins")                           # lista as imagens em static/img/<jogo>/ (arte realista que voce colocar la entra sozinha)
    @need("usuario")
    def skins(u):
        raiz = os.path.join(BASE, "static", "img"); out = {}
        if os.path.isdir(raiz):
            for d in sorted(os.listdir(raiz)):
                p = os.path.join(raiz, d)
                if os.path.isdir(p): out[d] = sorted(f for f in os.listdir(p) if f.rsplit(".", 1)[-1].lower() in ("png", "webp", "jpg", "jpeg", "avif", "svg"))
        return jsonify(ok=True, skins=out)
    @app.get("/api/slots/destaques")                 # maiores prêmios das últimas 24h (dados reais, nome mascarado)
    @need("usuario")
    def destaques(u):
        desde = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        rows = db().execute("SELECT s.jogo, s.aposta, s.premio, us.nome FROM slot_spins s JOIN users us ON us.id=s.user_id WHERE s.data>? AND s.aposta>0 AND s.premio>=s.aposta*5 ORDER BY s.premio DESC LIMIT 12", (desde,)).fetchall()
        return jsonify(ok=True, itens=[dict(nome=(r["nome"] or "?")[:1].upper() + "***", jogo=NOMES.get(r["jogo"], r["jogo"]), premio=r["premio"] / 100, mult=round(r["premio"] / r["aposta"], 1)) for r in rows])
    @app.get("/api/slots/perfil")                    # nivel do jogador pelo total apostado
    @need("usuario")
    def perfil(u):
        c = db(); tot = 0
        for q in ("SELECT COALESCE(SUM(aposta),0) FROM slot_spins WHERE user_id=?", "SELECT COALESCE(SUM(stake),0) FROM aviator_bets WHERE user_id=?", "SELECT COALESCE(SUM(stake),0) FROM mines_games WHERE user_id=?"):
            try: tot += int(c.execute(q, (u["id"],)).fetchone()[0])
            except Exception: pass
        NIV = [("Bronze", 0), ("Prata", 50000), ("Ouro", 200000), ("Platina", 1000000), ("Diamante", 5000000)]      # centavos apostados
        i = max(k for k, (n, v) in enumerate(NIV) if tot >= v); prox = NIV[i + 1] if i + 1 < len(NIV) else None
        return jsonify(ok=True, nome=u["nome"], nivel=NIV[i][0], indice=i, total=tot / 100, prox=prox[1] / 100 if prox else None, prox_nome=prox[0] if prox else None, progresso=1 if not prox else (tot - NIV[i][1]) / (prox[1] - NIV[i][1]))
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

    @app.get("/api/slots/tigre/info")
    @need("usuario")
    def tg_info(u):
        return jsonify(ok=True, saldo=u["saldo"] / 100, nome=TG["nome"], min=TG["min"] / 100, max=TG["max"] / 100, simbolos=TG["simbolos"], pay=TG["pay"], linhas=TG["linhas"])
    @app.post("/api/slots/tigre/spin")
    @need("usuario")
    def tg_spin_route(u):
        stake = cents((request.get_json(silent=True) or {}).get("valor"))
        if not TG["min"] <= stake <= TG["max"]: return err("Aposta de R$ %.2f a R$ %.2f." % (TG["min"] / 100, TG["max"] / 100))
        c = db(); c.execute("BEGIN")
        try:
            saldo = c.execute("SELECT saldo FROM users WHERE id=? FOR UPDATE", (u["id"],)).fetchone()[0]
            if stake > saldo: c.execute("ROLLBACK"); return err("Saldo insuficiente. Faça um depósito.")
            grid = tg_spin(); win, hits = tg_eval(grid, stake); win = min(win, MAX_PREMIO)
            c.execute("UPDATE users SET saldo=saldo-?+? WHERE id=?", (stake, win, u["id"]))
            c.execute("INSERT INTO slot_spins(data,user_id,jogo,aposta,premio,grid) VALUES(?,?,?,?,?,?)", (iso(), u["id"], "tigre", stake, win, json.dumps(grid)))
            tx(c, "slot", u["id"], None, stake, "Slot %s: aposta R$ %.2f, prêmio R$ %.2f" % (TG["nome"], stake / 100, win / 100)); c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK"); raise
        return jsonify(ok=True, grid=[[TG["simbolos"][x] for x in col] for col in grid], linhas=hits, premio=win / 100, saldo=(saldo - stake + win) / 100)

    @app.get("/api/slots/tigrinho/info")
    @need("usuario")
    def tj_info(u): return jsonify(ok=True, saldo=u["saldo"] / 100, min=TJ["min"] / 100, max=TJ["max"] / 100, simbolos=TJ["simbolos"], pay=TJ["pay"])
    @app.post("/api/slots/tigrinho/spin")
    @need("usuario")
    def tj_spin(u):
        stake = cents((request.get_json(silent=True) or {}).get("valor"))
        if not TJ["min"] <= stake <= TJ["max"]: return err("Aposta de R$ %.2f a R$ %.2f." % (TJ["min"] / 100, TJ["max"] / 100))
        c = db(); c.execute("BEGIN")
        try:
            saldo = c.execute("SELECT saldo FROM users WHERE id=? FOR UPDATE", (u["id"],)).fetchone()[0]
            if stake > saldo: c.execute("ROLLBACK"); return err("Saldo insuficiente. Faça um depósito.")
            r = [RNG.randrange(5) for _ in range(3)]; total, tipo, m = tj_eval(r, stake); total = min(total, MAX_PREMIO)
            c.execute("UPDATE users SET saldo=saldo-?+? WHERE id=?", (stake, total, u["id"]))
            c.execute("INSERT INTO slot_spins(data,user_id,jogo,aposta,premio,grid) VALUES(?,?,?,?,?,?)", (iso(), u["id"], "tigrinho", stake, total, json.dumps(r)))
            tx(c, "slot", u["id"], None, stake, "Slot %s: aposta R$ %.2f, retorno R$ %.2f" % (TJ["nome"], stake / 100, total / 100)); c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK"); raise
        return jsonify(ok=True, rolos=[TJ["simbolos"][x] for x in r], tipo=tipo, mult=m, premio=total / 100, saldo=(saldo - stake + total) / 100)

    @app.get("/api/jogo/<jid>/info")
    @need("usuario")
    def nv_info(u, jid):
        k = NOVOS.get(jid)
        if not k: return err("Jogo não encontrado.", 404)
        d = dict(ok=True, kind=k, saldo=u["saldo"] / 100, nome=NOMES[jid], min=100 / 100, max=10000 / 100)
        if k == "linhas": d.update(simbolos=TG["simbolos"], pay=TG["pay"], linhas=TG["linhas"])
        else: d.update(ordem=list(G["peso"]), pay=G["pay"], cols=G["cols"], rows=G["rows"], jackpots=[dict(nome=n, coroas=q, mult=m) for n, q, m in G["jackpots"]], rtp=round(rtp_slot() * 100, 2))
        return jsonify(d)
    @app.post("/api/jogo/<jid>/spin")
    @need("usuario")
    def nv_spin(u, jid):
        k = NOVOS.get(jid)
        if not k: return err("Jogo não encontrado.", 404)
        stake = cents((request.get_json(silent=True) or {}).get("valor")); lim = TG if k == "linhas" else G
        if not lim["min"] <= stake <= lim["max"]: return err("Aposta de R$ %.2f a R$ %.2f." % (lim["min"] / 100, lim["max"] / 100))
        c = db(); c.execute("BEGIN")
        try:
            saldo = c.execute("SELECT saldo FROM users WHERE id=? FOR UPDATE", (u["id"],)).fetchone()[0]
            if stake > saldo: c.execute("ROLLBACK"); return err("Saldo insuficiente. Faça um depósito.")
            if k == "linhas": grid = tg_spin(); win, hits = tg_eval(grid, stake); extra = dict(linhas=hits); out = [[TG["simbolos"][x] for x in col] for col in grid]
            else: grid = spin_grid(); win, vit, jp = evaluate(grid, stake); extra = dict(vitorias=vit, jackpot=jp); out = grid
            win = min(win, MAX_PREMIO)
            c.execute("UPDATE users SET saldo=saldo-?+? WHERE id=?", (stake, win, u["id"]))
            c.execute("INSERT INTO slot_spins(data,user_id,jogo,aposta,premio,grid) VALUES(?,?,?,?,?,?)", (iso(), u["id"], jid, stake, win, json.dumps(grid)))
            tx(c, "slot", u["id"], None, stake, "Slot %s: aposta R$ %.2f, prêmio R$ %.2f" % (NOMES[jid], stake / 100, win / 100)); c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK"); raise
        return jsonify(ok=True, grid=out, premio=win / 100, saldo=(saldo - stake + win) / 100, **extra)

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
    import mines; mines.register(app, db, raw, need, tx, err, cents, MAX_PREMIO)        # jogo Mines (mines.py)
