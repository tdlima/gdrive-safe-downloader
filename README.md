# GDrive Safe Downloader

![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)
![PowerShell](https://img.shields.io/badge/PowerShell-Windows-5391FE?logo=powershell&logoColor=white)
![Google Drive API](https://img.shields.io/badge/Google%20Drive%20API-v3-4285F4?logo=googledrive&logoColor=white)
![SQLite](https://img.shields.io/badge/SQLite-hist%C3%B3rico-003B57?logo=sqlite&logoColor=white)
![pytest](https://img.shields.io/badge/pytest-184%20testes-0A9EDC?logo=pytest&logoColor=white)
![Claude Code](https://img.shields.io/badge/Desenvolvido%20com-Claude%20Code-D97757?logo=claude&logoColor=white)

Ferramenta de linha de comando para Windows que localiza, baixa e organiza arquivos do
Google Drive — Meu Drive, arquivos e pastas compartilhados com você e drives
compartilhados — com **verificação de integridade**, **retomada segura** de downloads
interrompidos e **histórico persistente** em SQLite.

- Acesso **somente leitura** (`drive.readonly`), via OAuth 2.0 oficial. A senha do Google nunca passa pelo programa.
- Token guardado no **Gerenciador de Credenciais do Windows** (não em arquivo, não no banco).
- Arquivos baixados primeiro como `.gdpart` e movidos para o nome definitivo **só depois** de verificados.
- Nada é sobrescrito sem que você peça (`--on-conflict overwrite`).
- **Sincronização**: depois de baixar uma pasta, `python main.py sync` baixa somente os arquivos novos (ou alterados) e os coloca no lugar certo da estrutura local.

## Tecnologias utilizadas

| Tecnologia | Uso no projeto |
|---|---|
| **Python 3.11+** | Linguagem de todo o sistema (testado com 3.14 e 3.15 no Windows 11) |
| **PowerShell** | Instalação, execução no Windows e script de build (`build_exe.ps1`) |
| **Google Drive API v3** | Pesquisa, listagem, download e exportação de arquivos |
| **OAuth 2.0** (`google-auth`, `google-auth-oauthlib`) | Login oficial do Google, somente leitura |
| **requests** | HTTP com streaming e retomada por intervalo de bytes |
| **keyring** + Gerenciador de Credenciais do Windows | Armazenamento seguro do token |
| **SQLite** | Histórico, estado das tarefas e pastas sincronizadas |
| **tqdm** | Barras de progresso no terminal |
| **pytest** | 184 testes automatizados com simulação da Drive API |
| **PyInstaller** | Geração do executável `.exe` |
| **Git** e **GitHub** | Versionamento e hospedagem do código |
| **[Claude Code](https://claude.com/claude-code)** (Anthropic) | Desenvolvimento assistido por IA: arquitetura, código, testes e documentação |

---

## Sumário

1. [Estrutura do projeto](#1-estrutura-do-projeto)
2. [Decisões de projeto](#2-decisões-de-projeto)
3. [Instalação no Windows](#3-instalação-no-windows)
4. [Configurar o Google Cloud (uma vez)](#4-configurar-o-google-cloud-uma-vez)
5. [Primeiro login](#5-primeiro-login)
6. [Comandos](#6-comandos)
7. [Arquivos do Google Docs/Sheets/Slides](#7-arquivos-do-google-docssheetsslides)
8. [Integridade](#8-integridade)
9. [Interrupções, retomada e novas tentativas](#9-interrupções-retomada-e-novas-tentativas)
10. [Configuração](#10-configuração)
11. [Segurança](#11-segurança)
12. [Limitações conhecidas](#12-limitações-conhecidas)
13. [Testes](#13-testes)
14. [Gerar executável (.exe)](#14-gerar-executável-exe)
15. [Solução de problemas](#15-solução-de-problemas)

---

## 1. Estrutura do projeto

```
gdrive-safe-downloader/
├── main.py                    # ponto de entrada: python main.py COMANDO
├── gdsd/
│   ├── cli.py                 # comandos, argumentos e fluxo de cada comando
│   ├── auth.py                # OAuth 2.0, cofre de credenciais, renovação e revogação
│   ├── drive_client.py        # Drive API v3: metadados, pesquisa, listagem, streams, erros HTTP
│   ├── downloader.py          # download por blocos, Range, verificação, gravação atômica, paralelismo
│   ├── folder_downloader.py   # mapeamento recursivo, atalhos, nomes duplicados
│   ├── workspace_export.py    # formatos de exportação do Google Workspace
│   ├── integrity.py           # tamanho, SHA-256 local, MD5/SHA-256 do Google
│   ├── resume.py              # recuperação após reinício, retry, cancelamento, trava de instância
│   ├── history.py             # banco SQLite (tarefas e execuções)
│   ├── paths.py               # nomes válidos no Windows, anti path traversal, caminhos longos
│   ├── links.py               # extração/validação de IDs a partir de links
│   ├── retry.py               # backoff exponencial com jitter, limitador de taxa
│   ├── config.py              # configuração e diretórios
│   ├── logging_setup.py       # logs JSON com remoção de credenciais
│   ├── ui.py                  # progresso, tabelas, perguntas
│   └── errors.py              # exceções com mensagens em português
├── tests/                     # 184 testes automatizados (Drive simulado, sem conta real)
│   ├── fakes.py               # simulação da Drive API com injeção de falhas
│   └── test_*.py
├── docs/
│   └── TESTES_INTEGRACAO.md   # roteiro de testes com conta Google real
├── requirements.txt / requirements-dev.txt
├── build_exe.ps1              # gera o .exe com PyInstaller
├── pytest.ini
└── .gitignore
```

Dados do programa (fora da pasta do projeto): `%LOCALAPPDATA%\GDriveSafeDownloader\`

| Arquivo | Conteúdo |
|---|---|
| `client_secret.json` | ID do cliente OAuth (copiado no primeiro login) |
| `history.sqlite3` | histórico e estado das tarefas |
| `logs\gdsd.log` | log JSON rotativo (5 × 5 MB), sem tokens |
| `config.toml` | configuração opcional (`python main.py config --init`) |

O token OAuth fica no **Gerenciador de Credenciais do Windows** (entrada `gdrive-safe-downloader`).

## 2. Decisões de projeto

| Tema | Decisão | Motivo |
|---|---|---|
| Escopo OAuth | apenas `drive.readonly` | Único escopo que permite ler arquivos compartilhados e pesquisar; o e-mail vem de `about.get`, sem escopos extras. |
| Cliente HTTP | `google-auth` + `AuthorizedSession` (REST) | `google-api-python-client` usa `httplib2`, que não é thread-safe e dificulta `Range`. Ambas são bibliotecas oficiais do Google. |
| Token | só o *refresh token*, no cofre do Windows (DPAPI, via `keyring`) | O *access token* fica só em memória. Nada de token em arquivo ou no SQLite. |
| Download | streaming em blocos de 256 KB, *checkpoint* (flush + fsync + SQLite) a cada 8 MB | Memória constante e progresso durável para retomada. |
| Retomada | `Range: bytes=N-` somente se revisão/checksum remotos forem idênticos ao registrado; exige resposta `206` com `Content-Range` coerente | Nunca mistura duas versões do arquivo. Sem prova, reinicia do zero. |
| Exportações | nunca retomadas; sempre do zero | O conteúdo convertido pode mudar entre tentativas e não tem checksum. |
| Exportação > 10 MB | fallback automático para `exportLinks` | O endpoint `files.export` limita a 10 MB. |
| Integridade | SHA-256 do Google > MD5 do Google > tamanho > sem referência | Usa a referência mais forte disponível e registra qual foi usada. |
| Gravação | `.gdpart` na mesma pasta → verificação → `rename` atômico sem sobrescrita | Um arquivo com nome definitivo está sempre completo e verificado. |
| Nomes duplicados | ordenação determinística (nome, criação, ID) → `nome (2).ext` | Reexecuções geram os mesmos nomes: não duplica e permite retomar. Comparação sem diferenciar maiúsculas (NTFS). |
| Concorrência | 3 downloads simultâneos (1–8), limitador de 8 req/s, trava de instância única | Respeita cotas e evita duas execuções gravando as mesmas tarefas. |
| Estados | pendente, em andamento, concluída, interrompida, cancelada, com erro, **ignorada** | "Ignorada" foi acrescentado para itens não baixáveis/já existentes (o resumo exige essa contagem). |
| Comandos extras | `cancel`, `verify`, `formats`, `config` | Necessários para o estado "cancelada", para a verificação posterior por SHA-256 e para descobrir opções. |

## 3. Instalação no Windows

Requisitos: **Windows 10/11** e **Python 3.11 ou superior** (testado com 3.14).

```powershell
# 1. Instale o Python (marque "Add python.exe to PATH") — ou: winget install Python.Python.3.13
python --version

# 2. Na pasta do projeto, crie e ative um ambiente virtual
python -m venv .venv
.\.venv\Scripts\Activate.ps1
# Se o PowerShell bloquear scripts: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned

# 3. Instale as dependências
pip install -r requirements.txt

# 4. Confira
python main.py --help
```

No Prompt de Comando (cmd), ative o ambiente com `.venv\Scripts\activate.bat`.

## 4. Configurar o Google Cloud (uma vez)

O Google exige que todo programa que acessa o Drive tenha um "ID do cliente OAuth". Você cria o seu, gratuitamente:

1. Acesse <https://console.cloud.google.com/> com a conta que vai administrar o projeto.
2. **Crie um projeto**: seletor de projetos (topo) → *Novo projeto* → nome, ex.: `gdrive-downloader` → *Criar*.
3. **Ative a Drive API**: menu → *APIs e serviços* → *Biblioteca* → pesquise **Google Drive API** → *Ativar*.
4. **Configure a tela de consentimento** (menu *Google Auth Platform*, antes chamado *Tela de permissão OAuth*):
   - *Branding*: nome do app (ex.: `GDrive Downloader`), e-mail de suporte.
   - *Público-alvo (Audience)*:
     - **Interno** — se sua conta é de uma organização Google Workspace e só contas da organização vão usar. Recomendado: sem verificação do Google e sem expiração semanal do login.
     - **Externo** — para contas `@gmail.com`. Mantenha o status **Em teste** e adicione em *Usuários de teste* cada e-mail que vai fazer login.
       ⚠️ Em modo de teste, o Google **expira a autorização a cada 7 dias**; basta repetir `python main.py login`.
       Publicar o app para produção com o escopo `drive.readonly` (escopo *restrito*) exige verificação e avaliação de segurança do Google — desnecessário para uso pessoal.
   - *Acesso a dados (Data access)*: *Adicionar escopos* → `https://www.googleapis.com/auth/drive.readonly`.
5. **Crie o cliente OAuth**: *Clientes* (ou *Credenciais* → *Criar credenciais* → *ID do cliente OAuth*) → tipo **App para computador** (*Desktop app*) → *Criar*.
6. **Baixe o JSON** do cliente criado (*Fazer download do JSON*) e salve como `client_secret.json`.
   Não use o tipo "Aplicativo da Web": o programa recusa esse tipo com uma mensagem explicativa.

> O `client_secret.json` de um app de computador não é um segredo forte (o Google o considera público), mas mesmo assim **não o publique**. Ele está no `.gitignore`.

## 5. Primeiro login

```powershell
python main.py login --client-secret "C:\Users\voce\Downloads\client_secret.json"
```

1. O navegador abre a página de login do Google (se não abrir, copie o link exibido no terminal; ou use `--no-browser`).
2. Escolha a conta e autorize **"Ver e baixar todos os seus arquivos do Google Drive"**. Em modo de teste aparece o aviso "O Google não verificou este app" → *Continuar*.
3. O terminal mostra: `Login concluído. Conta conectada: voce@exemplo.com`.

O `client_secret.json` é copiado para `%LOCALAPPDATA%\GDriveSafeDownloader\`; nas próximas execuções não é preciso informá-lo.
O acesso é renovado automaticamente. Para trocar de conta: `python main.py login --switch`.

## 6. Comandos

Sintaxe geral: `python main.py [--no-input] [-v] COMANDO [opções]`

| Opção global | Efeito |
|---|---|
| `--no-input` | nunca faz perguntas (usa os padrões) — útil em scripts/agendamentos |
| `-v`, `--verbose` | mostra logs detalhados no terminal |
| `--version` | versão |
| `-h`, `--help` | ajuda (também em cada comando: `python main.py folder --help`) |

### `login` — autenticar ou confirmar a conta
```powershell
python main.py login
python main.py login --client-secret C:\caminho\client_secret.json
python main.py login --switch        # trocar de conta (revoga a autorização anterior)
python main.py login --no-browser    # apenas exibe o link de autorização
```

### `account` — conta conectada
```powershell
python main.py account
```
Exibe e-mail, nome, permissão concedida, pastas usadas e totais do histórico.

### `logout` — desconectar
```powershell
python main.py logout               # remove do computador e revoga no Google
python main.py logout --local-only  # só remove do computador
```

### `search` — pesquisar por nome
```powershell
python main.py search "relatórios"
python main.py search "contrato" --type file --scope shared
python main.py search "Projetos" --type folder --scope drives --limit 20
python main.py search "notas" --no-select
```
- `--type all|file|folder` · `--scope all|mydrive|shared|drives` · `--limit N` (padrão 50)
- Mostra número, tipo, nome, tamanho, data, **origem** (Meu Drive, compartilhado por X, drive compartilhado Y) e ID.
- Em seguida pergunta quais números baixar (`1,3-5`); pastas são baixadas inteiras. Aceita `--dest` e `--on-conflict`.

### `download` — baixar um arquivo
```powershell
python main.py download "https://drive.google.com/file/d/1AbC.../view?usp=sharing"
python main.py download 1AbCdEfGhIjKlMnOpQrStUvWxYz --dest "D:\Arquivos"
python main.py download "https://docs.google.com/document/d/1AbC.../edit" --format pdf
python main.py download ID --on-conflict skip
```
- Aceita links `file/d/…`, `open?id=…`, `uc?id=…`, `docs.google.com/document|spreadsheets|presentation/d/…`, com `resourcekey`, ou o ID puro.
- Atalhos do Drive são seguidos até o item de destino.
- `--format` escolhe a exportação de arquivos Google; sem ele, o programa pergunta (ou usa o padrão com `--no-input`).
- `--on-conflict rename` (padrão) cria `nome (2).ext` · `skip` não baixa · `overwrite` substitui (autorização explícita).

### `folder` — baixar uma pasta inteira
```powershell
python main.py folder "https://drive.google.com/drive/folders/1XyZ..." --dest "D:\Arquivos"
python main.py folder 1XyZ... --doc-format pdf --sheet-format xlsx --slide-format pdf
python main.py folder 1XyZ... --dry-run
python main.py folder 1XyZ... --workers 5
```
- Cria `DESTINO\<nome da pasta>\...` reproduzindo todas as subpastas.
- `--doc-format`, `--sheet-format`, `--slide-format`, `--drawing-format`: formatos por tipo.
- `--dry-run`: lista o que seria baixado, sem baixar.
- `--workers N`: downloads simultâneos (1–8, padrão 3).
- Itens sem permissão, não exportáveis ou com erro são registrados e **não interrompem** os demais. Ao final há um resumo de concluídos, ignorados e com erro.
- Executar de novo a mesma pasta **não baixa novamente** o que já está completo e inalterado, e **retoma** o que ficou pela metade.
- Toda pasta baixada fica registrada para sincronização (veja `sync`).

### `sync` — baixar só o que é novo em pastas já baixadas

Exemplo: você baixou a pasta `workshop` (com `dia1`, `dia2`, `dia3`, cada uma com `aula1`–`aula3`).
Depois, alguém acrescenta `aula4` em `dia2` no Drive. Basta rodar:

```powershell
python main.py sync
```

```
=== Sincronizando: workshop ===
Encontrados 10 arquivo(s) em 4 pasta(s): 1 novo(s), 0 alterado(s) no Drive, 9 já atualizado(s).
  [NOVO] workshop/dia2/aula4.mp4
[OK] aula4.mp4 (120.4 MB) — íntegro (MD5 confere com o Google)

Resumo:
  Concluídos:    1
  Já atualizados: 9 (nada a fazer)
```

Somente `aula4.mp4` é baixada, direto para `...\workshop\dia2\`. Nada mais é transferido.

```powershell
python main.py sync                       # todas as pastas já baixadas (não precisa do link nem do --dest)
python main.py sync --dry-run             # só mostra as novidades, sem baixar
python main.py sync "LINK_DA_PASTA"       # apenas uma pasta
python main.py sync --list                # pastas registradas e última sincronização
python main.py sync --remove 3            # deixa de sincronizar o registro 3 (não apaga arquivos)
python main.py sync --on-update replace   # veja abaixo
```

Como funciona:
- A árvore inteira da pasta é percorrida no Drive e comparada com o histórico. Um arquivo só é considerado "já atualizado" se o arquivo local existir, tiver o tamanho registrado e a versão remota (revisão/checksum) for a mesma.
- Arquivos e subpastas novos são criados no mesmo lugar relativo. Arquivos apagados do seu computador são baixados de novo.
- O destino, o nome da pasta local e os formatos de exportação usados no download original são lembrados. Mesmo que a pasta seja **renomeada no Drive**, a sincronização continua na mesma pasta local.
- **Arquivo que mudou no Drive** (ex.: aula corrigida), `--on-update`:
  - `keep-both` (padrão): mantém a sua cópia e salva a nova como `aula1 (2).mp4`.
  - `replace`: substitui a cópia local pela nova versão, **somente se você não alterou o arquivo no computador** (confere o SHA-256). Se alterou, mantém os dois.
  - `skip`: mantém a versão local e não baixa a nova.
- **Nada é apagado** no seu computador quando um arquivo é removido ou renomeado no Drive. Um arquivo renomeado no Drive é baixado com o nome novo, e a cópia antiga continua lá.
- Sincronizações sem novidades não acrescentam linhas ao histórico.

#### Sincronizar automaticamente (Agendador de Tarefas do Windows)

Para verificar novidades todo dia às 8h (ajuste os caminhos para a pasta do projeto):

```powershell
schtasks /Create /SC DAILY /ST 08:00 /TN "GDrive Sync" /TR "\"C:\Users\VOCE\Desktop\Projeto Sistema de Download\.venv\Scripts\python.exe\" \"C:\Users\VOCE\Desktop\Projeto Sistema de Download\main.py\" --no-input sync"
```

- A tarefa roda com o seu usuário do Windows, e usa o mesmo login e o mesmo histórico.
- O resultado de cada execução aparece em `python main.py sync --list` e no log.
- Para remover a tarefa: `schtasks /Delete /TN "GDrive Sync"`.

### `resume` — retomar downloads interrompidos
```powershell
python main.py resume          # lista e pergunta
python main.py resume --yes    # sem confirmação
python main.py resume 12 15    # tarefas específicas
```
Ao iniciar `login`, `account`, `download` ou `folder`, o programa avisa se existem tarefas interrompidas e oferece retomá-las.

### `retry` — repetir downloads com erro
```powershell
python main.py retry           # lista as tarefas com erro
python main.py retry --all     # repete todas
python main.py retry 7 9       # repete as tarefas 7 e 9
```

### `cancel` — cancelar tarefas
```powershell
python main.py cancel 7
python main.py cancel 7 8 --delete-partial   # apaga também o .gdpart
```

### `history` — histórico
```powershell
python main.py history
python main.py history --limit 100
python main.py history --status erro          # pendente|andamento|concluida|interrompida|cancelada|erro|ignorada
python main.py history --name "contrato"
python main.py history --id 42                # todos os campos de uma tarefa
python main.py history --all-accounts
```

### `status` — em andamento, pendentes e interrompidos
```powershell
python main.py status
```

### `verify` — conferir arquivos já baixados
```powershell
python main.py verify --all
python main.py verify 42 43
```
Recalcula o SHA-256 do arquivo local e compara com o registrado no download (detecta alteração ou corrupção posterior).

### `formats` e `config`
```powershell
python main.py formats         # formatos de exportação e padrões
python main.py config          # configuração efetiva e caminhos
python main.py config --init   # cria config.toml de exemplo
```

### Códigos de saída
`0` sucesso · `1` algum arquivo com erro · `2` uso/configuração/link inválido · `3` autenticação · `4` outra instância em execução · `130` interrompido (Ctrl+C).

## 7. Arquivos do Google Docs/Sheets/Slides

Documentos nativos do Google não têm um arquivo "original" para baixar: o Google os **converte** no momento da exportação. O programa avisa quando isso acontece.

| Tipo | Formatos (padrão em negrito) |
|---|---|
| Documento | **docx**, pdf, odt, rtf, txt, md, epub, html (zip) |
| Planilha | **xlsx**, ods, pdf, csv*, tsv*, html (zip) |
| Apresentação | **pptx**, odp, pdf, txt |
| Desenho | **pdf**, png, jpg, svg |
| Apps Script | **json** |

\* CSV/TSV exportam **apenas a primeira aba**.

- A conversão pode não preservar formatação, comentários, fórmulas específicas, scripts ou outros recursos.
- Exportações não têm checksum do Google → integridade registrada como "sem referência do Google".
- Formulários, Sites, My Maps, Jamboard e Vids **não** são exportáveis pela API e aparecem como *ignorados*.

## 8. Integridade

Depois de cada download, o arquivo `.gdpart` é verificado **antes** de receber o nome definitivo:

| Resultado no histórico | Significado |
|---|---|
| `sha256 ok` | SHA-256 local igual ao `sha256Checksum` informado pelo Google |
| `md5 ok` | MD5 local igual ao `md5Checksum` informado pelo Google |
| `tamanho ok` | o tamanho confere, mas o Google não forneceu checksum: conteúdo **não** verificável externamente |
| `não verificável` | nenhuma referência (ex.: exportações) |
| `FALHOU` | tamanho ou checksum divergente — o arquivo fica como `~gdsd-ID-nome.gdinvalid` e a tarefa como *com erro* |

O SHA-256 local é **sempre** registrado. Ele identifica o arquivo gravado e permite o `verify` posterior, mas sozinho não prova que é idêntico ao original.
Se a verificação falhar logo após uma retomada, o programa baixa de novo do zero automaticamente antes de declarar falha.

## 9. Interrupções, retomada e novas tentativas

- **Queda de rede durante o download**: até `max_retries` novas tentativas (padrão 6), com espera exponencial + jitter, respeitando `Retry-After`. Antes de continuar, os metadados remotos são consultados novamente.
- **Ctrl+C ou fechamento do terminal**: o arquivo parcial (`~gdsd-ID-nome.gdpart`) e o progresso ficam salvos; a tarefa vira *interrompida*.
- **Queda de energia / processo morto**: na próxima execução, tarefas que estavam "em andamento" viram *interrompidas* automaticamente.
- **Retomada** (`resume`, ou repetir o mesmo `folder`): só continua o arquivo parcial se a **revisão** (`headRevisionId`) e os **checksums** do Google forem os mesmos registrados. Caso contrário — ou se o servidor ignorar o `Range` ou responder com intervalo incoerente — **reinicia do zero**.
- **Exportações** do Google Workspace sempre recomeçam do zero.
- **Erros temporários da API** (429, 5xx, `rateLimitExceeded`) são repetidos; erros definitivos (404, 403 de permissão, download desativado pelo proprietário) não.

## 10. Configuração

`python main.py config --init` cria `%LOCALAPPDATA%\GDriveSafeDownloader\config.toml`:

```toml
[geral]
download_dir = "D:/Arquivos/GoogleDrive"   # padrão: .\downloads (pasta atual)
max_workers = 3            # 1 a 8
chunk_size_mb = 8          # intervalo de checkpoint do progresso
max_retries = 6
requests_per_second = 8
on_conflict = "rename"     # rename | skip | overwrite
min_free_space_mb = 200    # margem mínima de espaço livre
log_level = "INFO"

[exportacao]
document = "docx"
spreadsheet = "xlsx"
presentation = "pptx"
drawing = "pdf"
```

Variáveis de ambiente: `GDSD_HOME` (pasta de dados), `GDSD_DOWNLOAD_DIR`, `GDSD_MAX_WORKERS`, `GDSD_CLIENT_SECRET`.

## 11. Segurança

- Login exclusivamente pelo fluxo OAuth oficial no navegador; sem scraping, cookies ou senha.
- Somente leitura: o programa não consegue alterar nem apagar nada no seu Drive.
- Refresh token no Gerenciador de Credenciais do Windows; access token só em memória; `logout` revoga no Google.
- O histórico SQLite não contém senhas nem tokens; os logs passam por filtro que remove tokens, `client_secret` e códigos de autorização.
- Nomes vindos do Drive são sanitizados: caracteres proibidos, `..`, nomes reservados (`CON`, `NUL`, `COM1`…), pontos/espaços finais e comprimento. Todo caminho final é verificado para permanecer dentro do destino escolhido (proteção contra *path traversal*).
- Caminhos acima de 260 caracteres usam o prefixo `\\?\` automaticamente.
- Links de exportação alternativos só são aceitos de domínios do Google via HTTPS.
- **Não há contorno de permissões**: arquivos com download desativado pelo proprietário, bloqueados por política do domínio ou sinalizados como nocivos pelo Google são registrados como erro, com a explicação.
- Uma única instância por vez pode baixar (trava de arquivo liberada automaticamente se o processo morrer).
- Espaço em disco verificado antes da pasta inteira e antes de cada arquivo.

## 12. Limitações conhecidas

- **Pesquisa**: o Drive pesquisa por *prefixo de palavras* do nome (`relat` encontra "relatório", mas `atório` não). Arquivos que você só recebeu por link e nunca abriu **não aparecem** na pesquisa — use o link.
- Arquivos dentro de uma pasta compartilhada aos quais sua conta não tem acesso simplesmente não são listados pela API.
- Exportações dependem da conversão do Google e não têm checksum.
- O conteúdo da lixeira não é baixado.
- Arquivos sinalizados como nocivos pelo Google não são baixados (o programa não usa `acknowledgeAbuse`).
- A cota de download por arquivo do Google (`downloadQuotaExceeded`) só se resolve esperando algumas horas.
- Em projetos OAuth "Externos" em modo de teste, o login expira a cada 7 dias.

## 13. Testes

```powershell
pip install -r requirements-dev.txt
python -m pytest            # 184 testes, ~4 s
python -m pytest -v tests\test_resume.py
```

Os testes automatizados usam uma **simulação da Drive API** (`tests/fakes.py`) — nenhuma conta Google é necessária. Cobrem:

| Área | Arquivo |
|---|---|
| Autenticação, renovação, rotação e revogação de token, troca de conta | `test_auth.py` |
| Pesquisa, origens, drives compartilhados, paginação, 401/403/404/429/5xx, resourceKey | `test_drive_client.py` |
| Download binário, checksum, quedas de rede, `Range`, servidor que ignora `Range`, arquivo remoto alterado, conflitos, exportação, `exportLinks`, espaço em disco | `test_downloader.py` |
| Recursão, estrutura, duplicados, atalhos (arquivo, pasta, circular, sem acesso), nomes maliciosos, drive compartilhado | `test_folder.py` |
| Interrupção, retomada após "reiniciar o programa", tarefas órfãs, retry, cancelamento, trava de instância | `test_resume.py` |
| Sincronização: só arquivos novos (exemplo workshop/dia2/aula4), subpastas novas, arquivos alterados (`keep-both`/`replace`/`skip`), pasta renomeada no Drive, migração do banco | `test_sync.py` |
| Persistência e filtros do histórico, ausência de colunas de credencial | `test_history.py` |
| Links válidos/inválidos | `test_links.py` |
| Nomes no Windows, path traversal, caminhos longos | `test_paths.py` |
| Níveis de integridade, backoff, logs sem segredos | `test_integrity_retry_misc.py` |
| Comandos da CLI de ponta a ponta (com Drive simulado) | `test_cli.py` |

**O que os testes automatizados NÃO comprovam**: o comportamento contra os servidores reais do Google (login no navegador, consentimento, cotas reais, drives compartilhados reais). Para isso siga o roteiro em [docs/TESTES_INTEGRACAO.md](docs/TESTES_INTEGRACAO.md).

## 14. Gerar executável (.exe)

```powershell
.\.venv\Scripts\Activate.ps1
.\build_exe.ps1
.\dist\gdrive-downloader.exe --help
.\dist\gdrive-downloader.exe login --client-secret C:\caminho\client_secret.json
```

- Gera um único arquivo `dist\gdrive-downloader.exe` (~18 MB) que não exige Python instalado. Nos comandos, troque `python main.py` por `.\gdrive-downloader.exe` (as mensagens do programa já fazem essa troca).
- O `.exe` usa os mesmos dados (`%LOCALAPPDATA%\GDriveSafeDownloader`), então histórico e login são compartilhados com a versão em Python.
- Antivírus às vezes desconfiam de executáveis PyInstaller não assinados; se necessário, assine o `.exe` ou use a versão em Python.

## 15. Solução de problemas

| Mensagem / sintoma | O que fazer |
|---|---|
| `client_secret.json não encontrado` | Siga a seção 4 e rode `login --client-secret CAMINHO`. |
| `É do tipo 'Aplicativo da Web'` | Crie um cliente do tipo **App para computador**. |
| `access_denied` / "app não verificado" bloqueando | Adicione seu e-mail em *Usuários de teste* (projeto Externo). |
| `A autorização salva expirou ou foi revogada` | `python main.py login` (normal a cada 7 dias em projetos Externos em teste). |
| `A Google Drive API não está ativada` | Ative a API no mesmo projeto do `client_secret.json`. |
| `Recurso não encontrado ou a conta conectada não tem acesso` | Confira se está logado com a conta certa (`account`) e se o item foi compartilhado com ela. |
| `Download desativado pelo proprietário` | Restrição do dono do arquivo; não há como (nem se deve) contornar. |
| `Outra execução do programa está baixando` | Aguarde a outra janela terminar. |
| Caracteres estranhos no terminal | Use o Windows Terminal; ou execute `chcp 65001` antes. |
| `Python não foi encontrado; executar sem argumentos para instalar do Microsoft Store` | O atalho da Microsoft Store está à frente no PATH. Use `py` no lugar de `python`, ou desative em *Configurações → Aplicativos → Configurações avançadas → Aliases de execução do aplicativo* (python.exe / python3.exe). Com o ambiente virtual ativado, `python` funciona normalmente. |
| Detalhes técnicos | `%LOCALAPPDATA%\GDriveSafeDownloader\logs\gdsd.log` ou `-v`. |
