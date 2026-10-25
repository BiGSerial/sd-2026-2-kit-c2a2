# Servico de ingestao. Contexto de build: a raiz do repositorio (ver docker-compose.yml).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

# dependencias primeiro: a camada fica em cache enquanto o requirements nao mudar
COPY servicos/ingestao/requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY servicos/__init__.py servicos/__init__.py
COPY servicos/comum servicos/comum
COPY servicos/ingestao servicos/ingestao

EXPOSE 8001
CMD ["uvicorn", "servicos.ingestao.app:app", "--host", "0.0.0.0", "--port", "8001"]
