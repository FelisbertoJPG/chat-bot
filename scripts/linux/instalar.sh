#!/usr/bin/env bash
# Instala o chatbot num servidor Linux (testado para Debian 10) e deixa ele no ar como serviço,
# atrás do Apache em /chatbot/. Rode como root, de dentro da pasta do projeto:
#
#     sudo bash scripts/linux/instalar.sh
#
# Pode rodar mais de uma vez: o que já existe é pulado. Passos:
#   1. usuário "chatbot" (o serviço não roda como root) e dono da pasta do projeto
#   2. Ollama (serviço próprio, só em 127.0.0.1:11434) e os dois modelos (~3,8 GB)
#   3. Python 3.12 à parte, com o uv: o Debian 10 vem com Python 3.7, e o projeto precisa de
#      3.10+. O Python do sistema não é tocado (o site pode depender dele).
#   4. serviço systemd "chatbot" (app.py em 127.0.0.1:5000, sobe sozinho com a máquina)
#   5. Apache: /chatbot/ repassa para o 127.0.0.1:5000 (só se o Apache estiver instalado)

set -euo pipefail
# Root via "su" (sem hífen) fica sem /usr/sbin e /sbin no PATH: useradd, a2enmod, apache2ctl e
# runuser ficam lá.
export PATH="$PATH:/usr/local/sbin:/usr/sbin:/sbin"

PASTA="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
USUARIO=chatbot
MODELOS=("qwen3.5:4b" "qwen3-embedding:0.6b")
PYTHON_VERSAO=3.12
# Fora de /root: o usuário do serviço precisa ler o Python que o uv baixar.
export UV_INSTALL_DIR=/opt/uv
export UV_PYTHON_INSTALL_DIR=/opt/uv/python

passo() { echo; echo "== $*"; }
falha() { echo "ERRO: $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || falha "rode como root (sudo bash scripts/linux/instalar.sh)"
command -v curl >/dev/null || falha "curl não encontrado"
[ -f "$PASTA/app.py" ] || falha "não achei app.py em $PASTA"
echo "Projeto em: $PASTA"
case "$PASTA" in
    /var/www*|/srv/www*) falha "o projeto está dentro da pasta publicada pelo Apache ($PASTA): prestadores.json e o resto ficariam baixáveis. Mova para /opt (ex.: mv \"$PASTA\" /opt/) e rode de lá" ;;
esac

passo "1/5 Usuário do serviço ($USUARIO)"
if ! id "$USUARIO" >/dev/null 2>&1; then
    useradd --system --home-dir "$PASTA" --shell /usr/sbin/nologin "$USUARIO"
    echo "criado"
else
    echo "já existe"
fi
# O chatbot grava em dados/ (cache das respostas e índice do guia).
chown -R "$USUARIO:$USUARIO" "$PASTA"

passo "2/5 Ollama e modelos"
if ! command -v ollama >/dev/null; then
    # O instalador do Ollama vem em .tar.zst; o Debian 10 não traz o zstd.
    command -v zstd >/dev/null || apt-get install -y zstd \
        || falha "instale o zstd (apt-get install zstd; no Debian 10 o apt pode precisar do archive.debian.org)"
    curl -fsSL https://ollama.com/install.sh | sh
fi
ollama --version || falha "o Ollama não rodou nesta máquina (veja a mensagem acima)"
systemctl enable --now ollama >/dev/null 2>&1 || true
for i in $(seq 1 30); do
    curl -s http://127.0.0.1:11434/api/tags >/dev/null && break
    sleep 1
done
curl -s http://127.0.0.1:11434/api/tags >/dev/null || falha "o Ollama não respondeu em 127.0.0.1:11434"
for m in "${MODELOS[@]}"; do
    if ollama list | awk '{print $1}' | grep -qx "$m"; then
        echo "OK  $m"
    else
        ollama pull "$m"
    fi
done

passo "3/5 Python $PYTHON_VERSAO e bibliotecas"
# Sem "ls a b | head": com set -e + pipefail, o ls de um caminho que não existe encerrava o
# script calado logo depois de instalar o uv.
acha_uv() {
    for c in "$UV_INSTALL_DIR/uv" "$UV_INSTALL_DIR/bin/uv"; do
        if [ -x "$c" ]; then echo "$c"; return 0; fi
    done
    return 0
}
if [ -z "$(acha_uv)" ]; then
    curl -LsSf https://astral.sh/uv/install.sh | env INSTALLER_NO_MODIFY_PATH=1 sh
fi
UV="$(acha_uv)"
[ -n "$UV" ] || falha "o uv não foi instalado em $UV_INSTALL_DIR"
"$UV" python install "$PYTHON_VERSAO"
# .venv copiada do Windows (Scripts/python.exe) não serve no Linux: refaz.
if [ -d "$PASTA/.venv" ] && [ ! -x "$PASTA/.venv/bin/python" ]; then
    echo "removendo .venv que não é de Linux (copiada do Windows?)"
    rm -rf "$PASTA/.venv"
fi
if [ ! -x "$PASTA/.venv/bin/python" ]; then
    "$UV" venv --python "$PYTHON_VERSAO" "$PASTA/.venv"
fi
"$UV" pip install --quiet --python "$PASTA/.venv/bin/python" -r "$PASTA/requirements.txt"
chmod -R a+rX /opt/uv
chown -R "$USUARIO:$USUARIO" "$PASTA/.venv"
runuser -u "$USUARIO" -- "$PASTA/.venv/bin/python" -c "import flask, ollama; print('Bibliotecas OK')"

passo "4/5 Serviço systemd (chatbot)"
sed -e "s#__PASTA__#$PASTA#g" -e "s#__USUARIO__#$USUARIO#g" \
    "$PASTA/scripts/linux/chatbot.service" > /etc/systemd/system/chatbot.service
systemctl daemon-reload
systemctl enable chatbot >/dev/null 2>&1
systemctl restart chatbot
for i in $(seq 1 30); do
    curl -s http://127.0.0.1:5000/api/sugestoes >/dev/null && break
    sleep 1
done
curl -s http://127.0.0.1:5000/api/sugestoes >/dev/null \
    || falha "o chatbot não subiu: veja 'journalctl -u chatbot -n 50'"
echo "chatbot no ar em 127.0.0.1:5000"

passo "5/5 Apache (/chatbot/)"
if command -v apache2ctl >/dev/null; then
    a2enmod -q proxy proxy_http headers
    cp "$PASTA/scripts/linux/apache-chatbot.conf" /etc/apache2/conf-available/chatbot.conf
    a2enconf -q chatbot
    apache2ctl configtest
    systemctl reload apache2
    echo "Apache: http://<ip-da-vm>/chatbot/"
else
    echo "Apache não encontrado: pulei. O chatbot fica só em 127.0.0.1:5000."
fi

echo
echo "Pronto. Lembre de copiar à mão (ficam fora do Git), se quiser usá-los:"
echo "  $PASTA/prestadores.json                    (rede credenciada)"
echo "  $PASTA/docs/funcionalidades-segurado.md    (guia do app e do site)"
echo "e depois: chown $USUARIO:$USUARIO <arquivo> e systemctl restart chatbot"
