# Diário de desenvolvimento

Registro de cada etapa, com as tentativas que deram errado e os números de antes e depois.
O resumo está no [README](../README.md).

## Roteiro

| Versão | O que muda | Status |
|---|---|---|
| v0 | Claude via API: PDF inteiro no contexto, citações nativas com página, cache do documento | código pronto (`claude_v0.py`), não usado na versão local |
| v1 | RAG 100% local (Ollama): trechos por artigo, embeddings, busca, resposta com modelo pequeno | feito |
| v1.1 | Resoluções complementares e tabela única de 2024 como ferramenta (tool calling) | feito |
| tabela 2025 | Fonte da tabela passa a ser a OS 01/2025: citação com data de vigência e páginas do documento oficial | feito |
| evals | Perguntas com gabarito validado por especialistas, script que mede os acertos | em andamento |

## 1. Escolha do modelo local

Requisitos: rodar em um PC de escritório (Ryzen 5 de baixo consumo, 16 GB de RAM, sem placa de
vídeo dedicada), bom em português, licença aberta, fonte oficial.

- Resposta: `qwen3.5:4b` (Alibaba/Qwen, Apache 2.0, 3,4 GB). Cerca de 10 tokens/s na CPU.
- Embeddings: `qwen3-embedding:0.6b` (639 MB, multilíngue). O `nomic-embed-text`, comum em
  tutoriais, foi descartado por ser fraco fora do inglês.

Primeiro teste, com um trecho de regulamento inventado: sem raciocínio, o modelo errou uma conta
de carência ("2 meses de adesão, carência de 180 dias: já pode operar?" respondeu "sim") em 15 s.
Com o modo de raciocínio, acertou e citou o artigo, mas levou 3 min 11 s. Esse dilema entre
qualidade e tempo aparece no projeto inteiro.

## 2. Preparação do documento

O PDF do IPASEM é uma versão compilada: mantém a redação original de 2012 ao lado de todas as
alterações, com notas como "(Redação dada pela Resolução nº 07...)" e "(Revogado pela...)".
Sem tratamento, o chatbot poderia responder com uma regra que não vale mais e citar a página
como prova. Exemplo: a regra de psiquiatria aparece em três versões na mesma página (2012: duas
consultas por mês; 2014: sem limite; 2021: doze por ano).

Ferramentas que convertem PDF em Markdown (Docling, MarkItDown) foram avaliadas e descartadas
para este documento: o texto é digital, em uma coluna e sem tabelas, e a conversão perderia o
número das páginas, que é necessário para as citações.

`preparar_documento.py` aplica estas regras:

| Situação no PDF | O que o script faz |
|---|---|
| Cabeçalho do IPASEM repetido em toda página | Remove linhas que aparecem em mais da metade das páginas |
| Item com "(Revogado...)" | Descarta |
| Título de seção revogado | Descarta tudo até o próximo título (Seção VI, dependência química) |
| Item com "(Redação dada...)" | Descarta a versão anterior de mesmo rótulo no mesmo artigo |
| "Parágrafo único" virou "§1º" ou o contrário | Trata os dois rótulos como equivalentes |
| Artigo inteiro reescrito | Descarta também os parágrafos da versão antiga (caso do Art. 59) |
| Item "(Renumerado...)" | Descarta a cópia com o número antigo |

Resultado: 296 dispositivos, 95 descartados, 71 trechos (um por artigo, com seção e página).
Tudo que foi descartado fica em `dados/removidos.json` com o motivo, para auditoria.

Decisão de interpretação registrada: a redação de 2019 do Art. 59 remete a inscrição de
dependentes a "resolução própria". Os §§ 1º a 4º antigos foram tratados como sem efeito.

## 3. Busca

Eval da busca: perguntas de beneficiário, cada uma com o artigo que deveria ser encontrado.

| Configuração | Artigo certo em 1º | Artigo certo entre os 4 primeiros |
|---|---|---|
| Um vetor por artigo, sem instrução na consulta | 7/10 | 9/10 |
| Um vetor por artigo, com instrução na consulta | 8/10 | 9/10 |
| Pedaços pequenos (parágrafo/alínea + caput), resposta com artigo inteiro | 9/10 | 10/10 |
| + "Ficha do regulamento" (preâmbulo, Art. 1 e sumário), 2 perguntas gerais a mais | 11/12 | 12/12 |
| + resolução do implante dentário, 1 pergunta a mais | 12/13 | 13/13 |

- O erro que motivou os pedaços pequenos: "O IPASEM paga vacina?" não encontrava o Art. 51,
  porque ele lista 22 exclusões e a alínea "v) vacinas" ficava diluída no vetor do artigo.
- A ficha resolve perguntas gerais ("o que é o regulamento?", "onde fala de reembolso?"), que
  nenhum artigo isolado responde. Ela é gerada a partir dos títulos do próprio documento. Com a
  ficha encontrada, o modelo só a usou bem com o raciocínio ligado (30 s sem raciocínio: resposta
  genérica e sem citação; 4 min 44 s com raciocínio: resposta completa e citada).
- Limitação: as perguntas foram escritas por quem já conhecia o documento.

## 4. Resoluções complementares

38 PDFs de resoluções foram triados (ver [inventário](inventario_resolucoes.md)): 25 tabelas
revogadas pela Res. 33/2024, 5 alterações já incorporadas ao texto compilado, 4 fora do assunto.
Entraram:

- **Res. 09/2014 e 17/2016 (implante dentário):** escaneadas, transcritas em
  `docs/complementares/` com a alteração de 2016 aplicada e uma "nota de vigência": o valor de
  R$ 2.000,00 de 2014 foi atualizado para R$ 2.499,00 na tabela de 2024.
- **Res. 33/2024 (tabela única):** ver a próxima seção.

## 5. Tabela de procedimentos (tool calling)

A tabela única da Res. 33/2024 (164 páginas) não entra na busca por significado: preço e
código precisam vir exatos. `tabela.py` extrai 4.606 itens (código, descrição, valor em R$ ou
em CH, notas de rodapé) e 9 blocos de pacotes. O modelo consulta por uma ferramenta.

A tabela tem três formatos: valores em R$ (exames, materiais, odontologia), em CH (honorários
médicos; 1 CH = R$ 0,85, lido da p. 5) e pacotes em texto livre. As notas de rodapé trazem regras
úteis ao beneficiário, como "endodontia especial: laudo, perícia e 50% de coparticipação" e o
limite de 30 sessões anuais de psicologia.

| Tentativa | Resultado |
|---|---|
| Ferramenta junto com os artigos, regra no fim do prompt | Não chamou a ferramenta em nenhuma das 3 perguntas |
| Regra da ferramenta como "primeiro passo" | Chamou em 1 de 2. Na outra escreveu "vou pesquisar na tabela" e parou |
| Decisão separada da resposta | Chamou em 4 de 4 e não chamou quando não precisava |
| Resultado em JSON para a resposta | Disse que "canal especial" não estava na tabela, mas estava |
| Resultado em texto corrido | Acertou a coparticipação de 50% do canal especial |

Limites que continuam (modelo de 4B): às vezes liga artigos sem relação (ex.: aplica o Art. 22,
de alta complexidade, à polissonografia) e inventa pedidos estranhos.

Ponto da tabela a validar: o código 40103528 (polissonografia) aparece com dois valores
(R$ 840,00 na p. 103 e R$ 470,00 na p. 108 da OS 01/2025; p. 102 e 107 no PDF da Res. 33/2024).

### Tabela atualizada pela Ordem de Serviço nº 01/2025

O Art. 3º da Res. 33/2024 manda atualizar a tabela por ordem de serviço. A de 2025 é a OS
IPASEM nº 01/2025 (18/11/2025, vigente desde 01/11/2025), que republica o Anexo I inteiro.

Comparando com o PDF da Res. 33/2024 baixado do site, a surpresa: ele **já tinha o anexo de
2025**. As seções dizem "2025", os dois arquivos foram gerados no mesmo dia (16/12/2025) e as 162
páginas de tabela são idênticas: 4.606 itens, 0 valores diferentes, CH a R$ 0,85 nos dois. O site
colou o anexo novo dentro da resolução antiga.

Os valores estavam certos; a citação não. O chatbot dizia "tabela única de 2024" e citava páginas
de um documento que não é o ato que fixou os valores. O que mudou:

- A fonte passa a ser o PDF da OS (`docs/fontes/tabela_unica_os_01_2025.pdf`). A OS ocupa as
  p. 1-2, então cada item fica uma página à frente (implante: p. 164 -> p. 165).
- Fonte, citação e data de vigência ficam em constantes no `tabela.py` (`FONTE`,
  `FONTE_CITACAO`, `VIGENCIA`) e chegam ao aviso, ao prompt e ao resultado da ferramenta. Na
  próxima ordem de serviço, troca-se num lugar só.
- O chatbot informa "valores vigentes desde 01/11/2025" em vez de só avisar que a tabela muda.
- Conferido depois de regerar: só a página mudou nos 4.606 itens; nos trechos, só a nota de
  vigência do implante; busca igual (12/13 e 13/13).

## v0: Claude via API (`claude_v0.py`)

Versão inicial, antes da decisão de rodar tudo localmente:

- **Sem RAG.** Um regulamento de 29 páginas cabe inteiro no contexto do modelo, e mandar tudo
  evita erro de busca.
- **Citações nativas da API.** O PDF vai com `citations: {enabled: true}` e a resposta vem com a
  página e o trecho exato usado.
- **Cache do documento.** A primeira pergunta paga o PDF inteiro; as seguintes leem do cache.

Para rodar, defina a chave e passe o PDF:

```powershell
$env:ANTHROPIC_API_KEY = "sk-ant-..."
.\.venv\Scripts\python.exe claude_v0.py docs\fontes\regulamento.pdf
```
