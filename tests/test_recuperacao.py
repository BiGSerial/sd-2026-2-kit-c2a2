import unittest
from unittest import mock

import numpy as np
import requests
from fastapi import HTTPException
from sklearn.feature_extraction.text import TfidfVectorizer

import servicos.recuperacao.app as rec
from servicos.comum.embeddings import Vetorizador, similaridade
from servicos.ingestao import armazenamento
from tests.util import usar_indice_temporario

DOCS = [
    {"texto": "gato preto dorme no sofa", "origem": "a"},
    {"texto": "cachorro marrom corre no parque", "origem": "b"},
    {"texto": "gato e cachorro brincam no parque", "origem": "c"},
    {"texto": "a bolsa de valores fechou em alta", "origem": "d"},
]


class Resposta:
    def __init__(self, corpo=None, erro=None):
        self.corpo, self.erro = corpo, erro

    def raise_for_status(self):
        if self.erro:
            raise self.erro

    def json(self):
        if isinstance(self.corpo, Exception):
            raise self.corpo
        return self.corpo


class Cosseno(unittest.TestCase):

    def test_casos_basicos(self):
        self.assertAlmostEqual(similaridade([1, 2, 3], [1, 2, 3]), 1.0)
        self.assertAlmostEqual(similaridade([1, 0], [0, 1]), 0.0)
        self.assertAlmostEqual(similaridade([1, 2], [-1, -2]), -1.0)
        self.assertEqual(similaridade([0, 0], [1, 2]), 0.0)          # vetor nulo
        self.assertAlmostEqual(similaridade([1, 2, 3], [2, 4, 6]), 1.0)   # ignora a escala


class Busca(unittest.TestCase):

    def setUp(self):
        usar_indice_temporario(self)
        armazenamento.adicionar(DOCS)
        self.itens = armazenamento.carregar()

    def buscar(self, pergunta, top_k=3, itens=None):
        corpo = {"itens": [{"texto": i["texto"], "origem": i["origem"], "vetor": i["vetor"]}
                           for i in (self.itens if itens is None else itens)]}
        with mock.patch.object(rec.requests, "get", return_value=Resposta(corpo)):
            return rec.buscar(rec.Consulta(pergunta=pergunta, top_k=top_k))["resultados"]

    def esperado(self, pergunta):
        """Calculo independente, direto no scikit-learn, do ranking esperado."""
        tfidf = TfidfVectorizer(ngram_range=(1, 2), min_df=1)
        matriz = tfidf.fit_transform([d["texto"] for d in DOCS]).toarray()
        q = tfidf.transform([pergunta]).toarray()[0]
        scores = matriz @ q / (np.linalg.norm(matriz, axis=1) * np.linalg.norm(q))
        ordem = sorted(range(len(DOCS)), key=lambda i: -scores[i])
        return [(DOCS[i]["origem"], round(float(scores[i]), 4)) for i in ordem if scores[i] > 0]

    def test_vetores_do_indice_e_da_pergunta_usam_o_mesmo_vocabulario(self):
        v = Vetorizador()
        v.ajustar([i["texto"] for i in self.itens])
        for item in self.itens:
            self.assertEqual(v.vetorizar(item["texto"]), item["vetor"])
        self.assertEqual(len({len(i["vetor"]) for i in self.itens}), 1)

    def test_ranking_e_scores_batem_com_calculo_independente(self):
        for pergunta in ("gato", "cachorro no parque", "bolsa de valores", "gato preto sofa"):
            obtido = [(r["origem"], r["score"]) for r in self.buscar(pergunta, top_k=10)]
            self.assertEqual(obtido, self.esperado(pergunta), pergunta)

    def test_top_k_limita_e_mantem_a_ordem_decrescente(self):
        todos = self.buscar("gato cachorro parque no", top_k=10)
        self.assertGreater(len(todos), 2)
        self.assertEqual(self.buscar("gato cachorro parque no", top_k=2), todos[:2])
        scores = [r["score"] for r in todos]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_resultado_e_deterministico_em_empate(self):
        iguais = [{"texto": "gato", "origem": o, "vetor": self.itens[0]["vetor"]}
                  for o in "xyz"]
        self.itens = iguais + [self.itens[1]]
        self.assertEqual([r["origem"] for r in self.buscar("gato")], ["x", "y", "z"][:3])

    def test_sem_correspondencia_lexical_devolve_vazio(self):
        for pergunta in ("zzxqw", "   ", "!!!", "a"):
            self.assertEqual(self.buscar(pergunta), [], repr(pergunta))

    def test_documentos_sem_termo_em_comum_ficam_de_fora_mesmo_com_top_k_grande(self):
        origens = [r["origem"] for r in self.buscar("bolsa", top_k=10)]
        self.assertEqual(origens, ["d"])

    def test_encontra_documento_recem_indexado(self):
        armazenamento.adicionar([{"texto": "o dragao roxo voa alto", "origem": "novo"}])
        self.itens = armazenamento.carregar()
        self.assertEqual(self.buscar("dragao")[0]["origem"], "novo")


class IndiceProblematico(unittest.TestCase):

    def buscar(self, resposta=None, erro=None, pergunta="gato"):
        efeito = {"side_effect": erro} if erro else {"return_value": resposta}
        with mock.patch.object(rec.requests, "get", **efeito):
            return rec.buscar(rec.Consulta(pergunta=pergunta))

    def assertIndisponivel(self, **kwargs):
        with self.assertRaises(HTTPException) as ctx:
            self.buscar(**kwargs)
        self.assertEqual(ctx.exception.status_code, 503)

    def test_indice_vazio(self):
        self.assertEqual(self.buscar(Resposta({"total": 0, "itens": []})), {"resultados": []})

    def test_ingestao_fora_do_ar_ou_com_erro(self):
        self.assertIndisponivel(erro=requests.ConnectionError())
        self.assertIndisponivel(erro=requests.Timeout())
        self.assertIndisponivel(resposta=Resposta(erro=requests.HTTPError("500")))

    def test_resposta_que_nao_e_json_ou_sem_itens(self):
        self.assertIndisponivel(resposta=Resposta(ValueError("html")))
        self.assertIndisponivel(resposta=Resposta({"outra": 1}))

    def test_itens_com_formato_invalido(self):
        for itens in ("lixo", ["lixo"], [{"texto": "gato"}],
                      [{"texto": "gato", "origem": "a", "vetor": "x"}],
                      [{"texto": 1, "origem": "a", "vetor": [1.0]}]):
            self.assertIndisponivel(resposta=Resposta({"itens": itens}))

    def test_vetor_com_valores_invalidos(self):
        item = {"texto": "gato preto", "origem": "a", "vetor": ["a", "b", "c"]}
        self.assertIndisponivel(resposta=Resposta({"itens": [item]}))

    def test_vetor_de_dimensao_diferente_e_recalculado(self):
        item = {"texto": "gato preto", "origem": "a", "vetor": [1.0]}
        r = self.buscar(Resposta({"itens": [item]}))["resultados"]
        self.assertEqual([x["origem"] for x in r], ["a"])
        self.assertGreater(r[0]["score"], 0)

    def test_indice_so_com_textos_sem_palavras(self):
        item = {"texto": "!!!", "origem": "lixo", "vetor": [0.0]}
        self.assertEqual(self.buscar(Resposta({"itens": [item]})), {"resultados": []})


if __name__ == "__main__":
    unittest.main()
