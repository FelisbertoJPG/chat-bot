"""Eval das respostas: roda as perguntas do gabarito no chatbot e corrige cada resposta.

Mede o sistema inteiro (busca + decisão + resposta), sem memória exata e sem cache: a ideia
é saber o que o modelo responde, não o que foi guardado.

Correção automática (critérios em evals/perguntas_resposta.json, campo "criterios"):
- deve_ter: fatos do gabarito que a resposta precisa trazer (ex.: "12 consultas");
- nao_pode_ter: regra revogada ou invenção (ex.: "duas por mês", um valor em R$ inventado);
- usa_tabela: se a pergunta pede a tabela (true), se não deve consultar (false) ou tanto faz.
Resultado por pergunta: OK (todos os fatos e nada proibido), PARCIAL (parte dos fatos) ou ERRO.

A correção automática é uma triagem: ela confere palavras, não entende a resposta. Por isso
cada rodada também gera uma planilha (CSV) para uma pessoa avaliar (Sim / Parcial / Não).

Uso:
    python avaliar_respostas.py                     todas as perguntas (~20 min nesta máquina)
    python avaliar_respostas.py --so 7,8,20         só algumas (números do gabarito, a partir de 1)
    python avaliar_respostas.py --pensar            com raciocínio (bem mais lento)
    python avaliar_respostas.py --rotulo sem-ficha  nome extra nos arquivos de resultado
    python avaliar_respostas.py --arquivo evals/perguntas_rede.json   outro conjunto (a rede
                                                    credenciada: só roda com o prestadores.json)    python avaliar_respostas.py --comparar A.json B.json   compara duas rodadas
    python avaliar_respostas.py --recorrigir A.json        aplica os critérios atuais a uma rodada
                                                           salva (sem rodar o modelo de novo)
"""

import csv
import json
import re
import sys
import time
import unicodedata
from pathlib import Path

PASTA = Path(__file__).parent
ARQUIVO_PERGUNTAS = PASTA / "evals" / "perguntas_resposta.json"
PASTA_RESULTADOS = PASTA / "evals" / "resultados"


def sem_acento(texto: str) -> str:
    return unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode().lower()


RE_TELEFONE = re.compile(r"\(?\b\d{2}\)?\s?\d{4,5}[-\s]?\d{4}\b")


def telefones_da_rede() -> set[str]:
    import rede

    dados = rede.carregar() or {"linhas": []}
    return {re.sub(r"\D", "", t) for r in dados["linhas"]
            for t in (r["telefone_coml1a"], r["telefone_coml1b"], r["telefone_cel"]) if t}


# Cidades que o modelo poderia "lembrar" sem estarem no resultado (as da rede + vizinhas grandes).
CIDADES_RS = ["novo hamburgo", "sao leopoldo", "campo bom", "porto alegre", "esteio", "estancia velha",
              "canoas", "ivoti", "dois irmaos", "portao", "pelotas", "cachoeirinha", "sapucaia do sul",
              "tramandai", "gravatai", "alvorada", "santo antonio da patrulha", "imbe", "montenegro",
              "caxias do sul", "gramado", "canela", "santa maria", "passo fundo", "santa cruz do sul",
              "lajeado", "bento goncalves", "rio grande", "viamao", "taquara", "parobe", "sapiranga"]


def cidades_inventadas(resposta: str, pergunta: str, args_rede: list[dict]) -> list[str]:
    """Cidades citadas que não vieram da ferramenta nem da pergunta."""
    import rede

    permitidas = {c for c in CIDADES_RS if c in sem_acento(pergunta)}
    for args in args_rede:
        permitidas |= {rede.normalizar(c) for c in rede.consultar_rede(**args).get("por_cidade", {})}
    texto = sem_acento(resposta)
    return [c for c in CIDADES_RS if re.search(rf"\b{c}\b", texto) and c not in permitidas]


def corrigir(resposta: str, criterios: dict, consultou_tabela: bool, ferramentas: list[str] = (),
             pergunta: str = "", args_rede: list[dict] = ()) -> dict:
    texto = sem_acento(resposta).strip()
    faltou = [p for p in criterios["deve_ter"] if not re.search(p, texto)]
    proibido = [p for p in criterios["nao_pode_ter"] if re.search(p, texto)]
    if criterios.get("telefones_reais"):
        # Todo telefone citado precisa existir no arquivo da rede: pega prestador inventado.
        reais = telefones_da_rede()
        proibido += [f"telefone inventado: {t}" for t in RE_TELEFONE.findall(resposta)
                     if re.sub(r"\D", "", t) not in reais]
        # Toda cidade citada precisa ter vindo da ferramenta ou da pergunta.
        proibido += [f"cidade inventada: {c}" for c in cidades_inventadas(resposta, pergunta, args_rede)]
    total = len(criterios["deve_ter"])
    if not faltou and not proibido:
        nota = "OK"
    elif len(faltou) < total and not proibido:
        nota = "PARCIAL"
    else:
        nota = "ERRO"
    # Decisão das ferramentas: só confere o que o critério diz (None = tanto faz).
    checagens = []
    if criterios.get("usa_tabela") is not None:
        checagens.append(criterios["usa_tabela"] == consultou_tabela)
    if criterios.get("usa_rede") is not None:
        checagens.append(criterios["usa_rede"] == ("consultar_rede" in ferramentas))
    return {"nota": nota, "faltou": faltou, "proibido": proibido,
            "tabela_ok": all(checagens) if checagens else None}


def rodar(indices: list[int], pensar: bool) -> list[dict]:
    from rag import responder

    perguntas = json.loads(ARQUIVO_PERGUNTAS.read_text(encoding="utf-8"))
    resultados = []
    for n in indices:
        p = perguntas[n - 1]
        inicio = time.time()
        texto, fontes, termos, ferramentas, fim, alerta, na_tela = [], [], [], [], {}, False, 0
        args_rede = []
        for ev in responder(p["pergunta"], pensar=pensar, atalhos=False):
            if ev["tipo"] == "texto":
                texto.append(ev["texto"])
            elif ev["tipo"] == "fontes":
                fontes = [f["id"] for f in ev["fontes"]]
            elif ev["tipo"] == "ferramenta":
                ferramentas.append(ev["nome"])
                if ev["nome"] == "consultar_rede":
                    args_rede.append({k: v for k, v in ev["argumentos"].items()
                                      if k in ("especialidade", "cidade", "bairro")})
                if ev.get("lista"):
                    na_tela = len(ev["lista"]["prestadores"])
                if ev["nome"] == "consultar_tabela":
                    termos.append(ev["argumentos"].get("termo", ""))
            elif ev["tipo"] == "alerta":
                alerta = True
            elif ev["tipo"] == "fim":
                fim = ev
        resposta = "".join(texto).strip()
        r = {"n": n, "categoria": p["categoria"], "pergunta": p["pergunta"],
             "resposta_esperada": p["resposta_esperada"], "resposta": resposta,
             "fontes": fontes, "termos_tabela": termos, "ferramentas": ferramentas, "alerta": alerta,
             "args_rede": args_rede, "segundos": round(time.time() - inicio, 1),
             "tokens_por_segundo": fim.get("tokens_por_segundo", 0),
             **corrigir(resposta, p["criterios"], bool(termos), ferramentas, p["pergunta"], args_rede)}
        if "alerta" in p["criterios"] and p["criterios"]["alerta"] != alerta:
            r["nota"] = "ERRO"
            r["proibido"].append("alerta de emergência " + ("faltou" if p["criterios"]["alerta"] else "sobrando"))
        r["prestadores_na_tela"] = na_tela
        if p["criterios"].get("lista_na_tela") and not na_tela:
            r["nota"] = "ERRO"
            r["proibido"].append("lista de prestadores não apareceu na tela")
        resultados.append(r)
        tabela = {None: "", True: "", False: f" | FERRAMENTA errada: chamou {ferramentas or 'nenhuma'}"}[r["tabela_ok"]]
        print(f"[{r['nota']:<7}] {n:2d}. {p['pergunta'][:60]:<60} {r['segundos']:5.1f}s{tabela}", flush=True)
        if r["nota"] != "OK":
            print(f"           faltou: {r['faltou']} | proibido: {r['proibido']}")
            print(f"           resposta: {resposta[:220].replace(chr(10), ' ')}")
    return resultados


def resumo(resultados: list[dict]) -> str:
    total = len(resultados)
    contar = lambda nota: sum(r["nota"] == nota for r in resultados)  # noqa: E731
    com_regra = [r for r in resultados if r["tabela_ok"] is not None]
    segundos = [r["segundos"] for r in resultados]
    return (f"OK {contar('OK')}/{total} | parcial {contar('PARCIAL')} | erro {contar('ERRO')} | "
            f"decisão da ferramenta certa {sum(r['tabela_ok'] for r in com_regra)}/{len(com_regra)} | "
            f"tempo médio {sum(segundos) / total:.1f}s (total {sum(segundos) / 60:.1f} min)")


def salvar(resultados: list[dict], rotulo: str, pensar: bool, nome: str = "") -> Path:
    PASTA_RESULTADOS.mkdir(parents=True, exist_ok=True)
    nome = nome or (time.strftime("%Y-%m-%d_%H%M") + (f"_{rotulo}" if rotulo else "")
                    + ("_pensar" if pensar else ""))
    base = PASTA_RESULTADOS / nome

    base.with_suffix(".json").write_text(json.dumps(
        {"resumo": resumo(resultados), "pensar": pensar,
         "arquivo": ARQUIVO_PERGUNTAS.relative_to(PASTA).as_posix(), "resultados": resultados},
        ensure_ascii=False, indent=1), encoding="utf-8")

    linhas = [f"# Eval das respostas: {nome}", "", f"**{resumo(resultados)}**", ""]
    for r in resultados:
        linhas += [f"## {r['n']}. [{r['nota']}] {r['pergunta']}", "",
                   f"*{r['categoria']} · {r['segundos']}s · artigos: {', '.join(r['fontes'])}"
                   + (f" · tabela: {', '.join(r['termos_tabela'])}" if r["termos_tabela"] else "") + "*", "",
                   f"**Chatbot:** {r['resposta']}", "", f"**Gabarito:** {r['resposta_esperada']}", ""]
        if r["nota"] != "OK":
            linhas += [f"Faltou: `{r['faltou']}` · proibido: `{r['proibido']}`", ""]
    base.with_suffix(".md").write_text("\n".join(linhas), encoding="utf-8")

    # Planilha para quem conhece o regulamento avaliar. utf-8-sig e ";" abrem direto no Excel.
    with base.with_suffix(".csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["Nº", "Categoria", "Pergunta", "Resposta do chatbot", "Gabarito", "Correção automática",
                    "Avaliação (Sim / Parcial / Não)", "Resposta certa (se Não ou Parcial)"])
        for r in resultados:
            w.writerow([r["n"], r["categoria"], r["pergunta"], r["resposta"], r["resposta_esperada"],
                        r["nota"], "", ""])
    return base


def comparar(a: Path, b: Path) -> None:
    ra, rb = (json.loads(p.read_text(encoding="utf-8")) for p in (a, b))
    print(f"A: {a.name}\n   {ra['resumo']}\nB: {b.name}\n   {rb['resumo']}\n")
    por_n = {r["n"]: r for r in ra["resultados"]}
    for r in rb["resultados"]:
        antes = por_n.get(r["n"])
        if antes and antes["nota"] != r["nota"]:
            print(f"  {r['n']:2d}. {antes['nota']:<7} -> {r['nota']:<7} {r['pergunta'][:60]}")
        if antes:
            print(f"      {antes['segundos']:5.1f}s -> {r['segundos']:5.1f}s  {r['pergunta'][:50]}")


def recorrigir(arquivo: Path) -> None:
    """Critério ajustado (ex.: a régua puniu uma resposta certa): corrige de novo as respostas
    guardadas e regrava a rodada, sem gastar 20 minutos de modelo."""
    global ARQUIVO_PERGUNTAS
    rodada = json.loads(arquivo.read_text(encoding="utf-8"))
    ARQUIVO_PERGUNTAS = PASTA / rodada.get("arquivo", "evals/perguntas_resposta.json")
    perguntas = json.loads(ARQUIVO_PERGUNTAS.read_text(encoding="utf-8"))
    for r in rodada["resultados"]:
        antes = r["nota"]
        r.update(corrigir(r["resposta"], perguntas[r["n"] - 1]["criterios"], bool(r["termos_tabela"]),
                          r.get("ferramentas", []), r["pergunta"], r.get("args_rede", [])))
        if r["nota"] != antes:
            print(f"  {r['n']:2d}. {antes:<7} -> {r['nota']:<7} {r['pergunta'][:60]}")
    print(resumo(rodada["resultados"]))
    base = salvar(rodada["resultados"], "", rodada["pensar"], nome=arquivo.stem)
    print(f"Regravado em {base.relative_to(PASTA)}.md / .csv / .json")


def main() -> None:
    global ARQUIVO_PERGUNTAS
    args = sys.argv[1:]
    if "--arquivo" in args:
        ARQUIVO_PERGUNTAS = PASTA / args[args.index("--arquivo") + 1]
    if "--recorrigir" in args:
        recorrigir(Path(args[args.index("--recorrigir") + 1]))
        return
    if "--comparar" in args:
        i = args.index("--comparar")
        comparar(Path(args[i + 1]), Path(args[i + 2]))
        return
    pensar = "--pensar" in args
    rotulo = args[args.index("--rotulo") + 1] if "--rotulo" in args else ""
    total = len(json.loads(ARQUIVO_PERGUNTAS.read_text(encoding="utf-8")))
    indices = ([int(x) for x in args[args.index("--so") + 1].split(",")] if "--so" in args
               else list(range(1, total + 1)))

    resultados = rodar(indices, pensar)
    print(f"\n{resumo(resultados)}")
    base = salvar(resultados, rotulo, pensar)
    print(f"Salvo em {base.relative_to(PASTA)}.md / .csv / .json")


if __name__ == "__main__":
    main()
