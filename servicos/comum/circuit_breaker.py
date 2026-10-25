"""
Circuit breaker de referencia - Aula 13.

Estados:
  fechado  -> deixa passar (normal)
  aberto   -> recusa na hora, sem tentar (servico esta quebrado)
  meio     -> deixa passar UMA chamada de teste para ver se voltou

Adaptacoes sobre o modelo do kit: o estado e protegido por um lock (o FastAPI
atende requisicoes em varias threads) e, no estado "meio", so UMA chamada de
teste passa de cada vez; as demais continuam recusadas ate o teste terminar.
"""
import threading
import time


class CircuitoAberto(Exception):
    """Levantada quando o disjuntor esta aberto e recusa a chamada."""


class CircuitBreaker:

    def __init__(self, limite_falhas=5, espera_segundos=30):
        self.limite_falhas = limite_falhas
        self.espera_segundos = espera_segundos
        self.falhas = 0
        self.estado = "fechado"
        self.aberto_em = 0.0
        self._testando = False
        self._trava = threading.Lock()

    def chamar(self, funcao, *args, **kwargs):
        with self._trava:
            if self.estado == "aberto":
                if time.time() - self.aberto_em >= self.espera_segundos:
                    self.estado = "meio"
                    self._testando = False
                else:
                    raise CircuitoAberto("disjuntor aberto: chamada recusada")
            if self.estado == "meio":
                if self._testando:
                    raise CircuitoAberto("disjuntor em teste: chamada recusada")
                self._testando = True
        try:
            resultado = funcao(*args, **kwargs)
        except Exception:
            self._registrar_falha()
            raise
        self._registrar_sucesso()
        return resultado

    def _registrar_falha(self):
        with self._trava:
            self.falhas += 1
            if self.falhas >= self.limite_falhas or self.estado == "meio":
                self.estado = "aberto"
                self.aberto_em = time.time()
            self._testando = False

    def _registrar_sucesso(self):
        with self._trava:
            self.falhas = 0
            self.estado = "fechado"
            self._testando = False
