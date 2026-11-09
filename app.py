"""Servidor web local para testar o chatbot no navegador.

Uso:
    python app.py
    depois abra http://127.0.0.1:5000

O servidor só aceita conexões da própria máquina (127.0.0.1), não da rede.
Cada pergunta e cada clique nos botões vão para o registro (registro.py, dados/registro/).
"""

import json
import time
from pathlib import Path

from flask import Flask, Response, request, send_from_directory

import guia
import rede
import registro
from rag import ASSUNTOS, MODELO_RESPOSTA, responder

PASTA = Path(__file__).parent
app = Flask(__name__)


@app.get("/")
def pagina():
    return send_from_directory(PASTA / "web", "index.html")


@app.get("/api/sugestoes")
def sugestoes():
    perguntas = json.loads((PASTA / "evals" / "perguntas_resposta.json").read_text(encoding="utf-8"))
    return {"modelo": MODELO_RESPOSTA, "perguntas": perguntas}


@app.get("/api/menu")
def menu():
    # Botões de assunto com as respostas do guia já resolvidas.
    return guia.menu({"rede"} if rede.disponivel() else set())


# --- Rede credenciada por botões (especialidade -> cidade -> lista), sem o modelo -------------

@app.get("/api/rede/cidades")
def rede_cidades():
    return {"cidades": rede.cidades()}


@app.get("/api/rede/especialidades")
def rede_especialidades():
    # Só as especialidades que existem na cidade escolhida.
    return {"especialidades": rede.especialidades(request.args.get("cidade") or None)}


@app.get("/api/rede/lista")
def rede_lista():
    cidade, especialidade = request.args.get("cidade", ""), request.args.get("especialidade") or None
    destacar = (request.args.get("destacar") or "")[:60] or None  # nome vindo da busca por nome
    registro.registrar({"tipo": "rede_botoes", "cidade": cidade[:100], "especialidade": (especialidade or "")[:100],
                        "destacar": destacar or ""})
    return {"lista": rede.lista(cidade, especialidade, destacar) if cidade else None}


@app.post("/api/registrar")
def registrar_clique():
    """Cliques que não passam pelo /api/perguntar: assunto escolhido, opção de resposta pronta,
    resposta à oferta de troca de assunto. Só campos conhecidos, cortados."""
    dados = request.get_json(silent=True) or {}
    campos = ("tipo", "assunto", "opcao", "de", "para", "aceitou", "pergunta")
    registro.registrar({k: (str(dados[k])[:300] if isinstance(dados[k], str) else dados[k])
                        for k in campos if k in dados and isinstance(dados[k], (str, bool))})
    return {"ok": True}


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

    # Assunto do botão (menu.json): só os conhecidos; qualquer outro valor vira o caminho completo.
    assunto = dados.get("assunto") if dados.get("assunto") in ASSUNTOS else None
    manter = bool(dados.get("manter_assunto"))
    # O que está escolhido no quadro da rede (cidade, especialidade): a busca por nome usa.
    filtro = dados.get("filtro_rede") if isinstance(dados.get("filtro_rede"), dict) else {}
    filtro_rede = {k: str(filtro[k])[:100] for k in ("cidade", "especialidade") if filtro.get(k)}

    def eventos():
        # Uma linha JSON por evento (formato NDJSON); a página lê linha a linha. Ao mesmo tempo,
        # junta o que interessa para o registro.
        linha = {"tipo": "pergunta", "pergunta": pergunta[:500], "assunto": assunto,
                 "manter_assunto": manter, "trocas_anteriores": len(historico)}
        texto, inicio = [], time.time()
        try:
            for evento in responder(pergunta, pensar=bool(dados.get("pensar")), historico=historico,
                                    assunto=assunto, manter_assunto=manter, filtro_rede=filtro_rede):
                tipo = evento["tipo"]
                if tipo == "reescrita":
                    linha["reescrita"] = evento["pergunta"]
                elif tipo == "mudar_assunto":
                    linha["sugeriu_assunto"] = evento["sugerido"]
                elif tipo == "fontes":
                    linha["artigos"] = [f["id"] for f in evento["fontes"]]
                elif tipo == "guia":
                    linha["guia"] = evento["id"]
                elif tipo == "memoria":
                    linha["memoria"] = evento["modo"]
                elif tipo == "alerta":
                    linha["alerta"] = True
                elif tipo == "ferramenta":
                    linha.setdefault("ferramentas", []).append(
                        {"nome": evento["nome"], "argumentos": evento["argumentos"], "total": evento["total"]})
                elif tipo == "texto":
                    texto.append(evento["texto"])
                elif tipo == "fim":
                    linha.update({k: evento[k] for k in ("origem", "tempo_total", "tempo_busca",
                                                         "tokens_por_segundo") if k in evento})
                yield json.dumps(evento, ensure_ascii=False) + "\n"
        except Exception as erro:  # ex.: Ollama fechado
            linha["erro"] = str(erro)[:300]
            yield json.dumps({"tipo": "erro", "mensagem": str(erro)}, ensure_ascii=False) + "\n"
        finally:
            linha["resposta"] = "".join(texto)
            linha.setdefault("tempo_total", round(time.time() - inicio, 1))
            registro.registrar(linha)

    return Response(eventos(), mimetype="application/x-ndjson")


if __name__ == "__main__":
    print("Abra http://127.0.0.1:5000 no navegador")
    app.run(host="127.0.0.1", port=5000, threaded=True)
