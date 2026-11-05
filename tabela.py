"""Tabela única de procedimentos (Anexo I da Resolução nº 33/2024): extração e consulta.

A tabela não entra na busca por significado (RAG). Preço e código precisam vir exatos,
então ela vira dados estruturados, e o modelo consulta por uma ferramenta
(tool calling): ele decide quando chamar e com qual termo.

Os valores são atualizados todo ano por Ordem de Serviço (Art. 3º da Res. 33/2024), que
republica o Anexo I inteiro. A versão em uso é a da OS nº 01/2025. Na próxima atualização:
trocar FONTE, FONTE_CITACAO e VIGENCIA, conferir as páginas em SECOES pelo sumário e preparar
de novo.

Preparar (uma vez):
    python tabela.py preparar docs/fontes/tabela_unica_os_01_2025.pdf

Consultar no terminal:
    python tabela.py "implante dentario"
    python tabela.py 30912105
"""

import json
import re
import sys
import unicodedata
from pathlib import Path

from pypdf import PdfReader

ARQUIVO = Path(__file__).parent / "dados" / "tabela_procedimentos.json"

# De onde vêm os valores (vai no aviso, no prompt e na citação da resposta).
FONTE = "Anexo I da Resolução nº 33/2024, atualizado pela Ordem de Serviço IPASEM nº 01/2025"
FONTE_CITACAO = "Tabela única, OS 01/2025"
VIGENCIA = "01/11/2025"

# Seções do Anexo I, pelo sumário da p. 3 (páginas do PDF da OS 01/2025: a OS ocupa as
# p. 1-2, então cada seção está uma página à frente do PDF da Res. 33/2024).
# "linhas": dá para extrair código + valor; "texto": layout livre, vira bloco de texto.
SECOES = [
    {"nome": "Pacotes de hemodinâmica", "paginas": (4, 5), "tipo": "texto"},
    {"nome": "Procedimentos médicos (honorários)", "paginas": (6, 92), "tipo": "linhas", "unidade": "CH"},
    {"nome": "Exames diversos", "paginas": (93, 131), "tipo": "linhas", "unidade": "R$"},
    {"nome": "Pacotes diversos (partos, hemoterapia, bariátrica, oftalmologia...)", "paginas": (132, 132), "tipo": "texto"},
    {"nome": "Honorários de hemodinâmica, cardiologia e radiologia intervencionista", "paginas": (133, 138), "tipo": "texto"},
    {"nome": "Materiais, medicamentos e dietas hospitalares", "paginas": (139, 162), "tipo": "linhas", "unidade": "R$"},
    {"nome": "Honorários odontológicos", "paginas": (163, 165), "tipo": "linhas", "unidade": "R$"},
]

AVISO = (
    f"Valores da tabela única do IPASEM ({FONTE}), vigentes desde {VIGENCIA}: é o que o IPASEM "
    "paga ao prestador, não o que o beneficiário paga. A coparticipação só aparece quando há nota "
    "dizendo isso. A tabela é atualizada todo ano por ordem de serviço (Art. 3º da Res. 33/2024): "
    "confira no site do IPASEM se já há valores mais novos."
)

RE_CODIGO = re.compile(r"^(\d{3,8})(?:\s+(.+))?$")  # código, às vezes sozinho na linha
RE_SO_NUMEROS = re.compile(r"[\d.,\s]+")
RE_REAIS_FINAL = re.compile(r"R\$\s*([\d.]+,\d{2})\s*(\d+)?\s*$")  # valor (+ sessões/ano)
RE_REAIS = re.compile(r"R\$\s*([\d.]+,\d{2})")
RE_CH_FINAL = re.compile(r"\s(\d+(?:,\d+)?)((?:\s+\d+){0,2})\s*$")  # CH (+ aux, porte)
RE_VALOR_CH = re.compile(r"valor da CH.*?R\$\s*([\d.]+,\d{2})", re.IGNORECASE)
RODAPE = ("Instituto de Previd", "Rua Júlio", "Rua Cinco", "Fone/Fax")
CABECALHO_COLUNAS = re.compile(r"^(C[ÓO]DIGO|Ref\b|CODIGO)", re.IGNORECASE)


def numero(texto: str) -> float:
    return float(texto.replace(".", "").replace(",", "."))


def normalizar(texto: str) -> str:
    """minúsculas, sem acento e sem pontuação: 'Implante Dentário' -> 'implante dentario'."""
    sem_acento = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9 ]", " ", sem_acento.lower())


# --- Extração -----------------------------------------------------------------


def linhas_da_pagina(leitor: PdfReader, n: int) -> list[str]:
    texto = leitor.pages[n - 1].extract_text() or ""
    return [l.strip() for l in texto.splitlines() if l.strip() and not l.strip().startswith(RODAPE)]


def interpretar_valor(texto: str, unidade: str) -> dict | None:
    """Acha o valor no fim da linha. Devolve None se a linha ainda não terminou."""
    if m := RE_REAIS_FINAL.search(texto):
        valor = {"reais": numero(m.group(1))}
        if m.group(2):
            valor["sessoes_por_ano"] = int(m.group(2))
        return valor
    if unidade == "CH" and (m := RE_CH_FINAL.search(texto)):
        extras = m.group(2).split()
        valor = {"ch": numero(m.group(1))}
        if extras:
            valor["auxiliares"] = int(extras[0])
        if len(extras) > 1:
            valor["porte_anestesico"] = int(extras[1])
        return valor
    if unidade == "R$" and (achados := RE_REAIS.findall(texto)):
        return {"reais": numero(achados[-1])}
    return None


def extrair_linhas(leitor: PdfReader, secao: dict) -> tuple[list[dict], dict[str, list[str]]]:
    itens: list[dict] = []
    notas: dict[str, list[str]] = {}  # grupo -> notas de rodapé
    grupo = ""
    atual: dict | None = None
    ultimo_foi_nota = False

    def fechar():
        nonlocal atual, grupo
        if atual is None:
            return
        texto = re.sub(r"\s+", " ", atual["bruto"]).strip()
        valor = interpretar_valor(texto, secao["unidade"])
        if valor is None:
            # Linha com código mas sem valor (ex.: "2300 RESINAS") é título de grupo.
            grupo = texto
        else:
            descricao = RE_REAIS.split(texto)[0] if "reais" in valor else RE_CH_FINAL.split(texto)[0]
            itens.append({
                "codigo": atual["codigo"], "descricao": descricao.strip(" -"),
                "secao": secao["nome"], "grupo": grupo, "pagina": atual["pagina"], **valor,
            })
        atual = None

    for n in range(secao["paginas"][0], secao["paginas"][1] + 1):
        for linha in linhas_da_pagina(leitor, n):
            if linha.startswith("*"):
                fechar()
                notas.setdefault(grupo, []).append(linha.lstrip("* "))
                ultimo_foi_nota = True
                continue
            incompleto = atual is not None and interpretar_valor(
                re.sub(r"\s+", " ", atual["bruto"]), secao["unidade"]) is None
            so_numeros = RE_SO_NUMEROS.fullmatch(linha) is not None
            # "2400 2 7" logo depois de uma descrição sem valor é o CH da linha de cima,
            # não um código novo.
            if (m := RE_CODIGO.match(linha)) and not (incompleto and so_numeros):
                fechar()
                atual = {"codigo": m.group(1), "bruto": m.group(2) or "", "pagina": n}
                ultimo_foi_nota = False
                continue
            if incompleto:
                atual["bruto"] += " " + linha  # descrição ou valor na linha de baixo
                continue
            fechar()
            if ultimo_foi_nota and notas.get(grupo) and linha[:1].islower():
                notas[grupo][-1] += " " + linha  # nota que continua na linha de baixo
            elif linha.startswith("R$") or so_numeros or CABECALHO_COLUNAS.match(linha) or RE_VALOR_CH.search(linha):
                pass  # valor solto, número solto ou cabeçalho de colunas: ignora
            else:
                # Título de grupo (ex.: "Uti CH AUX PA", "MEDICINA NUCLEAR", "Pênis").
                grupo = re.sub(r"\s+(CH|AUX|PA|Sess[õo]es/Ano)\b.*$", "", linha).strip()
                ultimo_foi_nota = False
    fechar()
    return itens, notas


def preparar(caminho_pdf: Path) -> None:
    leitor = PdfReader(caminho_pdf)
    valor_ch = None
    # O valor da CH vem escrito no alto dos honorários médicos (p. 6 na OS 01/2025).
    if m := RE_VALOR_CH.search(leitor.pages[SECOES[1]["paginas"][0] - 1].extract_text() or ""):
        valor_ch = numero(m.group(1))

    itens, notas, blocos = [], {}, []
    for secao in SECOES:
        if secao["tipo"] == "linhas":
            novos, novas_notas = extrair_linhas(leitor, secao)
            itens.extend(novos)
            for grupo, lista in novas_notas.items():
                notas[f"{secao['nome']}|{grupo}"] = lista
        else:
            for n in range(secao["paginas"][0], secao["paginas"][1] + 1):
                texto = re.sub(r"[ \t]+", " ", "\n".join(linhas_da_pagina(leitor, n)))
                blocos.append({"secao": secao["nome"], "pagina": n, "texto": texto})

    dados = {"fonte": FONTE, "vigencia": VIGENCIA, "valor_ch_reais": valor_ch,
             "itens": itens, "notas": notas, "blocos": blocos}
    ARQUIVO.parent.mkdir(exist_ok=True)
    ARQUIVO.write_text(json.dumps(dados, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"Valor da CH: R$ {valor_ch}")
    print(f"Itens com código e valor: {len(itens)}")
    for secao in SECOES:
        if secao["tipo"] == "linhas":
            print(f"  {secao['nome']}: {sum(1 for i in itens if i['secao'] == secao['nome'])}")
    print(f"Grupos com notas: {len(notas)} | Blocos de texto (pacotes): {len(blocos)}")


# --- Consulta (a ferramenta) ----------------------------------------------------

PALAVRAS_VAZIAS = {"de", "da", "do", "das", "dos", "e", "com", "para", "por", "o", "a", "os", "as", "em", "no", "na"}
_dados: dict | None = None


def carregar() -> dict:
    global _dados
    if _dados is None:
        _dados = json.loads(ARQUIVO.read_text(encoding="utf-8"))
        for item in _dados["itens"]:
            item["_descricao"] = normalizar(item["descricao"]).split()
            item["_grupo"] = normalizar(item["grupo"]).split()
    return _dados


def casa(termo: str, palavras: list[str]) -> bool:
    """Igual ou com o mesmo começo (aceita plural e variações: implantes ~ implante).
    A raiz tem pelo menos 5 letras, para "parto" não casar com "partes"."""
    raiz = termo[: max(5, len(termo) - 2)]
    return any(p == termo or (len(termo) >= 4 and p.startswith(raiz)) for p in palavras)


LIMITE_RESULTADOS = 8


def consultar_tabela(termo: str) -> dict:
    """Procura procedimentos, exames, materiais ou pacotes na tabela única do IPASEM
    (Resolução 33/2024) e devolve código, descrição, valor e notas de cada um.

    Args:
        termo: nome do procedimento ou código. Use o nome técnico quando souber
            (ex.: "facectomia" para cirurgia de catarata, "endodontia" para
            tratamento de canal do dente, "polissonografia", "implante dentario",
            "tomografia", "parto").
    """
    dados = carregar()
    if (codigo := termo.strip()).isdigit():
        achados = [i for i in dados["itens"] if i["codigo"] == codigo]
    else:
        termos = [t for t in normalizar(termo).split() if t not in PALAVRAS_VAZIAS]
        if not termos:
            return {"aviso": AVISO, "resultados": [], "mensagem": "Termo vazio."}
        pontuados = []
        for item in dados["itens"]:
            # Termo achado na descrição vale 2; só no nome do grupo vale 1.
            na_descricao = sum(casa(t, item["_descricao"]) for t in termos)
            no_grupo = sum(casa(t, item["_grupo"]) and not casa(t, item["_descricao"]) for t in termos)
            acertos = na_descricao + no_grupo
            if acertos == len(termos) or (len(termos) > 2 and acertos >= len(termos) - 1):
                pontuados.append((acertos, 2 * na_descricao + no_grupo, -len(item["descricao"]), item))
        pontuados.sort(key=lambda x: x[:3], reverse=True)
        achados = [p[3] for p in pontuados]

    resultados, notas = [], {}
    for item in achados[:LIMITE_RESULTADOS]:
        r = {c: v for c, v in item.items() if not c.startswith("_")}
        if "ch" in r and dados["valor_ch_reais"]:
            r["reais_estimado"] = round(r["ch"] * dados["valor_ch_reais"], 2)
        # Notas de rodapé vão uma vez por grupo, não repetidas em cada item.
        if lista := dados["notas"].get(f"{item['secao']}|{item['grupo']}"):
            notas[item["grupo"]] = lista
        resultados.append(r)

    # Pacotes ficam em blocos de texto: devolve o trecho ao redor do termo.
    trechos = []
    termos_busca = [t for t in normalizar(termo).split() if t not in PALAVRAS_VAZIAS and len(t) >= 4]
    for bloco in dados["blocos"]:
        linhas = bloco["texto"].splitlines()
        for i, linha in enumerate(linhas):
            if termos_busca and all(casa(t, normalizar(linha).split()) for t in termos_busca):
                trechos.append({"secao": bloco["secao"], "pagina": bloco["pagina"],
                                "trecho": "\n".join(linhas[max(0, i - 1): i + 4])})
                break

    return {
        "aviso": AVISO,
        "valor_ch_reais": dados["valor_ch_reais"],
        "total_encontrado": len(achados),
        "resultados": resultados,
        "notas_por_grupo": notas,
        "pacotes": trechos[:3],
    }


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "preparar":
        preparar(Path(sys.argv[2]))
    elif len(sys.argv) >= 2:
        print(json.dumps(consultar_tabela(" ".join(sys.argv[1:])), ensure_ascii=False, indent=1))
    else:
        print(__doc__)
