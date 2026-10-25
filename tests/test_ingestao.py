import json
import os
import unittest
from unittest import mock

import redis
from fastapi import HTTPException
from pydantic import ValidationError

import servicos.ingestao.app as app
from servicos.comum.embeddings import Vetorizador
from servicos.ingestao import armazenamento, worker
from tests.util import FakeRedis, usar_indice_temporario


def msg(texto, origem="doc"):
    return json.dumps({"texto": texto, "origem": origem}, ensure_ascii=False)


def origens():
    return sorted(i["origem"] for i in armazenamento.carregar())


class Armazenamento(unittest.TestCase):

    def setUp(self):
        self.base = usar_indice_temporario(self)

    def test_persiste_em_disco_e_sobrevive_a_nova_leitura(self):
        armazenamento.adicionar([{"texto": "o teorema cap", "origem": "a"}])
        self.assertTrue((self.base / "indice.json").exists())
        self.assertEqual(origens(), ["a"])

    def test_reenvio_do_mesmo_documento_nao_duplica(self):
        doc = {"texto": "o teorema cap", "origem": "a"}
        self.assertEqual(armazenamento.adicionar([doc, doc]), {"novos": 1, "total": 1})
        self.assertEqual(armazenamento.adicionar([doc]), {"novos": 0, "total": 1})

    def test_mesmo_texto_com_origem_diferente_e_outro_documento(self):
        armazenamento.adicionar([{"texto": "o teorema cap", "origem": "a"},
                                 {"texto": "o teorema cap", "origem": "b"}])
        self.assertEqual(origens(), ["a", "b"])

    def test_ids_nao_colidem_entre_campos_com_quebra_de_linha(self):
        self.assertNotEqual(armazenamento._identificador("z", "x\ny"),
                            armazenamento._identificador("y\nz", "x"))
        armazenamento.adicionar([{"texto": "zz", "origem": "x\ny"},
                                 {"texto": "yy\nzz", "origem": "x"}])
        self.assertEqual(len(armazenamento.carregar()), 2)

    def test_indice_gravado_com_id_antigo_continua_reconhecido(self):
        armazenamento.adicionar([{"texto": "o teorema cap", "origem": "a"}])
        itens = armazenamento.carregar()
        itens[0]["id"] = "id-do-formato-antigo"
        armazenamento._salvar(itens)
        self.assertEqual(armazenamento.adicionar(
            [{"texto": "o teorema cap", "origem": "a"}]), {"novos": 0, "total": 1})

    def test_todos_os_vetores_usam_o_vocabulario_de_todos_os_textos(self):
        armazenamento.adicionar([{"texto": "gato preto", "origem": "a"}])
        armazenamento.adicionar([{"texto": "cachorro marrom", "origem": "b"}])
        itens = armazenamento.carregar()
        v = Vetorizador()
        v.ajustar([i["texto"] for i in itens])
        self.assertEqual([i["vetor"] for i in itens], [v.vetorizar(i["texto"]) for i in itens])

    def test_falha_na_escrita_preserva_o_indice_anterior_e_nao_deixa_lixo(self):
        armazenamento.adicionar([{"texto": "primeiro documento", "origem": "a"}])
        antes = (self.base / "indice.json").read_bytes()
        with mock.patch.object(armazenamento.os, "replace", side_effect=OSError("disco cheio")):
            with self.assertRaises(OSError):
                armazenamento.adicionar([{"texto": "segundo documento", "origem": "b"}])
        self.assertEqual((self.base / "indice.json").read_bytes(), antes)
        self.assertEqual([p.name for p in self.base.iterdir()], ["indice.json"])
        self.assertEqual(origens(), ["a"])

    def test_falha_ao_serializar_preserva_o_indice_anterior(self):
        armazenamento.adicionar([{"texto": "primeiro documento", "origem": "a"}])
        antes = (self.base / "indice.json").read_bytes()
        with mock.patch.object(armazenamento.json, "dump", side_effect=OSError("erro")):
            with self.assertRaises(OSError):
                armazenamento.adicionar([{"texto": "segundo documento", "origem": "b"}])
        self.assertEqual((self.base / "indice.json").read_bytes(), antes)
        self.assertEqual([p.name for p in self.base.iterdir()], ["indice.json"])

    def test_texto_indexavel(self):
        for texto in ("ab", "o teorema", "ação", "x1", "_a"):
            self.assertTrue(armazenamento.texto_indexavel(texto), texto)
        for texto in ("", "   ", "!!!", "a", "a b c", "? . ,"):
            self.assertFalse(armazenamento.texto_indexavel(texto), texto)


class ApiDeIngestao(unittest.TestCase):

    def setUp(self):
        usar_indice_temporario(self)
        self.r = FakeRedis()
        for alvo, valor in (("_redis", self.r), ("FILA", "fila:teste")):
            patcher = mock.patch.object(app, alvo, valor)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_indexar_enfileira_e_nao_indexa_na_hora(self):
        r = app.indexar([app.Documento(texto="o teorema cap", origem="a"),
                         app.Documento(texto="consenso raft", origem="b")])
        self.assertEqual(r, {"enfileirados": 2, "na_fila": 2})
        self.assertEqual(len(self.r.listas["fila:teste"]), 2)
        self.assertEqual(armazenamento.carregar(), [])

    def test_redis_fora_do_ar_responde_503(self):
        self.r.falhar("execute")
        with self.assertRaises(HTTPException) as ctx:
            app.indexar([app.Documento(texto="o teorema cap")])
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(self.r.listas.get("fila:teste", []), [])   # nada ficou pela metade

    def test_lista_vazia_e_rejeitada(self):
        with self.assertRaises(HTTPException) as ctx:
            app.indexar([])
        self.assertEqual(ctx.exception.status_code, 422)

    def test_texto_sem_palavras_e_rejeitado_na_entrada(self):
        for texto in ("", "  ", "!!!", "a"):
            with self.assertRaises(ValidationError):
                app.Documento(texto=texto)

    def test_saude_informa_a_fila_e_sobrevive_a_queda_do_redis(self):
        self.r.rpush("fila:teste", msg("o teorema cap"))
        self.assertEqual(app.saude()["pendentes_na_fila"], 1)
        self.r.falhar("llen")
        s = app.saude()
        self.assertEqual((s["status"], s["fila"], s["pendentes_na_fila"]), ("ok", "indisponivel", None))


class WorkerBase:
    """Cenarios que valem para qualquer Redis (falso ou real)."""

    def criar_redis(self):
        raise NotImplementedError

    def setUp(self):
        usar_indice_temporario(self)
        self.r = self.criar_redis()
        sufixo = os.urandom(4).hex()
        for alvo, valor in (("FILA", f"teste:{sufixo}:fila"),
                            ("FILA_PROCESSANDO", f"teste:{sufixo}:fila:processando"),
                            ("FILA_ERROS", f"teste:{sufixo}:fila:erros"),
                            ("TAMANHO_LOTE", 50)):
            patcher = mock.patch.object(worker, alvo, valor)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(lambda: self.r.delete(worker.FILA, worker.FILA_PROCESSANDO,
                                              worker.FILA_ERROS)
                        if hasattr(self.r, "delete") else None)
        self.w = worker.Worker(self.r)

    def enviar(self, *mensagens):
        self.r.rpush(worker.FILA, *mensagens)

    def tamanhos(self):
        return (self.r.llen(worker.FILA), self.r.llen(worker.FILA_PROCESSANDO),
                self.r.llen(worker.FILA_ERROS))

    def processar_tudo(self):
        while self.w.passo(espera_bloqueio=1):
            pass

    def test_processa_a_fila_e_confirma_somente_apos_gravar(self):
        self.enviar(msg("o teorema cap", "a"), msg("consenso raft", "b"))
        self.assertEqual(self.w.passo(espera_bloqueio=1), 2)
        self.assertEqual(origens(), ["a", "b"])
        self.assertEqual(self.tamanhos(), (0, 0, 0))

    def test_fila_vazia_nao_processa_nada(self):
        self.assertEqual(self.w.passo(espera_bloqueio=1), 0)

    def test_lotes_respeitam_o_tamanho_maximo(self):
        with mock.patch.object(worker, "TAMANHO_LOTE", 2):
            self.enviar(*[msg(f"documento numero {i}", f"d{i}") for i in range(5)])
            self.assertEqual([self.w.passo(espera_bloqueio=1) for _ in range(4)], [2, 2, 1, 0])
        self.assertEqual(len(origens()), 5)
        self.assertEqual(self.tamanhos(), (0, 0, 0))

    def test_mensagens_identicas_no_mesmo_lote_viram_um_documento_e_todas_sao_confirmadas(self):
        igual = msg("o teorema cap", "a")
        self.enviar(igual, igual, igual, msg("outro documento", "b"))
        self.assertEqual(self.w.passo(espera_bloqueio=1), 4)
        self.assertEqual(origens(), ["a", "b"])
        self.assertEqual(self.tamanhos(), (0, 0, 0))

    def test_reenvio_em_outro_lote_e_idempotente(self):
        self.enviar(msg("o teorema cap", "a"))
        self.processar_tudo()
        self.enviar(msg("o teorema cap", "a"))
        self.processar_tudo()
        self.assertEqual(origens(), ["a"])

    def test_worker_reiniciado_devolve_pendentes_na_ordem_original(self):
        self.r.rpush(worker.FILA_PROCESSANDO, msg("primeiro texto", "1"), msg("segundo texto", "2"))
        self.enviar(msg("terceiro texto", "3"))
        novo = worker.Worker(self.r)             # processo novo: nao sabe de nada
        with mock.patch.object(worker, "TAMANHO_LOTE", 1):
            novo.passo(espera_bloqueio=1)
            self.assertEqual(origens(), ["1"])   # a ordem original foi preservada
            novo.passo(espera_bloqueio=1)
            novo.passo(espera_bloqueio=1)
        self.assertEqual(origens(), ["1", "2", "3"])
        self.assertEqual(self.tamanhos(), (0, 0, 0))

    def test_mensagens_invalidas_vao_para_a_fila_de_erros_sem_travar_a_fila(self):
        invalidas = ["isto nao e json", json.dumps({"origem": "sem texto"}), json.dumps([1, 2]),
                     json.dumps("so uma string"), json.dumps({"texto": 123}),
                     json.dumps({"texto": "ok", "origem": 5}), json.dumps({"texto": "   "})]
        self.enviar(*invalidas, msg("documento valido", "ok"))
        self.processar_tudo()
        self.assertEqual(origens(), ["ok"])
        self.assertEqual(self.tamanhos(), (0, 0, len(invalidas)))

    def test_texto_sem_palavras_sozinho_nao_causa_laco_infinito(self):
        self.enviar(msg("!!!", "lixo"))           # com o indice vazio isso quebrava o TF-IDF
        self.assertEqual(self.w.passo(espera_bloqueio=1), 1)
        self.assertEqual(self.tamanhos(), (0, 0, 1))
        self.assertEqual(armazenamento.carregar(), [])


class WorkerComRedisFalso(WorkerBase, unittest.TestCase):
    """Mesmos cenarios, mais as falhas que so o Redis falso consegue injetar."""

    def criar_redis(self):
        r = FakeRedis()
        r.delete = lambda *nomes: [r.listas.pop(n, None) for n in nomes]
        return r

    def test_queda_entre_gravar_e_confirmar_reprocessa_sem_duplicar(self):
        self.enviar(msg("o teorema cap", "a"))
        self.r.falhar("lrem")                    # o ACK se perde depois de o indice ser gravado
        with self.assertRaises(redis.RedisError):
            self.w.passo(espera_bloqueio=1)
        self.assertEqual(origens(), ["a"])
        self.assertEqual(self.tamanhos(), (0, 1, 0))      # ainda "em processamento"
        self.assertEqual(self.w.passo(espera_bloqueio=1), 1)   # devolvida e reprocessada
        self.assertEqual(origens(), ["a"])                # at-least-once, sem duplicar
        self.assertEqual(self.tamanhos(), (0, 0, 0))

    def test_queda_do_redis_no_meio_do_lote_recupera_as_pendentes_sem_reiniciar(self):
        self.w.passo(espera_bloqueio=1)          # recuperacao inicial ja feita; worker no regime normal
        self.enviar(*[msg(f"documento numero {i}", f"d{i}") for i in range(3)])
        self.r.falhar("lmove")                   # cai depois de mover a primeira mensagem
        with self.assertRaises(redis.RedisError):
            self.w.passo(espera_bloqueio=1)
        self.assertEqual(self.tamanhos(), (2, 1, 0))
        self.processar_tudo()                    # o Redis voltou: nada fica preso
        self.assertEqual(origens(), ["d0", "d1", "d2"])
        self.assertEqual(self.tamanhos(), (0, 0, 0))

    def test_recuperacao_que_tambem_falha_e_repetida_ate_conseguir(self):
        self.w.passo(espera_bloqueio=1)          # recuperacao inicial ja feita
        self.r.rpush(worker.FILA_PROCESSANDO, msg("mensagem presa", "presa"))
        self.r.falhar("blmove")                  # queda: o worker passa a precisar recuperar
        with self.assertRaises(redis.RedisError):
            self.w.passo(espera_bloqueio=1)
        self.r.falhar("lmove")                   # na volta seguinte o Redis ainda esta ruim
        with self.assertRaises(redis.RedisError):
            self.w.passo(espera_bloqueio=1)
        self.assertEqual(self.tamanhos(), (0, 1, 0))      # continua presa, mas nao esquecida
        self.processar_tudo()                    # agora voltou: devolve e processa
        self.assertEqual(origens(), ["presa"])
        self.assertEqual(self.tamanhos(), (0, 0, 0))

    def test_falha_ao_gravar_o_indice_devolve_as_mensagens_e_elas_sao_processadas_depois(self):
        self.enviar(msg("o teorema cap", "a"), msg("consenso raft", "b"))
        with mock.patch.object(armazenamento, "adicionar", side_effect=OSError("disco cheio")):
            with self.assertRaises(OSError):
                self.w.passo(espera_bloqueio=1)
        self.processar_tudo()
        self.assertEqual(origens(), ["a", "b"])
        self.assertEqual(self.tamanhos(), (0, 0, 0))

    def test_invalida_nao_e_duplicada_na_fila_de_erros_quando_o_lote_falha(self):
        self.enviar("lixo", msg("o teorema cap", "a"))
        with mock.patch.object(armazenamento, "adicionar", side_effect=[OSError("x"), None]):
            with self.assertRaises(OSError):
                self.w.passo(espera_bloqueio=1)
        # a invalida ja esta na fila de erros (uma vez); a valida segue em processamento
        # e e devolvida no inicio da proxima volta
        self.assertEqual(self.tamanhos(), (0, 1, 1))
        self.processar_tudo()
        self.assertEqual(origens(), ["a"])
        self.assertEqual(self.tamanhos(), (0, 0, 1))      # a invalida nao foi reenfileirada

    def test_descartar_e_atomico(self):
        self.r.rpush(worker.FILA_PROCESSANDO, "lixo")
        self.r.falhar("execute")
        with self.assertRaises(redis.RedisError):
            worker._descartar(self.r, "lixo")
        self.assertEqual(self.tamanhos(), (0, 1, 0))      # nada pela metade: segue em processamento


REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")


def _redis_real():
    try:
        r = redis.Redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=1)
        r.ping()
        return r
    except redis.RedisError:
        return None


@unittest.skipUnless(_redis_real(), "sem Redis em REDIS_URL: cenarios com Redis real ignorados")
class WorkerComRedisReal(WorkerBase, unittest.TestCase):
    """Os mesmos cenarios contra um Redis de verdade (BLMOVE/LMOVE/LREM reais)."""

    def criar_redis(self):
        return _redis_real()


if __name__ == "__main__":
    unittest.main()
