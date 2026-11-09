"""Etapa 3: buscar os artigos, consultar a tabela se preciso e gerar a resposta.

Fluxo de uma pergunta:
    c. conversa: se há perguntas anteriores, o modelo reescreve a nova para ela ficar completa
       sozinha ("e de clínico geral?" -> "Quantas consultas de clínico geral posso fazer?").
       Daí em diante tudo usa a pergunta reescrita; a conversa não vai para a resposta.
    0. memória (memoria.py): resposta validada para a mesma pergunta sai direto; pergunta
       já respondida pelo modelo volta do cache. Nos dois casos, nenhuma etapa abaixo roda.
    1. busca: os 4 artigos mais parecidos com a pergunta (+ respostas validadas ligadas a eles)
       + a seção do guia do app e do site (guia.py), se a pergunta for sobre o que o segurado
       faz sozinho (autorização, 2ª via, cancelar...); os links do app/site vão para a tela
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
import os
import re
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import ollama

import guia
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
# Threads na CPU. Sem valor, o Ollama usa uma por núcleo FÍSICO que enxerga. Na VM de testes, os 4
# vCPUs aparecem como 2 núcleos x 2 threads e ele usava só 2: medido, 6,2 -> 7,6 tokens/s com 4.
# O serviço da VM define CHATBOT_THREADS (scripts/linux/chatbot.service); no PC fica o padrão.
# Igual em todas as chamadas: se mudar entre uma e outra, o Ollama recarrega o modelo.
THREADS = int(os.environ.get("CHATBOT_THREADS") or 0) or None


def opcoes(**extra) -> dict:
    """Opções do Ollama para o modelo de resposta: a mesma janela e as mesmas threads sempre."""
    resultado = {"num_ctx": JANELA_CONTEXTO, **extra}
    if THREADS:
        resultado["num_thread"] = THREADS
    return resultado

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
- Se a última mensagem é só uma especialidade e um lugar (cidade ou bairro), a pessoa quer \
saber quem atende ali: "Quem atende <especialidade> em <lugar>?". Não pergunte de valor.
- Escreva só a pergunta reescrita, em uma linha.

Exemplos:
Conversa sobre sessões de fisioterapia. Última pergunta: "e quanto custa?" -> \
Quanto custa uma sessão de fisioterapia?
Conversa sobre consultas de cardiologia. Última pergunta: "e de dermatologia?" -> \
Quantas consultas de dermatologia posso fazer?
Conversa sobre o valor de um exame. Última pergunta: "dermatologista sao leopoldo" -> \
Quem atende dermatologia em São Leopoldo?"""

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
# Quem tem pressa escreve curto ("contato de fono", "quero marcar consulta"): contato, telefone e
# endereço também são pedido de prestador (com o prompt geral, "contato de fono" foi respondido
# com o regulamento). "Marcar" só com "quero/preciso": "posso marcar consulta eletiva?" é regra.
_RE_PEDE_PRESTADOR = re.compile(
    r"que (especialista|medico|profissional)|qual (especialista|medico|profissional)|"
    r"quem (eu )?(devo )?(procur|atende|consult)|onde (eu )?(faco|fazer|posso fazer|consulto|encontro)|"
    r"perto de (mim|casa)|mais proxim|rede credenciada|credenciados?\b|"
    r"\bcontatos?\b|telefone|whats|endereco|consultorio|"
    r"(quero|preciso|vou|gostaria de|queria) (marcar|agendar)")
# Pergunta de valor que cita a cidade ("quanto custa a consulta em NH?") continua sendo da tabela.
_RE_PEDE_VALOR = re.compile(r"quanto (custa|pago|paga|e|sai)|valor|preco|coparticipa")
# Pedido de um profissional pelo nome: "quero o doutor Fernando", "dra. Ana Paula em NH". Vai
# direto ao cadastro (rede.buscar_nome), ANTES da reescrita: curta, essa pergunta parecia
# continuação, e a reescrita a trocou pelo assunto anterior ("análises clínicas em NH").
_RE_PEDE_NOME = re.compile(r"\b(dr|dra|doutor|doutora|doutor a)\b\s+(.+)")
_FIM_DO_NOME = re.compile(r"\s(em|no|na|nos|nas|que|pra|para|perto|por favor|atende|contato|telefone)\b.*")


def pedido_de_nome(pergunta: str) -> str | None:
    """'quero o doutor fernando silva em nh' -> 'fernando silva'; sem título + nome, None."""
    achou = _RE_PEDE_NOME.search(memoria.normalizar(pergunta))
    if not achou:
        return None
    nome = _FIM_DO_NOME.sub("", f" {achou.group(2)}").strip()
    palavras = [p for p in nome.split() if len(p) >= 2][:4]
    return " ".join(palavras) or None


MAX_NOMES_NO_TEXTO = 20


def _responder_nome(nome: str, pergunta: str, historico: list[dict] | None, inicio: float,
                    filtro: dict | None = None) -> Iterator[dict]:
    """Busca pelo nome, sem modelo. filtro: o que está escolhido no quadro da rede
    ({"cidade": "novo hamburgo", "especialidade": "ANALISES CLINICAS"}).
    - Alguém com esse nome na especialidade escolhida (ou só um no total): mostra a lista.
    - Senão: diz que ali não há, lista quem tem o nome e as especialidades, e oferece botões para
      abrir a lista da especialidade certa com a pessoa em destaque (evento "escolher_especialidade").
    Antes, com Análises Clínicas escolhida, "quero o doutor Fernando" voltava a lista de análises
    clínicas (a reescrita trocou o pedido pelo assunto anterior)."""
    filtro = filtro or {}
    cidade = rede.cidade_citada(pergunta)
    for troca in reversed((historico or [])[-MAX_TROCAS_NA_CONVERSA:]):
        cidade = cidade or rede.cidade_citada(troca.get("pergunta") or "")
    cidade_id = memoria.normalizar(filtro.get("cidade") or cidade or "") or None
    lista = rede.buscar_nome(nome, cidade_id)
    pessoas, nome_bonito = lista["prestadores"], nome.title()
    especialidade = filtro.get("especialidade") or None

    def fim():
        return {"tipo": "fim", "origem": "busca_nome", "tempo_total": round(time.time() - inicio, 1)}

    nome_cidade = next((c["nome"] for c in rede.cidades() if c["id"] == cidade_id), cidade_id or "")
    em_cidade = f" em {rede.bonito(nome_cidade)}" if cidade_id else ""
    if not pessoas:
        outra = ", nem em outra cidade da rede credenciada" if cidade_id else " na rede credenciada"
        yield {"tipo": "texto", "texto": f"Não encontrei nenhum profissional com o nome “{nome_bonito}”"
                                         f"{em_cidade}{outra}. Confira a grafia (basta o começo do nome) ou "
                                         "busque pela especialidade."}
        yield fim()
        return

    na_cidade = [p for p in pessoas if p["cidade_id"] == cidade_id] if cidade_id else pessoas
    na_escolha = [p for p in na_cidade if especialidade in p["especialidades"]] if especialidade else []
    if na_escolha or (not especialidade and len(na_cidade) == 1):
        lista["prestadores"] = na_escolha or na_cidade
        onde = (f" em {rede.bonito(especialidade)}" if na_escolha else "") + em_cidade
        texto = (f"Encontrei {len(lista['prestadores'])} profissional(is) com o nome “{nome_bonito}”{onde}: o "
                 "contato está na lista abaixo. A rede muda com frequência: confirme por telefone antes de ir.")
        yield {"tipo": "ferramenta", "nome": "buscar_nome", "argumentos": {"nome": nome},
               "total": len(lista["prestadores"]), "resumo": [], "lista": lista}
        yield {"tipo": "texto", "texto": texto}
        yield fim()
        return

    # Não está onde a pessoa procurou: diz isso e mostra quem tem o nome, e onde.
    if cidade_id and not na_cidade:
        aviso = (f"Não encontrei nenhum profissional com o nome “{nome_bonito}”{em_cidade}. "
                 "Quer ver em outra cidade? Na rede credenciada, há:\n")
        pedido = "Escolha abaixo para ver o contato na lista."
    elif especialidade:
        aviso = (f"Em {rede.bonito(especialidade)}{em_cidade} não há ninguém com o nome “{nome_bonito}”. "
                 f"Na rede credenciada, encontrei {len(pessoas)} profissional(is) com esse nome:\n")
        pedido = "Escolha abaixo a especialidade para ver o contato na lista."
    else:
        aviso = f"Encontrei {len(pessoas)} profissional(is) com o nome “{nome_bonito}” na rede credenciada:\n"
        pedido = "Escolha abaixo a especialidade para ver o contato na lista."
    linhas = [f"• {rede.bonito(p['nome'])}: {', '.join(rede.bonito(e) for e in p['especialidades'])} "
              f"({rede.bonito(p['cidade'])})" for p in pessoas[:MAX_NOMES_NO_TEXTO]]
    if len(pessoas) > MAX_NOMES_NO_TEXTO:
        linhas.append(f"• e mais {len(pessoas) - MAX_NOMES_NO_TEXTO}")
    texto = aviso + "\n".join(linhas) + "\n" + pedido
    grupos: dict[tuple, dict] = {}
    for p in pessoas:
        for e in p["especialidades"]:
            g = grupos.setdefault((e, p["cidade_id"]), {"especialidade": e, "cidade_id": p["cidade_id"],
                                                         "cidade": p["cidade"], "total": 0})
            g["total"] += 1
    opcoes = sorted(grupos.values(), key=lambda g: (g["cidade_id"] != cidade_id, g["especialidade"]))
    yield {"tipo": "texto", "texto": texto}
    yield {"tipo": "escolher_especialidade", "nome": nome, "opcoes": opcoes}
    yield fim()
# Sem lugar nenhum (nem na pergunta, nem antes na conversa): a lista sai para Novo Hamburgo, onde
# está a maior parte da rede e de quem pergunta, e a resposta diz para avisar se for outra cidade.
# Antes o chatbot só contava quantos havia e pedia a cidade: mais uma volta para quem tem pressa.
CIDADE_PADRAO = "Novo Hamburgo"
SYSTEM_DECISAO_REDE = """A pessoa quer saber quem atende ou que especialista procurar no plano \
de saúde do IPASEM. Chame consultar_rede com a especialidade mais provável para o caso (ex.: \
zumbido no ouvido -> otorrinolaringologia; tristeza e ansiedade -> psicologia). Para hospitais, \
use especialidade "hospital". Passe cidade e bairro só se a pessoa disser.
Se a pessoa não disse especialidade, sintoma nem exame (ex.: "quero marcar uma consulta"), \
não chame nada e responda apenas: SEM_CONSULTA"""

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


# Botões de assunto (menu.json). Com "rede", a resposta recebe só o resultado da rede e estas
# instruções curtas, no lugar do SYSTEM inteiro (~700 tokens sobre artigos e tabela que ela não usa):
# as regras de como responder a rede já vão junto com o resultado (rede.INSTRUCOES).
ASSUNTOS_DO_GUIA = {"autorizacoes", "app"}
ASSUNTOS = ASSUNTOS_DO_GUIA | {"rede", "regulamento"}  # "regulamento" = o caminho completo
SYSTEM_SO_REDE = """Você ajuda beneficiários do plano de saúde do IPASEM (Novo Hamburgo) a encontrar \
quem atende na rede credenciada. Use só o resultado da consulta à rede que vem com a pergunta e \
siga as instruções que vêm com ele. Responda em português simples, em no máximo 5 frases."""


# Troca de assunto: a pergunta digitada parece ser de outro assunto? Três sinais, um por família:
#   rede         -> pede_prestador() ("quem atende", "contato", especialidade + cidade...); nome de
#                   especialidade sozinho ("tem dermatologista em Gramado?") só quando não há outro
#                   sinal: "o plano cobre cirurgia?" cita uma especialidade e é pergunta de regra
#   autorizacoes -> palavra do vocabulario.json de um bloco desse assunto (menu.json, "blocos")
#   app          -> idem (senha, carteirinha, extrato...)
#   regulamento  -> _RE_REGRA (cobre, carência, quantas, valor, reembolso...)
# Só sugere quando há sinal de UM outro assunto e nenhum do atual; sem sinal claro, segue no
# assunto escolhido ("psicólogo no centro" no assunto rede não tem outro sinal, e está certo).
# Medido em avaliar_assunto.py.
_RE_REGRA = re.compile(
    r"\bcobre\b|\bcobrem\b|cobertura|carencia|reembolso|coparticipa|\bdependentes?\b|limite|"
    r"\bquant[oa]s\b|regulamento|\bartigo|valor|quanto (custa|paga|pago|e|sai)|preco|tabela|"
    r"exclu|internac|\bdireito\b|resolucao|\bposse\b|mensalidade|contribuicao")
_PALAVRAS_QUE_NAO_SAO_ESPECIALIDADE = {"geral", "gerais"}


def menciona_especialidade(texto: str) -> bool:
    """Alguma palavra da pergunta é nome de especialidade da rede ("dermatologista", "dentista")?"""
    if not rede.disponivel():
        return False
    return any(rede.achar_especialidades(p) for p in memoria.normalizar(texto).split()
               if len(p) >= 4 and p not in _PALAVRAS_QUE_NAO_SAO_ESPECIALIDADE)


def sugerir_assunto(atual: str, pergunta: str, original: str = "") -> str | None:
    """O assunto para o qual vale oferecer a troca, ou None (segue no atual)."""
    if atual not in ASSUNTOS:
        return None
    sinais: set[str] = set()
    if "consultar_rede" in FERRAMENTAS and pede_prestador(pergunta, original):
        sinais.add("rede")
    for texto in (pergunta, original):
        bloco = guia.assunto(texto) if texto else None
        if bloco:
            sinais.add(guia.assunto_do_bloco(bloco) or "autorizacoes")
            break
    if _RE_REGRA.search(memoria.normalizar(f"{pergunta} {original}")):
        sinais.add("regulamento")
    if not sinais and "consultar_rede" in FERRAMENTAS and menciona_especialidade(f"{pergunta} {original}"):
        sinais.add("rede")
    if atual in sinais or len(sinais) != 1:
        return None
    return next(iter(sinais))


def formatar_paginas(trecho: dict) -> str:
    inicio, fim = trecho["pagina_inicio"], trecho["pagina_fim"]
    return f"p. {inicio}" if inicio == fim else f"p. {inicio}-{fim}"


def montar_prompt(pergunta: str, artigos: list[dict], consultas: list[dict],
                  validadas: list[dict] = (), extras: list[str] = ()) -> str:
    blocos = [
        f"[{a['id']} | {formatar_paginas(a)} | {a['secao']}]\n{a['texto']}" for a in artigos
    ]
    partes = [f"Pergunta: {pergunta}"]
    if blocos:  # sem artigos (assunto "rede"), nem o rótulo
        partes += ["Artigos do regulamento que podem ajudar:", "\n\n".join(blocos)]
    if validadas:
        partes.append("Respostas validadas ligadas a esses artigos:\n\n" + "\n\n".join(
            f"Pergunta validada: {v['pergunta']}\nResposta correta: {v['resposta']}" for v in validadas))
    for c in consultas:
        if c["nome"] == "consultar_rede":
            partes.append(rede.resultado_em_texto(c["argumentos"], c["resultado"]))
        else:
            partes.append(resultado_em_texto(c["termo"], c["resultado"]))
    partes.extend(extras)
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
        options=opcoes(num_predict=80, temperature=0),
    )
    nova = (r.message.content or "").strip().splitlines()[0:1]
    nova = nova[0].strip().strip('"“”').removeprefix("Pergunta reescrita:").strip() if nova else ""
    # Saída vazia ou que virou resposta/explicação: fica a pergunta original.
    if not nova or len(nova) > 3 * len(pergunta) + 150:
        return pergunta
    return nova


# Arquivos que mudam a resposta. Se qualquer um mudar, o cache antigo deixa de valer.
ARQUIVOS_CODIGO = [Path(__file__).with_name(n)
                   for n in ("rag.py", "busca.py", "tabela.py", "memoria.py", "rede.py", "guia.py")]


def impressao_digital() -> str:
    """Resumo de tudo o que gerou uma resposta: código (com os prompts e o modelo), dados e
    respostas validadas. Para os dados grandes basta tamanho + data de modificação."""
    h = hashlib.sha256(memoria.versao_memoria().encode())
    for arquivo in ARQUIVOS_CODIGO:
        h.update(arquivo.read_bytes())
    for arquivo in (ARQUIVO_INDICE, ARQUIVO_TABELA, rede.ARQUIVO, guia.ARQUIVO, guia.ARQUIVO_VOCABULARIO):
        if not arquivo.exists():  # a rede é opcional
            continue
        info = arquivo.stat()
        h.update(f"{info.st_size}:{info.st_mtime_ns}".encode())
    return h.hexdigest()[:16]


def _validada_para_tela(entrada: dict) -> dict:
    return {"pergunta": entrada["pergunta"], "resposta": entrada["resposta"], "artigos": entrada["artigos"]}


def responder(pergunta: str, pensar: bool = False, atalhos: bool = True,
              historico: list[dict] | None = None, assunto: str | None = None,
              manter_assunto: bool = False, filtro_rede: dict | None = None) -> Iterator[dict]:
    """atalhos=False pula a resposta validada exata e o cache (nem lê, nem grava): os evals
    usam assim para medir o modelo, e não a memória.
    historico: trocas anteriores da conversa, para entender perguntas de continuação.
    assunto: o botão de assunto que a pessoa escolheu (menu.json: "rede", "autorizacoes",
    "app", "regulamento"); diz ao chatbot onde procurar e o que pular (ver _gerar). None: o
    caminho completo. Se a pergunta parece ser de outro assunto, sai só o evento "mudar_assunto"
    e nada é respondido; a página pergunta e, se a pessoa quiser continuar, manda de novo com
    manter_assunto=True (os evals também usam assim, para medir o caminho do assunto).

    Gera eventos para a interface:
    {"tipo": "reescrita", ...}   -> como a pergunta de continuação foi entendida
    {"tipo": "alerta", ...}      -> sinal de emergência: mensagem fixa (192 / CVV) antes da resposta
    {"tipo": "memoria", ...}     -> respostas validadas usadas ("exata": é a resposta;
                                    "ligada": foram junto com os artigos para o modelo)
    {"tipo": "fontes", ...}      -> artigos encontrados pela busca
    {"tipo": "guia", ...}        -> seção do guia do app e do site + links (para a tela)
    {"tipo": "mudar_assunto"}    -> a pergunta parece de outro assunto (nada é respondido)
    {"tipo": "escolher_especialidade"} -> busca por nome: especialidades onde a pessoa aparece
    {"tipo": "pensando"}         -> modelo está raciocinando
    {"tipo": "ferramenta", ...}  -> o modelo consultou a tabela ou a rede credenciada ("nome")
    {"tipo": "texto", ...}       -> pedaço da resposta
    {"tipo": "fim", ...}         -> tempos; "origem" diz de onde veio: memoria, cache ou modelo
    """
    inicio = time.time()
    original = pergunta

    # Profissional pelo nome: direto ao cadastro, antes da reescrita e sem modelo.
    nome = pedido_de_nome(pergunta) if rede.disponivel() else None
    if nome:
        yield from _responder_nome(nome, pergunta, historico, inicio, filtro_rede)
        return

    # c. Conversa: daqui em diante tudo (memória, cache, busca, resposta) usa a pergunta completa.
    if historico:
        completa = entender_pergunta(pergunta, historico)
        if memoria.normalizar(completa) != memoria.normalizar(pergunta):
            yield {"tipo": "reescrita", "original": pergunta, "pergunta": completa,
                   "segundos": round(time.time() - inicio, 1)}
            pergunta = completa

    # Cidade que a pessoa já disse numa pergunta anterior: a rede usa sem perguntar de novo (ela
    # tinha dito "novo hamburgo" e, na pergunta seguinte, o chatbot pediu a cidade outra vez).
    # Só das perguntas, não das respostas: a resposta pode citar cidades que a pessoa não disse.
    cidade_conversa = None
    if historico and not (rede.cidade_citada(original) or rede.cidade_citada(pergunta)):
        for troca in reversed(historico[-MAX_TROCAS_NA_CONVERSA:]):
            cidade_conversa = rede.cidade_citada(troca.get("pergunta") or "")
            if cidade_conversa:
                break

    # Emergência: alerta fixo, sempre (também quando a resposta vem da memória ou do cache).
    normalizada = memoria.normalizar(pergunta)
    if re.search(r"suicid|me matar|tirar minha vida", normalizada):
        yield {"tipo": "alerta", "mensagem": ALERTA_VIDA}
    elif _RE_EMERGENCIA.search(normalizada):
        yield {"tipo": "alerta", "mensagem": ALERTA_EMERGENCIA}

    # Pergunta de outro assunto: pergunta antes de responder (e não gasta o modelo à toa).
    if assunto and not manter_assunto:
        sugerido = sugerir_assunto(assunto, pergunta, original)
        if sugerido:
            yield {"tipo": "mudar_assunto", "atual": assunto, "sugerido": sugerido, "pergunta": pergunta}
            yield {"tipo": "fim", "origem": "mudar_assunto", "tempo_total": round(time.time() - inicio, 1)}
            return

    if not atalhos:
        yield from _gerar(pergunta, pensar, inicio, original, cidade_conversa, assunto)
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
    # A cidade lembrada da conversa e o assunto escolhido mudam a resposta: entram na chave do cache.
    chave = pergunta + (f" (cidade da conversa: {cidade_conversa})" if cidade_conversa else "") \
        + (f" (assunto: {assunto})" if assunto else "")
    guardada = memoria.cache_buscar(chave, pensar, impressao)
    if guardada:
        yield from guardada["eventos"]
        yield {"tipo": "fim", "origem": "cache", "tempo_total": round(time.time() - inicio, 1),
               "tempo_original": guardada["tempo_original"], "gerada_em": guardada["gerada_em"]}
        return

    # Etapas 1 a 3, guardando o que a tela precisa para repetir a resposta depois.
    eventos: list[dict] = []
    texto: list[str] = []
    for evento in _gerar(pergunta, pensar, inicio, original, cidade_conversa, assunto):
        yield evento
        if evento["tipo"] in ("memoria", "fontes", "guia", "ferramenta"):
            eventos.append(evento)
        elif evento["tipo"] == "texto":
            texto.append(evento["texto"])
        elif evento["tipo"] == "fim" and "".join(texto).strip():
            eventos.append({"tipo": "texto", "texto": "".join(texto)})
            memoria.cache_guardar(chave, pensar, impressao,
                                  {"eventos": eventos, "tempo_original": evento["tempo_total"]})


def pede_prestador(pergunta: str, original: str = "") -> bool:
    """Pedido de "quem me atende": vai para a decisão só com a rede. Olha também o que a pessoa
    escreveu antes da reescrita, porque a reescrita pode perder o pedido ("clinico geral novo
    hamburgo" virou "Quanto custa uma consulta de clínico geral?"). Especialidade + cidade da
    rede, sem falar de valor, também é pedido de prestador."""
    textos = [memoria.normalizar(t) for t in (pergunta, original) if t]
    if any(_RE_PEDE_PRESTADOR.search(t) for t in textos):
        return True
    escrito = textos[-1]  # o que a pessoa escreveu (ou a pergunta, se não houve reescrita)
    return bool(rede.cidade_citada(escrito)) and not _RE_PEDE_VALOR.search(escrito)


def _gerar(pergunta: str, pensar: bool, inicio: float, original: str = "",
           cidade_conversa: str | None = None, assunto: str | None = None) -> Iterator[dict]:
    """Etapas 1 a 3: busca, decisão e resposta do modelo.
    original: o que a pessoa escreveu, antes da reescrita. cidade_conversa: cidade dita antes.
    assunto: botão escolhido. "rede": só a rede, sem artigos, guia nem as instruções do
    regulamento (a resposta da rede não usa nada disso). "autorizacoes"/"app": o guia entra mesmo
    sem palavra do assunto, e a tabela não é oferecida."""
    so_rede = assunto == "rede" and "consultar_rede" in FERRAMENTAS
    do_guia = assunto in ASSUNTOS_DO_GUIA
    artigos = [] if so_rede else buscar(pergunta, k=ARTIGOS_NO_PROMPT)
    tempo_busca = time.time() - inicio
    yield {
        "tipo": "fontes",
        "fontes": [
            {"id": a["id"], "paginas": formatar_paginas(a), "secao": a["secao"],
             "similaridade": a["similaridade"], "pedaco": a["pedaco_encontrado"], "texto": a["texto"]}
            for a in artigos
        ],
    }
    # Guia do app e do site: a seção que explica como o segurado faz isso sozinho, se a pergunta
    # for sobre isso (busca separada da dos artigos; os links vão para a tela, não para o modelo).
    secao_guia = None if so_rede else guia.buscar(pergunta, assunto_escolhido=do_guia)
    if secao_guia:
        yield guia.para_tela(secao_guia)
    validadas = memoria.ligadas_aos_artigos([a["id"] for a in artigos])
    if validadas:
        yield {"tipo": "memoria", "modo": "ligada", "itens": [_validada_para_tela(v) for v in validadas]}

    # Etapa 2: decisão. Só a pergunta e a ferramenta, sem os artigos.
    consultas: list[dict] = []
    sistema, ferramentas = SYSTEM_DECISAO, list(FERRAMENTAS.values())
    rota_rede = so_rede or ("consultar_rede" in FERRAMENTAS and pede_prestador(pergunta, original))
    if rota_rede:
        # Pedido claro de "quem me atende": só a rede, com uma tarefa só (escolher a especialidade).
        sistema, ferramentas = SYSTEM_DECISAO_REDE, [rede.consultar_rede]
    elif (secao_guia and (do_guia or guia.fala_do_assunto(pergunta))
          and not _RE_PEDE_VALOR.search(memoria.normalizar(pergunta))):
        # Pergunta do app ou do site, sem falar de valor: nenhuma ferramenta, e a decisão nem roda.
        # Em "quero cancelar 3122505" o modelo tomou o número da autorização por código de
        # procedimento e consultou a tabela à toa (0 resultados, 233 s no total na VM).
        ferramentas = []
    mensagens = [{"role": "system", "content": sistema}, {"role": "user", "content": pergunta}]
    while ferramentas and len(consultas) < MAX_CONSULTAS:
        decisao = ollama.chat(
            model=MODELO_RESPOSTA, messages=mensagens, tools=ferramentas,
            think=False, keep_alive=MANTER_CARREGADO,
            # Se não for chamar ferramenta, não precisa escrever nada longo.
            # Temperatura 0: chamar ou não a tabela não pode depender de sorteio (medido: com o
            # padrão, 5 das 22 perguntas do eval mudaram de decisão entre duas rodadas).
            options=opcoes(num_predict=128, temperature=0),
        )
        chamadas = decisao.message.tool_calls or []
        if not chamadas:
            break
        mensagens.append({"role": "assistant", "content": decisao.message.content or "",
                          "tool_calls": chamadas})
        for chamada in chamadas[: MAX_CONSULTAS - len(consultas)]:
            nome, argumentos = chamada.function.name, dict(chamada.function.arguments or {})
            funcao = FERRAMENTAS.get(nome)
            lugar = None
            if nome == "consultar_rede" and not argumentos.get("cidade") and not argumentos.get("bairro"):
                # Sem lugar: a cidade dita antes na conversa ou, sem ela, a cidade padrão.
                lugar = ("cidade_da_conversa", cidade_conversa) if cidade_conversa else ("cidade_assumida", CIDADE_PADRAO)
                argumentos["cidade"] = lugar[1]
            resultado = funcao(**argumentos) if funcao else {"erro": f"ferramenta {nome} não existe"}
            if lugar and resultado.get("total_encontrado"):
                resultado[lugar[0]] = lugar[1]
            consultas.append({"nome": nome, "argumentos": argumentos,
                              "termo": argumentos.get("termo", ""), "resultado": resultado})
            yield {"tipo": "ferramenta", "nome": nome, "argumentos": argumentos,
                   "total": resultado.get("total_encontrado", 0),
                   "resumo": (rede.resumo(resultado) if nome == "consultar_rede"
                              else resumo_resultado(resultado)) if funcao else [],
                   # Contatos completos para a tela, direto do cadastro (o modelo não os copia).
                   "lista": rede.para_tela(resultado) if nome == "consultar_rede" else None}
            mensagens.append({"role": "tool", "tool_name": nome,
                              # Sem as chaves "_" (ex.: a lista completa da rede, que é só para a tela).
                              "content": json.dumps({k: v for k, v in resultado.items()
                                                     if not k.startswith("_")}, ensure_ascii=False)})
        # Achou algo: não precisa de outra rodada. Vazio: o modelo pode tentar outro termo.
        if any(c["resultado"].get("total_encontrado") for c in consultas):
            break

    # Pediu atendimento sem dizer a especialidade: a resposta pede a especialidade.
    extras = [guia.texto_para_modelo(secao_guia)] if secao_guia else []
    if rota_rede and not consultas:
        extras.append(rede.INSTRUCOES_SEM_ESPECIALIDADE)

    # Etapa 3: resposta, com artigos e resultado da tabela juntos.
    avisou_pensando = False
    tokens_gerados = segundos_gerando = 0.0
    for parte in ollama.chat(
        model=MODELO_RESPOSTA,
        messages=[
            {"role": "system", "content": SYSTEM_SO_REDE if so_rede else SYSTEM},
            {"role": "user", "content": montar_prompt(pergunta, artigos, consultas, validadas, extras)},
        ],
        stream=True,
        think=pensar,
        keep_alive=MANTER_CARREGADO,
        options=opcoes(seed=SEMENTE),
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
        "consultas_tabela": sum(c["nome"] == "consultar_tabela" for c in consultas),
        "consultas_rede": sum(c["nome"] == "consultar_rede" for c in consultas),
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
        elif evento["tipo"] == "guia":
            print(f"[guia] {evento['id']}: {evento['titulo']} ({evento['similaridade']}) | links na tela: "
                  + ", ".join(l["nome"] for l in evento["links"]) + "\n")
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
                  f"{evento['consultas_tabela']} consulta(s) à tabela | "
                  f"{evento.get('consultas_rede', 0)} à rede]")
