# Bola Esportiva (Flask + Supabase/Postgres)

## Estrutura do repositório
```
app.py
requirements.txt
README.md
static/
  index.html   (o site)
  logo.svg     (logo completa)
  icon.svg     (ícone / favicon)
```

## Variáveis de ambiente (Render → Environment)
| Variável | Obrigatória | O que é |
|---|---|---|
| `DATABASE_URL` | sim | String de conexão do Supabase (**Session pooler**) |
| `SECRET_KEY` | sim | Texto aleatório longo (mantém o login após cada deploy) |
| `ODDS_API_KEY` | sim | Chave da the-odds-api.com (nunca no código) |
| `HTTPS` | recomendado | `1` |
| `COMISSAO_PCT` | não | padrão 10 |
| `ODDS_CACHE_MINUTES` | não | padrão 15 |
| `ODDS_FORCE_MIN_SECONDS` | não | padrão 60 (botão Atualizar não gasta crédito antes disso) |
| `ODDS_IDLE_HOURS` | não | padrão 1 (só renova as odds sozinho se alguém usou nesse período) |
| `FOOTBALL_DATA_KEY` | recomendado | Token do football-data.org (escudos oficiais; nunca no código). Sem ele, usa TheSportsDB/Wikipedia |
| `SPORTSDB_KEY` | não | chave do TheSportsDB (padrão `3`, grátis, 30 req/min) |
| `DB_POOL` | não | conexões simultâneas com o banco (padrão 6) |

Start command: `gunicorn -w 1 --threads 8 --timeout 90 -b 0.0.0.0:$PORT app:app`

Login inicial: `admin` / `admin123` (troque na hora). As tabelas são criadas sozinhas no primeiro start.
