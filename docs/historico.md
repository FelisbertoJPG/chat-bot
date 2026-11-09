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
| evals | Perguntas com gabarito validado por especialistas, script que mede os acertos | script pronto (18/22); falta a validação por especialistas |
| conversa | Perguntas de continuação pela reescrita da pergunta; decisão e reescrita sem sorteio | feito (10/11) |
| rede | Rede credenciada como ferramenta (quem atende, onde), contatos e cidades direto do cadastro na tela, alerta de emergência | feito (8/9) |

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

## Eval das respostas, conversa e o sorteio do modelo

### A régua: `avaliar_respostas.py`

Roda as 22 perguntas do gabarito sem memória e sem cache e corrige cada resposta por regex:
fatos que precisam aparecer ("12 consultas"), coisas proibidas (a regra revogada "duas por mês",
um valor em R$ inventado) e se a tabela devia ser consultada. Cada rodada gera também uma planilha
para revisão humana. Antes de confiar na régua, ela corrigiu o próprio gabarito e foi ajustada
quando puniu respostas certas: "artigos de 53 a 55" não casava com "Art. 53", "Artigo 51" por
extenso, "paga a metade" em vez de "50%", "internação psiquiátrica ilimitada" caía na proibição
de "consultas ilimitadas". Para não gastar 15 minutos a cada ajuste, `--recorrigir` aplica os
critérios novos às respostas já salvas.

Primeira medição: **18/22**, 16/18 decisões da tabela certas, 42 s por resposta.

### O sorteio atrapalhava a medição

A segunda rodada (testando a Ficha só em 1º) mudou coisas que a mudança nem tocava: tempo de
perguntas não afetadas variou 20 s, uma pergunta não afetada caiu de nota e **5 das 22 decisões de
consultar a tabela mudaram**. A causa é a temperatura: o modelo sorteia cada palavra. Para o
usuário isso também é ruim (a mesma pergunta às vezes recebe o valor da tabela, às vezes não).

- Decisão da tabela e reescrita: **temperatura 0** (são tarefas mecânicas).
- Resposta: mantém o sorteio, porque o Qwen avisa que sem nenhum sorteio modelos pequenos entram em
  repetição, mas com **semente fixa**. Conferido: a mesma pergunta duas vezes deu texto idêntico
  (e a segunda levou 9,8 s em vez de 27,8 s, pelo cache de leitura do Ollama).

Com isso, a configuração atual mede **18/22, 16/18, 34 s** e duas rodadas iguais dão o mesmo
resultado. O tempo ainda varia entre rodadas pelo cache de leitura, então diferenças pequenas de
tempo não são conclusivas.

### Ficha só quando vem em 1º: testado e descartado

A Ficha do regulamento é o maior trecho (3.703 caracteres). Em 6 das 22 perguntas ela vinha em
2º-4º lugar numa pergunta específica. A ideia: mandar a Ficha só quando ela vem em 1º (pergunta
geral) e pôr o próximo artigo no lugar. A busca não perdia nada (13/13), e o texto enviado caía de
35% a 80% nessas perguntas. Medido com sorteio controlado:

| | Ficha sempre | Ficha só em 1º |
|---|---|---|
| Respostas certas | 18/22 | 16/22 |
| Tempo médio | 34,3 s | 30,2 s |

As duas que pioraram (implante e catarata) são das afetadas: sem a Ficha, o modelo parou de dizer
o valor da tabela (R$ 2.499, R$ 1.275). Perder o dado principal por 4 s não compensa; a regra saiu.

### Conversa: reescrever a pergunta

O chat passou a lembrar das últimas 3 trocas, sem mandar a conversa para a resposta (seria mais
texto para ler, e a leitura já é a parte lenta). Uma etapa curta reescreve a pergunta de
continuação para ela ficar completa, e daí em diante tudo funciona igual. Eval próprio
(`avaliar_conversa.py`, 8 casos: 6 continuações, 1 pergunta já completa que não pode ser
"contaminada" pela conversa e 1 com duas trocas).

| Versão do prompt | Resultado |
|---|---|
| Primeira, temperatura padrão | 8/8 na régua, mas lendo: "e quantos posso fazer?" (implante) virou "quanto custa" |
| + "mantenha o que a pessoa quer saber", régua mais rigorosa | 7/8, depois 5/8 e 4/8 na mesma versão (sorteio) |
| + "traga a situação da pessoa", temperatura 0 | 6/8 nas duas rodadas: pôs "consulta de psiquiatria" numa pergunta sobre óculos |
| Sem essa regra, temperatura 0 (atual) | **7/8 nas duas rodadas**, ~5 s |

A que falha: "e se não fosse emergência?" perde o "tomei posse há 2 meses". Ajustar o prompt até
passar seria decorar a prova com 8 casos; fica como limitação, e a tela mostra como a pergunta foi
entendida. Os exemplos do prompt usam assuntos que não estão nos testes (fisioterapia,
dermatologia), para não maquiar o resultado.

**Erro visto no uso:** depois de perguntar sobre vacina e óculos, "O que posso fazer de acordo com
o regulamento da assistência?" (pergunta nova, geral) foi entendida como "...quando o plano não
cobre óculos ou vacinas?". Virou teste, junto com outra mudança de assunto ("regras de carência"
depois de falar de implante, que virava "carência para implante"). Os dois falharam: 7/10.

| Correção | Resultado | Chamadas ao modelo |
|---|---|---|
| Regra + exemplo no prompt ("pergunta que traz o próprio assunto sai igual") | 7/10 | 10 |
| Filtro no código: só reescreve o que começa com "e"/"mas", tem "isso"/"dele"/"também" ou tem até 7 palavras | **9/10** | 8 |
| Os dois juntos | 9/10 | 8 |

O modelo de 4B ignorou a regra; o filtro resolveu e ainda poupa ~5 s nas perguntas longas. Ficou
só o filtro. Ele não pega pergunta curta e completa ("oq posso fazer?"), então a tela ganhou o
botão "Não era isso: perguntar sem a conversa", que também tira a troca mal entendida da conversa.

## Rede credenciada

O cadastro de prestadores (exportado do sistema do IPASEM, mais atual que o PDF do site) tem
1.835 linhas: 1.165 prestadores, 97 especialidades, 20 cidades. Fica fora do Git: muda o tempo
todo e tem nomes, endereços e 354 celulares de profissionais. Mesmo molde da tabela: dados
estruturados + ferramenta (`consultar_rede(especialidade, cidade, bairro)`), sem busca por
significado. Nome popular -> especialidade pelo começo da palavra ("cardiologista" ~
"cardiologia") e alguns sinônimos ("dentista" -> odontologia); grafias diferentes do mesmo lugar
unificadas ("H VELHO", "JARDIM MAUÁ"). Sem cidade nem bairro, a ferramenta não lista ninguém: só
conta, e o chatbot pedia o local (depois mudou: veja "O usuário com pressa").

| Versão | Rede (9) | O que aconteceu |
|---|---|---|
| Modelo recebe os contatos e os escreve | 4/9 | "dor nas costas, que especialista?" não consultou a rede; endereço copiado errado ("Rua Cideira"); telefones omitidos |
| Contatos só na tela; rota direta para a rede quando a pergunta diz "que especialista", "quem procuro", "onde faço" | 7/9 | Régua nova (telefone/citação inventados) pegou "código DER-CONSULTA, p. Y" e depois "Caxias do Sul", que não estava no resultado |
| Instrução "cite só cidades do resultado" | 7/9 | Continuou inventando Caxias do Sul |
| Cidades também só na tela; modelo recebe totais e nomes | **8/9** | Sobra a polissonografia (erro antigo) |

A lição se repetiu três vezes: pedir ao modelo de 4B que não invente não basta; o que funciona é
não dar a ele o fato para copiar e mostrar o fato direto do cadastro. Hospitais ("Quais hospitais
são credenciados?") não são especialidade: a ferramenta filtra pelo tipo de prestador, e a
pergunta 22 do eval principal, que antes só podia dizer "não encontrei", passou a listar os 2 de
Novo Hamburgo. As 22 continuam em 18/22 com a ferramenta nova.

Emergência (dor no peito, falta de ar, AVC, ideação suicida): alerta fixo na tela (192, CVV 188)
por regra no código, e a resposta continua, porque a pergunta pode ser de cobertura ("o plano
cobre tratamento de AVC?"). Os resultados dos evals da rede ficam fora do Git (citam nomes).

### O usuário com pressa

Uma conversa real, de quem quer o contato e não tem paciência:

| Mensagem | O que o chatbot fez | Por quê |
|---|---|---|
| "quero marcar uma consulta" | Regra do encaminhamento + "o valor não está nos documentos" | Ninguém pediu valor; não perguntou a especialidade |
| "contato de fono" | Procurou no regulamento, "não encontrei o contato" | "Contato" não estava na rota da rede |
| "clinico geral novo hamburgo" | Consultou a tabela: "não achei o valor" | A reescrita inventou "Quanto custa...?" |
| "quero contato de clinico geral, tem na rede?" | Rede, mas pediu a cidade de novo | A cidade dita na troca anterior não era lembrada |

"Treinar" o modelo (ajuste fino) não resolveria: os erros não são de conhecimento, são de rota, e
rota se conserta com regra medida. O que mudou (`rag.pede_prestador`):

- **Rota:** "contato", "telefone", "endereço", "quero/preciso marcar" e especialidade + cidade da
  rede sem falar de valor vão para a rede. A regra olha o que a pessoa escreveu e a reescrita.
  "Posso marcar consulta eletiva?" continua sendo pergunta de regra (só "quero/preciso marcar").
- **Cidade:** sem lugar na pergunta, vale a cidade das perguntas anteriores (nunca das respostas)
  e, sem nenhuma, Novo Hamburgo, onde está a maior parte da rede. A lista sai na hora e a
  resposta diz para avisar se for outra cidade.
- **"Quero marcar" sem especialidade:** a decisão não chama nada e a resposta recebe uma
  instrução para pedir a especialidade, sem falar de valor nem de tabela.
- **Reescrita:** regra e exemplo novos: especialidade + lugar é "Quem atende X em Y?".

Resultados: rede 13/13 (os 9 de antes + os 4 da conversa, um deles com a conversa anterior, que
o eval passou a aceitar), reescrita 12/13 (a que falha é a mesma de antes, "e se não fosse
emergência?"). Nenhuma das 22 perguntas mudou de rota, então elas não foram rodadas de novo; a
dos hospitais, que passa pelo prompt da rede, continua certa.

Duas tentativas pioraram, e mostram como o modelo de 4B reage ao que lê: com "0 no bairro" no
resultado (a pessoa nem falou de bairro), ele escrevia "nenhum no bairro que você pediu"; trocando
por "a pessoa não disse o bairro", passou a falar só disso e inventou "não há valor coberto". O
que funcionou foi tirar a frase: sem bairro, o resultado diz só quantos há na cidade.

## Guia do app e do site

O autor escreveu `docs/funcionalidades-segurado.md` a partir do sistema: o que o segurado faz
sozinho pelo app IPASEM NH Digital e pelo site (autorizações, 2ª via, cancelar, carteirinha,
extratos, senha, acesso dos dependentes), em 16 seções. A ideia: em vez de só responder,
mandar a pessoa para o lugar onde ela resolve. Como foi tirado do código do sistema, o guia fica
fora do Git até a publicação ser autorizada, junto com o índice e os resultados dos evals dele
(que copiam o texto); sem o arquivo, o chatbot funciona como antes.

**Por que uma busca separada.** Jogar as seções no mesmo índice dos artigos as faria disputar as 4
vagas do prompt; o Art. 51 (óculos) já vem em 4º. Então `guia.py` tem índice próprio
(`dados/indice_guia.json`) e manda no máximo 1 seção, e só quando ela serve.

**Quando a seção serve** (`avaliar_guia.py`: 25 perguntas do app/site com a seção esperada; as 22 e
as da rede como perguntas que não podem puxar o guia):

| Versão | Seção certa em 1º | Positivas que passam | De fora que puxam o guia |
|---|---|---|---|
| Uma seção = um pedaço inteiro + itens, limiar 0,60 | 19/25 | 16/25 | 3/34 |
| Sem o pedaço "seção inteira" (vetor genérico atraía tudo) | 20/25 | 16/25 | 3/34 |
| Sem a seção 1 na busca (tabela-resumo que ganhava das seções com passo a passo) | 22/25 | 17/25 | 3/34 |
| Duas pistas: palavra do assunto + nota 0,50, ou nota 0,70 sem palavra | 22/25 | 22/25 | 2/34 |
| Perguntas frequentes uma a uma; "marcar" só em "quero/preciso marcar" | 21/25 | 20/25 | 1/34 |
| Pedaços sem e-mail, telefone e negrito; limiar 0,45 com palavra; gabarito corrigido* | **23/25** | **23/25** | **1/34** |

\* "perdi a guia da consulta" trazia a seção 3, que tem "Perdeu a autorização? Use a 2ª via": a
resposta estava lá, o gabarito é que só aceitava as seções 6 e 16.

Só a nota não separa os dois grupos: perguntas do app começavam em ~0,50, e "meu filho fez 21 anos,
continua dependente?" (regra) dava 0,65 com "acesso dos dependentes". A palavra do assunto é a
pista forte; a nota escolhe a seção. A que puxa o guia sem ser do app é "Quantas autorizações
odontológicas minha família tem por mês?" (traz a seção da autorização odontológica), e a
resposta continuou certa.

**O "marcar" que inventou carência.** Na primeira versão, "marcar" contava como assunto do guia. A
pergunta 10 das 22 ("Tomei posse há 6 meses. Já posso marcar uma consulta eletiva?") trouxe a seção
de acesso, que tem a mensagem de carência do app mas não diz quanto ela dura, e o modelo escreveu
"a carência é de 12 meses" citando o guia. A pergunta já errava antes (a busca não traz o Art. 63),
mas passou a errar inventando. Agora "marcar" só conta em "quero/preciso/como marcar".

**Links e contatos.** Os links das lojas e do site ficam no código (`guia.LINKS`) e aparecem na tela
como botões; os endereços que estão no texto da seção são trocados por "(link na tela)" antes de ir
para o modelo. A régua (`perguntas_guia_respostas.json`) reprova link escrito pelo modelo e
telefone, e-mail ou ramal que não estejam no guia.

| Respostas (8) | Certas | O que aconteceu |
|---|---|---|
| Primeira versão | 5/8, 2 parciais, 1 erro | "Exame negado, e agora?" trouxe a seção 15 e o modelo inventou "o cancelamento não desmarca a data" |
| Versão atual | **6/8, 2 parciais** | Parciais: a consulta sem citar o prestador; o dentista (a seção 4 só diz "mesmo fluxo da consulta médica", e o modelo foi buscar implante nos artigos) |

As 22 não foram rodadas inteiras: só a 5 passa a receber o guia (continua certa) e a 10 voltou a
ser como antes. Na rede, "quero marcar uma consulta" passou a dizer para emitir a autorização no
app ou no site e perguntar a especialidade.

**Erro de digitação.** No uso, "quero tirar uma autroização de exame" respondeu só com o Art. 18,
sem falar do app: a busca achou a seção certa (5, nota 0,649), mas a regex procura "autoriza", e
sem palavra do assunto o limiar é 0,70. Agora cada palavra longa da pergunta é comparada com uma
lista (`PALAVRAS_ASSUNTO`: autorização, carteirinha, cancelar, extrato...) e, parecida o bastante
(semelhança >= 0,85: letra trocada, faltando ou sobrando), conta. Nenhuma das 34 perguntas de fora
mudou; com 2 casos de digitação no eval, 25/27.

**O vocabulário vira arquivo.** "Por onde tiro ordem médica?" também ficou sem o guia: muita gente
chama a autorização de "ordem" (o próprio sistema fala em "reimpressão de ordens"), e a palavra
não estava na lista. Em vez de mais uma palavra escondida no código, as palavras do assunto saíram
da expressão regular do `guia.py` para o `vocabulario.json`, em blocos ("Autorização", "Cancelar
autorização", "Carteirinha"...), com três jeitos de escrever: palavra exata (`ordem`), começo de
palavra (`cancel*`) e expressão (`segunda via`). O autor acrescenta a palavra no bloco certo e já
vale, sem reiniciar; JSON quebrado não derruba o chat (avisa no log e fica a versão anterior). A
troca foi conferida antes de qualquer palavra nova: nas 98 perguntas dos evals e testes, o arquivo
deu exatamente o mesmo resultado da regex (0 diferenças). Com "ordem" e "ordens" em Autorização e
2 casos novos no eval, 27/29, e as 34 de fora continuam iguais.

## Botões de assunto

"Quero cancelar 3122505" levou 233 s na VM: o modelo tomou o número da autorização por código de
procedimento, consultou a tabela à toa, e a busca mandou 4 artigos sem relação, quando a resposta
útil era a seção 7 do guia. A ideia do autor: deixar a pessoa escolher o assunto num botão e, com
isso, o chatbot já saber onde procurar.

- **Respostas prontas do guia** (`menu.json`, `guia.menu()`, `/api/menu`): "Autorizações" e "App,
  senha e carteirinha" abrem opções (tirar autorização de consulta, cancelar, 2ª via, senha,
  carteirinha...). Cada uma mostra a resposta curta das perguntas frequentes do guia, os links e o
  passo a passo da seção, **sem o modelo**. O `menu.json` só tem a estrutura (título do botão e a
  pergunta frequente ou a seção que ele mostra); o texto vem do guia, que segue fora do Git. Sem o
  guia, esses botões não aparecem. `guia.py menu` confere o arquivo e aponta referência quebrada.
- **Assunto que guia o modelo:** o botão marca o assunto, que vai junto com as perguntas
  digitadas (`responder(assunto=...)`, entra na chave do cache). "Rede": só a ferramenta da rede,
  sem os 4 artigos, sem o guia e com instruções curtas (`SYSTEM_SO_REDE`) no lugar das ~700 do
  regulamento. "Autorizações"/"App": o guia entra mesmo sem palavra do vocabulário, e nenhuma
  ferramenta é oferecida.
- **Sem botão também:** pergunta que casa com o guia pelo vocabulário e não fala de valor não
  recebe a tabela, e a etapa de decisão nem roda. As instruções do guia agora dizem que o chatbot
  não vê nem altera autorizações ("não consigo fazer por aqui; você faz pelo app ou pelo site").

| Medição | Resultado |
|---|---|
| Guia, 9 respostas (com "quero cancelar 3122505") | 9/9 (antes 6/8 + 2 parciais); o 3122505 sem consultar a tabela |
| Rede, 11 perguntas, sem o assunto | 11/11, 50,7 s em média |
| As mesmas 11, com o assunto "rede" | 11/11, **24,0 s** em média; "quero marcar uma consulta" 42 s -> 9,6 s |

Na rodada com assunto, duas respostas escreveram "53 clínicas gerais" e a régua só aceitava o
singular: o critério passou a aceitar o plural e as duas rodadas foram recorrigidas sem modelo
(`--recorrigir`). As 11 perguntas passaram sem os artigos, inclusive as que a rota da rede pega
sozinha (sem botão); aplicar o mesmo corte quando a rota da rede é detectada automaticamente é o
próximo passo natural, a medir.

## A conversa começa pelo assunto

Polimento da tela, a pedido do autor, testando ao vivo:

- **Sem cabeçalho nem perguntas de teste.** A lateral tem só "Nova conversa" (os evals seguem no
  terminal). A tela abre com "Escolha um assunto para iniciar a conversa", em lista, e a caixa de
  texto só libera depois: o assunto já leva o contexto. Entrou o assunto "Cobertura, regras e
  valores" (o caminho completo), senão "o plano cobre óculos?" não teria por onde entrar.
- **Troca de assunto** (`rag.sugerir_assunto`, `avaliar_assunto.py`): três sinais, um por assunto:
  pedir prestador (rede), palavra do vocabulário de um bloco (autorizações ou app, pelos "blocos"
  do `menu.json`) e palavra de regra (`_RE_REGRA`: cobre, carência, quantas, valor...). Só pergunta
  "Quer mudar de assunto?" quando há sinal de **um** outro assunto e nenhum do atual; nome de
  especialidade sozinho conta como rede só sem outro sinal ("o plano cobre cirurgia?" é regra).
  Primeira versão 19/22: Autorizações e App eram tratados como um só assunto, e "tem
  dermatologista em Gramado?" não tinha sinal (Gramado não tem prestador). Corrigido: 25/25. A
  pergunta de troca sai antes do modelo (menos de 1 s); "Não, continuar" manda de novo com
  `manter_assunto`, como os evals das respostas.
- **Rede por quadro, sem modelo:** cidade obrigatória (lista suspensa, com quantos prestadores) e
  especialidade opcional (lista com pesquisa, só as da cidade). Uma primeira versão com botões de
  especialidade abria "um quadro muito grande"; o autor preferiu a cidade primeiro. O cadastro tinha
  "SAO LEOPOLDO" e "SÃO LEOPOLDO" como cidades diferentes: a lista junta pela forma normalizada.
  A lista de contatos (também na pergunta escrita) mostra 5, "Ver todos" e filtro por bairro; o
  modelo continua recebendo só 5 nomes (a lista completa vai em `_todos`, que não vai ao modelo).
- **Registro das conversas** (`registro.py`, `dados/registro/AAAA-MM.jsonl`, fora do Git): cada
  pergunta com assunto, reescrita, artigos, guia, ferramentas, origem, tempo e resposta; e os
  cliques (assunto, resposta pronta, quadro da rede, aceitar ou recusar a troca). CPF, telefone e
  e-mail são mascarados antes de gravar; o número de uma autorização fica. `CHATBOT_REGISTRO=0`
  desliga.
- **Primeira resposta validada no `memoria.md`:** "tempo pra autorizar um exame" (e 8 formas
  parecidas). O modelo respondeu o prazo certo, mas acrescentou "carência de 10 meses para quem não
  é titular", que estava errado (misturou o Art. 63). Agora sai do texto conferido, em 0 s.

## Profissional pelo nome

"Quero o doutor Fernando", com Análises Clínicas escolhida no quadro da rede, voltou a lista de
análises clínicas: a ferramenta da rede só procura por especialidade e lugar, e a reescrita (a
pergunta é curta, parecia continuação) trocou o pedido pelo assunto anterior.

- `rag.pedido_de_nome`: doutor, doutora, dr. ou dra. + nome. Vai direto ao cadastro
  (`rede.buscar_nome`: cada palavra do nome como começo de uma palavra do nome do prestador),
  **antes da reescrita e sem modelo** (0,0 s).
- A página manda o que está escolhido no quadro (`filtro_rede`: cidade e especialidade). Se a
  pessoa está lá, mostra o contato. Se não está, diz onde não está ("Em Análises Clínicas em Novo
  Hamburgo não há ninguém com o nome Fernando"; "Não encontrei nenhum profissional com o nome Pinto
  em Campo Bom. Quer ver em outra cidade?", sugestão do autor), lista quem tem o nome com
  especialidade e cidade, e oferece botões que abrem a lista certa com a pessoa em primeiro e ⭐.
- Filtro por bairro agrupado pela forma normalizada: "Hamburgo Velho (116)" juntou "H VELHO", e
  "Jardim Mauá (26)" juntou "JARDIM MAUA" e "MAUA".
- Eval da rede com `filtro_rede` por caso e 3 casos do nome (3/3). A régua passou a aceitar
  cidades ditas na conversa e as mostradas pela busca por nome (texto montado pelo código, a partir
  do cadastro); antes reprovava como "cidade inventada".

## Fora do escopo

"Não consigo imprimir meu contra-cheque no app do IPASEM?" (no assunto App) levou 191 s e saiu
inventada, com até uma "Resolução nº 05/2012". O guia não fala de contracheque: a pergunta tinha
"app" e "imprimir" (vocabulário), entrou a seção de autorização de exames (nota 0,57) e o modelo
completou o resto. Contracheque é com o RH, não com o app nem com o plano.

`fora_do_escopo.json` (editado à mão, mesmo formato de palavras do vocabulário) lista assuntos que
o chatbot NÃO cobre, com uma resposta fixa: sai na hora, sem modelo, antes de tudo
(`guia.fora_do_escopo`). Primeiro assunto: contracheque e folha de pagamento ("Não tenho
informações sobre contracheque. Ele é tratado pelo RH, não pelo app do IPASEM nem por este
chatbot."). Só expressões e palavras específicas, sem tolerância a erro de digitação: "folha"
sozinha barraria "desconto em folha da coparticipação". `avaliar_assunto.py` confere 8 casos e que
nenhuma das 127 perguntas dos evals é barrada (8/8, 0 barradas).

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
