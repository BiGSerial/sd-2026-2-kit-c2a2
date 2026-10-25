"""
Demonstra, de verdade, a protecao da chamada ao LLM: tempo-limite, retentativa
e circuit breaker. Precisa de:
  - os tres servicos no ar, com documentos ja indexados (teste_ponta_a_ponta.py);
  - scripts/llm_falso.py no ar;
  - o servico de geracao apontando para o LLM falso, com espera curta do circuito:
      LLM_API_KEY=demo LLM_API_URL=http://localhost:9000/v1/chat/completions
      LLM_TIMEOUT=3 LLM_BACKOFF=0.2 LLM_ESPERA_CIRCUITO=6
    (o docker-compose ja traz isso no perfil "demo"; ver README)

Uso:  python scripts/teste_resiliencia.py
"""
import os
import sys
import time

import requests

URL_GERACAO = os.getenv("URL_GERACAO", "http://localhost:8003")
URL_LLM_FALSO = os.getenv("URL_LLM_FALSO", "http://localhost:9000")
ESPERA_CIRCUITO = float(os.getenv("LLM_ESPERA_CIRCUITO", "6"))
TIMEOUT_LLM = float(os.getenv("LLM_TIMEOUT", "3"))


def modo_llm(modo):
    requests.post(f"{URL_LLM_FALSO}/modo/{modo}", timeout=5).raise_for_status()


def perguntar():
    inicio = time.time()
    r = requests.post(f"{URL_GERACAO}/perguntar",
                      json={"pergunta": "o que diz o teorema CAP?"}, timeout=120)
    return r, time.time() - inicio


def disjuntor():
    return requests.get(f"{URL_GERACAO}/saude", timeout=5).json()["disjuntor"]


def conferir(condicao, mensagem):
    if not condicao:
        sys.exit(f"FALHOU: {mensagem}")
    print(f"   ok - {mensagem}")


def main():
    print("1. LLM saudavel")
    modo_llm("ok")
    r, _ = perguntar()
    conferir(r.status_code == 200 and disjuntor() == "fechado",
             "resposta 200 e disjuntor fechado")

    print("2. LLM quebrado (erro 500): retentativas e abertura do circuito")
    modo_llm("erro")
    r, _ = perguntar()
    conferir(r.status_code == 503 and r.json().get("degradado"),
             "falha vira 503 'degradado' (sem derrubar o servico)")
    conferir(bool(r.json().get("contexto")),
             "mesmo degradado, devolve os trechos recuperados dos documentos")
    conferir(disjuntor() == "aberto", "disjuntor aberto apos as falhas seguidas")

    print("3. Circuito aberto: recusa imediata, sem esperar o LLM")
    r, duracao = perguntar()
    conferir(r.status_code == 503 and duracao < 1.0,
             f"503 em {duracao:.2f}s (sem tentar chamar o LLM)")

    print(f"4. LLM volta; apos {ESPERA_CIRCUITO:g}s o circuito testa e fecha")
    modo_llm("ok")
    time.sleep(ESPERA_CIRCUITO + 1)
    r, _ = perguntar()
    conferir(r.status_code == 200 and disjuntor() == "fechado",
             "chamada de teste passou: resposta 200 e disjuntor fechado")

    print(f"5. LLM lento (acima do tempo-limite de {TIMEOUT_LLM:g}s)")
    modo_llm("lento")
    r, duracao = perguntar()
    conferir(r.status_code == 503 and duracao >= TIMEOUT_LLM,
             f"tempo-limite respeitado: 503 apos {duracao:.1f}s, em vez de ficar pendurado")

    modo_llm("ok")
    print("\nOK: resiliencia funcionando (timeout, retentativa e circuit breaker)")


if __name__ == "__main__":
    try:
        main()
    except requests.RequestException as erro:
        sys.exit(f"FALHOU: nao consegui falar com um dos servicos: {erro}")
