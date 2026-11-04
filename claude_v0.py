"""Versão 0 (Claude via API): chatbot que responde dúvidas sobre um regulamento em PDF.

O PDF inteiro vai junto na conversa (sem RAG). A API devolve citações com o
número da página, e o documento fica em cache pra as perguntas seguintes
saírem mais baratas.

Uso:
    python claude_v0.py docs/fontes/regulamento.pdf
"""

import base64
import sys
from pathlib import Path

import anthropic

MODEL = "claude-opus-5-5"

# Preço por 1 milhão de tokens (US$), só pra mostrar o custo de cada pergunta.
PRECO_ENTRADA = 4.00
PRECO_CACHE_ESCRITA = 5.00  # gravar no cache custa 1,25x a entrada
PRECO_CACHE_LEITURA = 0.20
PRECO_SAIDA = 20.00

SYSTEM = """Você é um assistente que tira dúvidas de beneficiários sobre o regulamento \
de um plano de saúde. O regulamento está anexado na conversa.

Regras:
- Responda só com base no regulamento. Não use conhecimento externo sobre planos de saúde.
- Se o regulamento não trata do assunto, diga isso claramente em vez de supor.
- Se a resposta depende de algo que o beneficiário não informou (tipo de plano, idade, \
tempo de adesão), diga do que depende.
- Escreva em português simples, em poucas frases, sem juridiquês."""


def carregar_pdf(caminho: Path) -> dict:
    dados = base64.standard_b64encode(caminho.read_bytes()).decode("utf-8")
    return {
        "type": "document",
        "source": {"type": "base64", "media_type": "application/pdf", "data": dados},
        "title": caminho.stem,
        "citations": {"enabled": True},
        # O PDF é a parte grande e fixa da conversa: fica em cache entre as perguntas.
        "cache_control": {"type": "ephemeral"},
    }


def paginas(citacao) -> str:
    # end_page_number é exclusivo: páginas 3 a 4 vêm como start=3, end=5.
    inicio, fim = citacao.start_page_number, citacao.end_page_number - 1
    return f"p. {inicio}" if fim <= inicio else f"p. {inicio}-{fim}"


def mostrar_resposta(resposta) -> None:
    """Imprime o texto com marcadores [1], [2]... e a lista de fontes no fim."""
    fontes: list[str] = []
    texto = ""
    for bloco in resposta.content:
        if bloco.type != "text":
            continue
        texto += bloco.text
        for citacao in bloco.citations or []:
            if citacao.type != "page_location":
                continue
            fontes.append(f"({paginas(citacao)}) \"{citacao.cited_text.strip()}\"")
            texto += f" [{len(fontes)}]"

    print(f"\nAssistente: {texto.strip()}\n")
    if fontes:
        print("Fontes:")
        for i, fonte in enumerate(fontes, 1):
            print(f"  [{i}] {fonte}")
        print()


def mostrar_custo(uso) -> None:
    escrita = uso.cache_creation_input_tokens or 0
    leitura = uso.cache_read_input_tokens or 0
    custo = (
        uso.input_tokens * PRECO_ENTRADA
        + escrita * PRECO_CACHE_ESCRITA
        + leitura * PRECO_CACHE_LEITURA
        + uso.output_tokens * PRECO_SAIDA
    ) / 1_000_000
    print(
        f"  tokens: entrada {uso.input_tokens} | cache gravado {escrita} | "
        f"cache lido {leitura} | saída {uso.output_tokens} | ~US$ {custo:.4f}\n"
    )


def main() -> None:
    if len(sys.argv) < 2:
        print("Uso: python claude_v0.py caminho/do/regulamento.pdf")
        sys.exit(1)

    caminho = Path(sys.argv[1])
    if not caminho.exists():
        print(f"Arquivo não encontrado: {caminho}")
        sys.exit(1)

    client = anthropic.Anthropic()
    documento = carregar_pdf(caminho)
    historico: list[dict] = []

    print(f"Regulamento carregado: {caminho.name}")
    print("Faça sua pergunta (ou digite 'sair').\n")

    while True:
        pergunta = input("Você: ").strip()
        if not pergunta:
            continue
        if pergunta.lower() in {"sair", "exit", "quit"}:
            break

        # O PDF vai só na primeira mensagem. Como o histórico só cresce no fim,
        # o começo da conversa (onde está o PDF) não muda e o cache continua valendo.
        conteudo = [documento, {"type": "text", "text": pergunta}] if not historico else pergunta
        historico.append({"role": "user", "content": conteudo})

        print("  (pensando...)")
        try:
            with client.beta.messages.stream(
                model=MODEL,
                max_tokens=16000,
                system=SYSTEM,
                messages=historico,
                output_config={"effort": "medium"},
                # Se o filtro de segurança recusar por engano, a API tenta de novo
                # com outro modelo automaticamente.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            ) as stream:
                resposta = stream.get_final_message()
        except anthropic.AuthenticationError:
            print("Chave da API inválida ou ausente. Defina ANTHROPIC_API_KEY (veja docs/historico.md).")
            sys.exit(1)
        except anthropic.APIStatusError as e:
            print(f"Erro da API ({e.status_code}): {e.message}")
            historico.pop()
            continue
        except anthropic.APIConnectionError:
            print("Sem conexão com a API. Verifique a internet.")
            historico.pop()
            continue

        if resposta.stop_reason == "refusal":
            print("\nAssistente: não consegui responder essa pergunta.\n")
            historico.pop()
            continue

        # Guarda a resposta completa (não só o texto) pra conversa continuar consistente.
        historico.append({"role": "assistant", "content": resposta.content})
        mostrar_resposta(resposta)
        mostrar_custo(resposta.usage)


if __name__ == "__main__":
    main()
