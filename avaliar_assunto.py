"""Eval da troca de assunto: com um assunto escolhido no botão, a pergunta digitada faz o chatbot
oferecer a troca certa, ou segue no assunto quando não há sinal claro?

Cada caso (evals/perguntas_assunto.json) tem o assunto escolhido, a pergunta e a troca esperada
(null = não pode oferecer troca). Não usa o modelo: roda em segundos. Errar para o lado de
"oferecer troca à toa" incomoda a pessoa; errar para o outro lado manda a pergunta para o caminho
errado (no assunto rede, por exemplo, sem os artigos do regulamento).

Uso:
    python avaliar_assunto.py
"""

import json
from pathlib import Path

from rag import sugerir_assunto

casos = json.loads((Path(__file__).parent / "evals" / "perguntas_assunto.json").read_text(encoding="utf-8"))
acertos = 0
for c in casos:
    veio = sugerir_assunto(c["assunto"], c["pergunta"])
    ok = veio == c["esperado"]
    acertos += ok
    print(f"[{'OK ' if ok else 'ERRO'}] {c['assunto']:<13} {c['pergunta'][:58]:<58} "
          f"esperado {str(c['esperado']):<13} veio {veio}")
print(f"\ntroca de assunto certa: {acertos}/{len(casos)}")
