"""
Worker de INGESTAO: consome a fila Redis, gera embeddings e persiste o indice.

Entrega "pelo menos uma vez": cada mensagem e MOVIDA da fila principal para uma
lista "em processamento" (BLMOVE) e so e apagada depois que o indice foi gravado
em disco. Se o worker cair no meio, ao subir de novo ele devolve as mensagens
pendentes para a fila. Como o indice ignora documentos repetidos, reprocessar
uma mensagem nao duplica nada (a operacao e idempotente).

Recuperacao de pendentes: acontece ao iniciar E depois de QUALQUER erro do Redis.
Uma queda de conexao no meio de um lote pode deixar mensagens em "processamento"
com o worker vivo; sem esta segunda recuperacao elas ficariam presas ate um reinicio.

Mensagens invalidas (JSON invalido, sem texto, texto sem nenhuma palavra) nao
travam a fila: vao para uma lista de erros, de forma atomica (MULTI/EXEC), para
inspecao manual.

Rodar: python -m servicos.ingestao.worker
Execute UM unico worker (ele e o unico escritor do arquivo de indice; um segundo
worker "devolveria" as mensagens em andamento do primeiro ao iniciar).
Requer Redis 6.2+ (BLMOVE/LMOVE).
"""
import json
import logging
import os
import time

import redis

from servicos.ingestao import armazenamento

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
FILA = os.getenv("FILA_INGESTAO", "fila:ingestao")
FILA_PROCESSANDO = FILA + ":processando"
FILA_ERROS = FILA + ":erros"
TAMANHO_LOTE = int(os.getenv("WORKER_TAMANHO_LOTE", "50"))
ESPERA_BLOQUEIO = 5   # segundos esperando mensagem antes de dar uma volta no laco
ESPERA_ERRO = 2       # segundos de pausa depois de uma falha

log = logging.getLogger("worker-ingestao")


def conectar() -> redis.Redis:
    return redis.Redis.from_url(REDIS_URL, decode_responses=True,
                                socket_connect_timeout=2, socket_timeout=ESPERA_BLOQUEIO + 5)


def recuperar_pendentes(r: redis.Redis) -> int:
    """
    Devolve para a fila, na ordem original, tudo o que esta em processamento.
    Cada mensagem e movida atomicamente (LMOVE): nunca existe uma janela em que
    ela nao esteja em nenhuma das duas listas.
    """
    devolvidas = 0
    while r.lmove(FILA_PROCESSANDO, FILA, "RIGHT", "LEFT") is not None:
        devolvidas += 1
    return devolvidas


def _validar(bruto: str):
    """Converte a mensagem em documento. Devolve None se for invalida."""
    try:
        doc = json.loads(bruto)
        texto, origem = doc["texto"], doc.get("origem", "desconhecida")
        if (isinstance(texto, str) and isinstance(origem, str)
                and armazenamento.texto_indexavel(texto)):
            return {"texto": texto, "origem": origem}
    except (ValueError, KeyError, TypeError, AttributeError):
        pass
    return None


def _descartar(r: redis.Redis, bruto: str) -> None:
    """Move a mensagem de "processando" para a fila de erros, em uma unica transacao."""
    pipe = r.pipeline()  # MULTI/EXEC: ou faz as duas coisas, ou nenhuma
    pipe.rpush(FILA_ERROS, bruto)
    pipe.lrem(FILA_PROCESSANDO, 1, bruto)
    pipe.execute()


def processar_lote(r: redis.Redis, brutos: list) -> None:
    validos = []
    for bruto in brutos:
        doc = _validar(bruto)
        if doc is None:
            log.warning("mensagem invalida enviada para %s: %.80s", FILA_ERROS, bruto)
            _descartar(r, bruto)
        else:
            validos.append((bruto, doc))

    if validos:
        resultado = armazenamento.adicionar([doc for _, doc in validos])  # grava em disco
        log.info("lote de %d documento(s): %d novo(s), indice com %d",
                 len(validos), resultado["novos"], resultado["total"])

    # so apos gravar: a mensagem deixa de estar "em processamento". Se cair aqui, o
    # reinicio devolve o que sobrou e o indice ignora o repetido.
    for bruto, _ in validos:
        r.lrem(FILA_PROCESSANDO, 1, bruto)


class Worker:

    def __init__(self, r: redis.Redis):
        self.r = r
        self.recuperar = True   # ao iniciar e depois de qualquer erro do Redis

    def passo(self, espera_bloqueio: int = ESPERA_BLOQUEIO) -> int:
        """
        Uma volta do laco: consome e processa um lote. Devolve quantas mensagens
        processou (0 se a fila estava vazia). Erros sobem para quem chamou.
        """
        r = self.r
        try:
            if self.recuperar:
                devolvidas = recuperar_pendentes(r)
                if devolvidas:
                    log.info("%d mensagem(ns) pendente(s) devolvida(s) para a fila", devolvidas)
                self.recuperar = False

            primeira = r.blmove(FILA, FILA_PROCESSANDO, espera_bloqueio, "LEFT", "RIGHT")
            if primeira is None:
                return 0
            lote = [primeira]
            while len(lote) < TAMANHO_LOTE:
                proxima = r.lmove(FILA, FILA_PROCESSANDO, "LEFT", "RIGHT")
                if proxima is None:
                    break
                lote.append(proxima)

            try:
                processar_lote(r, lote)
            except redis.RedisError:
                raise
            except Exception:
                log.exception("falha ao processar o lote; devolvendo para a fila")
                self.recuperar = True   # a proxima volta devolve o que ficou em processamento
                raise
            return len(lote)
        except redis.RedisError:
            self.recuperar = True       # o que estava em processamento volta na proxima volta
            raise


def executar() -> None:
    worker = Worker(conectar())
    log.info("worker pronto, consumindo %s em %s", FILA, REDIS_URL)
    while True:
        try:
            worker.passo()
        except redis.RedisError as erro:
            log.warning("Redis indisponivel (%s); tentando de novo", erro)
            time.sleep(ESPERA_ERRO)
        except Exception:
            time.sleep(ESPERA_ERRO)   # ja registrado por passo()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    executar()
