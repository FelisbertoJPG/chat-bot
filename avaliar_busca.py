"""Eval da busca: para cada pergunta, o artigo certo aparece entre os resultados?

Mede só a etapa de busca, sem o modelo que responde. Se a busca não traz o artigo
certo, nenhum prompt vai salvar a resposta, então vale medir isso separado.

Métricas:
- acerto@1: o artigo certo veio em primeiro lugar
- acerto@4: o artigo certo veio entre os 4 primeiros (é o que vai para o prompt)

Uso:
    python avaliar_busca.py
    python avaliar_busca.py --sem-instrucao   (compara sem a instrução na consulta)
"""

import json
import sys
from pathlib import Path

from busca import buscar

K = 4
casos = json.loads((Path(__file__).parent / "evals" / "perguntas_busca.json").read_text(encoding="utf-8"))
usar_instrucao = "--sem-instrucao" not in sys.argv

acertos_1 = acertos_k = 0
for caso in casos:
    resultados = [r["id"] for r in buscar(caso["pergunta"], k=K, usar_instrucao=usar_instrucao)]
    esperados = set(caso["artigos_esperados"])
    no_topo = resultados[0] in esperados
    entre_k = bool(esperados & set(resultados))
    acertos_1 += no_topo
    acertos_k += entre_k

    marca = "OK " if no_topo else ("~  " if entre_k else "ERRO")
    print(f"[{marca}] {caso['pergunta']}")
    print(f"        esperado: {', '.join(sorted(esperados))} | veio: {', '.join(resultados)}")

total = len(casos)
print(f"\nInstrução na consulta: {'sim' if usar_instrucao else 'não'}")
print(f"acerto@1: {acertos_1}/{total} ({acertos_1 / total:.0%})")
print(f"acerto@{K}: {acertos_k}/{total} ({acertos_k / total:.0%})")
