"""Eval da busca no guia do app e do site (guia.py): acha a seção certa? E fica quieto quando a
pergunta não é sobre o app/site?

- Positivas (evals/perguntas_guia.json): a 1ª seção precisa ser uma das esperadas e passar
  (guia.passa: nota acima de LIMIAR se a pergunta fala do assunto do guia, de LIMIAR_SEM_ASSUNTO se não).
- Negativas: as perguntas do regulamento (perguntas_resposta.json) e da rede (perguntas_rede.json).
  Nenhuma seção pode passar (senão a seção vai para o prompt sem necessidade: mais
  leitura e risco de o modelo falar do app quando a pergunta era de regra).

Uso:
    python avaliar_guia.py
"""

import json
from pathlib import Path

import guia

if not guia.disponivel():  # o guia fica fora do Git
    raise SystemExit(f"Guia não encontrado: {guia.ARQUIVO}")

PASTA = Path(__file__).parent / "evals"
positivas = json.loads((PASTA / "perguntas_guia.json").read_text(encoding="utf-8"))
textos_positivos = {p["pergunta"] for p in positivas}
negativas = [p["pergunta"] for arq in ("perguntas_resposta.json", "perguntas_rede.json")
             for p in json.loads((PASTA / arq).read_text(encoding="utf-8"))
             if p["pergunta"] not in textos_positivos]

pos, neg = [], []
print("POSITIVAS (seção esperada | 1ª seção encontrada)")
for p in positivas:
    primeira = guia.ranking(p["pergunta"])[0]
    certa = int(primeira["numero"]) in p["secoes"]
    ok = certa and guia.passa(p["pergunta"], primeira["similaridade"])
    pos.append(ok)
    print(f"[{'OK ' if ok else 'ERRO'}] {primeira['similaridade']:.3f}  {p['pergunta'][:55]:<55} "
          f"esperada {p['secoes']} | veio {primeira['numero']}")

print("\nNEGATIVAS (não deveriam trazer seção nenhuma)")
for q in negativas:
    primeira = guia.ranking(q)[0]
    ok = not guia.passa(q, primeira["similaridade"])
    neg.append(ok)
    print(f"[{'OK ' if ok else 'ERRO'}] {primeira['similaridade']:.3f}  {q[:55]:<55} | seção {primeira['numero']}")

print(f"\nPositivas certas: {sum(pos)}/{len(pos)} | negativas quietas: {sum(neg)}/{len(neg)} "
      f"(LIMIAR {guia.LIMIAR} com assunto do guia, {guia.LIMIAR_SEM_ASSUNTO} sem)")
