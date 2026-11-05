"""Memória do chatbot: respostas validadas por uma pessoa e cache das respostas já geradas.

O modelo não aprende nada entre uma pergunta e outra. A "memória" fica aqui, no código, em
duas partes:

1. Respostas validadas (memoria.md), escritas à mão por quem conhece o
   regulamento ("A resposta correta é: ..."). Elas valem de dois jeitos:
   - pergunta igual, ou uma das "outras formas" cadastradas: a resposta validada sai direto,
     sem passar pelo modelo;
   - ligada a artigos: sempre que a busca trouxer um desses artigos, a resposta validada vai
     junto no prompt, e o modelo a usa em perguntas sobre o mesmo assunto.

   Por que ligar por artigo e não por "pergunta parecida": medimos a semelhança entre perguntas
   com o modelo de embedding. "O IPASEM paga vacina?" x "O IPASEM paga óculos?" (respostas
   opostas) deu 0,80, mais que "O IPASEM paga vacina?" x "O plano cobre vacinas?" (mesma
   dúvida), que deu 0,59. A semelhança não separa "mesma dúvida" de "dúvida vizinha". Já a
   busca por artigo foi medida (13/13 entre os 4 primeiros) e as outras formas de perguntar
   são cadastradas por uma pessoa.

2. Cache (dados/cache_respostas.json, fora do Git): toda resposta gerada pelo modelo fica
   guardada, e a mesma pergunta de novo (sem contar acento, maiúscula e pontuação) volta na
   hora. O cache leva uma "impressão digital" do que gerou as respostas (código, prompts,
   dados e as respostas validadas): se algo mudar, ele é descartado sozinho.

Uso no terminal:
    python memoria.py                 lista as respostas validadas e confere os artigos citados
    python memoria.py limpar-cache    apaga as respostas guardadas
"""

import hashlib
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

from tabela import normalizar as _sem_acento_nem_pontuacao

PASTA = Path(__file__).parent
ARQUIVO_MEMORIA = PASTA / "memoria.md"
ARQUIVO_CACHE = PASTA / "dados" / "cache_respostas.json"
MAX_LIGADAS = 3  # respostas validadas que vão no prompt por pergunta (cada uma aumenta a leitura)


def normalizar(texto: str) -> str:
    """'O plano cobre ÓCULOS?' -> 'o plano cobre oculos'."""
    return " ".join(_sem_acento_nem_pontuacao(texto).split())


def _chave_artigo(texto: str) -> str:
    """'Res. 09/2014 (implante dentário)' e 'res 09/2014' viram a mesma chave."""
    return normalizar(texto.split(" (")[0])


# --- 1: respostas validadas ----------------------------------------------------

_RE_COMENTARIO = re.compile(r"<!--.*?-->", re.DOTALL)
_ROTULOS = {"a resposta correta e": "resposta", "artigos": "artigos", "tambem perguntam": "variacoes"}


def _ler_arquivo() -> list[dict]:
    """Formato: '## pergunta', 'A resposta correta é: ...', 'Artigos: ...' e 'Também perguntam:'
    seguido de uma linha '- ...' por variação. Comentários <!-- --> são ignorados."""
    if not ARQUIVO_MEMORIA.exists():
        return []
    texto = _RE_COMENTARIO.sub("", ARQUIVO_MEMORIA.read_text(encoding="utf-8"))
    entradas: list[dict] = []
    atual: dict | None = None
    campo = None
    for linha in texto.splitlines():
        if linha.startswith("## "):
            atual = {"pergunta": linha[3:].strip(), "resposta": "", "artigos": [], "variacoes": []}
            entradas.append(atual)
            campo = None
            continue
        if atual is None or not linha.strip():
            continue
        rotulo, separador, resto = linha.partition(":")
        if separador and normalizar(rotulo) in _ROTULOS:
            campo, linha = _ROTULOS[normalizar(rotulo)], resto
        if campo == "resposta":
            atual["resposta"] = f"{atual['resposta']}\n{linha.strip()}".strip()
        elif campo == "artigos":
            atual["artigos"] += [a.strip() for a in re.split(r"[;,]", linha) if a.strip()]
        elif campo == "variacoes" and linha.strip().lstrip("-*").strip():
            atual["variacoes"].append(linha.strip().lstrip("-*").strip())
    return [e for e in entradas if e["resposta"]]


_memoria: tuple[int, list[dict]] | None = None


def carregar_memoria() -> list[dict]:
    """Relê o arquivo quando ele muda: editou e salvou, já vale, sem reiniciar o servidor."""
    global _memoria
    marca = ARQUIVO_MEMORIA.stat().st_mtime_ns if ARQUIVO_MEMORIA.exists() else 0
    if _memoria is None or _memoria[0] != marca:
        _memoria = (marca, _ler_arquivo())
    return _memoria[1]


def versao_memoria() -> str:
    """Muda sempre que o arquivo muda (entra na impressão digital do cache)."""
    if not ARQUIVO_MEMORIA.exists():
        return "sem memória"
    return hashlib.sha256(ARQUIVO_MEMORIA.read_bytes()).hexdigest()


def resposta_validada(pergunta: str) -> dict | None:
    """A entrada cuja pergunta (ou uma das variações) é igual à pergunta feita."""
    alvo = normalizar(pergunta)
    for entrada in carregar_memoria():
        if alvo in {normalizar(p) for p in [entrada["pergunta"], *entrada["variacoes"]]}:
            return entrada
    return None


def ligadas_aos_artigos(ids_artigos: list[str]) -> list[dict]:
    """Respostas validadas ligadas aos artigos que a busca trouxe, na ordem da busca."""
    achadas: list[dict] = []
    for chave in map(_chave_artigo, ids_artigos):
        for entrada in carregar_memoria():
            if entrada not in achadas and chave in map(_chave_artigo, entrada["artigos"]):
                achadas.append(entrada)
    return achadas[:MAX_LIGADAS]


def artigos_desconhecidos(ids_validos: list[str]) -> list[tuple[str, str]]:
    """(pergunta, artigo) para cada artigo citado na memória que não existe no índice."""
    validos = set(map(_chave_artigo, ids_validos))
    return [(e["pergunta"], a) for e in carregar_memoria() for a in e["artigos"]
            if _chave_artigo(a) not in validos]


# --- 2: cache das respostas geradas --------------------------------------------

_trava = threading.Lock()  # o servidor atende em várias threads


def _chave_cache(pergunta: str, pensar: bool) -> str:
    return normalizar(pergunta) + (" |pensar" if pensar else "")


def _ler_cache(impressao: str) -> dict:
    try:
        dados = json.loads(ARQUIVO_CACHE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    # Impressão diferente: as respostas foram geradas com outro código, prompt ou dados.
    return dados.get("respostas", {}) if dados.get("impressao") == impressao else {}


def cache_buscar(pergunta: str, pensar: bool, impressao: str) -> dict | None:
    with _trava:
        return _ler_cache(impressao).get(_chave_cache(pergunta, pensar))


def cache_guardar(pergunta: str, pensar: bool, impressao: str, registro: dict) -> None:
    with _trava:
        respostas = _ler_cache(impressao)
        respostas[_chave_cache(pergunta, pensar)] = {
            "pergunta": pergunta, "gerada_em": time.strftime("%d/%m/%Y %H:%M"), **registro,
        }
        # Grava num arquivo temporário e troca: se cair no meio, o cache antigo continua inteiro.
        temporario = ARQUIVO_CACHE.with_suffix(".tmp")
        temporario.write_text(json.dumps({"impressao": impressao, "respostas": respostas},
                                         ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(temporario, ARQUIVO_CACHE)


# --- Terminal ------------------------------------------------------------------

def main() -> None:
    if sys.argv[1:] == ["limpar-cache"]:
        ARQUIVO_CACHE.unlink(missing_ok=True)
        print("Cache apagado.")
        return

    from busca import carregar_indice

    entradas = carregar_memoria()
    print(f"{len(entradas)} resposta(s) validada(s) em {ARQUIVO_MEMORIA.relative_to(PASTA)}")
    for e in entradas:
        artigos = ", ".join(e["artigos"]) or "sem artigo ligado"
        print(f"- {e['pergunta']}  [{artigos}] (+{len(e['variacoes'])} outra(s) forma(s) de perguntar)")
    for pergunta, artigo in artigos_desconhecidos([t["id"] for t in carregar_indice()["trechos"]]):
        print(f"  AVISO: '{artigo}' (em \"{pergunta}\") não existe no índice; essa ligação não vai valer")
    try:
        guardadas = len(json.loads(ARQUIVO_CACHE.read_text(encoding="utf-8"))["respostas"])
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        guardadas = 0
    print(f"\nCache: {guardadas} resposta(s) guardada(s) em {ARQUIVO_CACHE.relative_to(PASTA)}")


if __name__ == "__main__":
    main()
