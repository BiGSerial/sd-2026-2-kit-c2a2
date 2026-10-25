"""
Cliente de LLM - PRONTO, com modo simulado para funcionar offline.

Se a variavel de ambiente LLM_API_KEY existir, chama a API real.
Se NAO existir, entra em MODO SIMULADO e monta uma resposta a partir do
contexto recuperado. Assim o kit funciona no laboratorio sem internet.

IMPORTANTE: nunca escreva a chave no codigo. Use variavel de ambiente.
"""
import os

import requests

API_KEY = os.getenv("LLM_API_KEY", "")
API_URL = os.getenv("LLM_API_URL", "https://api.openai.com/v1/chat/completions")
MODELO = os.getenv("LLM_MODELO", "gpt-4o-mini")
TIMEOUT = float(os.getenv("LLM_TIMEOUT", "20"))


def erro_transitorio(erro: Exception) -> bool:
    """
    True se vale a pena tentar de novo e se o erro indica indisponibilidade do LLM:
    timeout, falha de conexao, resposta quebrada, HTTP 5xx, 408 e 429.
    False para erros permanentes - HTTP 400, 401, 403, 404... e URL mal formada: o
    servico respondeu, o problema esta no pedido ou na configuracao e repetir nao ajuda.
    """
    if isinstance(erro, requests.HTTPError):
        if erro.response is None:       # cuidado: Response de erro 4xx e "falsa"
            return True
        codigo = erro.response.status_code
        return codigo >= 500 or codigo in (408, 429)
    if isinstance(erro, (requests.exceptions.InvalidURL,
                         requests.exceptions.MissingSchema,
                         requests.exceptions.InvalidSchema)):
        return False
    return True


def descrever_erro(erro: Exception) -> str:
    """
    Descricao curta e segura do erro, para log e para a resposta ao cliente.
    Nunca usa str(erro): as mensagens do requests trazem a URL completa, e algumas
    APIs recebem a chave na query string.
    """
    if isinstance(erro, requests.HTTPError) and erro.response is not None:
        return f"HTTP {erro.response.status_code}"
    if isinstance(erro, requests.Timeout):
        return "tempo-limite excedido"
    if isinstance(erro, requests.ConnectionError):
        return "falha de conexao"
    if isinstance(erro, (ValueError, KeyError, IndexError, TypeError)):
        return "resposta do LLM fora do formato esperado"
    return type(erro).__name__


def _simulado(pergunta, contexto):
    trechos = " ".join(contexto)[:600]
    return (
        "[MODO SIMULADO - sem LLM_API_KEY configurada]\n"
        f"Pergunta: {pergunta}\n"
        f"Com base nos documentos recuperados: {trechos}"
    )


def gerar(pergunta: str, contexto: list) -> str:
    """Monta o prompt com o contexto recuperado e chama o LLM."""
    if not API_KEY:
        return _simulado(pergunta, contexto)

    prompt = (
        "Responda a pergunta usando SOMENTE o contexto abaixo. "
        "Se a resposta nao estiver no contexto, diga que nao sabe.\n\n"
        f"Contexto:\n{chr(10).join(contexto)}\n\nPergunta: {pergunta}"
    )
    resposta = requests.post(
        API_URL,
        headers={"Authorization": f"Bearer {API_KEY}"},
        json={"model": MODELO,
              "messages": [{"role": "user", "content": prompt}],
              "temperature": 0.2},
        timeout=TIMEOUT,
    )
    resposta.raise_for_status()
    return resposta.json()["choices"][0]["message"]["content"]
