"""Eval da troca de assunto: com um assunto escolhido no botão, a pergunta digitada faz o chatbot
oferecer a troca certa, ou segue no assunto quando não há sinal claro?

Cada caso (evals/perguntas_assunto.json) tem o assunto escolhido, a pergunta e a troca esperada
(null = não pode oferecer troca). Não usa o modelo: roda em segundos. Errar para o lado de
"oferecer troca à toa" incomoda a pessoa; errar para o outro lado manda a pergunta para o caminho
errado (no assunto rede, por exemplo, sem os artigos do regulamento).

Também confere o fora_do_escopo.json (evals/perguntas_fora.json): assunto que o chatbot não cobre
recebe a resposta fixa, e nenhuma pergunta que ele sabe responder (as de todos os evals) é barrada.

Uso:
    python avaliar_assunto.py
"""

import json
from pathlib import Path

import guia
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

# --- Fora do escopo ---
PASTA = Path(__file__).parent / "evals"
fora = json.loads((PASTA / "perguntas_fora.json").read_text(encoding="utf-8"))
certos = 0
for c in fora:
    veio = guia.fora_do_escopo(c["pergunta"]) is not None
    certos += veio == c["fora"]
    if veio != c["fora"]:
        print(f"[ERRO] fora do escopo? esperado {c['fora']}, veio {veio}: {c['pergunta']}")
barradas = [p["pergunta"] for arq in PASTA.glob("perguntas_*.json") if arq.name != "perguntas_fora.json"
            for p in json.loads(arq.read_text(encoding="utf-8")) if guia.fora_do_escopo(p["pergunta"])]
for q in barradas:
    print(f"[ERRO] pergunta de eval barrada como fora do escopo: {q}")
print(f"fora do escopo certo: {certos}/{len(fora)} | perguntas dos evals barradas por engano: {len(barradas)}")
