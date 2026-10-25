"""
Servico 1 - INGESTAO: recebe documentos, gera embeddings e armazena.

API REST do servico. A indexacao em si NAO acontece aqui: /indexar apenas
coloca os documentos numa FILA (Redis) e responde 202. Quem consome a fila,
gera os embeddings e grava o indice em disco e o worker (servicos/ingestao/worker.py).

Por que fila: gerar embeddings e reindexar custa tempo proporcional ao tamanho do
indice. Com a fila, quem envia documentos nao espera esse trabalho, picos de envio
sao absorvidos, e se o worker cair as mensagens esperam no Redis.

Rodar: uvicorn servicos.ingestao.app:app --port 8001
"""
import json
import os

import redis
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, field_validator

from servicos.ingestao import armazenamento

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
FILA = os.getenv("FILA_INGESTAO", "fila:ingestao")

app = FastAPI(title="Ingestao - C2.A2")

_redis = redis.Redis.from_url(
    REDIS_URL, decode_responses=True,
    socket_connect_timeout=2, socket_timeout=5,
)


class Documento(BaseModel):
    texto: str
    origem: str = "desconhecida"

    @field_validator("texto")
    @classmethod
    def texto_nao_vazio(cls, valor: str) -> str:
        if not valor.strip():
            raise ValueError("texto nao pode ser vazio")
        return valor


@app.post("/indexar", status_code=202)
def indexar(docs: list[Documento]):
    """Enfileira os documentos para indexacao assincrona."""
    if not docs:
        raise HTTPException(status_code=422, detail="envie ao menos um documento")
    mensagens = [json.dumps({"texto": d.texto, "origem": d.origem},
                            ensure_ascii=False) for d in docs]
    try:
        pipe = _redis.pipeline()
        pipe.rpush(FILA, *mensagens)
        pipe.llen(FILA)
        _, tamanho_fila = pipe.execute()
    except redis.RedisError:
        raise HTTPException(status_code=503,
                            detail="fila indisponivel: tente novamente em instantes")
    return {"enfileirados": len(mensagens), "na_fila": tamanho_fila}


@app.get("/indice")
def indice():
    """Consumido pelo servico de recuperacao."""
    itens = armazenamento.carregar()
    return {"total": len(itens),
            "itens": [{"texto": i["texto"], "origem": i["origem"],
                       "vetor": i["vetor"]} for i in itens]}


@app.get("/saude")
def saude():
    try:
        pendentes = _redis.llen(FILA)
        fila = "ok"
    except redis.RedisError:
        pendentes = None
        fila = "indisponivel"
    return {"status": "ok", "documentos": len(armazenamento.carregar()),
            "fila": fila, "pendentes_na_fila": pendentes}
