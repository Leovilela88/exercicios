# Exercícios

Webapp pessoal para controle de atividades físicas (corrida, natação e outros), com dashboard, totais, dias ativos e evolução semanal.

## Stack
- FastAPI + Jinja2 (Python 3.11)
- SQLAlchemy 2.x + PostgreSQL (SQLite local como fallback)
- Chart.js (via CDN)

## Rodar localmente

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload
```

Abre `http://localhost:8000`. Sem `DATABASE_URL` definida, usa SQLite (`exercicios.db`) automaticamente.

## Deploy no Railway

1. Crie um serviço PostgreSQL no Railway. Ele expõe `DATABASE_URL`.
2. Conecte o repositório GitHub ao serviço web.
3. O `Procfile` já está configurado: `web: uvicorn main:app --host 0.0.0.0 --port $PORT`.
4. As tabelas são criadas automaticamente no boot (`Base.metadata.create_all`).

## Rotas

- `GET /` — dashboard
- `GET /novo` — formulário de novo treino
- `POST /novo` — cria treino
- `POST /delete/{id}` — remove treino
