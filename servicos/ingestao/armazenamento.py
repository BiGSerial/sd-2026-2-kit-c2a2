"""
Armazenamento persistente do indice vetorial (TAREFA 1).

O indice fica em um arquivo JSON dentro de DATA_DIR. A escrita e atomica
(grava em arquivo temporario e troca com os.replace), entao quem le o arquivo
nunca enxerga um indice pela metade - nem se o processo cair no meio da gravacao.

Regra de consistencia: o vocabulario TF-IDF e reajustado com TODOS os textos do
indice a cada atualizacao e todos os vetores sao recalculados. Assim os vetores
guardados sempre pertencem ao mesmo vocabulario, e o servico de recuperacao
consegue reproduzi-lo ajustando um Vetorizador com os mesmos textos.

Apenas UM worker deve escrever (ver worker.py): nao ha trava de arquivo.
"""
import hashlib
import json
import os
import tempfile
from pathlib import Path

from servicos.comum.embeddings import Vetorizador

DATA_DIR = Path(os.getenv("DATA_DIR", "dados"))
ARQUIVO = DATA_DIR / "indice.json"


def _identificador(texto: str, origem: str) -> str:
    """Identifica um documento pelo conteudo: reenviar o mesmo nao duplica."""
    return hashlib.sha1(f"{origem}\n{texto}".encode("utf-8")).hexdigest()


def carregar() -> list:
    """Le o indice do disco. Devolve lista vazia se ainda nao existir."""
    try:
        with open(ARQUIVO, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return []


def _salvar(itens: list) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fd, caminho_tmp = tempfile.mkstemp(dir=DATA_DIR, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(itens, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(caminho_tmp, ARQUIVO)
    except BaseException:
        if os.path.exists(caminho_tmp):
            os.unlink(caminho_tmp)
        raise


def adicionar(docs: list) -> dict:
    """
    Acrescenta documentos ({"texto", "origem"}) ao indice, reindexa tudo e grava.
    Documentos repetidos (mesmo texto e mesma origem) sao ignorados.
    """
    itens = carregar()
    existentes = {i["id"] for i in itens}
    novos = 0
    for d in docs:
        ident = _identificador(d["texto"], d["origem"])
        if ident in existentes:
            continue
        existentes.add(ident)
        itens.append({"id": ident, "texto": d["texto"], "origem": d["origem"]})
        novos += 1

    if novos:
        vetorizador = Vetorizador()
        vetorizador.ajustar([i["texto"] for i in itens])
        for item in itens:
            item["vetor"] = vetorizador.vetorizar(item["texto"])
        _salvar(itens)

    return {"novos": novos, "total": len(itens)}
