"""Mines (tabuleiro 5x5): o cliente escolhe quantas minas (1 a 24), vira casas e saca quando quiser. As minas sao sorteadas e GUARDADAS no servidor
(o navegador so recebe as posicoes quando a rodada termina). Multiplicador justo x RTP:  mult(k) = RTP * prod_{i<k} (25-i)/(25-minas-i)
Ajuste no Render:  MINES_RTP (padrao 97 = casa fica com 3%; use 94 para a casa ficar com 6%).   Ligado dentro de slots.register()."""
import json, math, os, secrets
from flask import jsonify, request
from datetime import datetime, timezone

RNG = secrets.SystemRandom()
RTP = min(max(float(os.environ.get("MINES_RTP", "97")), 80), 99.5) / 100
MIN_BET, MAX_BET = 100, 50000                              # R$ 1,00 a R$ 500,00
iso = lambda: datetime.now(timezone.utc).isoformat()
def mult(m, k):
    if k <= 0: return 1.0
    p = 1.0
    for i in range(k): p *= (25 - i) / (25 - m - i)
    return math.floor(p * RTP * 100) / 100

def register(app, db, raw, need, tx, err, cents, MAX_PREMIO):
    c0 = raw()
    try: c0.execute("CREATE TABLE IF NOT EXISTS mines_games(id SERIAL PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), stake BIGINT NOT NULL, minas INTEGER NOT NULL, bombas TEXT NOT NULL, reveladas TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL DEFAULT 'aberto', premio BIGINT DEFAULT 0, criado TEXT)")
    finally: c0.close()
    def estado(g, saldo):
        rev = json.loads(g["reveladas"]); k = len(rev); aberto = g["status"] == "aberto"; m = g["minas"]
        r = dict(ok=True, saldo=saldo / 100, aberto=aberto, status=g["status"], minas=m, stake=g["stake"] / 100, reveladas=rev, mult=mult(m, k),
                 proximo=min(g["stake"] * mult(m, k + 1), MAX_PREMIO) / 100 if k < 25 - m else None, atual=min(int(g["stake"] * mult(m, k)), MAX_PREMIO) / 100, premio=g["premio"] / 100)
        if not aberto: r["bombas"] = json.loads(g["bombas"])
        return r
    def saldo_de(c, uid): return c.execute("SELECT saldo FROM users WHERE id=?", (uid,)).fetchone()[0]
    def aberto_de(c, uid, lock=False):
        return c.execute("SELECT * FROM mines_games WHERE user_id=? AND status='aberto' ORDER BY id DESC LIMIT 1" + (" FOR UPDATE" if lock else ""), (uid,)).fetchone()
    @app.get("/api/mines")
    @need("usuario")
    def mn_info(u):
        c = db(); g = aberto_de(c, u["id"]); h = c.execute("SELECT stake, premio FROM mines_games WHERE user_id=? AND status<>'aberto' ORDER BY id DESC LIMIT 10", (u["id"],)).fetchall()
        d = estado(g, u["saldo"]) if g else dict(ok=True, aberto=False, saldo=u["saldo"] / 100)
        d.update(rtp=RTP, min=MIN_BET / 100, max=MAX_BET / 100, tabela={str(m): [mult(m, 1), mult(m, 2), mult(m, 3)] for m in (1, 3, 5, 10, 24)}, historico=[dict(stake=x["stake"] / 100, premio=x["premio"] / 100) for x in h]); return jsonify(d)
    @app.post("/api/mines/start")
    @need("usuario")
    def mn_start(u):
        d = request.get_json(silent=True) or {}; stake = cents(d.get("valor"))
        try: m = int(d.get("minas"))
        except Exception: m = 0
        if not 1 <= m <= 24: return err("Escolha de 1 a 24 minas.")
        if not MIN_BET <= stake <= MAX_BET: return err("Aposta de R$ %.2f a R$ %.2f." % (MIN_BET / 100, MAX_BET / 100))
        c = db(); c.execute("BEGIN")
        try:
            saldo = c.execute("SELECT saldo FROM users WHERE id=? FOR UPDATE", (u["id"],)).fetchone()[0]
            if aberto_de(c, u["id"]): c.execute("ROLLBACK"); return err("Você já tem uma rodada em andamento.")
            if stake > saldo: c.execute("ROLLBACK"); return err("Saldo insuficiente. Faça um depósito.")
            bombas = sorted(RNG.sample(range(25), m))
            c.execute("UPDATE users SET saldo=saldo-? WHERE id=?", (stake, u["id"]))
            c.execute("INSERT INTO mines_games(user_id,stake,minas,bombas,criado) VALUES(?,?,?,?,?)", (u["id"], stake, m, json.dumps(bombas), iso()))
            tx(c, "slot", u["id"], None, stake, "Mines: aposta R$ %.2f (%d minas)" % (stake / 100, m)); c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK"); raise
        return jsonify(estado(aberto_de(c, u["id"]), saldo - stake))
    def fechar(c, g, status, premio):                           # fecha a rodada uma unica vez e paga o premio
        if c.execute("UPDATE mines_games SET status=?, premio=? WHERE id=? AND status='aberto'", (status, premio, g["id"])).rowcount == 1 and premio:
            c.execute("UPDATE users SET saldo=saldo+? WHERE id=?", (premio, g["user_id"])); tx(c, "slot", None, g["user_id"], premio, "Mines: prêmio R$ %.2f" % (premio / 100))
    @app.post("/api/mines/reveal")
    @need("usuario")
    def mn_reveal(u):
        try: idx = int((request.get_json(silent=True) or {}).get("idx"))
        except Exception: idx = -1
        if not 0 <= idx < 25: return err("Casa inválida.")
        c = db(); c.execute("BEGIN")
        try:
            g = aberto_de(c, u["id"], True)
            if not g: c.execute("ROLLBACK"); return err("Nenhuma rodada em andamento.")
            rev = json.loads(g["reveladas"]); bombas = json.loads(g["bombas"])
            if idx not in rev:
                if idx in bombas: fechar(c, g, "perdeu", 0)
                else:
                    rev.append(idx); c.execute("UPDATE mines_games SET reveladas=? WHERE id=?", (json.dumps(rev), g["id"]))
                    if len(rev) == 25 - g["minas"]: fechar(c, g, "sacou", min(int(g["stake"] * mult(g["minas"], len(rev))), MAX_PREMIO))      # limpou o tabuleiro: saque automatico
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK"); raise
        g2 = c.execute("SELECT * FROM mines_games WHERE id=?", (g["id"],)).fetchone(); return jsonify(estado(g2, saldo_de(c, u["id"])))
    @app.post("/api/mines/cashout")
    @need("usuario")
    def mn_cash(u):
        c = db(); c.execute("BEGIN")
        try:
            g = aberto_de(c, u["id"], True)
            if not g: c.execute("ROLLBACK"); return err("Nenhuma rodada em andamento.")
            k = len(json.loads(g["reveladas"]))
            if k < 1: c.execute("ROLLBACK"); return err("Vire pelo menos uma casa para sacar.")
            fechar(c, g, "sacou", min(int(g["stake"] * mult(g["minas"], k)), MAX_PREMIO)); c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK"); raise
        g2 = c.execute("SELECT * FROM mines_games WHERE id=?", (g["id"],)).fetchone(); return jsonify(estado(g2, saldo_de(c, u["id"])))
