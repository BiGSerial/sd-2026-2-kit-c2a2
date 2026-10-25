"""
Servico 3 - GERACAO: monta o prompt com o contexto e chama o LLM.

Fluxo de /perguntar:
  1. pede os trechos mais relevantes ao servico de recuperacao (REST);
  2. chama o LLM com esses trechos como contexto;
  3. devolve a resposta junto com os trechos e as fontes que a embasaram.

Protecao da chamada ao LLM (dependencia externa lenta e instavel), em camadas:
  - TEMPO-LIMITE: cada chamada tem timeout (LLM_TIMEOUT, ver servicos/comum/llm.py);
  - RETENTATIVA: falhas transitorias sao repetidas com espera crescente (backoff);
  - CIRCUIT BREAKER: depois de falhas seguidas o circuito abre e as chamadas sao
    recusadas na hora, sem esperar timeout, ate o LLM dar sinal de vida.
Se o LLM nao responde, o servico NAO quebra: devolve 503 com a mensagem do problema
e ainda entrega os trechos recuperados, que ja respondem a pergunta em parte.

Rodar: uvicorn servicos.geracao.app:app --port 8003
"""
import logging
import os
import time

import requests
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from servicos.comum.circuit_breaker import CircuitBreaker, CircuitoAberto
from servicos.comum.llm import descrever_erro, erro_transitorio, gerar

log = logging.getLogger("geracao")

app = FastAPI(title="Geracao - C2.A2")
URL_RECUPERACAO = os.getenv("URL_RECUPERACAO", "http://localhost:8002")
TIMEOUT_RECUPERACAO = float(os.getenv("TIMEOUT_RECUPERACAO", "10"))

TENTATIVAS_LLM = int(os.getenv("LLM_TENTATIVAS", "3"))
BACKOFF_LLM = float(os.getenv("LLM_BACKOFF", "0.5"))  # 0,5s, 1s, 2s...

disjuntor = CircuitBreaker(
    limite_falhas=int(os.getenv("LLM_LIMITE_FALHAS", "3")),
    espera_segundos=float(os.getenv("LLM_ESPERA_CIRCUITO", "20")),
    conta_como_falha=erro_transitorio,   # 400/401/403/404 nao abrem o circuito
)
# o servico de recuperacao tambem e uma dependencia remota: ganha o seu disjuntor
disjuntor_recuperacao = CircuitBreaker(limite_falhas=3, espera_segundos=10,
                                       conta_como_falha=erro_transitorio)


class Pergunta(BaseModel):
    pergunta: str = Field(min_length=1)


class RecuperacaoIndisponivel(Exception):
    pass


class LLMIndisponivel(Exception):
    """O LLM nao respondeu (timeout, conexao, 5xx...) mesmo apos as retentativas."""


class LLMRecusou(LLMIndisponivel):
    """O LLM respondeu com erro permanente (400/401/403/404...): problema de configuracao."""


def _buscar_trechos(pergunta: str) -> list:
    r = requests.post(f"{URL_RECUPERACAO}/buscar",
                      json={"pergunta": pergunta, "top_k": 3},
                      timeout=TIMEOUT_RECUPERACAO)
    r.raise_for_status()
    return r.json()["resultados"]


def buscar_trechos(pergunta: str) -> list:
    try:
        return disjuntor_recuperacao.chamar(_buscar_trechos, pergunta)
    except CircuitoAberto:
        raise RecuperacaoIndisponivel("servico de recuperacao com circuito aberto")
    except (requests.RequestException, ValueError, KeyError) as erro:
        raise RecuperacaoIndisponivel(
            f"servico de recuperacao falhou: {descrever_erro(erro)}")


def chamar_llm(pergunta: str, contexto: list) -> str:
    """
    LLM com retentativa por fora do disjuntor: CADA tentativa passa pelo circuit
    breaker e conta como falha. Assim o circuito abre depressa e, uma vez aberto,
    as tentativas restantes sao recusadas na hora (CircuitoAberto nao e retentado).
    """
    ultimo_erro = None
    for tentativa in range(1, TENTATIVAS_LLM + 1):
        try:
            return disjuntor.chamar(gerar, pergunta, contexto)
        except CircuitoAberto:
            raise
        except Exception as erro:
            ultimo_erro = erro
            permanente = not erro_transitorio(erro)
            log.warning("LLM falhou (tentativa %d/%d): %s%s", tentativa, TENTATIVAS_LLM,
                        descrever_erro(erro),
                        " [erro permanente: nao sera repetido]" if permanente else "")
            if permanente:
                raise LLMRecusou(descrever_erro(erro)) from erro
            if tentativa == TENTATIVAS_LLM:
                break
            time.sleep(BACKOFF_LLM * 2 ** (tentativa - 1))
    raise LLMIndisponivel(descrever_erro(ultimo_erro)) from ultimo_erro


def _fontes(trechos: list) -> list:
    """Origens dos documentos que embasaram a resposta, sem repeticao e na ordem de relevancia."""
    vistas = []
    for t in trechos:
        if t["origem"] not in vistas:
            vistas.append(t["origem"])
    return vistas


@app.post("/perguntar")
def perguntar(p: Pergunta):
    try:
        trechos = buscar_trechos(p.pergunta)
    except RecuperacaoIndisponivel as erro:
        log.error("%s", erro)
        return JSONResponse(status_code=503, content={
            "erro": "nao foi possivel consultar os documentos no momento",
            "detalhe": str(erro),
        })

    contexto = [t["texto"] for t in trechos]
    fontes = _fontes(trechos)

    if not contexto:
        # sem trechos relevantes nao ha com o que fundamentar: nem chama o LLM
        return {"pergunta": p.pergunta,
                "resposta": "Nao encontrei nos documentos indexados nada relacionado a essa pergunta.",
                "contexto": [], "fontes": []}

    try:
        resposta = chamar_llm(p.pergunta, contexto)
    except CircuitoAberto:
        return JSONResponse(status_code=503, content={
            "erro": "o servico de linguagem esta instavel; o circuito foi aberto "
                    f"e novas tentativas ocorrem em ate {disjuntor.espera_segundos:g}s",
            "degradado": True, "contexto": contexto, "fontes": fontes,
        })
    except LLMRecusou as erro:
        return JSONResponse(status_code=503, content={
            "erro": "o servico de linguagem recusou a requisicao; verifique a "
                    "configuracao (chave, URL e modelo)",
            "detalhe": str(erro),
            "degradado": True, "contexto": contexto, "fontes": fontes,
        })
    except LLMIndisponivel as erro:
        return JSONResponse(status_code=503, content={
            "erro": "o servico de linguagem nao respondeu; tente novamente",
            "detalhe": str(erro),
            "degradado": True, "contexto": contexto, "fontes": fontes,
        })

    return {"pergunta": p.pergunta, "resposta": resposta,
            "contexto": contexto, "fontes": fontes}


@app.get("/saude")
def saude():
    return {"status": "ok", "disjuntor": disjuntor.estado,
            "disjuntor_recuperacao": disjuntor_recuperacao.estado}
