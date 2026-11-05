"""Servidor web local para testar o chatbot no navegador.

Uso:
    python app.py
    depois abra http://127.0.0.1:5000

O servidor só aceita conexões da própria máquina (127.0.0.1), não da rede.
"""

import json
from pathlib import Path

from flask import Flask, Response, request, send_from_directory

from rag import MODELO_RESPOSTA, responder

PASTA = Path(__file__).parent
app = Flask(__name__)


@app.get("/")
def pagina():
    return send_from_directory(PASTA / "web", "index.html")


@app.get("/api/sugestoes")
def sugestoes():
    perguntas = json.loads((PASTA / "evals" / "perguntas_resposta.json").read_text(encoding="utf-8"))
    return {"modelo": MODELO_RESPOSTA, "perguntas": perguntas}


@app.post("/api/perguntar")
def perguntar():
    dados = request.get_json()
    pergunta = (dados.get("pergunta") or "").strip()
    if not pergunta:
        return {"erro": "pergunta vazia"}, 400

    # Conversa: as últimas trocas, para entender perguntas de continuação ("e de clínico geral?").
    historico = dados.get("historico") if isinstance(dados.get("historico"), list) else []
    historico = [
        {"pergunta": str(t.get("pergunta", ""))[:500], "resposta": str(t.get("resposta", ""))[:2000]}
        for t in historico[-3:] if isinstance(t, dict)
    ]

    def eventos():
        # Uma linha JSON por evento (formato NDJSON); a página lê linha a linha.
        try:
            for evento in responder(pergunta, pensar=bool(dados.get("pensar")), historico=historico):
                yield json.dumps(evento, ensure_ascii=False) + "\n"
        except Exception as erro:  # ex.: Ollama fechado
            yield json.dumps({"tipo": "erro", "mensagem": str(erro)}, ensure_ascii=False) + "\n"

    return Response(eventos(), mimetype="application/x-ndjson")


if __name__ == "__main__":
    print("Abra http://127.0.0.1:5000 no navegador")
    app.run(host="127.0.0.1", port=5000, threaded=True)
