"""Rede credenciada do IPASEM: quem atende cada especialidade, e onde.

Como a tabela de procedimentos, a rede não entra na busca por significado: são dados
estruturados (especialidade, cidade, bairro, telefone), e o modelo consulta por uma ferramenta
(tool calling). Ele traduz o pedido para uma especialidade ("dor nas costas" -> ortopedia) e
passa cidade e bairro se a pessoa disser.

"Mais próximo" é por bairro e cidade: o arquivo não tem coordenadas, e calcular distância
exigiria um serviço de mapas externo (os dados sairiam da máquina).

O arquivo (prestadores.json, exportado do sistema do IPASEM) fica FORA do Git: a rede muda o
tempo todo e tem nomes, endereços e celulares de profissionais. Sem ele, a ferramenta não é
oferecida ao modelo e o resto do chatbot funciona igual.

Consultar no terminal:
    python rede.py ortopedia
    python rede.py psicologia "novo hamburgo" canudos
"""

import json
import re
import sys
import time
from pathlib import Path

from tabela import normalizar as _sem_acento_nem_pontuacao

ARQUIVO = Path(__file__).parent / "prestadores.json"
MAX_PRESTADORES = 5  # na resposta; mais que isso o modelo de 4B se perde

# Nome popular -> palavra da especialidade, quando o começo da palavra não basta
# ("dentista" não começa como "odontologia").
SINONIMOS = {
    "dentista": "odontologia", "dente": "odontologia", "dentes": "odontologia",
    "nutricionista": "nutricao", "laboratorio": "analises clinicas", "sangue": "analises clinicas",
    "clinico": "clinica geral", "medico de familia": "clinica geral",
    "ultrassom": "ecografia", "ultrassonografia": "ecografia", "psicoterapia": "psicologia",
    "pronto socorro": "pronto atendimento", "emergencia": "pronto atendimento",
    "fono": "fonoaudiologia", "orto": "ortopedia", "eco": "ecografia",
}
# Grafias diferentes do mesmo lugar no arquivo.
APELIDOS_LUGAR = {"h velho": "hamburgo velho", "maua": "jardim maua", "nh": "novo hamburgo"}
PALAVRAS_VAZIAS = {"de", "da", "do", "das", "dos", "e", "em", "no", "na", "para", "com", "o", "a", "exame", "exames"}

AVISO = ("A rede credenciada muda com frequência (prestadores entram e saem): confirme por telefone "
         "antes de ir. 'Mais próximo' considera bairro e cidade, não a distância.")


def normalizar(texto: str | None) -> str:
    texto = " ".join(_sem_acento_nem_pontuacao(texto or "").split())
    return APELIDOS_LUGAR.get(texto, texto)


_dados: dict | None = None


def carregar() -> dict | None:
    global _dados
    if _dados is None and ARQUIVO.exists():
        linhas = json.loads(ARQUIVO.read_text(encoding="utf-8"))["rows"]
        for r in linhas:
            r["_especialidade"] = normalizar(r["especialidade"]).split()
            r["_cidade"] = normalizar(r["cidade_coml1"])
            r["_bairro"] = normalizar(r["bairro_coml1"])
        _dados = {"linhas": linhas, "dados_de": time.strftime("%d/%m/%Y", time.localtime(ARQUIVO.stat().st_mtime)),
                  "especialidades": sorted({r["especialidade"] for r in linhas})}
    return _dados


def disponivel() -> bool:
    return ARQUIVO.exists()


def cidade_citada(texto: str) -> str | None:
    """A cidade da rede que aparece no texto ("clinico geral novo hamburgo" -> "NOVO HAMBURGO").
    Serve para entender pedido curto ("especialidade + cidade" é pedido de quem atende ali) e para
    lembrar a cidade que a pessoa já disse na conversa, sem perguntar de novo."""
    dados = carregar()
    if dados is None:
        return None
    texto = f" {normalizar(texto)} "
    for apelido, nome in APELIDOS_LUGAR.items():
        texto = texto.replace(f" {apelido} ", f" {nome} ")
    cidades = {r["_cidade"]: r["cidade_coml1"] for r in dados["linhas"]}
    for chave in sorted(cidades, key=len, reverse=True):  # "sao leopoldo" antes de "leopoldo"
        if f" {chave} " in texto:
            return cidades[chave].title()
    return None


def _casa(termo: str, palavra: str) -> bool:
    """Mesmo começo de palavra: 'cardiologista' ~ 'cardiologia', 'pediatra' ~ 'pediatria'."""
    if len(termo) < 5:
        return palavra == termo
    return palavra.startswith(termo[: max(5, len(termo) - 4)])


def achar_especialidades(termo: str) -> list[str]:
    """As especialidades que melhor casam com o termo (podem empatar: 'gastro' -> duas)."""
    texto = normalizar(termo)
    texto = SINONIMOS.get(texto, " ".join(SINONIMOS.get(p, p) for p in texto.split()))
    palavras = [p for p in texto.split() if p not in PALAVRAS_VAZIAS]
    if not palavras:
        return []
    notas = {}
    for nome in carregar()["especialidades"]:
        partes = normalizar(nome).split()
        casadas = sum(any(_casa(p, w) for w in partes) for p in palavras)
        if casadas == len(palavras):  # todas as palavras do termo precisam casar
            notas[nome] = casadas / len(partes)  # menos palavras sobrando = mais específica
    melhor = max(notas.values(), default=0)
    return sorted(n for n, nota in notas.items() if nota == melhor)


def _telefones(r: dict) -> list[str]:
    vistos = []
    for t in (r["telefone_coml1a"], r["telefone_coml1b"], r["telefone_cel"]):
        if t and t not in vistos:
            vistos.append(t.strip())
    return vistos


def consultar_rede(especialidade: str, cidade: str | None = None, bairro: str | None = None) -> dict:
    """Procura na rede credenciada do IPASEM quem atende uma especialidade, e onde.

    Args:
        especialidade: especialidade ou tipo de atendimento (ex.: "ortopedia", "cardiologia",
            "psicologia", "pediatria", "analises clinicas" para exame de sangue, "odontologia").
        cidade: cidade da pessoa, só se ela disser (ex.: "Novo Hamburgo", "São Leopoldo").
        bairro: bairro da pessoa, só se ela disser (ex.: "Centro", "Canudos").
    """
    dados = carregar()
    if dados is None:
        return {"erro": "rede credenciada indisponível", "total_encontrado": 0}
    nomes = achar_especialidades(especialidade)
    if normalizar(especialidade).startswith("hospita"):
        # "Hospital" não é especialidade: filtra pelo tipo de prestador (HP no cadastro).
        nomes = sorted({r["especialidade"] for r in dados["linhas"] if r["tipo_prestador"] == "HP"})
        linhas_hp = [r for r in dados["linhas"] if r["tipo_prestador"] == "HP"]
    else:
        linhas_hp = None
    if not nomes:
        return {"total_encontrado": 0, "especialidade_pedida": especialidade,
                "mensagem": "Especialidade não encontrada. Tente um destes nomes.",
                "especialidades_da_rede": dados["especialidades"]}
    linhas = linhas_hp if linhas_hp is not None else [r for r in dados["linhas"] if r["especialidade"] in nomes]
    if linhas_hp is not None:
        nomes = ["HOSPITAIS"]

    # Um prestador por registro (o mesmo profissional aparece uma vez por especialidade).
    unicos: dict[int, dict] = {}
    for r in linhas:
        unicos.setdefault(r["registro"], r)
    por_cidade: dict[str, int] = {}
    for r in unicos.values():
        por_cidade[r["cidade_coml1"]] = por_cidade.get(r["cidade_coml1"], 0) + 1

    resultado = {"especialidades": nomes, "total_encontrado": len(unicos),
                 "por_cidade": dict(sorted(por_cidade.items(), key=lambda par: -par[1])[:8]),
                 "dados_de": dados["dados_de"], "aviso": AVISO}
    if not cidade and not bairro:
        resultado["pedir_local"] = True  # sem lista: o modelo pede cidade e bairro
        return resultado

    alvo_cidade, alvo_bairro = normalizar(cidade), normalizar(bairro)
    if not alvo_cidade:
        # Bairro sem cidade: quase toda a rede e quem pergunta são de Novo Hamburgo.
        alvo_cidade = "novo hamburgo"
        resultado["cidade_assumida"] = "Novo Hamburgo"
    na_cidade = [r for r in unicos.values()
                 if r["_cidade"].startswith(alvo_cidade) or alvo_cidade.startswith(r["_cidade"])]
    mesmo_bairro = [r for r in na_cidade if alvo_bairro and (alvo_bairro in r["_bairro"] or r["_bairro"] in alvo_bairro)]
    ordem = mesmo_bairro + [r for r in na_cidade if r not in mesmo_bairro]
    cartoes = [_cartao(r) for r in ordem[:MAX_NA_TELA]]
    resultado.update({
        "local_pedido": {"cidade": cidade or resultado.get("cidade_assumida"), "bairro": bairro},
        "no_bairro": len(mesmo_bairro), "na_cidade": len(na_cidade),
        "prestadores": cartoes[:MAX_PRESTADORES],  # o que o modelo vê (nomes e bairros)
        # Todos os da cidade, só para a tela ("Ver todos", filtro por bairro). Começa com "_":
        # rag.py tira antes de mandar o resultado ao modelo.
        "_todos": cartoes,
    })
    return resultado


MAX_NA_TELA = 300  # psicologia em Novo Hamburgo tem bem mais que 5; a tela mostra 5 e "Ver todos"


def _cartao(r: dict) -> dict:
    return {
        "nome": r["nome_prestador"],
        "local": r["nome_vinculo"] if r["nome_vinculo"] and r["nome_vinculo"] != r["nome_prestador"] else None,
        "especialidade": r["especialidade"],
        "endereco": r["endereco_coml1"], "bairro": r["bairro_coml1"], "cidade": r["cidade_coml1"],
        # O mesmo bairro aparece com grafias diferentes ("H VELHO", "MAUA", "JARDIM MAUÁ"): o filtro
        # por bairro da página agrupa por esta forma normalizada.
        "bairro_id": r["_bairro"],
        "telefones": _telefones(r),
    }


# --- Para os botões da página (sem modelo) ---------------------------------------------------

# Busca pelo nome do profissional ("quero o doutor Fernando"): a ferramenta do modelo só procura
# por especialidade e lugar, e a reescrita transformou esse pedido em "quem atende análises
# clínicas em Novo Hamburgo?" (assunto da troca anterior). Busca direta no cadastro, sem modelo.
_NAO_E_NOME = {"de", "da", "do", "das", "dos", "e"}


def bonito(texto: str) -> str:
    """"ORTOPEDIA E TRAUMATOLOGIA" -> "Ortopedia e Traumatologia" (o cadastro vem em maiúsculas)."""
    minusculas = {"e", "de", "da", "do", "das", "dos", "em"}
    return " ".join(p if i and p in minusculas else p.capitalize() for i, p in enumerate(texto.lower().split()))


def buscar_nome(nome: str, cidade_preferida: str | None = None) -> dict:
    """Prestadores cujo nome tem todas as palavras pedidas (cada uma como começo de uma palavra do
    nome: "fern" acha "FERNANDO"). Os da cidade preferida vêm primeiro."""
    dados = carregar()
    termos = [t for t in normalizar(nome).split() if t not in _NAO_E_NOME and len(t) >= 2]
    resultado = {"dados_de": dados["dados_de"] if dados else "", "aviso": AVISO, "especialidades": [],
                 "por_cidade": {}, "prestadores": [], "titulo": f"Profissionais com o nome “{nome}”"}
    if dados is None or not termos:
        return resultado
    unicos: dict = {}
    especialidades_do: dict = {}
    for r in dados["linhas"]:
        palavras = normalizar(r["nome_prestador"]).split()
        if all(any(p.startswith(t) for p in palavras) for t in termos):
            unicos.setdefault(r["registro"], r)
            especialidades_do.setdefault(r["registro"], []).append(r["especialidade"])
    alvo = normalizar(cidade_preferida) if cidade_preferida else None
    ordem = sorted(unicos.items(), key=lambda item: (item[1]["_cidade"] != alvo, item[1]["nome_prestador"]))
    for registro, r in ordem[:MAX_NA_TELA]:
        cartao = _cartao(r)
        cartao["especialidades"] = list(dict.fromkeys(especialidades_do[registro]))
        cartao["especialidade"] = ", ".join(cartao["especialidades"])
        cartao["cidade_id"] = r["_cidade"]
        resultado["prestadores"].append(cartao)
    return resultado


# Busca pela página: cidade obrigatória, especialidade opcional. A cidade é identificada pela forma
# normalizada ("sao leopoldo"), porque o cadastro tem a mesma cidade com e sem acento.
MAX_NA_CIDADE = 1000  # Novo Hamburgo tem 682 prestadores; a tela mostra 5 e "Ver todos"


def cidades() -> list[dict]:
    """Todas as cidades da rede, com quantos prestadores (únicos) em cada, da maior para a menor.
    "id" é a forma normalizada; "nome", a grafia do cadastro (a acentuada, se houver)."""
    dados = carregar()
    if dados is None:
        return []
    regs: dict[str, set] = {}
    grafias: dict[str, set] = {}
    for r in dados["linhas"]:
        regs.setdefault(r["_cidade"], set()).add(r["registro"])
        grafias.setdefault(r["_cidade"], set()).add(r["cidade_coml1"])
    def nome(c):  # "SÃO LEOPOLDO" no lugar de "SAO LEOPOLDO"
        return max(grafias[c], key=lambda g: (g != g.encode("ascii", "ignore").decode(), g))
    return sorted(({"id": c, "nome": nome(c), "total": len(v)} for c, v in regs.items()),
                  key=lambda c: (-c["total"], c["id"]))


def especialidades(cidade: str | None = None) -> list[dict]:
    """As especialidades (de uma cidade, ou de toda a rede), com quantos prestadores, em ordem
    alfabética (a página tem pesquisa; a ordem do alfabeto é a mais fácil de percorrer)."""
    dados = carregar()
    if dados is None:
        return []
    alvo = normalizar(cidade) if cidade else None
    por_nome: dict[str, set] = {}
    for r in dados["linhas"]:
        if alvo is None or r["_cidade"] == alvo:
            por_nome.setdefault(r["especialidade"], set()).add(r["registro"])
    return sorted(({"nome": n, "total": len(v)} for n, v in por_nome.items()), key=lambda e: e["nome"])


def lista(cidade: str, especialidade: str | None = None, destacar: str | None = None) -> dict | None:
    """Os prestadores de uma cidade (e de uma especialidade, se vier), para a tela, sem o modelo.
    Sem especialidade, cada prestador aparece uma vez, com todas as especialidades dele.
    destacar: um nome ("fernando"); quem tem esse nome vem primeiro e marcado (busca por nome)."""
    dados = carregar()
    if dados is None:
        return None
    alvo = normalizar(cidade)
    unicos: dict = {}
    especialidades_do: dict = {}
    for r in dados["linhas"]:
        if r["_cidade"] != alvo or (especialidade and r["especialidade"] != especialidade):
            continue
        unicos.setdefault(r["registro"], r)
        especialidades_do.setdefault(r["registro"], []).append(r["especialidade"])
    if not unicos:
        return None
    termos = [x for x in normalizar(destacar or "").split() if x not in _NAO_E_NOME and len(x) >= 2]
    def tem_nome(r):
        palavras = normalizar(r["nome_prestador"]).split()
        return bool(termos) and all(any(p.startswith(x) for p in palavras) for x in termos)
    cartoes = []
    for registro, r in sorted(unicos.items(), key=lambda item: not tem_nome(item[1]))[:MAX_NA_CIDADE]:
        cartao = _cartao(r)
        cartao["especialidade"] = ", ".join(dict.fromkeys(especialidades_do[registro]))
        cartao["destaque"] = tem_nome(r)
        cartoes.append(cartao)
    nome_cidade = next(c["nome"] for c in cidades() if c["id"] == alvo)
    return {"dados_de": dados["dados_de"], "aviso": AVISO, "cidade": nome_cidade,
            "especialidades": [especialidade] if especialidade else [],
            "por_cidade": {}, "prestadores": cartoes}


def resultado_em_texto(argumentos: dict, resultado: dict) -> str:
    """Texto corrido para o modelo de resposta (ele lê melhor que JSON, como na tabela).

    Vai SEM endereço e telefone: o modelo de 4B copiava errado ("Rua Cideira" no lugar de
    "Cidreira") e omitia telefones. Os contatos aparecem na tela direto do cadastro (para_tela)."""
    linhas = [f"Resultado da consulta à rede credenciada (pedido: {argumentos}; dados de "
              f"{resultado.get('dados_de', '?')}):"]
    if "especialidades_da_rede" in resultado or not resultado.get("total_encontrado"):
        linhas.append("Nenhum prestador encontrado para essa especialidade.")
        return "\n".join(linhas)
    # Sem a lista de cidades: com ela, o modelo ainda "completava" com cidade que não estava lá
    # (Caxias do Sul para dermatologia). As cidades aparecem na tela, direto do cadastro.
    linhas.append(f"Especialidade: {', '.join(resultado['especialidades'])}. "
                  f"{resultado['total_encontrado']} prestador(es) na rede, em "
                  f"{len(resultado['por_cidade'])} cidade(s) (a lista de cidades aparece na tela).")
    if resultado.get("pedir_local"):
        linhas.append("A pessoa não disse onde mora: não há lista. Peça a cidade e o bairro para "
                      "indicar os mais próximos.")
    else:
        local = resultado["local_pedido"]
        if resultado.get("cidade_da_conversa"):
            linhas.append(f"A pessoa disse antes, na conversa, que é de {resultado['cidade_da_conversa']}.")
        elif resultado.get("cidade_assumida"):
            linhas.append(f"Cidade não informada; considerada {resultado['cidade_assumida']}. Mostre "
                          "a lista e diga que, se a pessoa for de outra cidade, é só dizer qual.")
        if local["bairro"]:
            linhas.append(f"No local pedido ({local['bairro']}, {local['cidade']}): "
                          f"{resultado['no_bairro']} no bairro, {resultado['na_cidade']} na cidade.")
        else:
            # Sem bairro, "0 no bairro" fazia o modelo escrever "nenhum no bairro que você pediu";
            # e avisar "a pessoa não disse o bairro" fazia ele falar só disso (medido nos dois).
            linhas.append(f"Em {local['cidade']}: {resultado['na_cidade']} prestador(es).")
        if not resultado["prestadores"]:
            linhas.append("Nenhum prestador nessa cidade. As cidades onde há aparecem na tela.")
        for p in resultado["prestadores"]:
            onde = f" ({p['local']})" if p["local"] else ""
            linhas.append(f"- {p['nome']}{onde}, {p['especialidade']}: bairro {p['bairro']}, {p['cidade']}.")
    linhas.append(f"Aviso: {resultado['aviso']}")
    linhas.append(INSTRUCOES)
    return "\n".join(linhas)


# Vão junto com o resultado, e não no prompt fixo da resposta: assim as perguntas que não usam
# a rede recebem exatamente o mesmo prompt de antes (e, com a semente fixa, a mesma resposta).
INSTRUCOES = """Como usar este resultado da rede credenciada:
- Responda só sobre a rede (quem atende e onde). Não acrescente regras do regulamento, limites \
de consultas nem valores da tabela, a menos que a pessoa tenha perguntado.
- Se a pessoa contou um sintoma, diga qual especialidade procurar e deixe claro que é uma \
indicação, não um diagnóstico: quem avalia é o profissional.
- Sem lista (a pessoa não disse onde mora): diga quantos prestadores há e peça a cidade e o \
bairro para indicar os mais próximos.
- Não cite cidades além da que a pessoa disse: as cidades onde há prestador aparecem na tela.
- Com lista: diga quantos há no bairro e na cidade e cite os nomes, os do bairro pedido \
primeiro. NÃO escreva endereço nem telefone: a lista com os contatos aparece na tela, logo \
abaixo da sua resposta, direto do cadastro. Diga isso à pessoa.
- Cite só nomes e cidades que estão neste resultado. Não invente prestador. Avise que a rede muda e que é bom confirmar por telefone. Cite como \
(Rede credenciada, dados de DD/MM/AAAA)."""

# Pedido de atendimento sem especialidade ("quero marcar uma consulta"): não há o que buscar na
# rede, e o modelo enchia a resposta com "o valor não está nos documentos". Vai junto com o
# pedido, como as INSTRUCOES, para não mudar o prompt das outras perguntas.
INSTRUCOES_SEM_ESPECIALIDADE = """A pessoa quer ser atendida, mas não disse a especialidade:
- Se o guia do app e do site ou os artigos dizem como marcar (a autorização), diga em no máximo \
2 frases.
- Depois pergunte a especialidade (ou o que ela está sentindo) para mostrar na tela os contatos \
da rede credenciada.
- Não fale de valores, códigos nem da tabela de procedimentos."""


def para_tela(resultado: dict) -> dict | None:
    """Os fatos da rede para a página mostrar como vieram do cadastro: onde há prestador (por
    cidade) e, se a pessoa disse onde mora, os contatos."""
    if not resultado.get("total_encontrado") or "por_cidade" not in resultado:
        return None
    return {"dados_de": resultado["dados_de"], "aviso": resultado["aviso"],
            "especialidades": resultado["especialidades"], "por_cidade": resultado["por_cidade"],
            "prestadores": resultado.get("_todos") or resultado.get("prestadores", [])}


def resumo(resultado: dict) -> list[str]:
    """Versão curta para a tela."""
    if not resultado.get("total_encontrado"):
        return []
    linhas = [f"{', '.join(resultado['especialidades'])}: {resultado['total_encontrado']} na rede · "
              + ", ".join(f"{c} {n}" for c, n in list(resultado["por_cidade"].items())[:4])]
    for p in resultado.get("prestadores", []):
        linhas.append(f"{p['nome']} · {p['bairro']}, {p['cidade']} · {' / '.join(p['telefones'])}")
    return linhas


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    r = consultar_rede(*sys.argv[1:4])
    print(resultado_em_texto(dict(zip(("especialidade", "cidade", "bairro"), sys.argv[1:4])), r))
