# Assistente do Regulamento de Saúde do IPASEM

Chatbot que tira dúvidas de beneficiários sobre o plano de saúde do IPASEM (Novo Hamburgo/RS),
citando o artigo e a página de onde tirou cada informação. Roda 100% local, com modelos abertos:
nenhum dado sai do computador.

> **Projeto pessoal de estudo, não é canal oficial do IPASEM.** Usa apenas documentos públicos
> (regulamento e resoluções do Conselho Deliberativo). As respostas podem conter erros e não
> substituem o atendimento do Instituto.

<!-- Demonstração: grave o chat em ação e salve como docs/demo.gif, depois descomente a linha abaixo.
![Demonstração do chatbot](docs/demo.gif)
-->

## O problema

O regulamento de assistência à saúde tem 29 páginas e é uma versão compilada: a redação de 2012
convive com 19 resoluções de alteração (de 2012 a 2023), lado a lado no mesmo PDF. A regra de consultas de
psiquiatria, por exemplo, aparece em três versões na mesma página. Valores e códigos de
procedimentos ficam em outra resolução, uma tabela de 164 páginas republicada todo ano por
ordem de serviço.

Responder "quantas consultas de psiquiatria eu tenho?" ou "quanto pago num implante dentário?"
exige saber qual versão vale e cruzar documentos diferentes.

## Como funciona

**RAG (Retrieval-Augmented Generation)** funciona como uma prova com consulta: o modelo não é
treinado com o regulamento. Os documentos são preparados uma vez e guardados fora do modelo. A
cada pergunta, o sistema procura os trechos relevantes e entrega ao modelo junto com a pergunta.

```mermaid
flowchart LR
    subgraph prep["Preparação (uma vez)"]
        PDF["Regulamento compilado<br/>29 páginas"] --> V["Limpeza e vigência<br/>95 itens revogados<br/>ou substituídos"]
        V --> A["71 artigos + ficha<br/>+ resoluções"]
        A --> E["Embeddings<br/>237 pedaços"]
        T0["Tabela única<br/>OS 01/2025"] --> T1["4.606 itens<br/>código, valor, notas"]
    end
    subgraph pergunta["A cada pergunta"]
        P["Pergunta"] --> B["Busca semântica<br/>4 artigos"]
        P --> D{"Decisão<br/>(tool calling)"}
        D -- "procedimento, valor,<br/>código" --> F["consultar_tabela"]
        B --> R["Resposta<br/>Qwen3.5 4B"]
        F --> R
        D -- "só regra" --> R
    end
    E -.-> B
    T1 -.-> F
```

## Decisões que fizeram diferença

1. **Vigência antes de tecnologia.** O PDF mistura regras em vigor e revogadas. O script de
   preparação descarta 95 itens (65 substituídos por nova redação, 14 revogados, 11 de seção
   revogada, 5 de artigo reescrito ou renumerado) e guarda o motivo de cada descarte para
   auditoria. Sem isso, o chatbot citaria regra revogada com página e tudo.
2. **Busca em pedaços pequenos, resposta com o artigo inteiro.** O Art. 51 lista 22 exclusões;
   num vetor só, "vacinas" ficava diluída. Cada parágrafo e alínea ganhou um vetor próprio, com o
   caput junto para dar contexto, e o modelo recebe o artigo completo.
3. **Ficha do regulamento.** RAG é bom em "o que diz a regra X" e fraco em "do que trata o
   documento". Uma ficha gerada a partir dos títulos resolve perguntas gerais.
4. **Tabela como ferramenta, não como texto.** Preço e código precisam vir exatos. A tabela virou
   dados estruturados, e o modelo a consulta por *tool calling*, decidindo quando e com qual termo
   (ele traduz "catarata" para "facectomia" sozinho).
5. **Decisão separada da resposta.** Com o contexto cheio, o modelo de 4B escrevia "vou consultar
   a tabela" em vez de chamar a ferramenta. Separando as etapas, passou a chamar em 4 de 4 casos.
6. **Tudo local.** Modelos abertos rodando via Ollama num PC de escritório, sem enviar dados para
   APIs externas. Num órgão de saúde, é a resposta para a pergunta da LGPD.
7. **Memória ligada ao artigo, não à "pergunta parecida".** Respostas corrigidas por uma pessoa
   ficam em `memoria.md`. Pergunta igual sai na hora; pergunta diferente só recebe a correção se
   ela estiver ligada a um artigo que a busca trouxe. Medido com o modelo de embedding, "O IPASEM
   paga vacina?" e "O IPASEM paga óculos?" (respostas opostas) dão 0,80 de semelhança, mais que
   "paga vacina?" e "cobre vacinas?" (mesma dúvida), 0,59: semelhança não diz se é a mesma dúvida.
   Respostas já geradas ficam num cache (47 s na primeira vez, instantâneo depois), descartado
   sozinho quando código, dados ou correções mudam.
8. **Conversa pela reescrita da pergunta, não pelo histórico no prompt.** "E de clínico geral?"
   vira "Quantas consultas de clínico geral posso fazer?" antes da busca (~5 s a mais). Só essa
   etapa vê a conversa: mandar o histórico para a resposta aumentaria a leitura do prompt, que já
   é a parte mais lenta. Daí em diante tudo funciona igual, inclusive a memória e o cache.
   Só vai para a reescrita o que parece continuação (começa com "e", tem "isso"/"dele", ou é
   curta): o modelo "contaminava" perguntas completas com o assunto da conversa ("regras de
   carência" depois de falar de implante virava "carência para implante"). Uma regra no prompt
   não resolveu; o filtro no código sim.
9. **Sem sorteio onde a tarefa é mecânica.** Com a temperatura padrão, duas rodadas iguais do eval
   discordaram em 5 das 22 decisões de consultar a tabela, e a reescrita deu 5/8 e 4/8 com o mesmo
   prompt. Decisão e reescrita usam temperatura 0; a resposta mantém o sorteio (o Qwen avisa que
   sem ele modelos pequenos entram em repetição), mas com semente fixa: a mesma pergunta dá a
   mesma resposta, e o eval mede só o que mudou.
10. **Rede credenciada: o modelo escreve, o código mostra os fatos.** "Estou com dor nas costas,
    quem procuro?" vira uma consulta à rede (ortopedia), e "moro no Canudos" na conversa traz os
    mais próximos. Na primeira versão o modelo de 4B recebia os contatos e copiava errado
    ("Rua Cideira" no lugar de "Cidreira"), omitia telefones e, quando não havia prestador,
    inventava cidade ("Caxias do Sul"). Agora ele recebe só nomes, bairros e totais; endereço,
    telefone e as cidades onde há prestador aparecem na tela direto do cadastro. Sinais de
    emergência (dor no peito, falta de ar, AVC) mostram um alerta fixo (192 / CVV 188) por regra
    no código. O arquivo da rede fica fora do Git: muda o tempo todo e tem dados de profissionais.
    Quem tem pressa escreve "contato de fono" ou "clinico geral novo hamburgo": essas formas
    curtas também vão para a rede, a cidade dita antes na conversa é lembrada e, sem cidade
    nenhuma, a lista sai para Novo Hamburgo (com "se for outra cidade, é só dizer").
11. **Guia do app e do site: o chatbot manda o segurado fazer sozinho.** "Como cancelo uma
    autorização?", "perdi a guia", "cadê minha carteirinha?" trazem a seção certa do guia de
    funcionalidades (menu e passos; o guia fica fora do Git, veja "Como rodar"), e os links do
    app (Android e iPhone) e do site aparecem na tela, montados pelo código. O guia tem busca
    própria, separada da dos artigos, para não tirar um artigo certo do prompt, e só entra quando
    a pergunta fala do assunto e é parecida o bastante com uma seção. As palavras do assunto
    ficam em `vocabulario.json`, por bloco ("Autorização": autoriz*, ordem, guia...): quando alguém
    pergunta de um jeito novo, basta acrescentar a palavra lá, sem mexer no código.
12. **Botões de assunto: o chatbot sabe onde procurar.** A tela inicial oferece assuntos
    (`menu.json`). Em "Autorizações" e "App, senha e carteirinha", cada opção mostra na hora a
    resposta do guia, com os links e o passo a passo, **sem passar pelo modelo**. Em "Quem atende
    (rede credenciada)", o botão marca o assunto e a pergunta digitada vai só para a rede: sem os
    4 artigos, sem o guia e sem as instruções do regulamento, que a resposta da rede não usa.
    Medido nas mesmas 11 perguntas da rede: **50,7 s -> 24,0 s**, com 11/11 nas duas rodadas. O
    assunto aparece perto da caixa de texto e sai com um clique (o ✕ volta ao caminho completo).

## Resultados

**Busca** (13 perguntas de beneficiário, com o artigo esperado):

| Configuração | Artigo certo em 1º | Entre os 4 primeiros |
|---|---|---|
| Um vetor por artigo | 7/10 | 9/10 |
| + instrução na consulta | 8/10 | 9/10 |
| + pedaços pequenos, resposta com artigo inteiro | 9/10 | 10/10 |
| + ficha do regulamento e resolução do implante (13 perguntas) | **12/13** | **13/13** |

**Tool calling** (o modelo usa a tabela quando precisa?):

| Tentativa | Resultado |
|---|---|
| Ferramenta junto com os artigos | 0 de 3 chamadas |
| Regra como "primeiro passo" | 1 de 2 (na outra, escreveu "vou pesquisar" e parou) |
| Decisão separada da resposta | **4 de 4**, e nenhuma chamada quando não precisava |

**Respostas** (22 perguntas com gabarito; correção automática por fatos obrigatórios e proibidos,
mais uma planilha para revisão humana em `evals/resultados/`):

| Configuração | Respostas certas | Decisão da tabela certa | Tempo médio |
|---|---|---|---|
| Primeira medição (temperatura padrão) | 18/22 | 16/18 | 42 s |
| Decisão com temperatura 0 e semente fixa (atual) | **18/22** | **16/18** | **34 s** |
| Testado e descartado: Ficha do regulamento só quando vem em 1º | 16/22 | 16/18 | 30 s |

Tirar a Ficha (o maior trecho) deixava a resposta 4 s mais rápida, mas em 2 perguntas o modelo
parou de informar o valor da tabela. Os 4 erros que sobram: resposta vaga sobre o que é o
regulamento, carência (a busca não traz o Art. 63 quando a pergunta fala em "consulta"),
psicologia (não consulta a tabela, onde está o limite de 30 sessões) e polissonografia (aplica o
Art. 22 e divide o valor por 2).

**Rede credenciada** (9 perguntas: sintoma sem local, especialidade + cidade, bairro, exame,
emergência, cidade sem prestador e 2 de controle; a régua reprova telefone, cidade ou citação
que não vieram dos dados):

| Versão | Certas | Ferramenta certa |
|---|---|---|
| Modelo copiando os contatos | 4/9 | 8/9 |
| Contatos na tela + rota direta para a rede em "que especialista / onde faço" | 7/9 (régua nova) | 9/9 |
| Cidades também na tela, modelo só com totais | **8/9** | **9/9** |
| +4 casos reais de uso com pressa ("contato de fono", "clinico geral novo hamburgo", "quero marcar uma consulta", cidade dita antes na conversa). Contato/telefone/marcar e especialidade + cidade vão para a rede; cidade lembrada ou Novo Hamburgo (atual) | **13/13** | **13/13** |

O erro que sobra é a polissonografia, o mesmo das 22 perguntas. Com a rede, as 22 continuam em
18/22 e a pergunta dos hospitais passou a ter resposta (2 em Novo Hamburgo). A decisão ficou
alguns segundos mais lenta, porque o prompt descreve duas ferramentas.

**Conversa** (13 casos: 6 continuações e 4 perguntas completas que não podem herdar o assunto da
conversa, uma delas um erro real visto no uso, "moro no Canudos" depois de perguntar sobre dor
nas costas, e 2 pedidos curtos reais: "contato de fono" e "clinico geral novo hamburgo", que
virava "quanto custa"): **12/13**, ~5 s quando reescreve. A que falha:
"e se não fosse emergência?" perde o "tomei posse há 2 meses" da pergunta anterior.

**Guia do app e do site.** Busca (25 perguntas sobre o app/site, e as 22 + as da rede como
perguntas que não podem puxar o guia): seção certa em **23/25**, e **1 de 34** perguntas de fora
puxa o guia (a das autorizações odontológicas, que continua certa). Respostas (8 perguntas, a
régua reprova link escrito pelo modelo e contato que não está no guia): **6/8 certas, 2 parciais**
(o dentista: a seção 4 só diz "mesmo fluxo da consulta médica", sem os passos).

O passo a passo de cada etapa está no [diário de desenvolvimento](docs/historico.md).

## Limitações conhecidas

- **Modelo pequeno, raciocínio lento.** O Qwen3.5 4B erra contas e às vezes liga artigos sem
  relação. Com o modo de raciocínio ligado acerta mais, mas leva minutos na CPU.
- **O gabarito ainda não foi validado por especialistas.** As perguntas de teste foram escritas
  por quem já conhecia o documento.
- **Tabela pode estar desatualizada.** Os valores são reajustados todo ano por ordem de serviço
  (Art. 3º da Res. 33/2024). A versão em uso é a da OS nº 01/2025, vigente desde 01/11/2025: o
  chatbot informa essa data e avisa que pode haver valores mais novos.
- **Conversa curta.** O chat lembra das últimas 3 perguntas, reescrevendo a nova; às vezes a
  reescrita perde a situação contada antes (idade, tempo de posse), e uma pergunta curta e
  completa ("oq posso fazer?") ainda pode herdar o assunto anterior. A tela mostra como a
  pergunta foi entendida, com um botão "Não era isso" que pergunta de novo sem a conversa, e
  "Nova conversa" esquece tudo.
- **Rede: "mais próximo" é por bairro e cidade, não por distância.** O cadastro não tem
  coordenadas, e calcular distância exigiria um serviço de mapas externo. Indicar especialidade
  a partir de sintoma é orientação, não diagnóstico, e o modelo ainda acrescenta regras que
  ninguém pediu (corretas nos testes, mas é texto a mais).
- **A correção automática confere palavras, não entende a resposta.** Ela é uma triagem; a
  avaliação de verdade é a planilha revisada por gente.

## Próximos passos

- [ ] Gabarito e planilha das respostas (`evals/resultados/*.csv`) revisados por servidores da área
- [ ] Corrigir os 4 erros medidos: busca da carência, decisão da psicologia, Art. 22 na polissonografia
- [ ] Comparar o modelo de 4B com um maior (9B) no servidor dedicado, com os mesmos evals
- [ ] Incluir a resolução de inscrição de dependentes (citada no Art. 59, não incorporada)

## Como rodar

Requisitos: Windows 10/11, 16 GB de RAM, [Python 3.10+](https://www.python.org/downloads/) e
[Ollama](https://ollama.com/download).

```powershell
git clone <url-do-repositorio>
cd chatbot-ipasem-rag
.\instalar.bat     # baixa os modelos (~3,8 GB) e instala as bibliotecas
.\iniciar.bat      # abre http://127.0.0.1:5000
```

**Rede credenciada (opcional).** Para o chatbot indicar quem atende cada especialidade, salve a
lista de prestadores como `prestadores.json` na raiz do projeto (exportada do sistema do IPASEM,
no formato `{"total": ..., "rows": [...]}`). Ela não vai para o Git: muda o tempo todo (gente
entra, prestadores saem) e tem endereços e telefones de profissionais. Sem o arquivo, a
ferramenta não é oferecida ao modelo e o resto funciona igual.

**Guia do app e do site (opcional).** Para o chatbot explicar o que o segurado faz sozinho no app
e no site, salve o guia em `docs/funcionalidades-segurado.md` (seções `## N. Título`; as perguntas
frequentes, seção 16, uma por linha começando com `- **Pergunta?**`). Ele não vai para o Git: foi
escrito a partir do sistema do IPASEM e só será publicado com autorização. Sem o arquivo, o guia
não entra no prompt e o resto funciona igual; com ele, o índice (`dados/indice_guia.json`) é
montado sozinho na primeira pergunta.

**Servidor Linux (teste, Debian 10).** Copie a pasta do projeto para fora da pasta publicada pelo
Apache (ex.: `/opt/chatbot-ipasem-rag`; sem a `.venv` do Windows) e rode como root:

```bash
bash scripts/linux/instalar.sh
```

O script cria o usuário `chatbot`, instala o Ollama e os modelos, um Python 3.12 à parte (o do
Debian 10 é 3.7; o do sistema não é tocado), o serviço `chatbot` (sobe com a máquina) e a
configuração do Apache que repassa `http://<servidor>/chatbot/` para o chatbot, que continua
aceitando só conexões da própria máquina. Para testar numa página do site:
`<iframe src="/chatbot/" style="width:420px;height:640px;border:0"></iframe>`.

Os dados já processados estão em `dados/`, então os PDFs não são necessários para rodar. Para
regerar tudo a partir dos documentos originais (públicos no site do IPASEM), salve-os em
`docs/fontes/` e rode:

```powershell
.\.venv\Scripts\python.exe preparar_documento.py docs\fontes\regulamento.pdf
.\.venv\Scripts\python.exe busca.py indexar
.\.venv\Scripts\python.exe tabela.py preparar docs\fontes\tabela_unica_os_01_2025.pdf
```

Outros comandos:

```powershell
.\.venv\Scripts\python.exe rag.py "O plano cobre óculos?"      # pergunta pelo terminal
.\.venv\Scripts\python.exe busca.py "carência"                  # só a busca
.\.venv\Scripts\python.exe tabela.py "implante dentario"        # só a tabela
.\.venv\Scripts\python.exe avaliar_busca.py                     # eval da busca
.\.venv\Scripts\python.exe avaliar_respostas.py                 # eval das respostas (~15 min)
.\.venv\Scripts\python.exe avaliar_respostas.py --so 7,20       # só algumas perguntas
.\.venv\Scripts\python.exe avaliar_conversa.py                  # eval da reescrita na conversa
.\.venv\Scripts\python.exe rede.py ortopedia "novo hamburgo" centro   # só a rede credenciada
.\.venv\Scripts\python.exe avaliar_respostas.py --arquivo evals\perguntas_rede.json   # eval da rede
.\.venv\Scripts\python.exe memoria.py                           # confere as respostas validadas
.\.venv\Scripts\python.exe memoria.py limpar-cache              # apaga as respostas guardadas
```

## Estrutura

| Arquivo | Papel |
|---|---|
| `preparar_documento.py` | Lê o PDF, aplica as regras de vigência e gera um trecho por artigo + a ficha |
| `busca.py` | Embeddings e busca por significado |
| `tabela.py` | Extrai a tabela única (OS 01/2025) e expõe a ferramenta `consultar_tabela` |
| `rag.py` | Orquestra busca, decisão (tool calling) e resposta, em streaming |
| `rede.py` | Rede credenciada (opcional): ferramenta `consultar_rede` por especialidade, cidade e bairro |
| `guia.py` | Guia do app e do site (opcional, `docs/funcionalidades-segurado.md` fora do Git): busca própria e links na tela |
| `memoria.py`, `memoria.md` | Respostas validadas por uma pessoa e cache das respostas já geradas |
| `app.py`, `web/index.html` | Servidor local e interface de chat |
| `avaliar_busca.py`, `avaliar_respostas.py`, `avaliar_conversa.py`, `avaliar_guia.py`, `evals/` | Evals da busca, das respostas (correção automática + planilha para revisão humana) e da conversa; resultados em `evals/resultados/` |
| `dados/` | Dados já processados (trechos, índice, tabela, itens descartados) |
| `docs/` | Diário de desenvolvimento, inventário das resoluções, transcrições |
| `claude_v0.py` | Primeira versão, com a API do Claude (opcional, precisa de chave) |

## Stack

Python · Ollama · Qwen3.5 4B · Qwen3-Embedding 0.6B · Flask · pypdf · HTML/JS puro

## Licença

Código sob licença MIT. Os documentos do IPASEM são atos públicos e não estão incluídos no
repositório.
