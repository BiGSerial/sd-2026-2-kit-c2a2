"""
LLM FALSO, so para demonstrar a resiliencia: imita a API de chat da OpenAI
(POST /v1/chat/completions) e deixa voce escolher como ele se comporta.

  POST /modo/ok     -> responde normalmente
  POST /modo/erro   -> responde 500 (servico quebrado)
  POST /modo/lento  -> demora 60s para responder (estoura o tempo-limite)
  GET  /modo        -> mostra o modo atual

Para o servico de geracao usa-lo:
  LLM_API_KEY=demo LLM_API_URL=http://localhost:9000/v1/chat/completions

Usa apenas a biblioteca padrao. Rodar: python scripts/llm_falso.py [porta]
"""
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODO = {"valor": "ok"}
MODOS = ("ok", "erro", "lento")


class Handler(BaseHTTPRequestHandler):

    def _responder(self, codigo, corpo):
        dados = json.dumps(corpo).encode("utf-8")
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(dados)))
        self.end_headers()
        self.wfile.write(dados)

    def do_GET(self):
        if self.path == "/modo":
            self._responder(200, {"modo": MODO["valor"]})
        else:
            self._responder(404, {"erro": "rota desconhecida"})

    def do_POST(self):
        if self.path.startswith("/modo/"):
            novo = self.path.rsplit("/", 1)[1]
            if novo not in MODOS:
                return self._responder(400, {"erro": f"modos validos: {MODOS}"})
            MODO["valor"] = novo
            return self._responder(200, {"modo": novo})

        if self.path == "/v1/chat/completions":
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if MODO["valor"] == "erro":
                return self._responder(500, {"erro": "llm falso quebrado"})
            if MODO["valor"] == "lento":
                time.sleep(60)
            return self._responder(200, {"choices": [{"message": {
                "content": "[LLM FALSO] resposta gerada a partir do contexto recebido."}}]})

        self._responder(404, {"erro": "rota desconhecida"})

    def log_message(self, formato, *args):
        sys.stderr.write("llm-falso: " + formato % args + "\n")


if __name__ == "__main__":
    porta = int(sys.argv[1]) if len(sys.argv) > 1 else 9000
    print(f"LLM falso em http://0.0.0.0:{porta} (modo: {MODO['valor']})", flush=True)
    ThreadingHTTPServer(("0.0.0.0", porta), Handler).serve_forever()
