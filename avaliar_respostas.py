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
    python avaliar_respostas.py --rotulo sem-ficha  nome extra nos arquivos de resultado    python avaliar_respostas.py --comparar A.json B.json   compara duas rodadas
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


def corrigir(resposta: str, criterios: dict, consultou_tabela: bool) -> dict:
    texto = sem_acento(resposta).strip()
    faltou = [p for p in criterios["deve_ter"] if not re.search(p, texto)]
    proibido = [p for p in criterios["nao_pode_ter"] if re.search(p, texto)]
    total = len(criterios["deve_ter"])
    if not faltou and not proibido:
        nota = "OK"
    elif len(faltou) < total and not proibido:
        nota = "PARCIAL"
    else:
        nota = "ERRO"
    esperado = criterios.get("usa_tabela")
    return {"nota": nota, "faltou": faltou, "proibido": proibido,
            "tabela_ok": None if esperado is None else esperado == consultou_tabela}


def rodar(indices: list[int], pensar: bool) -> list[dict]:
    from rag import responder

    perguntas = json.loads(ARQUIVO_PERGUNTAS.read_text(encoding="utf-8"))
    resultados = []
    for n in indices:
        p = perguntas[n - 1]
        inicio = time.time()
        texto, fontes, termos, fim = [], [], [], {}
        for ev in responder(p["pergunta"], pensar=pensar, atalhos=False):
            if ev["tipo"] == "texto":
                texto.append(ev["texto"])
            elif ev["tipo"] == "fontes":
                fontes = [f["id"] for f in ev["fontes"]]
            elif ev["tipo"] == "ferramenta":
                termos.append(ev["argumentos"].get("termo", ""))
            elif ev["tipo"] == "fim":
                fim = ev
        resposta = "".join(texto).strip()
        r = {"n": n, "categoria": p["categoria"], "pergunta": p["pergunta"],
             "resposta_esperada": p["resposta_esperada"], "resposta": resposta,
             "fontes": fontes, "termos_tabela": termos, "segundos": round(time.time() - inicio, 1),
             "tokens_por_segundo": fim.get("tokens_por_segundo", 0),
             **corrigir(resposta, p["criterios"], bool(termos))}
        resultados.append(r)
        tabela = {None: "", True: "", False: " | TABELA: " + ("consultou sem precisar" if termos else "não consultou")}[r["tabela_ok"]]
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
            f"decisão da tabela certa {sum(r['tabela_ok'] for r in com_regra)}/{len(com_regra)} | "
            f"tempo médio {sum(segundos) / total:.1f}s (total {sum(segundos) / 60:.1f} min)")


def salvar(resultados: list[dict], rotulo: str, pensar: bool, nome: str = "") -> Path:
    PASTA_RESULTADOS.mkdir(parents=True, exist_ok=True)
    nome = nome or (time.strftime("%Y-%m-%d_%H%M") + (f"_{rotulo}" if rotulo else "")
                    + ("_pensar" if pensar else ""))
    base = PASTA_RESULTADOS / nome

    base.with_suffix(".json").write_text(json.dumps(
        {"resumo": resumo(resultados), "pensar": pensar, "resultados": resultados},
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
    rodada = json.loads(arquivo.read_text(encoding="utf-8"))
    perguntas = json.loads(ARQUIVO_PERGUNTAS.read_text(encoding="utf-8"))
    for r in rodada["resultados"]:
        antes = r["nota"]
        r.update(corrigir(r["resposta"], perguntas[r["n"] - 1]["criterios"], bool(r["termos_tabela"])))
        if r["nota"] != antes:
            print(f"  {r['n']:2d}. {antes:<7} -> {r['nota']:<7} {r['pergunta'][:60]}")
    print(resumo(rodada["resultados"]))
    base = salvar(rodada["resultados"], "", rodada["pensar"], nome=arquivo.stem)
    print(f"Regravado em {base.relative_to(PASTA)}.md / .csv / .json")


def main() -> None:
    args = sys.argv[1:]
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
