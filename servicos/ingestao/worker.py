"""
Worker de INGESTAO: consome a fila Redis, gera embeddings e persiste o indice.

Entrega "pelo menos uma vez": cada mensagem e MOVIDA da fila principal para uma
lista "em processamento" (BLMOVE) e so e apagada depois que o indice foi gravado
em disco. Se o worker cair no meio, ao subir de novo ele devolve as mensagens
pendentes para a fila. Como o indice ignora documentos repetidos, reprocessar
uma mensagem nao duplica nada (a operacao e idempotente).

Mensagens malformadas (JSON invalido ou sem texto) nao travam a fila: vao para
uma lista de erros para inspecao.

Rodar: python -m servicos.ingestao.worker
Execute UM unico worker (ele e o unico escritor do arquivo de indice).
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
ESPERA_BLOQUEIO = 5  # segundos esperando mensagem antes de dar uma volta no laco

log = logging.getLogger("worker-ingestao")


def conectar() -> redis.Redis:
    return redis.Redis.from_url(REDIS_URL, decode_responses=True,
                                socket_connect_timeout=2, socket_timeout=ESPERA_BLOQUEIO + 5)


def recuperar_pendentes(r: redis.Redis) -> int:
    """Devolve para a fila as mensagens que ficaram em processamento (queda anterior)."""
    devolvidas = 0
    while r.lmove(FILA_PROCESSANDO, FILA, "RIGHT", "LEFT") is not None:
        devolvidas += 1
    return devolvidas


def _validar(bruto: str):
    """Converte a mensagem em documento. Devolve None se for invalida."""
    try:
        doc = json.loads(bruto)
        texto, origem = doc["texto"], doc.get("origem", "desconhecida")
        if isinstance(texto, str) and texto.strip() and isinstance(origem, str):
            return {"texto": texto, "origem": origem}
    except (ValueError, KeyError, TypeError, AttributeError):
        pass
    return None


def processar_lote(r: redis.Redis, brutos: list) -> None:
    validos = []
    for bruto in brutos:
        doc = _validar(bruto)
        if doc is None:
            log.warning("mensagem invalida enviada para %s: %.80s", FILA_ERROS, bruto)
            r.rpush(FILA_ERROS, bruto)
        else:
            validos.append(doc)

    if validos:
        resultado = armazenamento.adicionar(validos)  # grava em disco
        log.info("lote de %d documento(s): %d novo(s), indice com %d",
                 len(validos), resultado["novos"], resultado["total"])

    # so apos gravar: a mensagem deixa de estar "em processamento"
    for bruto in brutos:
        r.lrem(FILA_PROCESSANDO, 1, bruto)


def devolver_lote(r: redis.Redis, brutos: list) -> None:
    """Falha ao processar: recoloca as mensagens no inicio da fila, na ordem original."""
    for bruto in reversed(brutos):
        r.lrem(FILA_PROCESSANDO, 1, bruto)
        r.lpush(FILA, bruto)


def executar() -> None:
    r = conectar()
    while True:
        try:
            devolvidas = recuperar_pendentes(r)
            if devolvidas:
                log.info("%d mensagem(ns) pendente(s) devolvida(s) para a fila", devolvidas)
            break
        except redis.RedisError as erro:
            log.warning("aguardando o Redis: %s", erro)
            time.sleep(2)

    log.info("worker pronto, consumindo %s em %s", FILA, REDIS_URL)
    while True:
        lote = []
        try:
            primeira = r.blmove(FILA, FILA_PROCESSANDO, ESPERA_BLOQUEIO, "LEFT", "RIGHT")
            if primeira is None:
                continue
            lote.append(primeira)
            while len(lote) < TAMANHO_LOTE:
                proxima = r.lmove(FILA, FILA_PROCESSANDO, "LEFT", "RIGHT")
                if proxima is None:
                    break
                lote.append(proxima)
            processar_lote(r, lote)
        except redis.RedisError as erro:
            # Redis fora do ar: as mensagens que ja estavam em processamento serao
            # recuperadas na proxima volta, quando a conexao voltar.
            log.warning("Redis indisponivel (%s); tentando de novo", erro)
            time.sleep(2)
            try:
                recuperar_pendentes(r)
            except redis.RedisError:
                pass
        except Exception:
            log.exception("falha ao processar o lote; devolvendo para a fila")
            try:
                devolver_lote(r, lote)
            except redis.RedisError:
                pass
            time.sleep(2)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    executar()
