# FORT.BET (Flask + SQLite)
Rodar local:  pip install -r requirements.txt && python app.py   → http://localhost:5000
Login inicial: admin / admin123 (troque em "Mudar Senha").
Produção: gunicorn -w 1 --threads 8 -b 0.0.0.0:8000 app:app   (use HTTPS=1 atrás de HTTPS)
Variáveis: ODDS_API_KEY, SECRET_KEY, FORTBET_DB, COMISSAO_PCT (padrão 10), ODDS_CACHE_MINUTES (padrão 15)
Banco: fortbet.db (SQLite, criado automático). Faça backup desse arquivo.
