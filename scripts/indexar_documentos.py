"""
Envia os arquivos .txt da pasta documentos/ para o servico de ingestao.

Cada paragrafo (separado por linha em branco) vira um trecho; a origem e o nome
do arquivo, e e ela que aparece depois em "fontes" na resposta.

Uso:  python scripts/indexar_documentos.py [pasta]
"""
import os
import sys
from pathlib import Path

import requests

URL_INGESTAO = os.getenv("URL_INGESTAO", "http://localhost:8001")
PASTA_PADRAO = Path(__file__).resolve().parent.parent / "documentos"


def montar_documentos(pasta: Path) -> list:
    docs = []
    for arquivo in sorted(pasta.glob("*.txt")):
        for paragrafo in arquivo.read_text(encoding="utf-8").split("\n\n"):
            if paragrafo.strip():
                docs.append({"texto": paragrafo.strip(), "origem": arquivo.stem})
    return docs


def main(pasta: Path = PASTA_PADRAO) -> int:
    docs = montar_documentos(pasta)
    if not docs:
        print(f"nenhum .txt encontrado em {pasta}")
        return 0
    resposta = requests.post(f"{URL_INGESTAO}/indexar", json=docs, timeout=10)
    resposta.raise_for_status()
    print(f"{len(docs)} trecho(s) enviados para a fila: {resposta.json()}")
    return len(docs)


if __name__ == "__main__":
    try:
        main(Path(sys.argv[1]) if len(sys.argv) > 1 else PASTA_PADRAO)
    except requests.RequestException as erro:
        sys.exit(f"falha ao falar com a ingestao em {URL_INGESTAO}: {erro}")
