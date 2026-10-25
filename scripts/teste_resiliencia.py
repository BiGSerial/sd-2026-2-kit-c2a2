"""
Resiliencia de verdade: tempo-limite, retentativa, circuit breaker e recuperacao.

Cenarios do LLM (sempre executados) precisam de:
  - os tres servicos no ar;
  - scripts/llm_falso.py no ar;
  - a geracao apontando para ele, com tempos curtos (perfil "demo" do compose):
      docker compose --env-file .env.demo --profile demo up --build -d
    ou, sem docker, LLM_API_KEY=demo LLM_API_URL=http://localhost:9000/v1/chat/completions
    LLM_TIMEOUT=3 LLM_BACKOFF=0.2 LLM_ESPERA_CIRCUITO=6 (mesmos valores de .env.demo).

Cenarios de infraestrutura (so com --docker) derrubam e religam containers com
`docker compose stop/start`: recuperacao, Redis e worker.

Usa dados proprios desta execucao (nao depende do indice) e espera por CONDICOES reais
(estado do circuito, documento indexado, resposta 200), nao por tempo fixo.

Uso:  python scripts/teste_resiliencia.py [--docker]
"""
import os
import sys
import time

import apoio
import requests
from apoio import conferir, esperar, perguntar, saude

ESPERA_CIRCUITO = float(os.getenv("LLM_ESPERA_CIRCUITO", "6"))
TIMEOUT_LLM = float(os.getenv("LLM_TIMEOUT", "3"))
TENTATIVAS = int(os.getenv("LLM_TENTATIVAS", "3"))
LIMITE_FALHAS = int(os.getenv("LLM_LIMITE_FALHAS", "3"))


def llm(caminho, metodo="post"):
    r = getattr(requests, metodo)(f"{apoio.URL_LLM_FALSO}{caminho}", timeout=5)
    r.raise_for_status()
    return r.json()


def chamadas_ao_llm():
    return llm("/contagem", "get")["chamadas"]


def disjuntor():
    return saude(apoio.URL_GERACAO)["disjuntor"]


def pergunta_do_teste(marcador):
    return f"o que diz o protocolo {marcador}?"


def fechar_circuito(marcador):
    """LLM saudavel de novo: espera o circuito testar e fechar (condicao real, nao sleep)."""
    llm("/modo/ok")
    inicio = time.time()
    esperar(lambda: perguntar(pergunta_do_teste(marcador))[0].status_code == 200,
            "o circuito fechar apos o LLM voltar", ESPERA_CIRCUITO + 30, intervalo=0.5)
    conferir(disjuntor() == "fechado", f"circuito fechou {time.time() - inicio:.1f}s depois (espera={ESPERA_CIRCUITO:g}s)")


def cenarios_llm(marcador):
    print("1. LLM saudavel")
    llm("/modo/ok")
    llm("/reset")
    r, _ = perguntar(pergunta_do_teste(marcador))
    conferir(r.status_code == 200 and disjuntor() == "fechado", "resposta 200 e circuito fechado")
    conferir(chamadas_ao_llm() == 1, "exatamente 1 chamada ao LLM")

    print("2. Erro PERMANENTE (HTTP 401, chave invalida): sem retentativa, sem abrir o circuito")
    llm("/modo/401")
    llm("/reset")
    n = LIMITE_FALHAS + 2
    for _ in range(n):
        r, _ = perguntar(pergunta_do_teste(marcador))
        conferir_resposta = r.status_code == 503 and "configura" in r.json()["erro"]
        if not conferir_resposta:
            apoio.falhou(f"esperava 503 de configuracao, veio {r.status_code}: {r.text[:160]}")
    conferir(chamadas_ao_llm() == n, f"{n} perguntas -> {n} chamadas ao LLM (nenhuma retentativa)")
    conferir(disjuntor() == "fechado", f"circuito segue fechado apos {n} erros permanentes (limite={LIMITE_FALHAS})")

    print("3. Erro TRANSITORIO (HTTP 500): retentativas e abertura do circuito")
    llm("/modo/erro")
    llm("/reset")
    r, _ = perguntar(pergunta_do_teste(marcador))
    corpo = r.json()
    conferir(r.status_code == 503 and corpo.get("degradado"), "falha vira 503 'degradado' (nao derruba o servico)")
    conferir(bool(corpo.get("contexto")) and bool(corpo.get("fontes")),
             "mesmo degradado, entrega os trechos e as fontes recuperados")
    conferir(chamadas_ao_llm() == TENTATIVAS, f"{TENTATIVAS} tentativas ao LLM em uma unica pergunta")
    conferir(disjuntor() == "aberto", f"circuito abriu apos {LIMITE_FALHAS} falhas seguidas")

    print("4. Circuito aberto: recusa imediata, o LLM nem e chamado")
    llm("/reset")
    r, duracao = perguntar(pergunta_do_teste(marcador))
    conferir(r.status_code == 503 and duracao < 1.0, f"503 em {duracao:.2f}s")
    conferir(chamadas_ao_llm() == 0, "0 chamadas ao LLM com o circuito aberto")

    print("5. Recuperacao: o LLM volta e o circuito fecha sozinho")
    fechar_circuito(marcador)

    print("6. HTTP 429 (transitorio): tambem e repetido")
    llm("/modo/429")
    llm("/reset")
    r, _ = perguntar(pergunta_do_teste(marcador))
    conferir(r.status_code == 503 and chamadas_ao_llm() == TENTATIVAS,
             f"503 apos {TENTATIVAS} tentativas")
    fechar_circuito(marcador)

    print(f"7. LLM lento (acima do tempo-limite de {TIMEOUT_LLM:g}s)")
    llm("/modo/lento")
    llm("/reset")
    r, duracao = perguntar(pergunta_do_teste(marcador))
    conferir(r.status_code == 503 and duracao >= TIMEOUT_LLM,
             f"503 apos {duracao:.1f}s: o tempo-limite impede ficar pendurado")
    conferir(chamadas_ao_llm() == TENTATIVAS, f"cada tentativa estourou o tempo-limite ({TENTATIVAS} chamadas)")
    fechar_circuito(marcador)


def cenarios_infra(marcador, origem):
    print("8. Recuperacao indisponivel")
    apoio.docker_compose("stop", "recuperacao")
    try:
        for _ in range(4):
            r, _ = perguntar(pergunta_do_teste(marcador))
            if r.status_code != 503:
                apoio.falhou(f"esperava 503 com a recuperacao fora, veio {r.status_code}")
        conferir(saude(apoio.URL_GERACAO)["disjuntor_recuperacao"] == "aberto",
                 "503 enquanto a recuperacao esta fora e o disjuntor dela abriu")
    finally:
        apoio.docker_compose("start", "recuperacao")
    esperar(lambda: perguntar(pergunta_do_teste(marcador))[0].status_code == 200,
            "a geracao voltar a responder 200 apos a recuperacao subir", 90, intervalo=1)
    conferir(True, "a recuperacao voltou e o circuito dela fechou sozinho")

    print("9. Redis indisponivel e recuperado")
    apoio.docker_compose("stop", "redis")
    try:
        doc, _ = apoio.novo_documento(" (redis fora)")
        r = apoio.indexar([doc])
        conferir(r.status_code == 503, "POST /indexar responde 503 com a fila fora do ar")
        conferir(saude(apoio.URL_INGESTAO)["fila"] == "indisponivel", "/saude da ingestao informa a fila indisponivel")
    finally:
        apoio.docker_compose("start", "redis")
    esperar(lambda: apoio.indexar([doc]).status_code == 202, "o Redis voltar a aceitar documentos", 90, intervalo=1)
    apoio.esperar_indexado(doc["origem"], 90)
    conferir(True, "Redis voltou, o worker reconectou sozinho e indexou o documento")

    print("10. Worker interrompido e reiniciado")
    apoio.docker_compose("stop", "ingestao-worker")
    try:
        doc, _ = apoio.novo_documento(" (worker parado)")
        conferir(apoio.indexar([doc]).status_code == 202, "a API continua aceitando documentos")
        esperar(lambda: saude(apoio.URL_INGESTAO)["pendentes_na_fila"] >= 1, "o documento ficar na fila")
        conferir(apoio.contar_origem(doc["origem"]) == 0, "sem worker, o documento espera na fila (nao foi perdido nem indexado)")
    finally:
        apoio.docker_compose("start", "ingestao-worker")
    apoio.esperar_indexado(doc["origem"], 90)
    esperar(lambda: saude(apoio.URL_INGESTAO)["pendentes_na_fila"] == 0, "a fila esvaziar")
    conferir(apoio.contar_origem(doc["origem"]) == 1, "worker reiniciado consumiu a fila: indexado 1 vez")


def main():
    usar_docker = "--docker" in sys.argv[1:]
    apoio.esperar_servicos()
    llm("/modo", "get")
    doc, marcador = apoio.novo_documento()
    apoio.indexar([doc])
    apoio.esperar_indexado(doc["origem"])

    cenarios_llm(marcador)
    if usar_docker:
        cenarios_infra(marcador, doc["origem"])
    else:
        print("\n(cenarios de infraestrutura - recuperacao, Redis e worker - pulados: use --docker)")
    llm("/modo/ok")
    print("\nOK: resiliencia funcionando")


if __name__ == "__main__":
    try:
        main()
    except requests.RequestException as erro:
        apoio.falhou(f"nao consegui falar com um dos servicos: {erro}")
