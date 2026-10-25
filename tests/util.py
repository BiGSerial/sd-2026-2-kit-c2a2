"""Apoio dos testes: Redis em memoria com injecao de falhas e indice em pasta temporaria."""
import tempfile
from pathlib import Path
from unittest import mock

import redis

from servicos.ingestao import armazenamento


class FakeRedis:
    """
    Redis em memoria, so com os comandos que o projeto usa (listas, LMOVE/BLMOVE,
    LREM, pipeline). `falhar("lrem", 2)` faz as proximas 2 chamadas de LREM
    levantarem ConnectionError, como se o Redis tivesse caido.
    """

    def __init__(self):
        self.listas = {}
        self._falhas = {}

    def falhar(self, comando, vezes=1):
        self._falhas[comando] = vezes

    def _entrada(self, comando):
        if self._falhas.get(comando, 0) > 0:
            self._falhas[comando] -= 1
            raise redis.ConnectionError(f"falha injetada em {comando}")

    def _lista(self, nome):
        return self.listas.setdefault(nome, [])

    # operacoes puras (sem injecao): usadas pelos comandos e pelo pipeline
    def _rpush(self, nome, *valores):
        lista = self._lista(nome)
        lista.extend(valores)
        return len(lista)

    def _lrem(self, nome, contagem, valor):
        lista, removidas = self._lista(nome), 0
        for i in range(len(lista)):
            if lista[i] == valor:
                del lista[i]
                removidas += 1
                break
        return removidas

    def _mover(self, origem, destino, src, dest):
        lista = self._lista(origem)
        if not lista:
            return None
        valor = lista.pop(0 if src == "LEFT" else -1)
        alvo = self._lista(destino)
        alvo.insert(0, valor) if dest == "LEFT" else alvo.append(valor)
        return valor

    # comandos
    def rpush(self, nome, *valores):
        self._entrada("rpush")
        return self._rpush(nome, *valores)

    def lpush(self, nome, *valores):
        self._entrada("lpush")
        for valor in valores:
            self._lista(nome).insert(0, valor)
        return len(self._lista(nome))

    def llen(self, nome):
        self._entrada("llen")
        return len(self._lista(nome))

    def lrem(self, nome, contagem, valor):
        self._entrada("lrem")
        assert contagem == 1, "o projeto so remove uma ocorrencia por vez"
        return self._lrem(nome, contagem, valor)

    def lmove(self, origem, destino, src="LEFT", dest="RIGHT"):
        self._entrada("lmove")
        return self._mover(origem, destino, src, dest)

    def blmove(self, origem, destino, timeout, src="LEFT", dest="RIGHT"):
        self._entrada("blmove")
        return self._mover(origem, destino, src, dest)

    def pipeline(self):
        return _Pipeline(self)


class _Pipeline:
    """MULTI/EXEC: ou aplica todos os comandos, ou nenhum."""

    def __init__(self, r):
        self._r, self._comandos = r, []

    def rpush(self, nome, *valores):
        self._comandos.append(lambda: self._r._rpush(nome, *valores))

    def lrem(self, nome, contagem, valor):
        self._comandos.append(lambda: self._r._lrem(nome, contagem, valor))

    def llen(self, nome):
        self._comandos.append(lambda: len(self._r._lista(nome)))

    def execute(self):
        self._r._entrada("execute")
        return [comando() for comando in self._comandos]


def usar_indice_temporario(teste):
    """Faz o armazenamento gravar em uma pasta temporaria, removida ao fim do teste."""
    pasta = tempfile.TemporaryDirectory()
    teste.addCleanup(pasta.cleanup)
    base = Path(pasta.name)
    for alvo, valor in (("DATA_DIR", base), ("ARQUIVO", base / "indice.json")):
        patcher = mock.patch.object(armazenamento, alvo, valor)
        patcher.start()
        teste.addCleanup(patcher.stop)
    return base
