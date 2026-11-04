"""Etapa 3: buscar os artigos, consultar a tabela se preciso e gerar a resposta.

Fluxo de uma pergunta:
    1. busca: os 4 artigos mais parecidos com a pergunta
    2. decisão (tool calling): o modelo vê só a pergunta e a ferramenta consultar_tabela,
       e decide se consulta a tabela e com qual termo
    3. resposta: o modelo recebe pergunta + artigos + resultado da tabela e responde

Tool calling: o modelo não executa nada. Ele responde "quero chamar consultar_tabela
com termo X"; o código executa a função, devolve o resultado como uma mensagem
"tool", e o modelo continua a partir daí.

Por que decisão e resposta separadas: com 4 artigos no contexto, o modelo de 4B às
vezes escrevia "vou consultar a tabela" em vez de chamar a ferramenta. Vendo só a
pergunta, ele chama de forma confiável. Cada etapa fica com uma tarefa só.

A resposta sai em pedaços (streaming), para a tela mostrar o texto enquanto ele é gerado.

Uso no terminal:
    python rag.py "O plano cobre óculos?"
    python rag.py "Quanto o IPASEM paga numa polissonografia?"
    python rag.py --pensar "Tomei posse há 6 meses, posso marcar consulta eletiva?"
"""

import json
import sys
import time
from collections.abc import Iterator

import ollama

from busca import buscar
from tabela import consultar_tabela

MODELO_RESPOSTA = "qwen3.5:4b"
ARTIGOS_NO_PROMPT = 4
MAX_CONSULTAS = 3  # limite de chamadas de ferramenta por pergunta
FERRAMENTAS = {"consultar_tabela": consultar_tabela}

SYSTEM_DECISAO = """Você decide se é preciso consultar a tabela de procedimentos do IPASEM \
para responder a uma dúvida de beneficiário. A tabela tem procedimentos, exames, cirurgias, \
tratamentos, materiais e pacotes, com código, valor e notas (por exemplo, coparticipação).

- Chame consultar_tabela se a pergunta citar um procedimento, exame, cirurgia ou tratamento \
específico, ou perguntar valor, preço, custo, código ou quanto se paga.
- Use o nome técnico: "facectomia" para catarata, "endodontia" para tratamento de canal \
(dente), "polissonografia" para exame do sono.
- Se o resultado vier vazio, tente outro termo.
- Se a pergunta for só sobre regras (carência, limite de consultas, dependentes, reembolso, \
internação), não chame nada e responda apenas: SEM_CONSULTA"""

SYSTEM = """Você tira dúvidas de beneficiários do plano de saúde do IPASEM (Novo Hamburgo).

Você tem duas fontes, que vêm junto com a pergunta:
1. Artigos do regulamento: trazem regras, mas não valores nem códigos de procedimentos.
2. Resultado da consulta à tabela única de 2024 (quando houver): código, valor e notas.

Regras:
- Use apenas o que está nos artigos e no resultado da tabela. Não use conhecimento geral \
sobre planos de saúde.
- Só ligue um artigo a um procedimento se o artigo falar dele de forma explícita.
- Se nada disso responde à pergunta, diga que não encontrou essa informação.
- Cite a fonte de cada afirmação no formato (Art. X, p. Y). Para outras resoluções, \
use (Res. N/AAAA, Art. X).
- Textos entre colchetes começando com "Nota de vigência" indicam que parte da regra foi \
atualizada depois. Siga a nota e avise a pessoa.
- Para perguntas gerais sobre o regulamento (o que é, de que trata, onde fala de algo), \
use a "Ficha do regulamento": ela traz o número e a data da resolução, a quem se aplica e \
os assuntos com seus artigos. Cite-a como (Ficha do regulamento).
- Se a resposta depende de algo que a pessoa não informou, diga do que depende.
- Responda em português simples, em no máximo 5 frases.

Tabela de procedimentos:
- Quando houver coparticipação, calcule quanto a pessoa paga (ex.: 50% de R$ 2.499,00 = \
R$ 1.249,50).
- Valores em CH: informe o CH e o valor estimado em reais que vem no resultado.
- Deixe claro que o valor é o que o IPASEM paga ao prestador, e não o que a pessoa paga, a \
menos que uma nota fale em coparticipação. Avise que a tabela é atualizada todo ano no site.
- Estar na tabela não garante cobertura: as exclusões do Art. 51 e as regras do regulamento \
continuam valendo.
- Cite a tabela como (Tabela Res. 33/2024, código X, p. Y)."""


def formatar_paginas(trecho: dict) -> str:
    inicio, fim = trecho["pagina_inicio"], trecho["pagina_fim"]
    return f"p. {inicio}" if inicio == fim else f"p. {inicio}-{fim}"


def montar_prompt(pergunta: str, artigos: list[dict], consultas: list[dict]) -> str:
    blocos = [
        f"[{a['id']} | {formatar_paginas(a)} | {a['secao']}]\n{a['texto']}" for a in artigos
    ]
    partes = [f"Pergunta: {pergunta}", "Artigos do regulamento que podem ajudar:", "\n\n".join(blocos)]
    for c in consultas:
        partes.append(resultado_em_texto(c["termo"], c["resultado"]))
    # A pergunta vem no começo e no fim: o modelo pequeno presta mais atenção nas pontas.
    partes.append(f"Pergunta (repetida): {pergunta}")
    return "\n\n".join(partes)


def reais(valor: float) -> str:
    return f"R$ {valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def resultado_em_texto(termo: str, resultado: dict) -> str:
    """Resultado da tabela em texto corrido para o modelo de resposta.
    O modelo de 4B lia mal o JSON (dizia que um item não estava lá quando estava)."""
    linhas = [f"Resultado da consulta à tabela única de 2024 (termo pesquisado: {termo}):"]
    if not resultado.get("resultados") and not resultado.get("pacotes"):
        linhas.append("Nenhum item encontrado na tabela.")
    for r in resultado.get("resultados", []):
        if "reais" in r:
            valor = reais(r["reais"])
            if "sessoes_por_ano" in r:
                valor += f", limite de {r['sessoes_por_ano']} sessões por ano"
        else:
            valor = f"{r['ch']:g} CH, equivalente a {reais(r['reais_estimado'])}"
        linhas.append(f"- Código {r['codigo']}: {r['descricao']}. Valor: {valor}. "
                      f"Grupo: {r['grupo']}. Página {r['pagina']}.")
    for grupo, notas in resultado.get("notas_por_grupo", {}).items():
        linhas.append(f"Notas da tabela para o grupo {grupo}:")
        linhas.extend(f"  * {n}" for n in notas)
    for p in resultado.get("pacotes", []):
        linhas.append(f"Pacote ({p['secao']}, página {p['pagina']}):\n{p['trecho']}")
    linhas.append(f"Aviso: {resultado['aviso']}")
    return "\n".join(linhas)


def resumo_resultado(resultado: dict) -> list[str]:
    """Versão curta do resultado da ferramenta, para mostrar na tela."""
    linhas = []
    for r in resultado["resultados"]:
        if "reais" in r:
            valor = f"R$ {r['reais']:,.2f}"
        else:
            valor = f"{r['ch']:g} CH (≈ R$ {r.get('reais_estimado', 0):,.2f})"
        linhas.append(f"{r['codigo']} · {r['descricao']} · {valor} · p. {r['pagina']}")
    for p in resultado.get("pacotes", []):
        linhas.append(f"Pacote (p. {p['pagina']}): {p['trecho'].splitlines()[0]}")
    return linhas


def responder(pergunta: str, pensar: bool = False) -> Iterator[dict]:
    """Gera eventos para a interface:
    {"tipo": "fontes", ...}      -> artigos encontrados pela busca
    {"tipo": "pensando"}         -> modelo está raciocinando
    {"tipo": "ferramenta", ...}  -> o modelo consultou a tabela
    {"tipo": "texto", ...}       -> pedaço da resposta
    {"tipo": "fim", ...}         -> tempos e velocidade
    """
    inicio = time.time()
    artigos = buscar(pergunta, k=ARTIGOS_NO_PROMPT)
    tempo_busca = time.time() - inicio
    yield {
        "tipo": "fontes",
        "fontes": [
            {"id": a["id"], "paginas": formatar_paginas(a), "secao": a["secao"],
             "similaridade": a["similaridade"], "pedaco": a["pedaco_encontrado"], "texto": a["texto"]}
            for a in artigos
        ],
    }

    # Etapa 2: decisão. Só a pergunta e a ferramenta, sem os artigos.
    consultas: list[dict] = []
    mensagens = [{"role": "system", "content": SYSTEM_DECISAO}, {"role": "user", "content": pergunta}]
    while len(consultas) < MAX_CONSULTAS:
        decisao = ollama.chat(
            model=MODELO_RESPOSTA, messages=mensagens, tools=list(FERRAMENTAS.values()),
            think=False,
            # Se não for chamar ferramenta, não precisa escrever nada longo.
            options={"num_ctx": 8192, "num_predict": 128},
        )
        chamadas = decisao.message.tool_calls or []
        if not chamadas:
            break
        mensagens.append({"role": "assistant", "content": decisao.message.content or "",
                          "tool_calls": chamadas})
        for chamada in chamadas[: MAX_CONSULTAS - len(consultas)]:
            nome, argumentos = chamada.function.name, dict(chamada.function.arguments or {})
            funcao = FERRAMENTAS.get(nome)
            resultado = funcao(**argumentos) if funcao else {"erro": f"ferramenta {nome} não existe"}
            consultas.append({"termo": argumentos.get("termo", ""), "resultado": resultado})
            yield {"tipo": "ferramenta", "nome": nome, "argumentos": argumentos,
                   "total": resultado.get("total_encontrado", 0),
                   "resumo": resumo_resultado(resultado) if funcao else []}
            mensagens.append({"role": "tool", "tool_name": nome,
                              "content": json.dumps(resultado, ensure_ascii=False)})
        # Achou algo: não precisa de outra rodada. Vazio: o modelo pode tentar outro termo.
        if any(c["resultado"].get("total_encontrado") for c in consultas):
            break

    # Etapa 3: resposta, com artigos e resultado da tabela juntos.
    avisou_pensando = False
    tokens_gerados = segundos_gerando = 0.0
    for parte in ollama.chat(
        model=MODELO_RESPOSTA,
        messages=[
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": montar_prompt(pergunta, artigos, consultas)},
        ],
        stream=True,
        think=pensar,
        # 4 artigos + resultado da tabela + raciocínio precisam de uma janela maior.
        options={"num_ctx": 16384},
    ):
        if parte.message.thinking and not avisou_pensando:
            avisou_pensando = True
            yield {"tipo": "pensando"}
        if parte.message.content:
            yield {"tipo": "texto", "texto": parte.message.content}
        if parte.done and parte.eval_duration:
            tokens_gerados, segundos_gerando = parte.eval_count, parte.eval_duration / 1e9

    yield {
        "tipo": "fim",
        "tempo_total": round(time.time() - inicio, 1),
        "tempo_busca": round(tempo_busca, 1),
        "tokens_por_segundo": round(tokens_gerados / segundos_gerando, 1) if segundos_gerando else 0,
        "consultas_tabela": len(consultas),
    }


if __name__ == "__main__":
    pensar = "--pensar" in sys.argv
    pergunta = " ".join(a for a in sys.argv[1:] if a != "--pensar")
    if not pergunta:
        print(__doc__)
        sys.exit(1)
    for evento in responder(pergunta, pensar=pensar):
        if evento["tipo"] == "fontes":
            print("Fontes: " + ", ".join(f"{f['id']} ({f['paginas']})" for f in evento["fontes"]) + "\n")
        elif evento["tipo"] == "pensando":
            print("(pensando...)")
        elif evento["tipo"] == "ferramenta":
            print(f"[ferramenta] {evento['nome']}({evento['argumentos']}) -> {evento['total']} resultado(s)")
            for linha in evento["resumo"]:
                print(f"    {linha}")
            print()
        elif evento["tipo"] == "texto":
            print(evento["texto"], end="", flush=True)
        elif evento["tipo"] == "fim":
            print(f"\n\n[{evento['tempo_total']}s no total | {evento['tokens_por_segundo']} tokens/s | "
                  f"{evento['consultas_tabela']} consulta(s) à tabela]")
