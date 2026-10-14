# Projeto: Sistema de Download Seguro do Google Drive em Python (especificação revisada)

## 0. Contexto e restrições

- **Plataforma**: Windows 10/11, Python **3.11+**, uso pelo terminal (sem interface gráfica). Também gerar `.exe` com PyInstaller.
- **Idioma**: mensagens, ajuda e documentação em português do Brasil; nomes de comandos/opções em inglês.
- **Bibliotecas**: somente oficiais do Google para autenticação/HTTP (`google-auth`, `google-auth-oauthlib`), mais `requests`, `keyring`, `tqdm`. Não usar `google-api-python-client` (o `httplib2` não é thread-safe).
- **Proibido**: scraping, cookies do navegador, solicitar senha, contornar permissões/políticas, `acknowledgeAbuse`.
- **Entrega**: projeto completo e executável, não trechos. Começar apresentando estrutura e decisões.

## 1. Autenticação (OAuth 2.0)

1. Cliente OAuth do tipo **App para computador**, fluxo *loopback* (`run_local_server`, porta livre). Recusar `client_secret.json` do tipo "web" com mensagem explicativa.
2. Escopo único: `https://www.googleapis.com/auth/drive.readonly`. Verificar se foi concedido (consentimento granular).
3. E-mail da conta via `about.get` (`user.emailAddress`).
4. Persistir **apenas o refresh token** (e e-mail, client_id, escopos) no Gerenciador de Credenciais do Windows via `keyring`. Access token só em memória.
5. Renovar o access token a cada execução; `RefreshError` → mensagem pedindo `login`.
6. `login --switch` troca de conta (revoga a anterior); `logout` revoga no Google (`oauth2.googleapis.com/revoke`) e apaga localmente; `logout --local-only` só apaga.
7. Documentar: criação do projeto, ativação da Drive API, tela de consentimento **Interna** (Workspace) × **Externa** (status "Em teste", usuários de teste, **expiração do login em 7 dias**), escopo restrito e verificação do Google.

## 2. Localização

1. Links aceitos: `drive.google.com/file/d/ID`, `/drive/folders/ID` (inclui `/u/N/`), `open?id=`, `uc?id=`, `drive.usercontent.google.com/download?id=`, `docs.google.com/{document,spreadsheets,presentation,drawings,forms}/d/ID`, ID puro. Preservar `resourcekey` e enviá-lo no cabeçalho `X-Goog-Drive-Resource-Keys`. Recusar outros domínios, links "Publicar na Web" (`/d/e/…`) e seções (`my-drive`, `shared-with-me`…).
2. Pesquisa por nome: `corpora=user` (Meu Drive + compartilhados comigo) e **uma consulta por drive compartilhado** (`corpora=drive&driveId=…`), sempre com `supportsAllDrives=true` e `includeItemsFromAllDrives=true`; escapar `\` e `'`; tratar `incompleteSearch`; filtros `--type` e `--scope`; exibir origem do item.
3. Documentar limitações: `name contains` casa por prefixo de palavras; itens só acessados por link não aparecem na pesquisa; itens sem acesso não aparecem nas listagens.
4. Atalhos (`application/vnd.google-apps.shortcut`): seguir `shortcutDetails.targetId`; registrar erro se o destino for inacessível; detectar ciclos.

## 3. Download

1. Binários: `files.get?alt=media` em streaming (blocos de 256 KB), sem carregar o arquivo em memória.
2. Gravar em `~gdsd-<tarefa>-<nome>.gdpart` **na mesma pasta** do destino; *checkpoint* (flush + fsync + SQLite) a cada N MB.
3. Verificar (seção 4) e só então `rename` atômico **sem sobrescrita** para o nome final.
4. Conflito com arquivo local: `--on-conflict rename` (padrão, `nome (2).ext`) | `skip` | `overwrite` (autorização explícita). Rechecar o conflito no momento de mover.
5. Respeitar `capabilities.canDownload=false` e erros 403 de política/permissão: registrar como erro com explicação, nunca contornar.
6. Pastas: mapeamento iterativo (sem recursão Python), `corpora=drive` para pastas de drives compartilhados, `trashed=false`, nomes duplicados resolvidos de forma **determinística** (nome, `createdTime`, ID) e **sem diferenciar maiúsculas**; um erro não interrompe os demais; `--dry-run`; resumo final de concluídos/ignorados/com erro/interrompidos.
7. Reexecutar a mesma pasta: ignorar o que já foi concluído e está inalterado; retomar tarefas incompletas; não criar duplicatas.
8. Google Workspace: exportar (`files.export`) com formatos por tipo (Docs: docx/pdf/odt/rtf/txt/md/epub/html; Sheets: xlsx/ods/pdf/csv/tsv/html; Slides: pptx/odp/pdf/txt; Desenhos: pdf/png/jpg/svg; Apps Script: json). Avisar que haverá **conversão** e possível perda de fidelidade; CSV/TSV só exportam a primeira aba. Perguntar o formato no modo interativo; opções `--format`, `--doc-format` etc. Formulários, Sites, My Maps, Jamboard e Vids: ignorados com explicação. Erro `exportSizeLimitExceeded` (10 MB) → usar `exportLinks`.
9. Paralelismo limitado (padrão 3, máx. 8) + limitador de taxa compartilhado (padrão 8 req/s).
10. Espaço em disco: verificar antes da pasta (soma dos tamanhos conhecidos) e antes de cada arquivo, com margem configurável.

## 4. Integridade

1. Após o download, calcular SHA-256 e MD5 do arquivo local numa única leitura.
2. Comparar, nesta ordem: `sha256Checksum` do Google → `md5Checksum` → tamanho (`size`) → sem referência.
3. Registrar o nível obtido: `verified_sha256`, `verified_md5`, `size_only`, `unverifiable` (exportações), `failed`.
4. Falha: renomear para `.gdinvalid`, tarefa "com erro", permitir `retry`. Se a falha ocorreu após uma retomada, refazer do zero uma vez automaticamente.
5. Nunca inventar checksum; nunca declarar íntegro só porque a transferência terminou.
6. Comando `verify` compara arquivos concluídos com o SHA-256 registrado.

## 5. Retomada

1. Registrar no SQLite, antes de gravar dados: ID remoto, destino, arquivo parcial e *fingerprint* remoto (`headRevisionId`, `md5Checksum`, `sha256Checksum`, `size`, `modifiedTime`).
2. Retomar com `Range: bytes=N-` **somente** se o fingerprint atual for igual ao registrado e houver ao menos uma referência forte (revisão ou checksum). Validar `206` + `Content-Range`; `200` → regravar do zero; `416` ou inconsistência → reiniciar.
3. Exportações: sempre do zero.
4. Retentativas automáticas para rede, 429, 5xx e `rateLimitExceeded`: backoff exponencial com *full jitter*, teto, respeito a `Retry-After`; espera interrompível por Ctrl+C.
5. Ctrl+C: parar todos os workers, manter parciais, marcar "interrompida" (inclusive tarefas não iniciadas).
6. Ao iniciar: tarefas "em andamento" de processo morto → "interrompida"; avisar e oferecer `resume`.
7. Trava de instância única (arquivo bloqueado pelo SO).

## 6. Histórico (SQLite)

- Tabelas `jobs` (execuções) e `tasks` (arquivos) com versão de schema (`PRAGMA user_version`), WAL.
- Campos mínimos da tarefa: datas (criação, atualização, última tentativa), e-mail, ID e nome remotos, MIME, ID da pasta de origem, drive, caminho remoto, destino, parcial, tamanho esperado e baixado, SHA-256 local, MD5/SHA-256 do Google, nível e detalhe de integridade, formato de exportação, revisão e `modifiedTime` remotos, política de conflito, estado, mensagem de erro, tentativas.
- Estados: `pending`, `running`, `completed`, `interrupted`, `cancelled`, `error`, `skipped` (exibidos em português).
- Sem tokens ou senhas.

## 7. Linha de comando

`login [--client-secret ARQ] [--switch] [--no-browser]` · `account` · `logout [--local-only]` ·
`search TERMO [--type] [--scope] [--limit] [--no-select] [--dest] [--on-conflict]` ·
`download LINK_OU_ID [--dest] [--format] [--on-conflict]` ·
`folder LINK_OU_ID [--dest] [--doc-format] [--sheet-format] [--slide-format] [--drawing-format] [--workers] [--dry-run] [--on-conflict]` ·
`resume [ID…] [--yes] [--workers]` · `retry [ID…] [--all] [--workers]` · `cancel ID… [--delete-partial]` ·
`history [--limit] [--status] [--name] [--id] [--all-accounts]` · `status [--all-accounts]` ·
`verify [ID…] [--all]` · `formats` · `config [--init]`.
Globais: `--no-input`, `-v/--verbose`, `--version`, `-h/--help`.
Códigos de saída: 0 ok, 1 algum erro, 2 uso/link inválido, 3 autenticação, 4 outra instância, 130 interrompido.
Progresso por arquivo (nome, tamanho, velocidade), barra geral, aviso de cada nova tentativa e resumo final.

## 8. Segurança

Sanitização de nomes para Windows (caracteres proibidos, `..`, nomes reservados, pontos/espaços finais, 255 unidades UTF-16), verificação de que todo caminho final fica dentro do destino, prefixo `\\?\` para caminhos longos, links de exportação aceitos só de domínios Google via HTTPS, logs JSON rotativos com remoção de tokens/segredos, `.gitignore` para credenciais, bancos, logs e downloads.

## 9. Testes

Automatizados (pytest, sem conta real, com Drive simulado e injeção de falhas): autenticação/renovação/revogação, pesquisa e origens, drives compartilhados, paginação, recursão, atalhos e ciclos, download binário, exportação e `exportLinks`, níveis de integridade, quedas de rede e respostas truncadas, servidor que ignora `Range`, arquivo remoto alterado durante a retomada, retomada após reiniciar o programa, tarefas órfãs, duplicados, conflitos, links inválidos, 401/403/404/429/5xx, path traversal, persistência do histórico, CLI de ponta a ponta.
Manuais (documentados): roteiro de integração com conta real.

## 10. Critérios de conclusão (verificáveis)

| Critério | Como verificar |
|---|---|
| Autenticar e identificar o e-mail | `login`, `account` (manual) · `test_auth.py` |
| Pesquisar / usar links autorizados | `search`, `download LINK` · `test_drive_client.py`, `test_links.py` |
| Baixar arquivos individuais | `download` · `test_downloader.py`, `test_cli.py` |
| Baixar pastas com subpastas | `folder` · `test_folder.py` |
| Retomar com segurança | Ctrl+C + `resume` · `test_resume.py` |
| Histórico após reiniciar | `history` · `test_history.py`, `test_resume.py` |
| Integridade quando houver referência | `history --id N` · `test_downloader.py`, `test_integrity_retry_misc.py` |
| Exportar Google Workspace | `download --format` · `test_downloader.py` |
| Erros, limitações e pendências claros | `status`, `retry`, resumo · `test_cli.py` |
