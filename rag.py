"""Etapa 3: buscar os artigos, consultar a tabela se preciso e gerar a resposta.

Fluxo de uma pergunta:
    c. conversa: se há perguntas anteriores, o modelo reescreve a nova para ela ficar completa
       sozinha ("e de clínico geral?" -> "Quantas consultas de clínico geral posso fazer?").
       Daí em diante tudo usa a pergunta reescrita; a conversa não vai para a resposta.
    0. memória (memoria.py): resposta validada para a mesma pergunta sai direto; pergunta
       já respondida pelo modelo volta do cache. Nos dois casos, nenhuma etapa abaixo roda.
    1. busca: os 4 artigos mais parecidos com a pergunta (+ respostas validadas ligadas a eles)
    2. decisão (tool calling): o modelo vê só a pergunta e as ferramentas consultar_tabela
       (valores) e consultar_rede (quem atende, onde; só se o arquivo da rede existir), e
       decide qual chamar e com que argumentos
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

import hashlib
import json
import re
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import ollama

import memoria
import rede
from busca import ARQUIVO_INDICE, buscar
from tabela import ARQUIVO as ARQUIVO_TABELA
from tabela import FONTE_CITACAO, VIGENCIA, consultar_tabela

MODELO_RESPOSTA = "qwen3.5:4b"
ARTIGOS_NO_PROMPT = 4
MAX_CONSULTAS = 3  # limite de chamadas de ferramenta por pergunta
FERRAMENTAS = {"consultar_tabela": consultar_tabela}
if rede.disponivel():  # o arquivo da rede fica fora do Git: sem ele, a ferramenta não existe
    FERRAMENTAS["consultar_rede"] = rede.consultar_rede

# Sinais de emergência: a tela mostra um alerta fixo (192 / pronto atendimento) antes da
# resposta. É regra no código, não no prompt, para não depender do modelo. A resposta continua
# (a pergunta pode ser sobre cobertura: "o plano cobre tratamento de AVC?").
_RE_EMERGENCIA = re.compile(
    r"dor no peito|aperto no peito|falta de ar|nao consigo respirar|dificuldade (de|para) respirar|"
    r"desmai|convuls|infarto|\bavc\b|derrame|boca torta|perda de forca|sangramento (forte|intenso)|"
    r"envenen|overdose|suicid|me matar|tirar minha vida")
ALERTA_EMERGENCIA = ("Se isso está acontecendo agora, é uma emergência: ligue 192 (SAMU) ou vá ao "
                     "pronto atendimento mais próximo. O regulamento garante urgência e emergência "
                     "mesmo durante a carência (Art. 63, §2º).")
ALERTA_VIDA = ("Se você está pensando em tirar a própria vida, ligue agora para o CVV, 188 "
               "(24 horas, gratuito), ou para o SAMU, 192. Você não está sozinho.")

# Janela de contexto igual nas duas etapas: se o num_ctx muda entre uma chamada e outra, o Ollama
# recarrega o modelo (medido: ~3,5 s a cada troca, 2 trocas por pergunta). 16384 cabe 4 artigos +
# resultado da tabela + raciocínio.
JANELA_CONTEXTO = 16384
# Quanto tempo o modelo fica na memória depois da última pergunta (o padrão do Ollama é 5 min).
MANTER_CARREGADO = "60m"
# Semente fixa na resposta: o texto continua sorteado (temperatura padrão, porque o Qwen avisa
# que sem sorteio nenhum modelos pequenos entram em repetição), mas a mesma pergunta com o mesmo
# prompt dá sempre a mesma resposta. Sem isso, duas rodadas iguais do eval variavam tanto quanto
# a mudança que se queria medir.
SEMENTE = 42

# Conversa: quantas trocas anteriores entram na reescrita e quanto de cada resposta.
# Só a reescrita vê a conversa: mandar tudo para a resposta aumentaria a leitura do prompt,
# que já é a parte mais lenta (~30 s).
MAX_TROCAS_NA_CONVERSA = 3
MAX_CARACTERES_RESPOSTA = 400

SYSTEM_REESCRITA = """Você reescreve a última pergunta de uma conversa sobre o plano de saúde \
do IPASEM para que ela possa ser entendida sozinha, sem a conversa.

- Se a última pergunta já é completa, devolva-a igual.
- Se ela depende da conversa ("e de clínico geral?", "quanto custa?", "e se for dependente?", \
"e isso tem coparticipação?"), complete com o assunto que falta, tirado das perguntas e \
respostas anteriores.
- Mantenha exatamente o que a pessoa quer saber (quantos, quanto custa, até quando, se \
precisa de algo): só acrescente o assunto. Não troque "quantos" por "quanto custa".
- Não responda à pergunta e não acrescente dados das respostas (números, prazos, artigos).
- Escreva só a pergunta reescrita, em uma linha.

Exemplos:
Conversa sobre sessões de fisioterapia. Última pergunta: "e quanto custa?" -> \
Quanto custa uma sessão de fisioterapia?
Conversa sobre consultas de cardiologia. Última pergunta: "e de dermatologia?" -> \
Quantas consultas de dermatologia posso fazer?"""

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
if "consultar_rede" in FERRAMENTAS:
    SYSTEM_DECISAO += """
- Chame consultar_rede (e não consultar_tabela) se a pessoa quer saber quem atende, que \
especialista procurar, onde fazer uma consulta ou exame, ou pede um prestador perto dela. \
Passe a especialidade; cidade e bairro só se a pessoa disser.
- Para um sintoma, escolha a especialidade mais provável (ex.: zumbido no ouvido -> \
otorrinolaringologia; tristeza e ansiedade -> psicologia)."""

# Pedido claro de prestador ("que especialista procuro?", "onde faço exame?"): a decisão recebe
# só a ferramenta da rede e um prompt focado. Com as duas ferramentas e o prompt geral, o modelo
# de 4B respondeu "dor nas costas, que especialista procuro?" sem consultar a rede.
_RE_PEDE_PRESTADOR = re.compile(
    r"que (especialista|medico|profissional)|qual (especialista|medico|profissional)|"
    r"quem (eu )?(devo )?(procur|atende|consult)|onde (eu )?(faco|fazer|posso fazer|consulto|encontro)|"
    r"perto de (mim|casa)|mais proxim|rede credenciada|credenciados?\b")
SYSTEM_DECISAO_REDE = """A pessoa quer saber quem atende ou que especialista procurar no plano \
de saúde do IPASEM. Chame consultar_rede com a especialidade mais provável para o caso (ex.: \
zumbido no ouvido -> otorrinolaringologia; tristeza e ansiedade -> psicologia). Para hospitais, \
use especialidade "hospital". Passe cidade e bairro só se a pessoa disser."""

SYSTEM = f"""Você tira dúvidas de beneficiários do plano de saúde do IPASEM (Novo Hamburgo).

Você tem duas fontes, que vêm junto com a pergunta:
1. Artigos do regulamento: trazem regras, mas não valores nem códigos de procedimentos.
2. Resultado da consulta à tabela única de procedimentos, com valores vigentes desde \
{VIGENCIA} (quando houver): código, valor e notas.

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
- "Respostas validadas" foram conferidas por uma pessoa e valem mais que a sua leitura dos \
artigos. Use uma resposta validada quando ela responde à mesma dúvida da pergunta; se for \
sobre outro assunto (outra especialidade, outro procedimento), não a aplique. Cite como \
(Resposta validada).
- Responda em português simples, em no máximo 5 frases.

Tabela de procedimentos:
- Quando houver coparticipação, calcule quanto a pessoa paga (ex.: 50% de R$ 2.499,00 = \
R$ 1.249,50).
- Valores em CH: informe o CH e o valor estimado em reais que vem no resultado.
- Deixe claro que o valor é o que o IPASEM paga ao prestador, e não o que a pessoa paga, a \
menos que uma nota fale em coparticipação. Informe que são os valores vigentes desde \
{VIGENCIA} e que a tabela é atualizada todo ano.
- Estar na tabela não garante cobertura: as exclusões do Art. 51 e as regras do regulamento \
continuam valendo.
- Cite a tabela como ({FONTE_CITACAO}, código X, p. Y)."""


def formatar_paginas(trecho: dict) -> str:
    inicio, fim = trecho["pagina_inicio"], trecho["pagina_fim"]
    return f"p. {inicio}" if inicio == fim else f"p. {inicio}-{fim}"


def montar_prompt(pergunta: str, artigos: list[dict], consultas: list[dict],
                  validadas: list[dict] = ()) -> str:
    blocos = [
        f"[{a['id']} | {formatar_paginas(a)} | {a['secao']}]\n{a['texto']}" for a in artigos
    ]
    partes = [f"Pergunta: {pergunta}", "Artigos do regulamento que podem ajudar:", "\n\n".join(blocos)]
    if validadas:
        partes.append("Respostas validadas ligadas a esses artigos:\n\n" + "\n\n".join(
            f"Pergunta validada: {v['pergunta']}\nResposta correta: {v['resposta']}" for v in validadas))
    for c in consultas:
        if c["nome"] == "consultar_rede":
            partes.append(rede.resultado_em_texto(c["argumentos"], c["resultado"]))
        else:
            partes.append(resultado_em_texto(c["termo"], c["resultado"]))
    # A pergunta vem no começo e no fim: o modelo pequeno presta mais atenção nas pontas.
    partes.append(f"Pergunta (repetida): {pergunta}")
    return "\n\n".join(partes)


def reais(valor: float) -> str:
    return f"R$ {valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def resultado_em_texto(termo: str, resultado: dict) -> str:
    """Resultado da tabela em texto corrido para o modelo de resposta.
    O modelo de 4B lia mal o JSON (dizia que um item não estava lá quando estava)."""
    linhas = [f"Resultado da consulta à tabela única ({FONTE_CITACAO}, valores vigentes desde "
              f"{VIGENCIA}; termo pesquisado: {termo}):"]
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


# Palavras que apontam para algo dito antes ("isso", "dele", "também"...).
_RE_REFERENCIA = re.compile(
    r"\b(isso|disso|nisso|esse|essa|esses|essas|desse|dessa|nesse|nessa|este|esta|deste|desta|"
    r"ele|ela|eles|elas|dele|dela|deles|delas|mesmo|mesma|tambem|ai|la|entao)\b")
MAX_PALAVRAS_CONTINUACAO = 7


def parece_continuacao(pergunta: str) -> bool:
    """Só pergunta com cara de continuação vai para a reescrita: começa com "e"/"mas", aponta
    para algo dito antes ("isso", "dele") ou é curta. Uma pergunta longa sem nada disso já é
    completa, e o modelo de 4B tendia a "contaminá-la" com o assunto da conversa: "Quais são as
    regras de carência...?" depois de falar de implante virava "carência para implante"; uma regra
    no prompt não resolveu (7/10), o filtro sim (9/10), e ainda poupa ~5 s nessas perguntas."""
    texto = memoria.normalizar(pergunta)
    return bool(re.match(r"(e|mas)\b", texto) or _RE_REFERENCIA.search(texto)
                or len(texto.split()) <= MAX_PALAVRAS_CONTINUACAO)


def entender_pergunta(pergunta: str, historico: list[dict]) -> str:
    """A pergunta como ela deve ser respondida: reescrita se for continuação da conversa."""
    if not historico or not parece_continuacao(pergunta):
        return pergunta
    return reescrever(pergunta, historico)


def reescrever(pergunta: str, historico: list[dict]) -> str:
    """Pergunta de continuação -> pergunta completa, usando as últimas trocas da conversa.
    historico: [{"pergunta": ..., "resposta": ...}], da mais antiga para a mais nova."""
    trocas = [
        f"Pergunta: {t['pergunta']}\nResposta: {t['resposta'][:MAX_CARACTERES_RESPOSTA]}"
        for t in historico[-MAX_TROCAS_NA_CONVERSA:] if t.get("pergunta")
    ]
    if not trocas:
        return pergunta
    r = ollama.chat(
        model=MODELO_RESPOSTA, think=False, keep_alive=MANTER_CARREGADO,
        messages=[{"role": "system", "content": SYSTEM_REESCRITA},
                  {"role": "user", "content": "Conversa até aqui:\n\n" + "\n\n".join(trocas)
                   + f"\n\nÚltima pergunta: {pergunta}"}],
        # Temperatura 0: reescrever é tarefa mecânica, a mesma entrada deve dar a mesma saída
        # (com o padrão do Ollama, duas rodadas do eval deram 5/8 e 4/8 com o mesmo prompt).
        options={"num_ctx": JANELA_CONTEXTO, "num_predict": 80, "temperature": 0},
    )
    nova = (r.message.content or "").strip().splitlines()[0:1]
    nova = nova[0].strip().strip('"“”').removeprefix("Pergunta reescrita:").strip() if nova else ""
    # Saída vazia ou que virou resposta/explicação: fica a pergunta original.
    if not nova or len(nova) > 3 * len(pergunta) + 150:
        return pergunta
    return nova


# Arquivos que mudam a resposta. Se qualquer um mudar, o cache antigo deixa de valer.
ARQUIVOS_CODIGO = [Path(__file__).with_name(n)
                   for n in ("rag.py", "busca.py", "tabela.py", "memoria.py", "rede.py")]


def impressao_digital() -> str:
    """Resumo de tudo o que gerou uma resposta: código (com os prompts e o modelo), dados e
    respostas validadas. Para os dados grandes basta tamanho + data de modificação."""
    h = hashlib.sha256(memoria.versao_memoria().encode())
    for arquivo in ARQUIVOS_CODIGO:
        h.update(arquivo.read_bytes())
    for arquivo in (ARQUIVO_INDICE, ARQUIVO_TABELA, rede.ARQUIVO):
        if not arquivo.exists():  # a rede é opcional
            continue
        info = arquivo.stat()
        h.update(f"{info.st_size}:{info.st_mtime_ns}".encode())
    return h.hexdigest()[:16]


def _validada_para_tela(entrada: dict) -> dict:
    return {"pergunta": entrada["pergunta"], "resposta": entrada["resposta"], "artigos": entrada["artigos"]}


def responder(pergunta: str, pensar: bool = False, atalhos: bool = True,
              historico: list[dict] | None = None) -> Iterator[dict]:
    """atalhos=False pula a resposta validada exata e o cache (nem lê, nem grava): os evals
    usam assim para medir o modelo, e não a memória.
    historico: trocas anteriores da conversa, para entender perguntas de continuação.

    Gera eventos para a interface:
    {"tipo": "reescrita", ...}   -> como a pergunta de continuação foi entendida
    {"tipo": "alerta", ...}      -> sinal de emergência: mensagem fixa (192 / CVV) antes da resposta
    {"tipo": "memoria", ...}     -> respostas validadas usadas ("exata": é a resposta;
                                    "ligada": foram junto com os artigos para o modelo)
    {"tipo": "fontes", ...}      -> artigos encontrados pela busca
    {"tipo": "pensando"}         -> modelo está raciocinando
    {"tipo": "ferramenta", ...}  -> o modelo consultou a tabela ou a rede credenciada ("nome")
    {"tipo": "texto", ...}       -> pedaço da resposta
    {"tipo": "fim", ...}         -> tempos; "origem" diz de onde veio: memoria, cache ou modelo
    """
    inicio = time.time()

    # c. Conversa: daqui em diante tudo (memória, cache, busca, resposta) usa a pergunta completa.
    if historico:
        completa = entender_pergunta(pergunta, historico)
        if memoria.normalizar(completa) != memoria.normalizar(pergunta):
            yield {"tipo": "reescrita", "original": pergunta, "pergunta": completa,
                   "segundos": round(time.time() - inicio, 1)}
            pergunta = completa

    # Emergência: alerta fixo, sempre (também quando a resposta vem da memória ou do cache).
    normalizada = memoria.normalizar(pergunta)
    if re.search(r"suicid|me matar|tirar minha vida", normalizada):
        yield {"tipo": "alerta", "mensagem": ALERTA_VIDA}
    elif _RE_EMERGENCIA.search(normalizada):
        yield {"tipo": "alerta", "mensagem": ALERTA_EMERGENCIA}

    if not atalhos:
        yield from _gerar(pergunta, pensar, inicio)
        return

    # 0a. Resposta validada por uma pessoa para esta pergunta: sai direto, sem modelo.
    validada = memoria.resposta_validada(pergunta)
    if validada:
        yield {"tipo": "memoria", "modo": "exata", "itens": [_validada_para_tela(validada)]}
        yield {"tipo": "texto", "texto": validada["resposta"]}
        yield {"tipo": "fim", "origem": "memoria", "tempo_total": round(time.time() - inicio, 1)}
        return

    # 0b. Mesma pergunta já respondida pelo modelo, com o mesmo código e os mesmos dados.
    impressao = impressao_digital()
    guardada = memoria.cache_buscar(pergunta, pensar, impressao)
    if guardada:
        yield from guardada["eventos"]
        yield {"tipo": "fim", "origem": "cache", "tempo_total": round(time.time() - inicio, 1),
               "tempo_original": guardada["tempo_original"], "gerada_em": guardada["gerada_em"]}
        return

    # Etapas 1 a 3, guardando o que a tela precisa para repetir a resposta depois.
    eventos: list[dict] = []
    texto: list[str] = []
    for evento in _gerar(pergunta, pensar, inicio):
        yield evento
        if evento["tipo"] in ("memoria", "fontes", "ferramenta"):
            eventos.append(evento)
        elif evento["tipo"] == "texto":
            texto.append(evento["texto"])
        elif evento["tipo"] == "fim" and "".join(texto).strip():
            eventos.append({"tipo": "texto", "texto": "".join(texto)})
            memoria.cache_guardar(pergunta, pensar, impressao,
                                  {"eventos": eventos, "tempo_original": evento["tempo_total"]})


def _gerar(pergunta: str, pensar: bool, inicio: float) -> Iterator[dict]:
    """Etapas 1 a 3: busca, decisão e resposta do modelo."""
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
    validadas = memoria.ligadas_aos_artigos([a["id"] for a in artigos])
    if validadas:
        yield {"tipo": "memoria", "modo": "ligada", "itens": [_validada_para_tela(v) for v in validadas]}

    # Etapa 2: decisão. Só a pergunta e a ferramenta, sem os artigos.
    consultas: list[dict] = []
    sistema, ferramentas = SYSTEM_DECISAO, list(FERRAMENTAS.values())
    if "consultar_rede" in FERRAMENTAS and _RE_PEDE_PRESTADOR.search(memoria.normalizar(pergunta)):
        # Pedido claro de "quem me atende": só a rede, com uma tarefa só (escolher a especialidade).
        sistema, ferramentas = SYSTEM_DECISAO_REDE, [rede.consultar_rede]
    mensagens = [{"role": "system", "content": sistema}, {"role": "user", "content": pergunta}]
    while len(consultas) < MAX_CONSULTAS:
        decisao = ollama.chat(
            model=MODELO_RESPOSTA, messages=mensagens, tools=ferramentas,
            think=False, keep_alive=MANTER_CARREGADO,
            # Se não for chamar ferramenta, não precisa escrever nada longo.
            # Temperatura 0: chamar ou não a tabela não pode depender de sorteio (medido: com o
            # padrão, 5 das 22 perguntas do eval mudaram de decisão entre duas rodadas).
            options={"num_ctx": JANELA_CONTEXTO, "num_predict": 128, "temperature": 0},
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
            consultas.append({"nome": nome, "argumentos": argumentos,
                              "termo": argumentos.get("termo", ""), "resultado": resultado})
            yield {"tipo": "ferramenta", "nome": nome, "argumentos": argumentos,
                   "total": resultado.get("total_encontrado", 0),
                   "resumo": (rede.resumo(resultado) if nome == "consultar_rede"
                              else resumo_resultado(resultado)) if funcao else [],
                   # Contatos completos para a tela, direto do cadastro (o modelo não os copia).
                   "lista": rede.para_tela(resultado) if nome == "consultar_rede" else None}
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
            {"role": "user", "content": montar_prompt(pergunta, artigos, consultas, validadas)},
        ],
        stream=True,
        think=pensar,
        keep_alive=MANTER_CARREGADO,
        options={"num_ctx": JANELA_CONTEXTO, "seed": SEMENTE},
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
        "origem": "modelo",
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
        elif evento["tipo"] == "reescrita":
            print(f"[entendi como: {evento['pergunta']}]\n")
        elif evento["tipo"] == "alerta":
            print(f"[ALERTA] {evento['mensagem']}\n")
        elif evento["tipo"] == "memoria":
            rotulo = "resposta validada" if evento["modo"] == "exata" else "respostas validadas ligadas aos artigos"
            print(f"[memória: {rotulo}] " + " | ".join(i["pergunta"] for i in evento["itens"]) + "\n")
        elif evento["tipo"] == "pensando":
            print("(pensando...)")
        elif evento["tipo"] == "ferramenta":
            print(f"[ferramenta] {evento['nome']}({evento['argumentos']}) -> {evento['total']} resultado(s)")
            for linha in evento["resumo"]:
                print(f"    {linha}")
            print()
        elif evento["tipo"] == "texto":
            print(evento["texto"], end="", flush=True)
        elif evento["tipo"] == "fim" and evento["origem"] == "memoria":
            print(f"\n\n[resposta validada da memória, sem modelo: {evento['tempo_total']}s]")
        elif evento["tipo"] == "fim" and evento["origem"] == "cache":
            print(f"\n\n[do cache: {evento['tempo_total']}s | a original levou {evento['tempo_original']}s, "
                  f"gerada em {evento['gerada_em']}]")
        elif evento["tipo"] == "fim":
            print(f"\n\n[{evento['tempo_total']}s no total | {evento['tokens_por_segundo']} tokens/s | "
                  f"{evento['consultas_tabela']} consulta(s) à tabela]")
