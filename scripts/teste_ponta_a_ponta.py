"""
Teste de ponta a ponta, repetivel: pode rodar quantas vezes quiser, com o indice
vazio ou ja cheio. Cada execucao usa um documento com marcador e origem UNICOS, entao
nao depende do que ja esta indexado.

Confere: indexacao assincrona pela fila, idempotencia (reenvio nao duplica) e uma
pergunta cuja resposta vem do documento certo. Os documentos de teste ficam no indice
(ele nao tem rota de remocao); sao pequenos e identificaveis pelo prefixo "teste-".

Uso:  python scripts/teste_ponta_a_ponta.py
"""
import apoio
from apoio import conferir


def main():
    print("1. servicos no ar")
    apoio.esperar_servicos()
    conferir(True, "ingestao, recuperacao e geracao respondendo em /saude")

    print("2. indexacao assincrona via fila")
    doc, marcador = apoio.novo_documento()
    conferir(apoio.contar_origem(doc["origem"]) == 0, "o documento desta execucao ainda nao existe")
    r = apoio.indexar([doc])
    conferir(r.status_code == 202 and r.json()["enfileirados"] == 1,
             "POST /indexar responde 202: foi para a fila, nao indexou na hora")
    apoio.esperar_indexado(doc["origem"])
    conferir(True, "o worker consumiu a fila e o documento apareceu no indice")

    print("3. idempotencia: reenviar o mesmo documento nao duplica")
    sentinela, _ = apoio.novo_documento(" (sentinela)")
    apoio.indexar([doc])
    apoio.indexar([doc, doc])
    apoio.indexar([sentinela])       # a fila e FIFO: quando a sentinela entra, os reenvios ja passaram
    apoio.esperar_indexado(sentinela["origem"])
    conferir(apoio.contar_origem(doc["origem"]) == 1, "continua exatamente 1 copia apos 3 reenvios")

    print("4. pergunta -> resposta fundamentada no documento")
    r, _ = apoio.perguntar(f"o que diz o protocolo {marcador}?")
    conferir(r.status_code == 200, f"/perguntar responde 200 (veio {r.status_code}: {r.text[:120]})")
    corpo = r.json()
    conferir(bool(corpo["resposta"]), "a resposta nao e vazia")
    conferir(corpo["fontes"][:1] == [doc["origem"]], f"a fonte principal e o documento certo: {corpo['fontes']}")
    conferir(marcador in corpo["contexto"][0], "o contexto enviado ao LLM contem o trecho indexado")

    print("5. pergunta sem relacao com nenhum documento")
    r, _ = apoio.perguntar("xyzzyplugh" + marcador[-6:])
    corpo = r.json()
    conferir(r.status_code == 200 and corpo["fontes"] == [] and corpo["contexto"] == [],
             "sem trechos relevantes: responde que nao encontrou, sem inventar fontes")

    print("\nOK: fluxo de ponta a ponta funcionando")


if __name__ == "__main__":
    try:
        main()
    except apoio.requests.RequestException as erro:
        apoio.falhou(f"nao consegui falar com um dos servicos: {erro}")
