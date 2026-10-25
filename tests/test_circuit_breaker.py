import threading
import time
import unittest

from servicos.comum.circuit_breaker import CircuitBreaker, CircuitoAberto


class Relogio:
    def __init__(self):
        self.agora = 1000.0

    def __call__(self):
        return self.agora


def falha():
    raise RuntimeError("falha")


def erro_permanente():
    raise PermissionError("401")


def so_runtime_conta(erro):
    return isinstance(erro, RuntimeError)


class CicloDeVida(unittest.TestCase):

    def setUp(self):
        self.relogio = Relogio()
        self.b = CircuitBreaker(limite_falhas=3, espera_segundos=10, relogio=self.relogio)

    def falhar(self, vezes, b=None):
        for _ in range(vezes):
            with self.assertRaises(RuntimeError):
                (b or self.b).chamar(falha)

    def test_abre_apos_o_limite_de_falhas_seguidas(self):
        self.falhar(2)
        self.assertEqual(self.b.estado, "fechado")
        self.falhar(1)
        self.assertEqual(self.b.estado, "aberto")

    def test_sucesso_zera_a_contagem(self):
        self.falhar(2)
        self.assertEqual(self.b.chamar(lambda: "ok"), "ok")
        self.falhar(2)
        self.assertEqual(self.b.estado, "fechado")

    def test_aberto_recusa_sem_chamar_a_funcao(self):
        self.falhar(3)
        chamadas = []
        with self.assertRaises(CircuitoAberto):
            self.b.chamar(lambda: chamadas.append(1))
        self.assertEqual(chamadas, [])

    def test_so_testa_depois_da_espera(self):
        self.falhar(3)
        self.relogio.agora += 9.9
        with self.assertRaises(CircuitoAberto):
            self.b.chamar(lambda: "ok")
        self.relogio.agora += 0.2
        self.assertEqual(self.b.chamar(lambda: "ok"), "ok")
        self.assertEqual(self.b.estado, "fechado")

    def test_sonda_com_falha_reabre_e_reinicia_a_espera(self):
        self.falhar(3)
        self.relogio.agora += 10
        self.falhar(1)                       # a sonda falha
        self.assertEqual(self.b.estado, "aberto")
        self.relogio.agora += 9
        with self.assertRaises(CircuitoAberto):
            self.b.chamar(lambda: "ok")      # a espera recomecou

    def test_usa_relogio_monotonico_por_padrao(self):
        self.assertIs(CircuitBreaker()._relogio, time.monotonic)


class ErrosPermanentes(unittest.TestCase):
    """HTTP 400/401/403/404 provam que o servico respondeu: nao sao indisponibilidade."""

    def setUp(self):
        self.relogio = Relogio()
        self.b = CircuitBreaker(limite_falhas=2, espera_segundos=10,
                                conta_como_falha=so_runtime_conta, relogio=self.relogio)

    def permanente(self):
        with self.assertRaises(PermissionError):
            self.b.chamar(erro_permanente)

    def test_erros_permanentes_nao_abrem_o_circuito(self):
        for _ in range(10):
            self.permanente()
        self.assertEqual((self.b.estado, self.b.falhas), ("fechado", 0))

    def test_erro_permanente_nao_zera_nem_soma_falhas_seguidas(self):
        with self.assertRaises(RuntimeError):
            self.b.chamar(falha)
        self.permanente()
        self.assertEqual(self.b.falhas, 1)
        with self.assertRaises(RuntimeError):
            self.b.chamar(falha)
        self.assertEqual(self.b.estado, "aberto")

    def test_erro_permanente_na_sonda_fecha_o_circuito(self):
        for _ in range(2):
            with self.assertRaises(RuntimeError):
                self.b.chamar(falha)
        self.relogio.agora += 10
        self.permanente()                    # sonda: o servico respondeu (com erro de pedido)
        self.assertEqual(self.b.estado, "fechado")
        self.assertEqual(self.b.chamar(lambda: "ok"), "ok")   # e nao prende a sonda


class Concorrencia(unittest.TestCase):

    def setUp(self):
        self.relogio = Relogio()

    def aberto(self, limite=2):
        b = CircuitBreaker(limite_falhas=limite, espera_segundos=10, relogio=self.relogio)
        for _ in range(limite):
            with self.assertRaises(RuntimeError):
                b.chamar(falha)
        self.assertEqual(b.estado, "aberto")
        return b

    def em_andamento(self, b, retorno="ok", erro=None):
        """Inicia, em outra thread, uma chamada que fica presa ate `liberar` ser acionado."""
        iniciou, liberar, resultado = threading.Event(), threading.Event(), {}

        def funcao():
            iniciou.set()
            liberar.wait(5)
            if erro:
                raise erro
            return retorno

        def alvo():
            try:
                resultado["valor"] = b.chamar(funcao)
            except Exception as e:
                resultado["erro"] = e

        thread = threading.Thread(target=alvo)
        thread.start()
        self.assertTrue(iniciou.wait(5))
        return thread, liberar, resultado

    def test_sucesso_tardio_nao_fecha_circuito_que_abriu_depois(self):
        b = CircuitBreaker(limite_falhas=2, espera_segundos=10, relogio=self.relogio)
        thread, liberar, _ = self.em_andamento(b)          # inicia com o circuito fechado
        for _ in range(2):
            with self.assertRaises(RuntimeError):
                b.chamar(falha)
        self.assertEqual(b.estado, "aberto")
        liberar.set()
        thread.join(5)
        self.assertEqual(b.estado, "aberto")               # o sucesso antigo e ignorado

    def test_falha_tardia_nao_conta_em_circuito_que_ja_fechou_de_novo(self):
        b = CircuitBreaker(limite_falhas=2, espera_segundos=10, relogio=self.relogio)
        thread, liberar, resultado = self.em_andamento(b, erro=RuntimeError("antiga"))
        for _ in range(2):
            with self.assertRaises(RuntimeError):
                b.chamar(falha)
        self.relogio.agora += 10
        self.assertEqual(b.chamar(lambda: "ok"), "ok")     # sonda fecha o circuito
        self.assertEqual(b.estado, "fechado")
        liberar.set()
        thread.join(5)
        self.assertIsInstance(resultado["erro"], RuntimeError)
        self.assertEqual((b.estado, b.falhas), ("fechado", 0))

    def test_meio_aberto_so_deixa_passar_uma_sonda(self):
        b = self.aberto()
        self.relogio.agora += 10
        execucoes, trava, liberar = [], threading.Lock(), threading.Event()
        recusadas, resultados = [], []

        def sonda():
            with trava:
                execucoes.append(1)
            liberar.wait(5)
            return "ok"

        def tentar():
            try:
                resultados.append(b.chamar(sonda))
            except CircuitoAberto:
                recusadas.append(1)

        threads = [threading.Thread(target=tentar) for _ in range(8)]
        for t in threads:
            t.start()
        deadline = time.time() + 5
        while len(recusadas) < 7 and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(len(recusadas), 7)                # as outras 7 foram recusadas...
        self.assertEqual(execucoes, [1])                   # ...enquanto a sonda ainda roda
        liberar.set()
        for t in threads:
            t.join(5)
        self.assertEqual(resultados, ["ok"])
        self.assertEqual(b.estado, "fechado")

    def test_sonda_interrompida_nao_prende_o_circuito(self):
        b = self.aberto()
        self.relogio.agora += 10
        with self.assertRaises(KeyboardInterrupt):
            b.chamar(lambda: (_ for _ in ()).throw(KeyboardInterrupt()))
        self.assertEqual(b.estado, "aberto")               # reabriu, nao ficou "em teste"
        self.relogio.agora += 10
        self.assertEqual(b.chamar(lambda: "ok"), "ok")
        self.assertEqual(b.estado, "fechado")

    def test_muitas_threads_falhando_abrem_o_circuito_uma_vez(self):
        b = CircuitBreaker(limite_falhas=5, espera_segundos=10, relogio=self.relogio)

        def tentar():
            try:
                b.chamar(falha)
            except (RuntimeError, CircuitoAberto):
                pass

        threads = [threading.Thread(target=tentar) for _ in range(40)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        self.assertEqual(b.estado, "aberto")
        self.assertEqual(b.falhas, 5)


if __name__ == "__main__":
    unittest.main()
