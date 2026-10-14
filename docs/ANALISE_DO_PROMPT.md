# Análise do prompt original e melhorias aplicadas

O prompt original é sólido nos objetivos (segurança, integridade, retomada,
histórico), mas deixa em aberto ou trata de forma imprecisa vários pontos que,
na prática, decidem se o sistema funciona contra a API real do Google. Abaixo,
cada problema encontrado, a melhoria proposta e onde ela foi implementada.

## 1. Lacunas técnicas (afetam o funcionamento real)

| # | Problema no prompt | Melhoria | Onde |
|---|---|---|---|
| 1 | Não define o escopo OAuth ("apenas as permissões necessárias"). | Escopo único `drive.readonly`. `drive.file` não enxerga arquivos compartilhados; o e-mail vem de `about.get`, sem `openid`/`email`. | `auth.py` |
| 2 | Ignora o ciclo de vida do app OAuth: `drive.readonly` é **escopo restrito**; apps "Externos" em modo de teste têm login expirando a cada **7 dias** e exigem usuários de teste. | Documentar Interno × Externo, usuários de teste e a expiração; mensagem clara quando o token expira. | README §4, `auth.py` |
| 3 | Não diz qual tipo de cliente OAuth criar. | Tipo **App para computador** com fluxo *loopback* (o fluxo "OOB" foi descontinuado). Recusa explícita do tipo "Web". | `auth.py` |
| 4 | "Armazenamento seguro" genérico. | Refresh token no Gerenciador de Credenciais do Windows (DPAPI) via `keyring`; access token só em memória (também evita o limite de ~2,5 KB por credencial do Windows). | `auth.py` |
| 5 | Menciona só `md5Checksum`. | A API v3 também fornece `sha256Checksum`/`sha1Checksum` para arquivos binários. Hierarquia: SHA-256 do Google > MD5 > tamanho > sem referência. | `integrity.py` |
| 6 | "Confirmar que o recurso remoto continua sendo o mesmo" sem critério. | Critério objetivo: `headRevisionId` + checksums + tamanho + `modifiedTime`; exige ao menos uma referência forte, senão reinicia do zero. | `downloader.same_remote_content` |
| 7 | Range "quando suportado" sem validação da resposta. | Só anexa se vier `206` com `Content-Range` começando no offset e total igual ao esperado; `200` (Range ignorado) → regrava do zero; `416` → reinicia. | `downloader._download_media` |
| 8 | Não menciona o limite de **10 MB** do `files.export`. | Fallback automático para `exportLinks` quando a API responde `exportSizeLimitExceeded`. | `downloader._download_export` |
| 9 | Não menciona `resourceKey` (arquivos antigos compartilhados por link). | Lê `resourcekey` do link e envia `X-Goog-Drive-Resource-Keys`. | `links.py`, `drive_client.py` |
| 10 | "Não contornar permissões" sem casos concretos. | Respeita `capabilities.canDownload`, `cannotDownloadAbusiveFile` (sem `acknowledgeAbuse`), `domainPolicy`, `downloadQuotaExceeded`, com mensagens explicativas. | `drive_client.py`, `downloader.py` |
| 11 | Pesquisa: "não presuma que tudo aparece" sem dizer o que fazer. | `corpora=user` + uma pesquisa por drive compartilhado (`corpora=drive`), tratamento de `incompleteSearch`; documentar que `name contains` casa por prefixo e que itens só acessados por link não aparecem. | `drive_client.search`, README §12 |
| 12 | Não cita o limite de 260 caracteres de caminho do Windows nem a comparação sem maiúsculas do NTFS. | Prefixo `\\?\` automático; limite por componente em unidades UTF-16; deduplicação *case-insensitive*. | `paths.py` |
| 13 | `google-api-python-client` (implícito) usa `httplib2`, que não é thread-safe — conflita com "downloads simultâneos". | Cliente REST sobre `google-auth` `AuthorizedSession`, uma sessão por thread. Continua sendo biblioteca oficial. | `drive_client.py` |

## 2. Ambiguidades e contradições

| # | Ponto | Resolução adotada |
|---|---|---|
| 14 | Estados listados não incluem "ignorado", mas o resumo exige "ignorados". | Estado `skipped` (ignorada) adicionado. |
| 15 | Existe o estado "cancelada", mas nenhum comando para cancelar. | Comando `cancel [--delete-partial]`. |
| 16 | "SHA-256 serve como registro de integridade posterior", mas nenhum comando o usa. | Comando `verify`. |
| 17 | "Evitar sobrescritas" × reexecutar a mesma pasta (deveria pular o que já existe, não criar `(2)`). | Reconhece pelo histórico arquivos já baixados e inalterados (ignora) e tarefas incompletas (retoma); `--on-conflict rename|skip|overwrite` para arquivos alheios. |
| 18 | Nomes duplicados "tratados", mas sem garantir estabilidade entre execuções. | Ordenação determinística (nome, criação, ID) → mesmos nomes a cada execução. |
| 19 | "Arquivo inválido identificado" × "mover só após verificação". | Falha → `~gdsd-ID-nome.gdinvalid`, mantido até uma nova tentativa bem-sucedida. |
| 20 | `--help` listado como "comando". | É opção global e de cada subcomando. |
| 21 | "Oferecer a retomada na próxima execução" sem dizer quando. | Aviso + pergunta ao iniciar `login`, `account`, `download`, `folder`; sem pergunta com `--no-input`. |
| 22 | Downloads simultâneos sem proteção contra duas execuções do programa. | Trava de instância única liberada pelo SO se o processo morrer; tarefas "em andamento" órfãs viram "interrompidas". |
| 23 | Tarefas de contas diferentes no mesmo histórico. | Tudo filtrado pela conta conectada; `--all-accounts` para ver todas. |
| 24 | Versão do Python não definida. | Python 3.11+ (usa `tomllib`); testado com 3.14 no Windows 11. |

## 3. Critérios de verificação

| # | Problema | Melhoria |
|---|---|---|
| 25 | "Não afirme que foi testado" sem separar o que é testável sem conta real. | Testes automatizados com Drive simulado e injeção de falhas (168 testes) + roteiro manual de integração (`docs/TESTES_INTEGRACAO.md`) para o que só a API real comprova. |
| 26 | Critérios de conclusão qualitativos. | Cada critério mapeado para comandos concretos e testes (ver `PROMPT_MELHORADO.md` §10). |
| 27 | Códigos de saída não definidos (importante para automação/agendamento). | `0/1/2/3/4/130` documentados. |

## 4. Resultado

A versão revisada da especificação, já incorporando todas as decisões acima,
está em [PROMPT_MELHORADO.md](PROMPT_MELHORADO.md). O código do repositório
implementa essa versão.
