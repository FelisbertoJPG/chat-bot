"""Etapa 2 do RAG: embeddings dos trechos e busca por significado.

Indexar (uma vez, ou sempre que o regulamento mudar):
    python busca.py indexar

Buscar:
    python busca.py "quantas consultas de psiquiatria eu tenho?"

Como funciona:
- Cada pedaço de texto vira um vetor de 1024 números que representa o significado dele.
- A pergunta também vira um vetor.
- A semelhança entre dois vetores é o cosseno do ângulo entre eles: perto de 1 = mesmo
  assunto, perto de 0 = nada a ver. Os mais parecidos são os resultados.

Busca em pedaços pequenos, resposta com o artigo inteiro:
- Um artigo longo (como o Art. 51, com 22 exclusões) tem um vetor "genérico": a alínea
  "v) vacinas" some no meio. Por isso, além do artigo inteiro, cada parágrafo e cada
  alínea ganham um vetor próprio, sempre com o caput do artigo junto para dar contexto.
- A nota de um artigo é a melhor nota entre os seus pedaços. O resultado devolvido é
  sempre o artigo inteiro, que é o que vai para o modelo responder.
"""

import json
import math
import sys
from pathlib import Path

import ollama

MODELO_EMBEDDING = "qwen3-embedding:0.6b"
PASTA_DADOS = Path(__file__).parent / "dados"
ARQUIVO_TRECHOS = PASTA_DADOS / "trechos.json"
ARQUIVO_INDICE = PASTA_DADOS / "indice.json"

# O Qwen3-Embedding foi treinado para receber uma instrução junto com a pergunta
# (mas não junto com os documentos). Isso costuma melhorar a busca.
INSTRUCAO = (
    "Instruct: Dada a dúvida de um beneficiário de plano de saúde, encontre o artigo do "
    "regulamento que responde a ela\nQuery: "
)


def normalizar(vetor: list[float]) -> list[float]:
    """Deixa o vetor com tamanho 1. Assim o cosseno vira um simples produto escalar."""
    tamanho = math.sqrt(sum(x * x for x in vetor))
    return [x / tamanho for x in vetor]


def pedacos_para_indexar(trecho: dict) -> list[str]:
    """O artigo inteiro + um pedaço por parágrafo/alínea, cada um com o caput junto.

    A seção entra em todos porque dá contexto: "Art. 44" sozinho não diz que é odontologia.
    No trecho, cada linha é um dispositivo: a primeira é o caput, as outras são
    parágrafos (§, Parágrafo único) ou alíneas (a), b)...).
    """
    linhas = trecho["texto"].split("\n")
    caput, partes = linhas[0], linhas[1:]
    pedacos = [f"{trecho['secao']}\n{trecho['texto']}"]

    paragrafo_atual = ""
    for parte in partes:
        if parte.startswith(("§", "Parágrafo")):
            paragrafo_atual = parte
            pedacos.append(f"{trecho['secao']}\n{caput}\n{parte}")
        else:
            # Alínea: leva o caput e o parágrafo a que pertence (se houver).
            contexto = f"{caput}\n{paragrafo_atual}" if paragrafo_atual else caput
            pedacos.append(f"{trecho['secao']}\n{contexto}\n{parte}")
    return pedacos


def indexar() -> None:
    trechos = json.loads(ARQUIVO_TRECHOS.read_text(encoding="utf-8"))
    itens = [(t["id"], p) for t in trechos for p in pedacos_para_indexar(t)]
    print(f"Gerando embeddings de {len(itens)} pedaços ({len(trechos)} artigos) com {MODELO_EMBEDDING}...")

    resposta = ollama.embed(model=MODELO_EMBEDDING, input=[texto for _, texto in itens])
    indice = {
        "modelo": MODELO_EMBEDDING,
        "trechos": trechos,
        "itens": [
            {"artigo": artigo, "texto": texto, "vetor": normalizar(v)}
            for (artigo, texto), v in zip(itens, resposta["embeddings"])
        ],
    }
    ARQUIVO_INDICE.write_text(json.dumps(indice, ensure_ascii=False), encoding="utf-8")
    print(f"Índice salvo em {ARQUIVO_INDICE} ({len(resposta['embeddings'][0])} dimensões por vetor)")


_indice_em_memoria: dict | None = None


def carregar_indice() -> dict:
    global _indice_em_memoria
    if _indice_em_memoria is None:
        if not ARQUIVO_INDICE.exists():
            sys.exit("Índice não encontrado. Rode antes: python busca.py indexar")
        _indice_em_memoria = json.loads(ARQUIVO_INDICE.read_text(encoding="utf-8"))
    return _indice_em_memoria


def buscar(pergunta: str, k: int = 4, usar_instrucao: bool = True) -> list[dict]:
    """Devolve os k artigos mais parecidos com a pergunta, do mais para o menos parecido."""
    indice = carregar_indice()
    consulta = (INSTRUCAO + pergunta) if usar_instrucao else pergunta
    vetor = normalizar(ollama.embed(model=indice["modelo"], input=consulta)["embeddings"][0])

    # Nota de cada artigo = melhor nota entre os seus pedaços.
    melhor: dict[str, tuple[float, str]] = {}
    for item in indice["itens"]:
        nota = sum(a * b for a, b in zip(vetor, item["vetor"]))
        if item["artigo"] not in melhor or nota > melhor[item["artigo"]][0]:
            melhor[item["artigo"]] = (nota, item["texto"].split("\n")[-1])

    por_id = {t["id"]: t for t in indice["trechos"]}
    ranking = sorted(melhor.items(), key=lambda par: par[1][0], reverse=True)[:k]
    return [
        {"similaridade": round(nota, 3), "pedaco_encontrado": pedaco, **por_id[artigo]}
        for artigo, (nota, pedaco) in ranking
    ]


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    if sys.argv[1] == "indexar":
        indexar()
        return

    pergunta = " ".join(sys.argv[1:])
    for r in buscar(pergunta):
        paginas = f"p. {r['pagina_inicio']}" + (f"-{r['pagina_fim']}" if r["pagina_fim"] != r["pagina_inicio"] else "")
        print(f"\n[{r['similaridade']:.3f}] {r['id']} ({paginas}) | {r['secao']}")
        print(f"   pedaço que casou: {r['pedaco_encontrado'][:200]}")


if __name__ == "__main__":
    main()
