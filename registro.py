"""Registro das conversas: cada pergunta (e cada clique nos botões) vira uma linha em
dados/registro/AAAA-MM.jsonl, para depois descobrir o que as pessoas mais perguntam, o que
demora, o que dá para virar resposta pronta (memoria.md, menu.json) e o que vira caso de eval.

Privacidade: o arquivo fica FORA do Git e só na máquina do servidor. Antes de gravar, CPF,
telefone e e-mail digitados na pergunta são trocados por [CPF], [telefone] e [e-mail]. Números
curtos (como o de uma autorização) ficam, porque ajudam a entender a pergunta.

Desligar: variável de ambiente CHATBOT_REGISTRO=0.
"""

import json
import os
import re
import threading
from datetime import datetime
from pathlib import Path

PASTA = Path(__file__).parent / "dados" / "registro"
LIGADO = os.environ.get("CHATBOT_REGISTRO", "1") != "0"

_RE_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
_RE_CPF = re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b")
_RE_TELEFONE = re.compile(r"\(?\b\d{2}\)?\s?9?\d{4}[-\s]?\d{4}\b")
_trava = threading.Lock()  # o servidor atende várias pessoas ao mesmo tempo


def mascarar(texto: str) -> str:
    texto = _RE_EMAIL.sub("[e-mail]", texto)
    texto = _RE_CPF.sub("[CPF]", texto)
    return _RE_TELEFONE.sub("[telefone]", texto)


def _limpo(valor):
    if isinstance(valor, str):
        return mascarar(valor)
    if isinstance(valor, list):
        return [_limpo(v) for v in valor]
    if isinstance(valor, dict):
        return {k: _limpo(v) for k, v in valor.items()}
    return valor


def registrar(dados: dict) -> None:
    """Acrescenta uma linha ao registro do mês. Nunca derruba o chat: erro ao gravar só é ignorado."""
    if not LIGADO:
        return
    agora = datetime.now()
    linha = {"quando": agora.isoformat(timespec="seconds"), **_limpo(dados)}
    try:
        with _trava:
            PASTA.mkdir(parents=True, exist_ok=True)
            with open(PASTA / f"{agora:%Y-%m}.jsonl", "a", encoding="utf-8") as arquivo:
                arquivo.write(json.dumps(linha, ensure_ascii=False) + "\n")
    except OSError:
        pass


if __name__ == "__main__":
    for t in ["meu cpf é 123.456.789-09", "liga (51) 99876-5432", "joao@x.com.br", "cancelar 3122505"]:
        print(f"{t!r} -> {mascarar(t)!r}")
