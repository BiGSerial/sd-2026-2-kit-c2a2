"""
LLM FALSO, so para demonstrar a resiliencia: imita a API de chat da OpenAI
(POST /v1/chat/completions) e deixa voce escolher como ele se comporta.

  POST /modo/ok     -> responde normalmente
  POST /modo/erro   -> responde 500 (servico quebrado: erro transitorio)
  POST /modo/429    -> responde 429 (limite de requisicoes: transitorio)
  POST /modo/401    -> responde 401 (chave invalida: erro PERMANENTE)
  POST /modo/lento  -> demora 60s para responder (estoura o tempo-limite)
  GET  /modo        -> mostra o modo atual
  GET  /contagem    -> quantas chamadas de chat chegaram desde o ultimo /reset
  POST /reset       -> zera a contagem

A contagem permite PROVAR o comportamento: quantas retentativas foram feitas e que,
com o circuito aberto, o LLM nem chega a ser chamado.

Para o servico de geracao usa-lo:
  LLM_API_KEY=demo LLM_API_URL=http://localhost:9000/v1/chat/completions

Usa apenas a biblioteca padrao. Rodar: python scripts/llm_falso.py [porta]
"""
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ESTADO = {"modo": "ok", "chamadas": 0}
TRAVA = threading.Lock()
MODOS = ("ok", "erro", "429", "401", "lento")
CODIGOS = {"erro": 500, "429": 429, "401": 401}


class Handler(BaseHTTPRequestHandler):

    def _responder(self, codigo, corpo):
        dados = json.dumps(corpo).encode("utf-8")
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(dados)))
        self.end_headers()
        self.wfile.write(dados)

    def do_GET(self):
        with TRAVA:
            if self.path == "/modo":
                return self._responder(200, {"modo": ESTADO["modo"]})
            if self.path == "/contagem":
                return self._responder(200, {"chamadas": ESTADO["chamadas"]})
        self._responder(404, {"erro": "rota desconhecida"})

    def do_POST(self):
        tamanho = int(self.headers.get("Content-Length", 0))
        if tamanho:
            self.rfile.read(tamanho)

        if self.path.startswith("/modo/"):
            novo = self.path.rsplit("/", 1)[1]
            if novo not in MODOS:
                return self._responder(400, {"erro": f"modos validos: {MODOS}"})
            with TRAVA:
                ESTADO["modo"] = novo
            return self._responder(200, {"modo": novo})

        if self.path == "/reset":
            with TRAVA:
                ESTADO["chamadas"] = 0
            return self._responder(200, {"chamadas": 0})

        if self.path == "/v1/chat/completions":
            with TRAVA:
                ESTADO["chamadas"] += 1
                modo = ESTADO["modo"]
            if modo in CODIGOS:
                return self._responder(CODIGOS[modo], {"erro": f"llm falso em modo {modo}"})
            if modo == "lento":
                time.sleep(60)
            return self._responder(200, {"choices": [{"message": {
                "content": "[LLM FALSO] resposta gerada a partir do contexto recebido."}}]})

        self._responder(404, {"erro": "rota desconhecida"})

    def log_message(self, formato, *args):
        sys.stderr.write("llm-falso: " + formato % args + "\n")


if __name__ == "__main__":
    porta = int(sys.argv[1]) if len(sys.argv) > 1 else 9000
    print(f"LLM falso em http://0.0.0.0:{porta} (modo: {ESTADO['modo']})", flush=True)
    ThreadingHTTPServer(("0.0.0.0", porta), Handler).serve_forever()
