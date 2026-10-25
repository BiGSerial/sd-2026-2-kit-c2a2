"""
Servico 2 - RECUPERACAO: dada uma pergunta, acha os trechos mais relevantes.

Busca por similaridade do cosseno entre o vetor da pergunta e o vetor de cada
trecho indexado. O servico nao guarda estado: a cada consulta ele le o indice do
servico de ingestao, entao sempre enxerga o que o worker acabou de indexar.

Rodar: uvicorn servicos.recuperacao.app:app --port 8002
"""
import os

import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from servicos.comum.embeddings import Vetorizador, similaridade

app = FastAPI(title="Recuperacao - C2.A2")
URL_INGESTAO = os.getenv("URL_INGESTAO", "http://localhost:8001")
TIMEOUT_INGESTAO = float(os.getenv("TIMEOUT_INGESTAO", "10"))


class Consulta(BaseModel):
    pergunta: str = Field(min_length=1)
    top_k: int = Field(default=3, ge=1, le=20)


@app.post("/buscar")
def buscar(consulta: Consulta):
    """Devolve os top_k trechos mais parecidos com a pergunta, do mais ao menos relevante."""
    try:
        r = requests.get(f"{URL_INGESTAO}/indice", timeout=TIMEOUT_INGESTAO)
        r.raise_for_status()
        itens = r.json()["itens"]
    except (requests.RequestException, ValueError, KeyError):
        raise HTTPException(status_code=503,
                            detail="servico de ingestao indisponivel")

    if not itens:
        return {"resultados": []}

    # O indice foi vetorizado com um vocabulario ajustado em TODOS os textos.
    # Ajustar aqui com os mesmos textos reproduz exatamente o mesmo vocabulario,
    # entao o vetor da pergunta fica no mesmo espaco dos vetores guardados.
    vetorizador = Vetorizador()
    vetorizador.ajustar([i["texto"] for i in itens])
    vetor_pergunta = vetorizador.vetorizar(consulta.pergunta)

    pontuados = [(similaridade(vetor_pergunta, i["vetor"]), i) for i in itens]
    pontuados.sort(key=lambda par: par[0], reverse=True)

    # score 0 = nenhuma palavra em comum: nao e contexto util para o LLM
    resultados = [
        {"texto": item["texto"], "origem": item["origem"], "score": round(score, 4)}
        for score, item in pontuados[:consulta.top_k]
        if score > 0
    ]
    return {"resultados": resultados}


@app.get("/saude")
def saude():
    return {"status": "ok"}
