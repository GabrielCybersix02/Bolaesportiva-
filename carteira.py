"""Carteira da Bola Esportiva: deposito (Pix) e saque (Pix) pelo gateway ASAAS + senha de saque de 4 digitos.
Ligacao no app.py (1 linha, junto da dos slots):   import carteira; carteira.register(app, db, raw, need, tx, err, cents, BASE)
VARIAVEIS NO RENDER (nunca coloque a chave no codigo):  ASAAS_API_KEY, ASAAS_WEBHOOK_TOKEN (voce inventa; o mesmo nos webhooks do Asaas), ASAAS_AMBIENTE=producao (ou sandbox para testes)
Limites (opcionais):  DEPOSITO_MIN=10  SAQUE_MIN=10  SAQUE_MAX=20   (valores em reais)"""
import hmac, os, re, secrets, time
import requests
from flask import jsonify, request, send_from_directory, session, redirect
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timezone

DEP_MIN = int(float(os.environ.get("DEPOSITO_MIN", "10")) * 100)
SAQ_MIN = int(float(os.environ.get("SAQUE_MIN", "10")) * 100)
SAQ_MAX = int(float(os.environ.get("SAQUE_MAX", "20")) * 100)
iso = lambda: datetime.now(timezone.utc).isoformat()

# ------------------------------------------------------------------ GATEWAY ASAAS (API v3)
TIPOS = {"cpf": "CPF", "email": "EMAIL", "telefone": "PHONE", "aleatoria": "EVP"}
class Asaas:
    def _url(self): return "https://api-sandbox.asaas.com/v3" if os.environ.get("ASAAS_AMBIENTE", "producao") == "sandbox" else "https://api.asaas.com/v3"
    def _req(self, metodo, path, body=None):
        if not os.environ.get("ASAAS_API_KEY"): raise RuntimeError("Gateway de pagamento não configurado.")
        r = requests.request(metodo, self._url() + path, json=body, timeout=25, headers={"access_token": os.environ["ASAAS_API_KEY"], "Content-Type": "application/json", "User-Agent": "BolaEsportiva"})
        try: d = r.json()
        except Exception: d = {}
        if r.status_code >= 400: raise RuntimeError((d.get("errors") or [{}])[0].get("description") or "Gateway recusou (%s)." % r.status_code)
        return d
    def cliente(self, nome, cpf): return self._req("POST", "/customers", {"name": nome, "cpfCnpj": cpf, "notificationDisabled": True})["id"]
    def criar_cobranca(self, cliente_id, centavos, ref):    # cobranca Pix com vencimento hoje -> dict(ext_id, copia_cola, qr_base64)
        p = self._req("POST", "/payments", {"customer": cliente_id, "billingType": "PIX", "value": centavos / 100, "dueDate": datetime.now(timezone.utc).strftime("%Y-%m-%d"), "externalReference": ref, "description": "Depósito Bola Esportiva"})
        q = self._req("GET", "/payments/%s/pixQrCode" % p["id"])
        return dict(ext_id=p["id"], copia_cola=q.get("payload"), qr_base64=q.get("encodedImage"))
    def consultar_cobranca(self, ext_id):                   # -> dict(pago, centavos)
        d = self._req("GET", "/payments/" + ext_id)
        return dict(pago=d.get("status") in ("RECEIVED", "CONFIRMED"), centavos=int(round(float(d.get("value", 0)) * 100)))
    def enviar_pix(self, centavos, tipo, chave, ref):       # transferencia Pix -> dict(ext_id, pago)  (pago=False: aceito, aguardando o webhook TRANSFER_DONE)
        if tipo == "telefone" and not chave.startswith("+"): chave = "+55" + chave
        d = self._req("POST", "/transfers", {"value": centavos / 100, "pixAddressKey": chave, "pixAddressKeyType": TIPOS[tipo], "description": "Saque Bola Esportiva", "externalReference": ref})
        if d.get("status") in ("FAILED", "CANCELLED"): raise RuntimeError("Transferência recusada.")
        return dict(ext_id=d.get("id", ""), pago=d.get("status") == "DONE")
gw = Asaas()
def cpf_ok(c):
    c = re.sub(r"\D", "", c or "")
    if len(c) != 11 or c == c[0] * 11: return None
    for n in (9, 10):
        if (sum(int(c[i]) * (n + 1 - i) for i in range(n)) * 10 % 11) % 10 != int(c[n]): return None
    return c

def register(app, db, raw, need, tx, err, cents, BASE):
    c0 = raw()
    try:
        c0.execute("CREATE TABLE IF NOT EXISTS pagamentos(id SERIAL PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), tipo TEXT NOT NULL, valor BIGINT NOT NULL, status TEXT NOT NULL, ext_id TEXT, ref TEXT UNIQUE, chave TEXT, copia_cola TEXT, qr TEXT, criado TEXT, atualizado TEXT)")
        c0.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS saque_pw TEXT"); c0.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS cpf TEXT"); c0.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS asaas_id TEXT"); c0.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS saque_erros INTEGER NOT NULL DEFAULT 0"); c0.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS saque_bloq DOUBLE PRECISION NOT NULL DEFAULT 0")
    finally: c0.close()
    brl = lambda v: "R$ %.2f" % (v / 100)
    sacavel = lambda u: max(0, u["saldo"] - (u["credito"] or 0))      # fiado (credito dado pelo cambista) NAO pode ser sacado

    @app.get("/carteira")
    def carteira_page():
        uid = session.get("uid"); row = db().execute("SELECT role FROM users WHERE id=? AND ativo=1", (uid,)).fetchone() if uid else None
        if not row or row["role"] != "usuario": return redirect("/")
        r = send_from_directory(os.path.join(BASE, "static"), "carteira.html"); r.headers["Cache-Control"] = "no-cache"; return r
    @app.get("/api/carteira")
    @need("usuario")
    def cart_info(u):
        h = db().execute("SELECT id,tipo,valor,status,criado FROM pagamentos WHERE user_id=? ORDER BY id DESC LIMIT 20", (u["id"],)).fetchall()
        return jsonify(ok=True, saldo=u["saldo"] / 100, sacavel=sacavel(u) / 100, dep_min=DEP_MIN / 100, saq_min=SAQ_MIN / 100, saq_max=SAQ_MAX / 100, tem_senha=bool(u["saque_pw"]), tem_cpf=bool(u["cpf"]),
                       bloqueado=max(0, int(u["saque_bloq"] - time.time())), historico=[dict(id=r["id"], tipo=r["tipo"], valor=r["valor"] / 100, status=r["status"], data=r["criado"]) for r in h])

    @app.post("/api/carteira/senha")                   # cria (pede a senha de LOGIN) ou troca (pede a senha de saque ATUAL) a senha de saque de 4 digitos
    @need("usuario")
    def cart_senha(u):
        d = request.get_json(silent=True) or {}; nova = str(d.get("nova", ""))
        if not re.fullmatch(r"\d{4}", nova): return err("A senha de saque deve ter exatamente 4 números.")
        if u["saque_pw"]:
            if time.time() < u["saque_bloq"]: return err("Muitas tentativas. Aguarde alguns minutos.", 429)
            if not check_password_hash(u["saque_pw"], str(d.get("atual", ""))): return err("Senha de saque atual incorreta.")
        elif not check_password_hash(u["pw"], str(d.get("login_senha", ""))): return err("Senha de login incorreta.")
        db().execute("UPDATE users SET saque_pw=?, saque_erros=0, saque_bloq=0 WHERE id=?", (generate_password_hash(nova), u["id"]))
        return jsonify(ok=True)

    def confirmar_deposito(c, p):                      # idempotente: so credita uma vez, e so se o GATEWAY confirmar o pagamento e o valor
        if p["status"] != "pendente": return p["status"]
        try: r = gw.consultar_cobranca(p["ext_id"])
        except Exception: return "pendente"
        if not r["pago"] or r["centavos"] < p["valor"]: return "pendente"
        c.execute("BEGIN")
        try:
            if c.execute("UPDATE pagamentos SET status='pago', atualizado=? WHERE id=? AND status='pendente'", (iso(), p["id"])).rowcount == 1:
                c.execute("UPDATE users SET saldo=saldo+? WHERE id=?", (p["valor"], p["user_id"])); tx(c, "deposito", None, p["user_id"], p["valor"], "Depósito Pix " + brl(p["valor"]))
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK"); raise
        return "pago"
    @app.post("/api/carteira/depositar")
    @need("usuario")
    def cart_dep(u):
        v = cents((request.get_json(silent=True) or {}).get("valor"))
        if v < DEP_MIN: return err("Depósito mínimo de " + brl(DEP_MIN) + ".")
        if v > 5000000: return err("Valor acima do permitido.")
        c = db(); cpf = u["cpf"] or cpf_ok((request.get_json(silent=True) or {}).get("cpf"))
        if not cpf: return err("Informe um CPF válido (exigido para gerar o Pix).")
        ref = "dep-%d-%s" % (u["id"], secrets.token_hex(6))
        try:
            cid = u["asaas_id"] or gw.cliente(u["nome"], cpf)
            if not u["cpf"] or not u["asaas_id"]: c.execute("UPDATE users SET cpf=?, asaas_id=? WHERE id=?", (cpf, cid, u["id"]))
            g = gw.criar_cobranca(cid, v, ref)
        except Exception as e: return err(str(e), 502)
        pid = c.execute("INSERT INTO pagamentos(user_id,tipo,valor,status,ext_id,ref,copia_cola,qr,criado,atualizado) VALUES(?,?,?,?,?,?,?,?,?,?) RETURNING id",
                                  (u["id"], "deposito", v, "pendente", g["ext_id"], ref, g["copia_cola"], g["qr_base64"], iso(), iso())).fetchone()[0]
        return jsonify(ok=True, id=pid, copia_cola=g["copia_cola"], qr=g["qr_base64"])
    @app.get("/api/carteira/deposito/<int:pid>")       # o site pergunta de tempos em tempos; confirma direto no gateway
    @need("usuario")
    def cart_dep_status(u, pid):
        c = db(); p = c.execute("SELECT * FROM pagamentos WHERE id=? AND user_id=? AND tipo='deposito'", (pid, u["id"])).fetchone()
        if not p: return err("Não encontrado.", 404)
        st = confirmar_deposito(c, p); s = c.execute("SELECT saldo FROM users WHERE id=?", (u["id"],)).fetchone()[0]
        return jsonify(ok=True, status=st, saldo=s / 100)
    def token_ok():
        sec = os.environ.get("ASAAS_WEBHOOK_TOKEN", ""); return bool(sec) and hmac.compare_digest(sec, request.headers.get("asaas-access-token", ""))
    @app.post("/api/carteira/webhook")                 # Asaas > Integracoes > Webhooks: URL https://SEU-SITE/api/carteira/webhook, token = ASAAS_WEBHOOK_TOKEN, eventos de COBRANCA e de TRANSFERENCIA
    def cart_webhook():
        if not token_ok(): return "", 403
        d = request.get_json(silent=True) or {}; ev = str(d.get("event", "")); c = raw()
        try:
            if ev in ("PAYMENT_RECEIVED", "PAYMENT_CONFIRMED"):
                p = c.execute("SELECT * FROM pagamentos WHERE ext_id=? AND tipo='deposito'", (str((d.get("payment") or {}).get("id", "")),)).fetchone()
                if p: confirmar_deposito(c, p)         # confere de novo direto na API do Asaas antes de creditar
            elif ev.startswith("TRANSFER_"):
                t = d.get("transfer") or {}; p = c.execute("SELECT * FROM pagamentos WHERE tipo='saque' AND (ext_id=? OR ref=?)", (str(t.get("id", "")), str(t.get("externalReference", "")))).fetchone()
                if p and p["status"] == "processando":
                    if ev == "TRANSFER_DONE": c.execute("UPDATE pagamentos SET status='pago', atualizado=? WHERE id=? AND status='processando'", (iso(), p["id"]))
                    elif ev in ("TRANSFER_FAILED", "TRANSFER_CANCELLED"): estornar(c, p)
        finally: c.close()
        return "ok"
    @app.post("/api/carteira/validar-saque")           # Asaas > Integracoes > "Mecanismo de validacao de saque via webhook": so aprova transferencia que ESTE site criou (mesmo ref e valor)
    def cart_validar():
        if not token_ok(): return "", 403
        t = (request.get_json(silent=True) or {}).get("transfer") or {}; c = raw()
        try:
            p = c.execute("SELECT * FROM pagamentos WHERE tipo='saque' AND status='processando' AND ref=?", (str(t.get("externalReference", "")),)).fetchone()
            ok = p and int(round(float(t.get("value", 0)) * 100)) == p["valor"]
        finally: c.close()
        return jsonify(status="APPROVED") if ok else jsonify(status="REFUSED", refuseReason="Transferência não reconhecida")

    def estornar(c, p):                                # saque falhou: devolve o valor ao saldo (uma unica vez)
        c.execute("BEGIN")
        try:
            if c.execute("UPDATE pagamentos SET status='falhou', atualizado=? WHERE id=? AND status IN ('processando','pendente')", (iso(), p["id"])).rowcount == 1:
                c.execute("UPDATE users SET saldo=saldo+? WHERE id=?", (p["valor"], p["user_id"])); tx(c, "saque", None, p["user_id"], p["valor"], "Saque estornado " + brl(p["valor"]))
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK"); raise
    @app.post("/api/carteira/sacar")
    @need("usuario")
    def cart_saque(u):
        d = request.get_json(silent=True) or {}; v = cents(d.get("valor")); tipo = str(d.get("tipo", "")); chave = re.sub(r"\s", "", str(d.get("chave", "")))
        if not u["saque_pw"]: return err("Crie sua senha de saque primeiro.")
        if time.time() < u["saque_bloq"]: return err("Saque bloqueado por tentativas erradas. Aguarde alguns minutos.", 429)
        c = db()
        if not check_password_hash(u["saque_pw"], str(d.get("senha", ""))):
            erros = (u["saque_erros"] or 0) + 1; bloq = time.time() + 900 if erros >= 5 else 0
            c.execute("UPDATE users SET saque_erros=?, saque_bloq=? WHERE id=?", (0 if bloq else erros, bloq, u["id"]))
            return err("Senha de saque incorreta." + ("" if bloq else " Restam %d tentativas." % (5 - erros)) if not bloq else "Senha incorreta. Saque bloqueado por 15 minutos.")
        regras = {"cpf": r"\d{11}", "email": r"[^@\s]+@[^@\s]+\.[^@\s]+", "telefone": r"\+?\d{10,13}", "aleatoria": r"[0-9a-fA-F-]{32,36}"}
        if tipo not in regras or not re.fullmatch(regras[tipo], chave): return err("Chave Pix inválida.")
        if tipo == "cpf" and u["cpf"] and chave != u["cpf"]: return err("A chave CPF deve ser o seu próprio CPF.")
        if not SAQ_MIN <= v <= SAQ_MAX: return err("Saque de %s a %s." % (brl(SAQ_MIN), brl(SAQ_MAX)))
        ref = "saq-%d-%s" % (u["id"], secrets.token_hex(6)); c.execute("BEGIN")
        try:
            row = c.execute("SELECT saldo, COALESCE(credito,0) FROM users WHERE id=? FOR UPDATE", (u["id"],)).fetchone()
            if v > max(0, row[0] - row[1]): c.execute("ROLLBACK"); return err("Saldo disponível para saque: %s (crédito/fiado não pode ser sacado)." % brl(max(0, row[0] - row[1])))
            if c.execute("SELECT 1 FROM pagamentos WHERE user_id=? AND tipo='saque' AND status='processando'", (u["id"],)).fetchone(): c.execute("ROLLBACK"); return err("Você já tem um saque em processamento.")
            c.execute("UPDATE users SET saldo=saldo-?, saque_erros=0 WHERE id=?", (v, u["id"]))
            pid = c.execute("INSERT INTO pagamentos(user_id,tipo,valor,status,ref,chave,criado,atualizado) VALUES(?,?,?,?,?,?,?,?) RETURNING id", (u["id"], "saque", v, "processando", ref, tipo + ":" + chave, iso(), iso())).fetchone()[0]
            tx(c, "saque", u["id"], None, v, "Saque Pix " + brl(v)); c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK"); raise
        try:                                           # o dinheiro ja saiu do saldo (reservado); se o gateway falhar, estorna
            g = gw.enviar_pix(v, tipo, chave, ref); c.execute("UPDATE pagamentos SET ext_id=?, status=?, atualizado=? WHERE id=?", (g["ext_id"], "pago" if g["pago"] else "processando", iso(), pid))
        except Exception as e:
            estornar(c, dict(id=pid, user_id=u["id"], valor=v)); return err("Não foi possível enviar o Pix agora. Seu saldo foi devolvido. " + str(e), 502)
        return jsonify(ok=True, saldo=(row[0] - v) / 100)
