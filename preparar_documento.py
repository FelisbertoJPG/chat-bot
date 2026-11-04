"""Etapa 1 do RAG: transformar o PDF do regulamento em trechos limpos, um por artigo.

O PDF é uma versão compilada: guarda a redação original de 2012 e todas as
alterações posteriores lado a lado. Este script:

1. lê o PDF página por página (guardando o número de cada página);
2. remove o cabeçalho que se repete em todas as páginas;
3. separa o texto em dispositivos (artigo, parágrafo, alínea);
4. descarta o que foi revogado ou substituído por redação mais nova;
5. agrupa o que sobrou por artigo;
6. monta uma "ficha do regulamento" com a visão geral e salva tudo em dados/trechos.json.

Tudo que é descartado vai para dados/removidos.json com o motivo, para auditoria.

Uso:
    python preparar_documento.py docs/fontes/regulamento.pdf
    python preparar_documento.py docs/fontes/regulamento.pdf --mostrar "Art. 12"
"""

import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from pypdf import PdfReader

PASTA_DADOS = Path(__file__).parent / "dados"

# --- Padrões de texto (expressões regulares) ---------------------------------

RE_ARTIGO = re.compile(r"^Art\.\s*(\d+)\s*º?\s*(?:-\s*([A-Z]))?(?=\s|$)")
RE_PARAGRAFO = re.compile(r"^(?:§\s*(\d+)\s*º|(Par[áa]grafo\s+[úu]n\s?ico))", re.IGNORECASE)
RE_ALINEA = re.compile(r"^([a-z])\)\s")
RE_TITULO = re.compile(r"^(T[ÍI]TULO|CAP[ÍI]TULO|SE[ÇC][ÃA]O)\s+([IVXLC]+)\s*$")
RE_FIM_DOCUMENTO = re.compile(r"^Novo Hamburgo,\s*\d")

RE_REVOGADO = re.compile(r"\(\s*Revogad[oa]", re.IGNORECASE)
RE_NOVA_REDACAO = re.compile(r"Reda\s?[çc][ãa]o\s+dada", re.IGNORECASE)
RE_RENUMERADO = re.compile(r"\(\s*Renumerad[oa]", re.IGNORECASE)


@dataclass
class Dispositivo:
    """Uma unidade do regulamento: caput de artigo, parágrafo ou alínea."""

    tipo: str  # "preambulo", "artigo", "paragrafo" ou "alinea"
    rotulo: str  # ex.: "Art. 12", "§1º", "Parágrafo único", "a)"
    artigo: str  # artigo ao qual pertence, ex.: "Art. 12"
    secao: str  # capítulo/seção onde está, para dar contexto
    pagina_inicio: int
    pagina_fim: int
    linhas: list[str] = field(default_factory=list)
    removido: str | None = None  # motivo do descarte, se houver

    @property
    def texto(self) -> str:
        return re.sub(r"\s+", " ", " ".join(self.linhas)).strip()


# --- 1 e 2: ler o PDF e tirar o cabeçalho -------------------------------------


def ler_linhas(caminho_pdf: Path) -> list[tuple[int, str]]:
    """Devolve (página, linha) para cada linha do PDF, sem o cabeçalho repetido.

    O cabeçalho é detectado por frequência: uma linha que aparece em mais da
    metade das páginas não é conteúdo, é cabeçalho ou rodapé.
    """
    paginas = [(p.extract_text() or "").splitlines() for p in PdfReader(caminho_pdf).pages]

    frequencia = Counter()
    for linhas in paginas:
        frequencia.update({l.strip() for l in linhas if l.strip()})
    repetidas = {l for l, n in frequencia.items() if n > len(paginas) / 2}

    resultado = []
    for numero, linhas in enumerate(paginas, 1):
        for linha in linhas:
            linha = linha.strip()
            if linha and linha not in repetidas:
                resultado.append((numero, linha))
    return resultado


def corrigir_linha(linha: str) -> str:
    """Conserta falhas da extração no início de artigos."""
    linha = re.sub(r"^Art\.\s*(\d+)\s+(\d)\s", r"Art. \1\2 ", linha)  # "Art. 5 7" -> "Art. 57"
    linha = re.sub(r"^Art\.\s*(\d+)\s*-\s*([A-Z])\b", r"Art. \1-\2", linha)  # "Art. 50 -A" -> "Art. 50-A"
    return linha


def eh_nome_de_titulo(linha: str) -> bool:
    """Linha toda em maiúsculas, sem números (ex.: "DA ASSISTÊNCIA MÉDICA")."""
    return linha == linha.upper() and re.search(r"[A-ZÀ-Ú]", linha) and not re.search(r"\d", linha)


# --- 3: separar em dispositivos -----------------------------------------------


def separar_dispositivos(linhas: list[tuple[int, str]]) -> list[Dispositivo]:
    dispositivos: list[Dispositivo] = []
    contexto = {"TÍTULO": "", "CAPÍTULO": "", "SEÇÃO": ""}
    titulo_pendente: tuple[str, str] | None = None  # (nível, numeral) aguardando o nome
    descartando_secao = False  # True depois de um título revogado
    artigo_atual = "Preâmbulo"

    atual = Dispositivo("preambulo", "Preâmbulo", "Preâmbulo", "", 1, 1)
    dispositivos.append(atual)

    def secao_atual() -> str:
        return " › ".join(v for v in contexto.values() if v)

    def aplicar_titulo(nivel: str, numeral: str, nome: str = "") -> None:
        contexto[nivel] = f"{nivel} {numeral}" + (f" – {nome}" if nome else "")
        # Um título novo zera os níveis abaixo dele.
        niveis = list(contexto)
        for abaixo in niveis[niveis.index(nivel) + 1 :]:
            contexto[abaixo] = ""

    def novo(tipo: str, rotulo: str, pagina: int) -> Dispositivo:
        d = Dispositivo(tipo, rotulo, artigo_atual, secao_atual(), pagina, pagina)
        if descartando_secao:
            d.removido = "seção revogada"
        dispositivos.append(d)
        return d

    for pagina, linha in linhas:
        linha = corrigir_linha(linha)

        if RE_FIM_DOCUMENTO.match(linha):  # local, data e assinatura
            break

        # Títulos: TÍTULO / CAPÍTULO / SEÇÃO + numeral romano.
        if m := RE_TITULO.match(linha):
            nivel = {"T": "TÍTULO", "C": "CAPÍTULO", "S": "SEÇÃO"}[m.group(1)[0]]
            if titulo_pendente:  # título anterior ficou sem nome (ex.: "CAPÍTULO V" renumerado)
                aplicar_titulo(*titulo_pendente)
            titulo_pendente = (nivel, m.group(2))
            descartando_secao = False
            atual = None
            continue

        # Linhas logo depois de um título: nome do título ou nota sobre ele.
        if atual is None and not (RE_ARTIGO.match(linha)):
            if RE_REVOGADO.search(linha):
                titulo_pendente = None  # título revogado não entra no contexto
                descartando_secao = True
            elif linha.startswith("("):
                pass  # nota como "(Acrescido pela Resolução...)" sobre o título
            elif titulo_pendente and eh_nome_de_titulo(linha):
                aplicar_titulo(*titulo_pendente, linha)
                titulo_pendente = None
            continue

        # Título sem numeral (ex.: "DISPOSIÇÕES GERAIS") no meio do texto.
        if eh_nome_de_titulo(linha) and len(linha) > 8 and not RE_ARTIGO.match(linha):
            contexto["CAPÍTULO"], contexto["SEÇÃO"] = linha, ""
            descartando_secao = False
            atual = None
            continue

        if titulo_pendente:
            aplicar_titulo(*titulo_pendente)
            titulo_pendente = None

        # Início de dispositivo?
        if m := RE_ARTIGO.match(linha):
            artigo_atual = f"Art. {m.group(1)}" + (f"-{m.group(2)}" if m.group(2) else "")
            atual = novo("artigo", artigo_atual, pagina)
        elif m := RE_PARAGRAFO.match(linha):
            rotulo = f"§{m.group(1)}º" if m.group(1) else "Parágrafo único"
            atual = novo("paragrafo", rotulo, pagina)
        elif m := RE_ALINEA.match(linha):
            atual = novo("alinea", f"{m.group(1)})", pagina)

        atual.linhas.append(linha)
        atual.pagina_fim = pagina

    return dispositivos


# --- 4: descartar o que não vale mais -----------------------------------------


def mesmo_rotulo(a: Dispositivo, b: Dispositivo) -> bool:
    """Rótulos equivalentes. "Parágrafo único" e "§1º" contam como o mesmo,
    porque o regulamento troca um pelo outro quando acrescenta parágrafos."""
    if a.tipo != b.tipo:
        return False
    equivalentes = {"Parágrafo único", "§1º"}
    return a.rotulo == b.rotulo or (a.rotulo in equivalentes and b.rotulo in equivalentes)


def aplicar_vigencia(dispositivos: list[Dispositivo]) -> None:
    """Marca como removido tudo que foi substituído, renumerado ou revogado."""
    for i, d in enumerate(dispositivos):
        anteriores = [x for x in dispositivos[:i] if x.artigo == d.artigo and not x.removido]

        if RE_NOVA_REDACAO.search(d.texto):
            # A nova redação substitui a versão anterior de mesmo rótulo no mesmo artigo.
            antigo = next((x for x in reversed(anteriores) if mesmo_rotulo(x, d)), None)
            if antigo:
                antigo.removido = f"substituído por nova redação (p. {d.pagina_inicio})"
                if d.tipo == "artigo":
                    # Artigo reescrito: parágrafos e alíneas da versão antiga caem junto.
                    for filho in dispositivos[dispositivos.index(antigo) + 1 : i]:
                        if filho.artigo == d.artigo and not filho.removido:
                            filho.removido = f"artigo reescrito (p. {d.pagina_inicio})"

        if RE_RENUMERADO.search(d.texto) and anteriores:
            anteriores[-1].removido = f"renumerado como {d.rotulo} (p. {d.pagina_inicio})"

    for d in dispositivos:
        if not d.removido and RE_REVOGADO.search(d.texto):
            d.removido = "revogado"


# --- 5: agrupar por artigo ----------------------------------------------------


def agrupar_por_artigo(dispositivos: list[Dispositivo]) -> list[dict]:
    trechos: dict[str, dict] = {}
    for d in dispositivos:
        if d.removido:
            continue
        t = trechos.setdefault(d.artigo, {
            "id": d.artigo,
            "secao": d.secao,
            "pagina_inicio": d.pagina_inicio,
            "pagina_fim": d.pagina_fim,
            "partes": [],
        })
        t["pagina_inicio"] = min(t["pagina_inicio"], d.pagina_inicio)
        t["pagina_fim"] = max(t["pagina_fim"], d.pagina_fim)
        t["partes"].append(d.texto)

    resultado = []
    for t in trechos.values():
        resultado.append({
            "id": t["id"],
            "secao": t["secao"],
            "pagina_inicio": t["pagina_inicio"],
            "pagina_fim": t["pagina_fim"],
            "texto": "\n".join(t.pop("partes")),
        })
    return resultado


# --- 6: ficha do regulamento (visão geral) -------------------------------------


def montar_ficha(trechos: list[dict]) -> list[dict]:
    """Troca o preâmbulo por uma ficha com a visão geral do documento.

    A busca devolve só 4 trechos, então perguntas gerais ("o que é o regulamento?",
    "onde fala de reembolso?") não acham resposta em nenhum artigo isolado. A ficha
    junta o preâmbulo, o Art. 1 e um sumário com os títulos e a faixa de artigos de
    cada um. Tudo sai do próprio documento: nada é escrito à mão.
    """
    preambulo = next(t for t in trechos if t["id"] == "Preâmbulo")
    artigos = [t for t in trechos if t["id"] != "Preâmbulo"]
    art1 = next((t for t in artigos if t["id"] == "Art. 1"), None)

    # Agrupa artigos seguidos que estão na mesma seção.
    grupos: list[dict] = []
    for t in artigos:
        if grupos and grupos[-1]["secao"] == t["secao"]:
            grupos[-1]["ultimo"], grupos[-1]["pagina_fim"] = t["id"], t["pagina_fim"]
        else:
            grupos.append({"secao": t["secao"], "primeiro": t["id"], "ultimo": t["id"],
                           "pagina_inicio": t["pagina_inicio"], "pagina_fim": t["pagina_fim"]})

    linhas = [
        "Ficha do regulamento: resumo montado a partir do preâmbulo, do Art. 1 e dos títulos do documento.",
        f"Documento: {preambulo['texto']}",
    ]
    if art1:
        linhas.append(f"A quem se aplica: {art1['texto']}")
    linhas.append("Assuntos do regulamento e onde estão:")
    for g in grupos:
        faixa = g["primeiro"] if g["primeiro"] == g["ultimo"] else f"{g['primeiro']} a {g['ultimo']}"
        paginas = f"p. {g['pagina_inicio']}" + (f"-{g['pagina_fim']}" if g["pagina_fim"] != g["pagina_inicio"] else "")
        linhas.append(f"- {g['secao']}: {faixa} ({paginas})")

    ficha = {
        "id": "Ficha do regulamento",
        "secao": "Visão geral",
        "pagina_inicio": 1,
        "pagina_fim": max(t["pagina_fim"] for t in artigos),
        "texto": "\n".join(linhas),
    }
    return [ficha] + artigos


# --- 7: documentos complementares ---------------------------------------------

PASTA_COMPLEMENTARES = Path(__file__).parent / "docs" / "complementares"


def carregar_complementares() -> list[dict]:
    """Lê as resoluções avulsas de docs/complementares/*.md (uma por arquivo).

    Formato do arquivo: cabeçalho "chave: valor" (id, titulo, fonte, paginas), uma
    linha "---" e depois o texto, um dispositivo por linha. Usado para documentos
    escaneados que foram transcritos, já com as alterações posteriores aplicadas.
    """
    trechos = []
    for arquivo in sorted(PASTA_COMPLEMENTARES.glob("*.md")):
        cabecalho, corpo = arquivo.read_text(encoding="utf-8").split("\n---\n", 1)
        meta = dict(linha.split(": ", 1) for linha in cabecalho.strip().splitlines())
        inicio, _, fim = meta["paginas"].partition("-")
        trechos.append({
            "id": meta["id"],
            "secao": meta["titulo"],
            "pagina_inicio": int(inicio),
            "pagina_fim": int(fim or inicio),
            "fonte": meta["fonte"],
            "texto": "\n".join(l.strip() for l in corpo.strip().splitlines() if l.strip()),
        })
    return trechos


# --- Execução -----------------------------------------------------------------


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    dispositivos = separar_dispositivos(ler_linhas(Path(sys.argv[1])))
    aplicar_vigencia(dispositivos)
    trechos = montar_ficha(agrupar_por_artigo(dispositivos))

    # Resoluções avulsas entram depois dos artigos e ficam listadas na ficha.
    complementares = carregar_complementares()
    if complementares:
        trechos[0]["texto"] += "\nOutras resoluções consultadas:\n" + "\n".join(
            f"- {c['secao']}" for c in complementares
        )
        trechos.extend(complementares)

    removidos = [
        {"artigo": d.artigo, "rotulo": d.rotulo, "pagina": d.pagina_inicio, "motivo": d.removido, "texto": d.texto}
        for d in dispositivos if d.removido
    ]

    PASTA_DADOS.mkdir(exist_ok=True)
    (PASTA_DADOS / "trechos.json").write_text(json.dumps(trechos, ensure_ascii=False, indent=2), encoding="utf-8")
    (PASTA_DADOS / "removidos.json").write_text(json.dumps(removidos, ensure_ascii=False, indent=2), encoding="utf-8")

    motivos = Counter(r["motivo"].split(" (")[0] for r in removidos)
    tamanhos = [len(t["texto"]) for t in trechos]
    print(f"Dispositivos encontrados: {len(dispositivos)}")
    print(f"Removidos: {len(removidos)} -> " + ", ".join(f"{m}: {n}" for m, n in motivos.most_common()))
    print(f"Trechos gerados (um por artigo): {len(trechos)}")
    print(f"Tamanho dos trechos: mín {min(tamanhos)}, médio {sum(tamanhos) // len(tamanhos)}, máx {max(tamanhos)} caracteres")
    print(f"Salvo em {PASTA_DADOS / 'trechos.json'} e {PASTA_DADOS / 'removidos.json'}")

    if "--mostrar" in sys.argv:
        alvo = sys.argv[sys.argv.index("--mostrar") + 1]
        for t in trechos:
            if t["id"] == alvo:
                print(f"\n===== {t['id']} | {t['secao']} | p. {t['pagina_inicio']}-{t['pagina_fim']} =====")
                print(t["texto"])
        for r in removidos:
            if r["artigo"] == alvo:
                print(f"\n  [REMOVIDO: {r['motivo']}] {r['rotulo']}: {r['texto'][:150]}")


if __name__ == "__main__":
    main()
