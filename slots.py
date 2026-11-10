"""Area de Slots da Bola Esportiva. O sorteio e o dinheiro ficam SO no servidor; o navegador apenas anima o resultado.
Ligacao no app.py (2 linhas, antes de @app.get("/health")):   import slots; slots.register(app, db, raw, need, tx, err, cents, MAX_PREMIO, BASE)
Cada jogo e um dict em GAMES: para adicionar um jogo novo (dos repositorios), basta criar outro dict com strip + pay."""
import json, os, secrets
from flask import jsonify, request, send_from_directory
from datetime import datetime, timezone

RNG = secrets.SystemRandom()
LINES = [(0, 0, 0), (1, 1, 1), (2, 2, 2), (0, 1, 2), (2, 1, 0)]          # linha de cada rolo (0=cima, 1=meio, 2=baixo): 3 retas + 2 diagonais
GAMES = {
    "classico": dict(nome="Clássico 777", sub="3 rolos • 5 linhas", min=100, max=10000,
        symbols=["🍒", "🍋", "🔔", "⭐", "7️⃣", "💎"],
        strip=[0, 1, 0, 2, 1, 0, 3, 1, 0, 2, 1, 0, 4, 1, 2, 0, 3, 1, 2, 5],        # fita de cada rolo (indices em symbols)
        pay={0: 9, 1: 13, 2: 22, 3: 58, 4: 180, 5: 580}),                          # multiplicador da LINHA (aposta total / 5 por linha)
}
def window(g, stop):                                  # 3 simbolos consecutivos da fita a partir do ponto de parada
    s = g["strip"]; return [s[(stop + r) % len(s)] for r in range(3)]
def spin_grid(g):
    stops = [RNG.randrange(len(g["strip"])) for _ in range(3)]
    return [window(g, s) for s in stops]               # grid[rolo][linha]
def evaluate(g, grid, stake):
    """Devolve (premio_em_centavos, linhas_vencedoras). Premio por linha = mult * aposta/5."""
    win, hits = 0.0, []
    for i, ln in enumerate(LINES):
        sy = [grid[c][ln[c]] for c in range(3)]
        if sy[0] == sy[1] == sy[2]: win += g["pay"][sy[0]] * stake / len(LINES); hits.append(dict(linha=i, simbolo=sy[0]))
    return int(win), hits
def rtp(g):                                           # RTP exato: percorre todas as combinacoes de paradas (usado em simular_rtp.py)
    n = len(g["strip"]); tot = 0.0
    for a in range(n):
        for b in range(n):
            for c in range(n): tot += evaluate(g, [window(g, a), window(g, b), window(g, c)], 1000)[0] / 1000
    return tot / n ** 3

def register(app, db, raw, need, tx, err, cents, MAX_PREMIO, BASE):
    def schema():
        c = raw()
        try: c.execute("CREATE TABLE IF NOT EXISTS slot_spins(id SERIAL PRIMARY KEY, data TEXT, user_id INTEGER NOT NULL REFERENCES users(id), jogo TEXT, aposta BIGINT, premio BIGINT, grid TEXT)")
        finally: c.close()
    schema()
    @app.get("/slots")
    def slots_page():
        r = send_from_directory(os.path.join(BASE, "static"), "slots.html"); r.headers["Cache-Control"] = "no-cache"; return r
    @app.get("/api/slots")
    @need("usuario")                                  # so cliente com conta joga (o saldo dele e o fiado dado pelo cambista)
    def slots_info(u):
        h = db().execute("SELECT jogo, aposta, premio, data FROM slot_spins WHERE user_id=? ORDER BY id DESC LIMIT 10", (u["id"],)).fetchall()
        return jsonify(ok=True, saldo=u["saldo"] / 100, jogos={k: dict(nome=v["nome"], sub=v["sub"], min=v["min"] / 100, max=v["max"] / 100, symbols=v["symbols"],
                       pay={v["symbols"][s]: m for s, m in v["pay"].items()}) for k, v in GAMES.items()},
                       historico=[dict(jogo=r["jogo"], aposta=r["aposta"] / 100, premio=r["premio"] / 100, data=r["data"]) for r in h])
    @app.post("/api/slots/<jogo>/spin")
    @need("usuario")
    def slots_spin(u, jogo):
        g = GAMES.get(jogo)
        if not g: return err("Jogo não encontrado.", 404)
        stake = cents((request.get_json(silent=True) or {}).get("valor"))
        if not g["min"] <= stake <= g["max"]: return err("Aposta de R$ %.2f a R$ %.2f." % (g["min"] / 100, g["max"] / 100))
        c = db(); c.execute("BEGIN")
        try:
            saldo = c.execute("SELECT saldo FROM users WHERE id=? FOR UPDATE", (u["id"],)).fetchone()[0]       # trava: giros simultaneos nao gastam o mesmo saldo
            if stake > saldo: c.execute("ROLLBACK"); return err("Saldo insuficiente. Peça uma recarga ao seu cambista.")
            grid = spin_grid(g); win, hits = evaluate(g, grid, stake); win = min(win, MAX_PREMIO)
            c.execute("UPDATE users SET saldo=saldo-?+? WHERE id=?", (stake, win, u["id"]))
            c.execute("INSERT INTO slot_spins(data,user_id,jogo,aposta,premio,grid) VALUES(?,?,?,?,?,?)", (datetime.now(timezone.utc).isoformat(), u["id"], jogo, stake, win, json.dumps(grid)))
            tx(c, "slot", u["id"], None, stake, "Slot %s: aposta R$ %.2f, prêmio R$ %.2f" % (g["nome"], stake / 100, win / 100))
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK"); raise
        return jsonify(ok=True, grid=[[g["symbols"][s] for s in col] for col in grid], linhas=[h["linha"] for h in hits], premio=win / 100, saldo=(saldo - stake + win) / 100)
