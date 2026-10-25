"""
Circuit breaker de referencia - Aula 13.

Estados:
  fechado  -> deixa passar (normal)
  aberto   -> recusa na hora, sem tentar (servico esta quebrado)
  meio     -> deixa passar UMA chamada de teste para ver se voltou

Adaptacoes sobre o modelo do kit:
  - o estado e protegido por um lock (o FastAPI atende em varias threads);
  - no estado "meio" so UMA chamada de teste (a "sonda") passa de cada vez;
  - so a sonda decide o destino do circuito meio-aberto. O resultado de uma chamada
    iniciada ANTES de o circuito mudar de estado e "obsoleto" e e ignorado: sem isso,
    um sucesso tardio fecharia um circuito que acabou de abrir;
  - conta_como_falha(erro) decide quais excecoes indicam indisponibilidade. Um erro
    permanente (ex.: HTTP 401 por chave invalida) prova que o servico RESPONDEU: nao
    abre o circuito, mas tambem nao zera a contagem de falhas seguidas. Se acontecer
    na sonda, o servico voltou a responder e o circuito fecha;
  - usa relogio monotonico (nao e afetado por ajuste do relogio do sistema).
"""
import threading
import time


class CircuitoAberto(Exception):
    """Levantada quando o disjuntor esta aberto e recusa a chamada."""


class CircuitBreaker:

    def __init__(self, limite_falhas=5, espera_segundos=30,
                 conta_como_falha=None, relogio=time.monotonic):
        self.limite_falhas = limite_falhas
        self.espera_segundos = espera_segundos
        self.falhas = 0
        self.estado = "fechado"
        self.aberto_em = 0.0
        self._conta_como_falha = conta_como_falha or (lambda erro: True)
        self._relogio = relogio
        self._testando = False   # ha uma sonda em andamento no estado "meio"
        self._geracao = 0        # muda a cada transicao de estado
        self._trava = threading.Lock()

    # -- transicoes (sempre chamadas com a trava) --------------------------------
    def _abrir(self):
        self.estado = "aberto"
        self.aberto_em = self._relogio()
        self._testando = False
        self._geracao += 1

    def _fechar(self):
        self.estado = "fechado"
        self.falhas = 0
        self._testando = False
        self._geracao += 1

    # -- chamada protegida --------------------------------------------------------
    def chamar(self, funcao, *args, **kwargs):
        with self._trava:
            if self.estado == "aberto":
                if self._relogio() - self.aberto_em >= self.espera_segundos:
                    self.estado = "meio"
                    self._testando = False
                    self._geracao += 1
                else:
                    raise CircuitoAberto("disjuntor aberto: chamada recusada")
            sonda = False
            if self.estado == "meio":
                if self._testando:
                    raise CircuitoAberto("disjuntor em teste: chamada recusada")
                self._testando = sonda = True
            geracao = self._geracao

        try:
            resultado = funcao(*args, **kwargs)
        except Exception as erro:
            self._finalizar(sonda, geracao,
                            "falha" if self._conta_como_falha(erro) else "neutro")
            raise
        except BaseException:
            # interrupcao do processo: nao diz nada sobre o servico, mas uma sonda
            # nao pode ficar presa para sempre
            self._finalizar(sonda, geracao, "falha" if sonda else "neutro")
            raise
        self._finalizar(sonda, geracao, "sucesso")
        return resultado

    def _finalizar(self, sonda, geracao, resultado):
        with self._trava:
            if sonda:
                if self.estado != "meio":
                    return
                if resultado == "falha":
                    self._abrir()
                else:                       # sucesso ou erro permanente: o servico respondeu
                    self._fechar()
                return

            if self.estado != "fechado" or geracao != self._geracao:
                return                      # resultado obsoleto: ignorado

            if resultado == "falha":
                self.falhas += 1
                if self.falhas >= self.limite_falhas:
                    self._abrir()
            elif resultado == "sucesso":
                self.falhas = 0
