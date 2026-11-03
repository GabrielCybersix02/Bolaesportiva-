# FORT.BET (Flask + Supabase/Postgres)
Variáveis obrigatórias: DATABASE_URL (Supabase, "Session pooler"), SECRET_KEY (texto aleatório longo)
Opcionais: ODDS_API_KEY, HTTPS=1, COMISSAO_PCT (10), ODDS_CACHE_MINUTES (15)
Start: gunicorn -w 1 --threads 8 -b 0.0.0.0:$PORT app:app
Login inicial: admin / admin123 (troque na hora). As tabelas são criadas sozinhas no primeiro start.
