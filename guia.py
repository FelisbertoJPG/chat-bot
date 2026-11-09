"""Guia do app e do site: o que o segurado faz sozinho (autorizações, 2ª via, cancelar,
carteirinha, extratos, senha, acesso de dependentes).

Fonte: docs/funcionalidades-segurado.md, escrito à mão a partir do sistema (FORA do Git, como a
rede: sem ele, buscar() devolve None e o chatbot funciona como antes). Cada seção "## N."
vira um trecho; a busca é por significado, como a dos artigos, mas num índice SEPARADO
(dados/indice_guia.json). Se as seções entrassem no mesmo índice do regulamento, disputariam as 4
vagas dos artigos e poderiam tirar do prompt um artigo certo. Aqui, a seção só vai para o modelo
quando a pergunta é parecida o bastante com ela (LIMIAR, medido com avaliar_guia.py).

Os links (lojas do app, site) não vão para o modelo: aparecem na tela, direto do código. Mesma
lição da rede: o modelo de 4B copia fato exato errado.

Salvou o .md, já vale: o índice é refeito sozinho na próxima pergunta (~15 s, uma vez).

Uso no terminal:
    python guia.py "como cancelo uma autorização?"
    python guia.py indexar
"""

import difflib
import hashlib
import json
import re
import sys
from pathlib import Path

import ollama

from memoria import normalizar

PASTA = Path(__file__).parent
ARQUIVO = PASTA / "docs" / "funcionalidades-segurado.md"
ARQUIVO_INDICE = PASTA / "dados" / "indice_guia.json"
MODELO_EMBEDDING = "qwen3-embedding:0.6b"  # o mesmo da busca dos artigos
FONTE_CITACAO = "Guia do app e do site"

# Quando a seção vai junto com a pergunta. Só a nota não separa: medido em avaliar_guia.py,
# perguntas do app tinham nota a partir de 0,52 e perguntas de regra chegavam a 0,65 ("meu filho
# fez 21 anos, continua dependente?" parece com "acesso dos dependentes"). Então são duas pistas:
# a pergunta fala do assunto do guia (app, site, autorização, senha...) e a nota passa de LIMIAR;
# ou não fala, e a nota precisa passar de LIMIAR_SEM_ASSUNTO.
LIMIAR = 0.45  # com palavra do assunto, a palavra é a pista forte; a nota escolhe a seção
LIMIAR_SEM_ASSUNTO = 0.70
# As palavras do assunto ficam em vocabulario.json (editado à mão, como o memoria.md): é lá que se
# acrescenta, por exemplo, "ordem" em Autorização quando alguém pergunta de um jeito novo.
# ("Marcar" só como pedido, "quero marcar": "já posso marcar consulta?" é regra de carência, e com
# a seção de carência do app o modelo inventou "carência de 12 meses".)
ARQUIVO_VOCABULARIO = PASTA / "vocabulario.json"

# A seção 1 (Visão geral) é uma tabela que só resume as outras: cada linha é o nome de uma
# função, e ela ganhava das seções que explicam o passo a passo.
FORA_DA_BUSCA = {"1"}
# Perguntas frequentes: cada pergunta é uma unidade. Inteira, a seção 16 (~800 tokens) mistura
# tudo e perdia por pouco para seções que não respondiam ("exame negado" trazia a 15, sem o
# "veja o motivo"); sozinha, cada resposta curta casa melhor e pesa pouco no prompt.
POR_ITEM = {"16"}

INSTRUCAO = (
    "Instruct: Dada a dúvida de um segurado de plano de saúde, encontre a seção do guia do "
    "aplicativo e do site que explica como ele mesmo faz isso\nQuery: "
)

# Links oficiais, mostrados na tela (nunca escritos pelo modelo).
LINKS = [
    {"nome": "App IPASEM NH Digital (Android)",
     "url": "https://play.google.com/store/apps/details?id=br.gov.ipasemnhdigital"},
    {"nome": "App IPASEM NH Digital (iPhone)",
     "url": "https://apps.apple.com/br/app/ipasem-nh-digital/id6752838782"},
    {"nome": "Site do segurado (assistweb)", "url": "https://assistweb.ipasemnh.com.br/site/login"},
]

_RE_URL = re.compile(r"https?://[^\s)>\]]+")


def carregar_secoes() -> list[dict]:
    """As seções "## N. Título" do guia, com o texto de cada uma (sem as linhas "---").
    As perguntas frequentes viram uma unidade por pergunta (ver POR_ITEM)."""
    texto = ARQUIVO.read_text(encoding="utf-8")
    secoes = []
    for bloco in re.split(r"^## ", texto, flags=re.M)[1:]:
        titulo, _, corpo = bloco.partition("\n")
        numero = titulo.split(".")[0].strip()
        titulo = titulo.split(".", 1)[-1].strip()
        corpo = "\n".join(l for l in corpo.strip().splitlines() if l.strip() != "---").strip()
        if numero in POR_ITEM:
            for i, item in enumerate(re.split(r"\n(?=- )", corpo), 1):
                if item.startswith("- "):
                    pergunta = re.match(r"- \*\*(.+?)\*\*", item)
                    secoes.append({"id": f"{FONTE_CITACAO}, seção {numero}.{i}", "numero": numero,
                                   "titulo": f"{titulo}: {pergunta.group(1) if pergunta else ''}",
                                   "texto": " ".join(l.strip() for l in item.splitlines())})
            continue
        secoes.append({"id": f"{FONTE_CITACAO}, seção {numero}", "numero": numero,
                       "titulo": titulo, "texto": corpo})
    return secoes


def pedacos_para_indexar(secao: dict) -> list[str]:
    """Um pedaço por item, parágrafo ou linha de tabela, com o título da seção junto. Linhas que
    continuam um item (começam com espaço) ficam no mesmo pedaço.

    Sem o pedaço "seção inteira" (a busca dos artigos tem): aqui ele virava um vetor genérico,
    "sobre o app", e puxava qualquer pergunta para as seções de resumo (1 e 13)."""
    cabeca = secao["titulo"]
    pedacos, atual, subtitulo = [], "", ""
    for linha in secao["texto"].splitlines() + [""]:
        if linha.startswith("  ") and atual:
            atual += " " + linha.strip()
            continue
        if atual and not re.fullmatch(r"\|[-| ]+\|", atual):  # linha separadora de tabela não
            pedacos.append(f"{cabeca}{' - ' + subtitulo if subtitulo else ''}\n{_limpar(atual)}")
        atual = linha.strip()
        if atual.startswith("### "):
            subtitulo, atual = atual[4:], ""
    return pedacos


def _limpar(texto: str) -> str:
    """Só para a busca: sem negrito, link, e-mail e telefone. Medido: a linha "Dados errados...
    e-mail ... telefone ..." puxava perguntas sem relação ("perdi a guia", "informe do IR")."""
    texto = _RE_URL.sub("", texto.replace("**", ""))
    texto = re.sub(r"[\w.]+@[\w.]+\w|\(?\d{2}\)?\s?\d{4,5}-?\d{4}", "", texto)
    return " ".join(texto.split())


def _versao() -> str:
    """Muda quando o guia OU este código (o jeito de cortar os pedaços) muda."""
    return hashlib.sha256(ARQUIVO.read_bytes() + Path(__file__).read_bytes()).hexdigest()[:16]


def indexar() -> dict:
    secoes = carregar_secoes()
    itens = [(s["id"], p) for s in secoes if s["numero"] not in FORA_DA_BUSCA
             for p in pedacos_para_indexar(s)]
    print(f"[guia] indexando {len(itens)} pedaços de {len(secoes)} seções...", file=sys.stderr)
    vetores = ollama.embed(model=MODELO_EMBEDDING, input=[t for _, t in itens], keep_alive="60m")["embeddings"]
    indice = {"versao": _versao(), "modelo": MODELO_EMBEDDING, "secoes": secoes,
              "itens": [{"secao": i, "texto": t, "vetor": _unitario(v)} for (i, t), v in zip(itens, vetores)]}
    ARQUIVO_INDICE.parent.mkdir(exist_ok=True)
    ARQUIVO_INDICE.write_text(json.dumps(indice, ensure_ascii=False), encoding="utf-8")
    return indice


def _unitario(vetor: list[float]) -> list[float]:
    tamanho = sum(x * x for x in vetor) ** 0.5
    return [x / tamanho for x in vetor]


_indice: dict | None = None


def disponivel() -> bool:
    return ARQUIVO.exists()


def carregar_indice() -> dict:
    """Índice em memória; refeito se o .md mudou desde a última indexação."""
    global _indice
    versao = _versao()
    if _indice is None and ARQUIVO_INDICE.exists():
        _indice = json.loads(ARQUIVO_INDICE.read_text(encoding="utf-8"))
    if _indice is None or _indice["versao"] != versao:
        _indice = indexar()
    return _indice


def ranking(pergunta: str) -> list[dict]:
    """Todas as seções, da mais para a menos parecida com a pergunta (nota = melhor pedaço)."""
    indice = carregar_indice()
    vetor = _unitario(ollama.embed(model=MODELO_EMBEDDING, input=INSTRUCAO + pergunta,
                                   keep_alive="60m")["embeddings"][0])
    melhor: dict[str, tuple[float, str]] = {}
    for item in indice["itens"]:
        nota = sum(a * b for a, b in zip(vetor, item["vetor"]))
        if item["secao"] not in melhor or nota > melhor[item["secao"]][0]:
            melhor[item["secao"]] = (nota, item["texto"].split("\n")[-1])
    por_id = {s["id"]: s for s in indice["secoes"]}
    return [{**por_id[i], "similaridade": round(n, 3), "pedaco": p}
            for i, (n, p) in sorted(melhor.items(), key=lambda par: -par[1][0])]


# Erro de digitação: quem tem pressa escreve "autroização", "carterinha". Sem isso a seção certa
# vinha com nota 0,649 e ficava de fora (sem palavra do assunto, o limiar é 0,70). Palavra da
# pergunta parecida o bastante com uma entrada do vocabulário (letra trocada, faltando ou
# sobrando) conta como ela. Na prática só vale para entradas de 7 letras ou mais: em palavras
# mais curtas, 0,85 de semelhança só aceita a palavra igual.
SEMELHANCA_DIGITACAO = 0.85
_vocabulario: dict = {"versao": None, "blocos": {}}


def _ler_entrada(entrada: str) -> tuple[str, str] | None:
    """'cancel*' -> ('prefixo', 'cancel'); 'segunda via' -> ('expressao', ...); 'ordem' -> ('palavra', ...)."""
    prefixo = entrada.strip().endswith("*")
    termo = normalizar(entrada.strip().rstrip("*"))
    if not termo:
        return None
    if " " in termo:
        return ("expressao", termo)
    return ("prefixo" if prefixo else "palavra", termo)


def vocabulario() -> dict[str, list[tuple[str, str]]]:
    """Os blocos de vocabulario.json, relidos quando o arquivo muda. Arquivo com erro (vírgula
    faltando, por exemplo): avisa no log e continua com a última versão boa, sem derrubar o chat."""
    try:
        versao = ARQUIVO_VOCABULARIO.stat().st_mtime_ns
    except FileNotFoundError:
        return {}
    if versao != _vocabulario["versao"]:
        _vocabulario["versao"] = versao
        try:
            dados = json.loads(ARQUIVO_VOCABULARIO.read_text(encoding="utf-8"))
            _vocabulario["blocos"] = {
                nome: [e for e in map(_ler_entrada, entradas) if e]
                for nome, entradas in dados.items() if not nome.startswith("_")
            }
        except (ValueError, AttributeError, TypeError) as erro:
            print(f"[guia] {ARQUIVO_VOCABULARIO.name} com erro, usando a versão anterior: {erro}",
                  file=sys.stderr)
    return _vocabulario["blocos"]


def _parecida(palavra: str, termo: str) -> bool:
    return (len(termo) >= 5 and len(palavra) >= 5
            and difflib.SequenceMatcher(None, palavra, termo).ratio() >= SEMELHANCA_DIGITACAO)


def assunto(pergunta: str) -> str | None:
    """O bloco do vocabulário de que a pergunta fala ("Autorização"), ou None."""
    texto = normalizar(pergunta)
    palavras, com_espacos = texto.split(), f" {texto} "
    blocos = vocabulario()
    for nome, entradas in blocos.items():
        for tipo, termo in entradas:
            if ((tipo == "expressao" and f" {termo} " in com_espacos)
                    or (tipo == "palavra" and termo in palavras)
                    or (tipo == "prefixo" and any(p.startswith(termo) for p in palavras))):
                return nome
    for nome, entradas in blocos.items():  # segunda volta: erro de digitação
        for tipo, termo in entradas:
            if tipo == "palavra" and any(_parecida(p, termo) for p in palavras):
                return nome
            if tipo == "prefixo" and any(_parecida(p[:len(termo)], termo) for p in palavras):
                return nome
    return None


def fala_do_assunto(pergunta: str) -> bool:
    return assunto(pergunta) is not None


def passa(pergunta: str, nota: float, assunto_escolhido: bool = False) -> bool:
    """assunto_escolhido: a pessoa clicou num assunto do app/site (botão), e isso vale como a
    palavra do assunto."""
    com_assunto = assunto_escolhido or fala_do_assunto(pergunta)
    return nota >= (LIMIAR if com_assunto else LIMIAR_SEM_ASSUNTO)


def buscar(pergunta: str, assunto_escolhido: bool = False) -> dict | None:
    """A seção do guia que responde à pergunta, ou None se nenhuma passa (ver LIMIAR)."""
    if not disponivel():
        return None
    primeira = ranking(pergunta)[0]
    return primeira if passa(pergunta, primeira["similaridade"], assunto_escolhido) else None


def links_da_secao(secao: dict) -> list[dict]:
    """Os links fixos + os que aparecem no texto da seção (ex.: a página da rede credenciada)."""
    links = list(LINKS)
    conhecidos = {l["url"].split("/site")[0] for l in LINKS}
    for url in dict.fromkeys(_RE_URL.findall(secao["texto"])):
        url = url.rstrip(".,;")
        if not any(url.startswith(c) for c in conhecidos):
            links.append({"nome": url.split("//", 1)[-1], "url": url})
    return links


# Vão junto com a seção, e não no prompt fixo: pergunta sem seção do guia tem o prompt de antes.
INSTRUCOES = f"""Como usar o guia do app e do site:
- Se a pessoa quer fazer algo que ela mesma faz pelo app IPASEM NH Digital ou pelo site \
(autorização, segunda via, cancelar autorização, carteirinha, extrato, senha), diga isso, onde \
fica no menu e os passos principais, em poucas frases.
- Não escreva endereço de site nem link: os links do app e do site aparecem na tela, logo abaixo \
da sua resposta. Diga isso à pessoa.
- Você não vê nem altera autorizações, cadastro ou dados da pessoa. Se ela der um número de \
autorização, diga que não consegue fazer por aqui e que ela mesma faz pelo app ou pelo site.
- Fale do app e do site; "{FONTE_CITACAO}" é só o nome da fonte, para a citação.
- Regras de cobertura, limites de consultas e valores continuam vindo dos artigos e da tabela.
- Cite como ({FONTE_CITACAO}, seção N)."""


def texto_para_modelo(secao: dict) -> str:
    """A seção sem os endereços de site (vão para a tela), mais as instruções."""
    texto = _RE_URL.sub("(link na tela)", secao["texto"])
    return (f"Guia do app e do site do IPASEM (o que o segurado faz sozinho) - "
            f"[{secao['id']}: {secao['titulo']}]\n{texto}\n\n{INSTRUCOES}")


def para_tela(secao: dict) -> dict:
    return {"tipo": "guia", "id": secao["id"], "titulo": secao["titulo"],
            "similaridade": secao["similaridade"], "pedaco": secao["pedaco"],
            "texto": secao["texto"], "links": links_da_secao(secao)}


# --- Botões de assunto (menu.json) -----------------------------------------------------------
# Respostas instantâneas: o botão mostra o texto do guia como está, sem passar pelo modelo. Para
# perguntas de autorização e de app isso é mais certo e mais rápido ("quero cancelar 3122505"
# levou 233 s na VM e o modelo tomou o número da autorização por código de procedimento).

ARQUIVO_MENU = PASTA / "menu.json"


def _resposta_da_frequente(unidade: dict) -> str:
    """'- **Perdi a autorização.** Gerenciamento...' -> 'Gerenciamento...'"""
    return re.sub(r"^- \*\*.+?\*\*\s*", "", unidade["texto"]).strip()


def menu(recursos: set[str] = frozenset()) -> dict:
    """Os assuntos de menu.json com o texto do guia já resolvido, para a página.
    Dois tipos de assunto: com "opcoes" (respostas do guia na hora; precisam do guia) e sem
    opções, com "requer" (ex.: "rede"): o botão só marca o assunto e a pessoa escreve a pergunta;
    aparece se o recurso estiver em `recursos`. "problemas" lista referências que não existem."""
    if not ARQUIVO_MENU.exists():
        return {"assuntos": [], "problemas": []}
    estrutura = json.loads(ARQUIVO_MENU.read_text(encoding="utf-8"))
    unidades = carregar_secoes() if disponivel() else []
    secoes = {s["numero"]: s for s in unidades if s["numero"] not in POR_ITEM}
    frequentes = {normalizar(s["titulo"].split(":", 1)[-1]): s for s in unidades if s["numero"] in POR_ITEM}
    assuntos, problemas = [], []
    for assunto in estrutura.get("assuntos", []):
        campos = {k: assunto.get(k, "") for k in ("id", "icone", "titulo", "aviso", "exemplo")}
        if "opcoes" not in assunto:  # assunto de pergunta livre
            if assunto.get("requer") in recursos:
                assuntos.append(campos | {"opcoes": []})
            continue
        if not unidades:
            continue  # sem o guia, os assuntos de respostas prontas não aparecem
        opcoes = []
        for opcao in assunto.get("opcoes", []):
            secao = secoes.get(str(opcao.get("secao", "")).strip()) if opcao.get("secao") else None
            frequente = frequentes.get(normalizar(opcao.get("pergunta_frequente", ""))) \
                if opcao.get("pergunta_frequente") else None
            if opcao.get("secao") and not secao:
                problemas.append(f"{assunto['titulo']} > {opcao['titulo']}: seção {opcao['secao']} não existe no guia")
            if opcao.get("pergunta_frequente") and not frequente:
                problemas.append(f"{assunto['titulo']} > {opcao['titulo']}: pergunta frequente "
                                 f"{opcao['pergunta_frequente']!r} não existe na seção 16 do guia")
            if not secao and not frequente:
                continue  # sem nada para mostrar: o botão não aparece
            fonte = frequente or secao
            opcoes.append({
                "titulo": opcao["titulo"],
                "resposta": _resposta_da_frequente(frequente) if frequente else secao["texto"],
                "fonte": fonte["id"],
                "passo_a_passo": ({"id": secao["id"], "titulo": secao["titulo"], "texto": secao["texto"]}
                                  if secao and frequente else None),
                "links": links_da_secao(secao or frequente),
            })
        if opcoes:
            assuntos.append(campos | {"opcoes": opcoes})
    for p in problemas:
        print(f"[guia] menu.json: {p}", file=sys.stderr)
    return {"assuntos": assuntos, "problemas": problemas}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    if sys.argv[1] == "indexar":
        indexar()
        sys.exit(0)
    if sys.argv[1] == "menu":
        # Confere o menu.json contra o guia: o que cada botão mostra e o que está quebrado.
        m = menu({"rede"})
        for a in m["assuntos"]:
            print(f"{a['icone']} {a['titulo']}" + ("" if a["opcoes"] else "  (pergunta livre: só marca o assunto)"))
            for o in a["opcoes"]:
                extra = f" + passo a passo ({o['passo_a_passo']['id']})" if o["passo_a_passo"] else ""
                print(f"   [{o['titulo']}] -> {o['fonte']}{extra}: {o['resposta'][:70]}...")
        print(f"\n{len(m['problemas'])} problema(s)" + "".join(f"\n  - {p}" for p in m["problemas"]))
        sys.exit(1 if m["problemas"] else 0)
    if sys.argv[1] == "vocabulario":
        # Confere o vocabulario.json: um JSON quebrado para aqui, com a linha do erro.
        json.loads(ARQUIVO_VOCABULARIO.read_text(encoding="utf-8"))
        for nome, entradas in vocabulario().items():
            print(f"{nome} ({len(entradas)}): " + ", ".join(
                {"prefixo": f"{t}*", "expressao": f'"{t}"', "palavra": t}[tipo] for tipo, t in entradas))
        sys.exit(0)
    pergunta = " ".join(sys.argv[1:])
    for i, s in enumerate(ranking(pergunta)[:3]):
        marca = "->" if i == 0 and passa(pergunta, s["similaridade"]) else "  "
        print(f"{marca} {s['similaridade']:.3f}  {s['id']}: {s['titulo']}\n      pedaço: {s['pedaco'][:150]}")
    bloco = assunto(pergunta)
    limiar = LIMIAR if bloco else LIMIAR_SEM_ASSUNTO
    print(f"\n(fala do assunto do guia: {'sim, bloco ' + repr(bloco) if bloco else 'não'}; limiar {limiar}. "
          "Só a seção marcada com -> vai para o modelo)")
