# Roteiro de testes de integração (conta Google real)

Os testes automatizados (`python -m pytest`) usam uma simulação da Drive API.
Este roteiro valida o que **só a API real** comprova. Execute-o com uma conta de
teste e arquivos sem importância. Registre o resultado de cada item.

## Preparação

1. Siga o README §4 e §5 (projeto no Google Cloud, `login`).
2. No Drive da conta de teste, crie:
   - pasta `GDSD-Teste` contendo:
     - `binario.zip` (≥ 200 MB, para testar interrupção);
     - `foto.jpg` e uma **cópia** com o mesmo nome `foto.jpg` (o Drive permite);
     - `Documento` (Google Docs), `Planilha` (Google Sheets com 2 abas), `Apresentação` (Slides);
     - um Google Forms;
     - subpasta `Nível 1/Nível 2/arquivo.txt`;
     - um atalho para um arquivo de outra pasta e um atalho para uma pasta;
     - um arquivo chamado `CON.txt` e outro `a:b?.txt` (se o Drive aceitar).
3. Peça a outra conta para compartilhar com você: um arquivo, uma pasta e (se houver Workspace) um drive compartilhado.
4. Peça a outra conta para compartilhar um arquivo com **"Desativar download para leitores"**.

## Roteiro

| # | Ação | Resultado esperado |
|---|---|---|
| 1 | `python main.py login` | Navegador abre; após autorizar, terminal mostra o e-mail. |
| 2 | `python main.py account` | E-mail correto; "somente leitura". |
| 3 | `python main.py search "GDSD"` | Pasta listada como "Meu Drive". |
| 4 | `python main.py search "<nome do compartilhado>" --scope shared` | Item com "Compartilhado por …". |
| 5 | `python main.py search "<item>" --scope drives` | Item com "Drive compartilhado: …". |
| 6 | `python main.py download <link de foto.jpg>` | `[OK] … íntegro (SHA-256…)` ou `(MD5…)`. |
| 7 | `python main.py download <link do Documento>` (escolher pdf) | Aviso de conversão; arquivo `.pdf` abre normalmente. |
| 8 | `python main.py folder <link de GDSD-Teste> --dry-run` | Lista completa, com `foto (2).jpg`, sem baixar. |
| 9 | `python main.py folder <link de GDSD-Teste>`; pressione **Ctrl+C** durante `binario.zip` | "Interrompido"; existe `~gdsd-…binario.zip.gdpart`. |
| 10 | `python main.py status` | `binario.zip` como interrompido com porcentagem. |
| 11 | `python main.py resume --yes` | Mostra "Retomando 'binario.zip' a partir de N bytes"; termina `[OK]` com checksum confirmado. |
| 12 | Repita o item 9 e **feche a janela do terminal**; abra outra e rode `python main.py status` | Tarefa aparece como interrompida (recuperada). |
| 13 | Com download pela metade, substitua `binario.zip` no Drive por nova versão; `resume` | Mensagem "conteúdo remoto alterado… reiniciando do zero"; arquivo final igual à nova versão. |
| 14 | `python main.py folder <link de GDSD-Teste>` novamente | Tudo "ignorado: já baixado e inalterado"; nada duplicado. |
| 15 | Verifique a pasta local | Estrutura `Nível 1/Nível 2`, `foto.jpg` + `foto (2).jpg`, `_CON.txt`, atalhos resolvidos, Forms ignorado. |
| 16 | `python main.py download <arquivo com download desativado>` | Erro "download desativado pelo proprietário"; nada baixado. |
| 17 | `python main.py download <link de arquivo não compartilhado com você>` | "não tem acesso". |
| 18 | `python main.py history` e `history --id N` | Todos os campos preenchidos; sem tokens. |
| 19 | `python main.py verify --all` | Todos `[OK]`. Altere um byte de um arquivo e repita → `[ALTERADO]`. |
| 20 | Desconecte a rede durante um download grande | Mensagens "[nova tentativa N]"; ao reconectar, conclui. |
| 21 | Exporte um Google Docs grande (> 10 MB) | Mensagem "usando link de exportação alternativo"; conclui. |
| 22 | `python main.py logout`; depois `account` | Desconectado; autorização some de <https://myaccount.google.com/permissions>. |
| 23 | Abra o Gerenciador de Credenciais do Windows (Credenciais do Windows) | Entrada `gdrive-safe-downloader` existe após login e some após logout. |
| 24 | Procure tokens: `Select-String -Path "$env:LOCALAPPDATA\GDriveSafeDownloader\logs\*" -Pattern "ya29\|1//"` | Nenhum resultado. |

## Sincronização (`sync`)

| # | Ação | Resultado esperado |
|---|---|---|
| 25 | Após o item 14, rode `python main.py sync` | "Nenhuma novidade: a pasta local já está igual ao Drive." |
| 26 | No Drive, envie `aula4.mp4` para `GDSD-Teste/Nível 1`; rode `python main.py sync --dry-run` | Lista apenas `[NOVO] GDSD-Teste/Nível 1/aula4.mp4`; nada baixado. |
| 27 | `python main.py sync` | Só `aula4.mp4` é baixada, dentro de `Nível 1` local; resumo mostra os demais como "já atualizados". |
| 28 | Substitua no Drive o conteúdo de `foto.jpg` (Gerenciar versões → nova versão); `python main.py sync` | Nova versão salva como `foto (3).jpg` ou similar; a cópia antiga é mantida. |
| 29 | Repita com `python main.py sync --on-update replace` após outra versão nova | `foto.jpg` local é substituída (se não foi editada no computador). |
| 30 | Renomeie `GDSD-Teste` no Drive e rode `sync` | Continua sincronizando na pasta local `GDSD-Teste`. |
| 31 | Agende com `schtasks` (README) e aguarde a execução | `python main.py sync --list` mostra o horário e o resultado da última sincronização. |

## Automatizando (opcional)

Para transformar parte do roteiro em testes pytest com a conta real, crie testes
marcados com `@pytest.mark.integration` que leiam IDs de arquivos de teste de
variáveis de ambiente (ex.: `GDSD_IT_FILE_ID`) e execute somente eles com:

```powershell
python -m pytest -m integration
```

Esses testes não fazem parte da entrega porque dependem de uma conta e de
arquivos específicos.
