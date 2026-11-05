"""Eval da conversa: a pergunta de continuação vira uma pergunta completa?

Mede só a etapa de reescrita (rag.reescrever), que é o que faz o chatbot "lembrar" da conversa.
Se a reescrita errar o assunto, a busca procura a coisa errada e nenhuma etapa depois conserta.

Cada caso (evals/perguntas_conversa.json) tem a conversa anterior, a pergunta nova e regex que a
pergunta reescrita precisa ter (deve_ter) ou não pode ter (nao_pode_ter: assunto da conversa
que não tem a ver com a pergunta nova).

Uso:
    python avaliar_conversa.py
"""

import json
import re
import time
from pathlib import Path

from avaliar_respostas import sem_acento
from rag import reescrever

casos = json.loads((Path(__file__).parent / "evals" / "perguntas_conversa.json").read_text(encoding="utf-8"))
acertos, tempos = 0, []
for caso in casos:
    inicio = time.time()
    nova = reescrever(caso["pergunta"], caso["conversa"])
    tempos.append(time.time() - inicio)
    texto = sem_acento(nova)
    faltou = [p for p in caso["deve_ter"] if not re.search(p, texto)]
    proibido = [p for p in caso["nao_pode_ter"] if re.search(p, texto)]
    ok = not faltou and not proibido
    acertos += ok
    print(f"[{'OK ' if ok else 'ERRO'}] {tempos[-1]:4.1f}s  {caso['pergunta']}\n        -> {nova}")
    if not ok:
        print(f"        faltou: {faltou} | proibido: {proibido}")

print(f"\nreescrita certa: {acertos}/{len(casos)} | tempo médio {sum(tempos) / len(tempos):.1f}s")
