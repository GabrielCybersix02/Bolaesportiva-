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
| `SPORTMONKS_KEY` | não | Token do Sportmonks (escudos de reserva; nunca no código) |
| `SPORTSDB_KEY` | não | chave do TheSportsDB (padrão `3`, grátis, 30 req/min) |
| `ODDSPAPI_KEY` | recomendado | Chave da oddspapi.io (nunca no código). Soma jogos de futebol de muitas outras ligas |
| `ODDSPAPI_REFRESH_HOURS` | não | mínimo entre atualizações (padrão 24). O código aumenta sozinho esse intervalo para a cota de 250 chamadas/mês durar o mês todo |
| `ODDSPAPI_DAYS` | não | padrão 3 (quantos dias à frente buscar, máx. 9) |
| `ODDSPAPI_BOOKMAKERS` | não | padrão `pinnacle,bet365,betano` (até 3; se alguma não existir, o código usa só a primeira) |
| `ODDSPAPI_MONTHLY_LIMIT` | não | padrão 250 (o código para sozinho antes de estourar a cota) |
| `AUTO_SETTLE` | não | padrão 1 (bilhetes de futebol/basquete são liquidados sozinhos; `0` desliga) |
| `DB_POOL` | não | conexões simultâneas com o banco (padrão 6) |

Start command: `gunicorn -w 1 --threads 8 --timeout 90 -b 0.0.0.0:$PORT app:app`

Login inicial: `admin` / `admin123` (troque na hora). As tabelas são criadas sozinhas no primeiro start.

Comissões: menu lateral → *Minha Comissão* (cambista) e *Comissões dos Cambistas* (ADM). Não aparecem em mais nenhuma tela.

Diagnóstico (logado como admin): no Painel do ADM, cartão *Fontes de jogos*; ou `/api/admin/oddspapi` (status), `?run=1` (atualiza agora), `?conta=1` (consulta a conta).
