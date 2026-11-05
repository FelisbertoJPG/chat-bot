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

O passo a passo de cada etapa está no [diário de desenvolvimento](docs/historico.md).

## Limitações conhecidas

- **Modelo pequeno, raciocínio lento.** O Qwen3.5 4B erra contas e às vezes liga artigos sem
  relação. Com o modo de raciocínio ligado acerta mais, mas leva minutos na CPU.
- **O gabarito ainda não foi validado por especialistas.** As perguntas de teste foram escritas
  por quem já conhecia o documento.
- **Tabela pode estar desatualizada.** Os valores são reajustados todo ano por ordem de serviço
  (Art. 3º da Res. 33/2024). A versão em uso é a da OS nº 01/2025, vigente desde 01/11/2025: o
  chatbot informa essa data e avisa que pode haver valores mais novos.
- **Cada pergunta é independente.** O chat não lembra das anteriores.

## Próximos passos

- [ ] Eval das respostas: 22 perguntas com gabarito revisado por servidores da área
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
| `memoria.py`, `memoria.md` | Respostas validadas por uma pessoa e cache das respostas já geradas |
| `app.py`, `web/index.html` | Servidor local e interface de chat |
| `avaliar_busca.py`, `evals/` | Evals: perguntas de teste e gabaritos |
| `dados/` | Dados já processados (trechos, índice, tabela, itens descartados) |
| `docs/` | Diário de desenvolvimento, inventário das resoluções, transcrições |
| `claude_v0.py` | Primeira versão, com a API do Claude (opcional, precisa de chave) |

## Stack

Python · Ollama · Qwen3.5 4B · Qwen3-Embedding 0.6B · Flask · pypdf · HTML/JS puro

## Licença

Código sob licença MIT. Os documentos do IPASEM são atos públicos e não estão incluídos no
repositório.
