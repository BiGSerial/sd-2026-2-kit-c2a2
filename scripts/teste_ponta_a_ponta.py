"""
Teste de ponta a ponta: com os servicos no ar, indexa os documentos, faz uma
pergunta e confere que a resposta vem fundamentada nos documentos.

Uso:  python scripts/teste_ponta_a_ponta.py
"""
import os
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import indexar_documentos  # noqa: E402

URL_INGESTAO = os.getenv("URL_INGESTAO", "http://localhost:8001")
URL_RECUPERACAO = os.getenv("URL_RECUPERACAO", "http://localhost:8002")
URL_GERACAO = os.getenv("URL_GERACAO", "http://localhost:8003")


def esperar(condicao, descricao, limite=60):
    inicio = time.time()
    while time.time() - inicio < limite:
        try:
            if condicao():
                return
        except requests.RequestException:
            pass
        time.sleep(1)
    sys.exit(f"FALHOU: tempo esgotado esperando {descricao}")


def main():
    for nome, url in (("ingestao", URL_INGESTAO), ("recuperacao", URL_RECUPERACAO),
                      ("geracao", URL_GERACAO)):
        esperar(lambda url=url: requests.get(f"{url}/saude", timeout=2).ok,
                f"servico de {nome}")
    print("1. os tres servicos estao de pe")

    enviados = indexar_documentos.main()
    esperar(lambda: requests.get(f"{URL_INGESTAO}/saude", timeout=2).json()["documentos"] >= enviados,
            "o worker indexar os documentos (via fila)")
    print(f"2. {enviados} trecho(s) passaram pela fila e foram indexados")

    r = requests.post(f"{URL_GERACAO}/perguntar",
                      json={"pergunta": "o que diz o teorema CAP?"}, timeout=60)
    if r.status_code != 200:
        sys.exit(f"FALHOU: /perguntar devolveu {r.status_code}: {r.text}")
    corpo = r.json()
    assert corpo["resposta"], "resposta vazia"
    assert "teorema_cap" in corpo["fontes"], f"fonte esperada ausente: {corpo['fontes']}"
    assert any("CAP" in trecho for trecho in corpo["contexto"]), "contexto sem o trecho do CAP"
    print("3. pergunta -> resposta fundamentada nos documentos")
    print(f"   fontes: {corpo['fontes']}")
    print(f"   resposta: {corpo['resposta'][:200]}")
    print("\nOK: fluxo de ponta a ponta funcionando")


if __name__ == "__main__":
    main()
