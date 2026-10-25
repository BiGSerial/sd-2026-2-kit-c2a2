"""Funcoes comuns aos scripts de teste de ponta a ponta e de resiliencia."""
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import requests

RAIZ = Path(__file__).resolve().parent.parent
URL_INGESTAO = os.getenv("URL_INGESTAO", "http://localhost:8001")
URL_RECUPERACAO = os.getenv("URL_RECUPERACAO", "http://localhost:8002")
URL_GERACAO = os.getenv("URL_GERACAO", "http://localhost:8003")
URL_LLM_FALSO = os.getenv("URL_LLM_FALSO", "http://localhost:9000")


def falhou(mensagem):
    sys.exit(f"FALHOU: {mensagem}")


def conferir(condicao, mensagem):
    if not condicao:
        falhou(mensagem)
    print(f"   ok - {mensagem}")


def esperar(condicao, descricao, limite=60.0, intervalo=0.3):
    """Repete `condicao` ate ela devolver algo verdadeiro (ou estourar o limite)."""
    fim = time.time() + limite
    while True:
        try:
            valor = condicao()
            if valor:
                return valor
        except requests.RequestException:
            pass
        if time.time() >= fim:
            falhou(f"tempo esgotado ({limite:g}s) esperando {descricao}")
        time.sleep(intervalo)


def esperar_servicos(limite=90):
    for nome, url in (("ingestao", URL_INGESTAO), ("recuperacao", URL_RECUPERACAO),
                      ("geracao", URL_GERACAO)):
        esperar(lambda url=url: requests.get(f"{url}/saude", timeout=3).ok,
                f"o servico de {nome}", limite)


def novo_documento(complemento=""):
    """Documento com marcador e origem unicos desta execucao: nao depende do indice."""
    uid = uuid.uuid4().hex[:10]
    marcador = f"marcador{uid}"
    return {"texto": f"Teste {uid}: o protocolo {marcador} usa quorum de escrita{complemento}.",
            "origem": f"teste-{uid}"}, marcador


def indexar(docs):
    r = requests.post(f"{URL_INGESTAO}/indexar", json=docs, timeout=10)
    return r


def itens_do_indice():
    return requests.get(f"{URL_INGESTAO}/indice", timeout=30).json()["itens"]


def contar_origem(origem):
    return sum(1 for i in itens_do_indice() if i["origem"] == origem)


def esperar_indexado(origem, limite=60):
    esperar(lambda: contar_origem(origem) >= 1, f"o worker indexar '{origem}'", limite)


def saude(url):
    return requests.get(f"{url}/saude", timeout=5).json()


def perguntar(pergunta, limite=120):
    inicio = time.time()
    r = requests.post(f"{URL_GERACAO}/perguntar", json={"pergunta": pergunta}, timeout=limite)
    return r, time.time() - inicio


def docker_compose(*args):
    """Controla os containers (so usado com --docker). Falha com mensagem clara."""
    try:
        subprocess.run(["docker", "compose", *args], cwd=RAIZ, check=True,
                       capture_output=True, text=True, timeout=180)
    except FileNotFoundError:
        falhou("o comando 'docker' nao foi encontrado; --docker exige o Docker Compose")
    except subprocess.CalledProcessError as erro:
        falhou(f"docker compose {' '.join(args)}: {erro.stderr.strip()}")
