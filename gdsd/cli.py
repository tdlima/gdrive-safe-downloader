"""Interface de linha de comando (português do Brasil)."""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
import threading
from pathlib import Path
from typing import Any

from . import __version__
from . import history as H
from . import integrity
from .auth import AuthManager
from .config import CONFLICT_POLICIES, EXAMPLE_CONFIG, Settings, load_settings
from .downloader import UPDATE_POLICIES, Downloader, Outcome, human_size
from .drive_client import FOLDER_MIME, SHORTCUT_MIME, DriveClient
from .errors import ConfigError, ExportNotSupported, GDSDError, Interrupted
from .folder_downloader import FolderDownloader, FolderOptions, local_file_name
from .history import History
from .links import parse_drive_reference
from .logging_setup import setup_logging
from .paths import exists, safe_join
from .resume import InstanceLock, cancel_tasks, prepare_resume, prepare_retry, recover_after_restart, resumable_tasks
from .retry import RateLimiter, RetryPolicy
from .ui import (
    ConsoleReporter,
    ask,
    command_name,
    confirm,
    echo,
    err,
    parse_selection,
    print_summary,
    print_table,
    shorten,
)
from .workspace_export import (
    EXPORT_FORMATS,
    KIND_LABEL,
    NOT_EXPORTABLE,
    export_kind,
    formats_for,
    is_google_native,
    resolve_format,
)

log = logging.getLogger(__name__)

STATUS_ARG = {
    "pendente": H.PENDING, "andamento": H.RUNNING, "concluida": H.COMPLETED,
    "interrompida": H.INTERRUPTED, "cancelada": H.CANCELLED, "erro": H.ERROR, "ignorada": H.SKIPPED,
}
KIND_ARGS = {"doc_format": "document", "sheet_format": "spreadsheet",
             "slide_format": "presentation", "drawing_format": "drawing"}


# ====================================================================== app
class App:
    """Contexto de uma execução: configuração, histórico, autenticação e cliente."""

    def __init__(self, args: argparse.Namespace, settings: Settings):
        self.args = args
        self.settings = settings
        self.history = History(settings.db_path)
        self.auth = AuthManager(settings)
        self.stop_event = threading.Event()
        self._client: DriveClient | None = None
        self._lock: InstanceLock | None = None
        self._account: str | None = None

    @property
    def interactive(self) -> bool:
        return sys.stdin.isatty() and not self.args.no_input

    def close(self) -> None:
        if self._lock:
            self._lock.release()
        self.history.close()

    def ensure_lock(self) -> None:
        if self._lock is None:
            lock = InstanceLock(self.settings.lock_path)
            lock.acquire()
            self._lock = lock
            recovered = recover_after_restart(self.history)
            if recovered:
                echo(f"{recovered} download(s) de uma execução anterior encerrada inesperadamente "
                     "foram marcados como interrompidos.")

    def try_lock(self) -> bool:
        try:
            self.ensure_lock()
            return True
        except GDSDError:
            return False

    @property
    def client(self) -> DriveClient:
        if self._client is None:
            policy = RetryPolicy(self.settings.max_retries, self.settings.backoff_base, self.settings.backoff_max)
            self._client = DriveClient(
                self.auth.session_factory(), policy,
                RateLimiter(self.settings.requests_per_second), self.settings.timeout, self.stop_event,
            )
        return self._client

    @property
    def account(self) -> str:
        if self._account is None:
            self.client  # valida/renova credenciais
            email = self.auth.stored_account()
            if not email:
                email = self.client.get_about()["email"]
            self._account = email
        return self._account

    def downloader(self, reporter: ConsoleReporter) -> Downloader:
        return Downloader(self.client, self.history, self.settings, self.account,
                          stop_event=self.stop_event, reporter=reporter)

    def reporter(self) -> ConsoleReporter:
        return ConsoleReporter(self.settings.max_workers, enabled=sys.stdout.isatty())

    def dest(self) -> Path:
        dest = Path(self.args.dest).expanduser() if getattr(self.args, "dest", None) else self.settings.download_dir
        dest = Path(dest).resolve()
        if dest.exists() and not dest.is_dir():
            raise ConfigError(f"O destino {dest} existe e não é uma pasta.")
        dest.mkdir(parents=True, exist_ok=True)
        return dest

    def offer_resume(self) -> None:
        """Na inicialização, informa tarefas interrompidas e oferece retomá-las."""
        email = self.auth.stored_account()
        if not email or not self.try_lock():
            return
        tasks = resumable_tasks(self.history, email)
        if not tasks:
            return
        echo(f"Há {len(tasks)} download(s) interrompido(s) ou pendente(s) de execuções anteriores.")
        if self.interactive and confirm("Deseja retomá-los agora?"):
            cmd_resume(self, assume_yes=True)
            echo("")
        else:
            echo("Para retomar depois: python main.py resume\n")


# ================================================================ helpers
def _apply_runtime_overrides(app: App) -> None:
    workers = getattr(app.args, "workers", None)
    if workers:
        app.settings.max_workers = workers
        app.settings.validate()


def _finish(outcomes: list[Outcome], interrupted: bool) -> int:
    print_summary(outcomes, interrupted)
    if interrupted:
        return 130
    return 1 if any(o.status == H.ERROR for o in outcomes) else 0


def _resolve_ref(app: App, text: str) -> tuple[dict[str, Any], str | None]:
    """Resolve link/ID em metadados. Atalhos são seguidos. Retorna (meta, nome_do_atalho)."""
    ref = parse_drive_reference(text)
    app.client.register_resource_key(ref.file_id, ref.resource_key)
    meta = app.client.get_file(ref.file_id)
    shortcut_name = None
    if meta.get("mimeType") == SHORTCUT_MIME:
        target = (meta.get("shortcutDetails") or {}).get("targetId")
        if not target:
            raise GDSDError("O atalho não possui destino.")
        echo(f"'{meta.get('name')}' é um atalho; usando o item de destino.")
        shortcut_name = meta.get("name")
        meta = app.client.get_file(target)
    return meta, shortcut_name


def _choose_export(app: App, meta: dict[str, Any], requested: str | None):
    mime = meta.get("mimeType", "")
    if not is_google_native(mime):
        if requested:
            echo("Aviso: --format só se aplica a arquivos do Google Docs/Sheets/Slides; ignorado.")
        return None
    if mime in NOT_EXPORTABLE:
        raise ExportNotSupported(NOT_EXPORTABLE[mime])
    kind = export_kind(mime)
    if kind is None:
        raise ExportNotSupported(f"Tipo Google sem exportação suportada: {mime}.")
    label = KIND_LABEL[kind]
    echo(f"'{meta.get('name')}' é um {label}: o arquivo será CONVERTIDO (exportado) pelo Google.")
    echo("A conversão pode não preservar toda a formatação, comentários ou recursos do original.")
    if requested or not app.interactive:
        return resolve_format(mime, requested, app.settings.export_formats)
    options = formats_for(mime)
    default = resolve_format(mime, None, app.settings.export_formats)
    for i, fmt in enumerate(options, 1):
        mark = " (padrão)" if fmt.key == default.key else ""
        note = f" — {fmt.note}" if fmt.note else ""
        echo(f"  {i}. {fmt.key}{mark}{note}")
    while True:
        answer = ask(f"Formato de exportação [Enter = {default.key}]: ", default.key)
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return options[int(answer) - 1]
        try:
            return resolve_format(mime, answer, app.settings.export_formats)
        except ExportNotSupported as exc:
            echo(str(exc))


def download_file(app: App, meta: dict[str, Any], requested_format: str | None,
                  name_override: str | None = None) -> int:
    if meta.get("mimeType") == FOLDER_MIME:
        raise GDSDError("O link aponta para uma PASTA. Use: python main.py folder \"LINK\"")
    export = _choose_export(app, meta, requested_format)
    dest = app.dest()
    name = local_file_name(meta, export, name=name_override, target_name=meta.get("name") if name_override else None)
    local = safe_join(dest, name)
    size = meta.get("size")
    echo(f"Arquivo: {meta.get('name')}  |  Tamanho: {human_size(int(size)) if size else 'desconhecido'}"
         f"  |  Origem: {app.client.describe_origin(meta)}")
    echo(f"Destino: {local}")
    job = app.history.create_job(app.account, "file", meta["id"], meta.get("name", ""), str(dest))
    reporter = app.reporter()
    dl = app.downloader(reporter)
    task_id, early = dl.plan(meta, local, dest_root=dest, export=export, job_id=job,
                             on_conflict=getattr(app.args, "on_conflict", None))
    if early is not None:
        summary = print_summary([early])
        app.history.finish_job(job, summary)
        return 1 if early.status == H.ERROR else 0
    try:
        result = dl.run_tasks([task_id])
    finally:
        reporter.close()
    code = _finish(result.outcomes, result.interrupted)
    app.history.finish_job(job, f"{len(result.outcomes)} tarefa(s)")
    return code


def _folder_options_from_args(args: argparse.Namespace, base: dict[str, Any] | None = None) -> FolderOptions:
    """Opções da pasta: as salvas na sincronização (``base``) com as da linha de comando por cima."""
    base = base or {}
    overrides = dict(base.get("export_overrides") or {})
    overrides.update({kind: getattr(args, attr) for attr, kind in KIND_ARGS.items() if getattr(args, attr, None)})
    return FolderOptions(
        export_overrides=overrides,
        on_conflict=getattr(args, "on_conflict", None) or base.get("on_conflict"),
        on_update=getattr(args, "on_update", None) or base.get("on_update") or "keep-both",
        dry_run=getattr(args, "dry_run", False),
    )


def _print_changes(plan: Any, limit: int = 50) -> None:
    changes = [("NOVO", p) for p in plan.new_files] + [("ALTERADO NO DRIVE", p) for p in plan.updated_files]
    for label, path in changes[:limit]:
        echo(f"  [{label}] {path}")
    if len(changes) > limit:
        echo(f"  ... e mais {len(changes) - limit}")


def run_folder(app: App, meta: dict[str, Any], dest: Path, options: FolderOptions,
               resource_key: str | None = None) -> tuple[int, str]:
    """Baixa/sincroniza uma pasta. Retorna (código de saída, resumo)."""
    if meta.get("mimeType") != FOLDER_MIME:
        raise GDSDError("O link aponta para um ARQUIVO. Use: python main.py download \"LINK\"")
    # Se esta pasta já foi baixada para este destino, reutiliza o mesmo nome de
    # pasta local (mesmo que tenha sido renomeada no Drive) e as opções salvas.
    known = [r for r in app.history.list_synced_folders(app.account, meta["id"])
             if os.path.normcase(r["dest_root"]) == os.path.normcase(str(dest))]
    if known and not options.root_dir_name:
        options.root_dir_name = known[0]["local_root"]

    reporter = app.reporter()
    dl = app.downloader(reporter)
    fd = FolderDownloader(app.client, dl, app.settings.export_formats)
    echo(f"Pasta: {meta.get('name')}  |  Origem: {app.client.describe_origin(meta)}")
    echo("Mapeando a estrutura no Drive (pode demorar em pastas grandes)...")
    job = None if options.dry_run else app.history.create_job(
        app.account, "folder", meta["id"], meta.get("name", ""), str(dest))
    try:
        plan = fd.plan(meta, dest, job, options)
    except BaseException:
        reporter.close()
        raise

    echo(f"\nEncontrados {plan.files} arquivo(s) em {plan.folders} pasta(s): "
         f"{len(plan.new_files)} novo(s), {len(plan.updated_files)} alterado(s) no Drive, "
         f"{plan.unchanged} já atualizado(s).")
    if plan.new_files or plan.updated_files:
        echo(f"A baixar: {human_size(plan.known_bytes)}"
             + (f" (+ {plan.unknown_sizes} exportação(ões)/arquivo(s) sem tamanho informado)"
                if plan.unknown_sizes else ""))
        _print_changes(plan)
    echo(f"Pasta local: {plan.local_root}")

    if not options.dry_run:
        app.history.register_synced_folder(
            app.account, meta["id"], meta.get("name", ""), str(dest), plan.local_root.name,
            {"export_overrides": options.export_overrides, "on_conflict": options.on_conflict,
             "on_update": options.on_update},
            resource_key=resource_key or meta.get("resourceKey"),
        )
    if options.dry_run:
        reporter.close()
        echo("\nSimulação (--dry-run): nada foi baixado.")
        others = [line for line in plan.dry_items if not line.startswith(("[NOVO]", "[ALTERADO"))]
        for line in others:
            echo(f"  {line}")
        return 0, "simulação"

    if not plan.task_ids and not any(o.status == H.ERROR for o in plan.early):
        reporter.close()
        echo("\nNenhuma novidade: a pasta local já está igual ao Drive.")
        summary = print_summary(plan.early)
        if job is not None:
            app.history.finish_job(job, summary)
        return 0, summary

    free = shutil.disk_usage(dest).free
    if plan.known_bytes + app.settings.min_free_space > free:
        reporter.close()
        raise GDSDError(f"Espaço livre insuficiente em {dest}: livre {human_size(free)}, "
                        f"necessário ao menos {human_size(plan.known_bytes)} + margem.")
    reporter.set_total(len(plan.task_ids))
    try:
        result = dl.run_tasks(plan.task_ids)
    finally:
        reporter.close()
    outcomes = plan.early + result.outcomes
    summary = print_summary(outcomes, result.interrupted)
    if job is not None:
        app.history.finish_job(job, summary)
    if result.interrupted:
        return 130, summary
    return (1 if any(o.status == H.ERROR for o in outcomes) else 0), summary


def download_folder(app: App, meta: dict[str, Any], resource_key: str | None = None) -> int:
    code, _ = run_folder(app, meta, app.dest(), _folder_options_from_args(app.args), resource_key)
    return code


# =============================================================== comandos
def cmd_login(app: App) -> int:
    account, new = app.auth.login(app.args.client_secret, switch=app.args.switch,
                                  open_browser=not app.args.no_browser)
    if new:
        echo(f"Login concluído. Conta conectada: {account.email}")
    else:
        echo(f"Já autenticado como {account.email}. Para trocar de conta: python main.py login --switch")
    app.offer_resume()
    return 0


def cmd_account(app: App) -> int:
    stored = app.auth.stored_account()
    if not stored:
        echo("Nenhuma conta conectada. Execute: python main.py login")
        return 3
    about = app.client.get_about()
    echo(f"Conta conectada: {about['email']}" + (f" ({about['name']})" if about.get("name") else ""))
    echo("Permissão concedida: somente leitura do Google Drive (drive.readonly)")
    echo(f"Pasta de downloads padrão: {app.settings.download_dir.resolve()}")
    echo(f"Dados do programa (histórico, logs): {app.settings.home}")
    counts = app.history.counts(about["email"])
    if counts:
        echo("Tarefas no histórico: " + ", ".join(f"{H.STATUS_LABELS.get(k, k)}={v}" for k, v in counts.items()))
    app.offer_resume()
    return 0


def cmd_logout(app: App) -> int:
    echo(app.auth.logout(revoke=not app.args.local_only))
    return 0


def cmd_search(app: App) -> int:
    args = app.args
    items, warnings = app.client.search(args.term, kind=args.type, scope=args.scope, limit=args.limit)
    for w in warnings:
        echo(f"Aviso: {w}")
    if not items:
        echo("Nenhum resultado. Observação: arquivos acessíveis apenas por link e nunca abertos pela conta "
             "não aparecem na pesquisa; nesse caso use o link diretamente.")
        return 0
    rows = []
    for i, it in enumerate(items, 1):
        mime = it.get("mimeType", "")
        kind = "Pasta" if mime == FOLDER_MIME else ("Atalho" if mime == SHORTCUT_MIME
                                                     else KIND_LABEL.get(export_kind(mime) or "", "Arquivo"))
        size = human_size(int(it["size"])) if it.get("size") else "-"
        rows.append([i, kind, it.get("name", ""), size, (it.get("modifiedTime") or "")[:10],
                     app.client.describe_origin(it), it["id"]])
    print_table(["#", "Tipo", "Nome", "Tamanho", "Modificado", "Origem", "ID"], rows,
                [4, 18, 50, 10, 10, 40, 44])
    if not app.interactive or args.no_select:
        echo("\nPara baixar: python main.py download ID   |   python main.py folder ID")
        return 0
    answer = ask("\nNúmeros para baixar (ex.: 1,3-5) ou Enter para sair: ")
    if not answer:
        return 0
    try:
        chosen = parse_selection(answer, len(items))
    except ValueError as exc:
        err(f"Seleção inválida: {exc}")
        return 2
    app.ensure_lock()
    code = 0
    for idx in chosen:
        meta = items[idx - 1]
        echo(f"\n=== {meta.get('name')} ===")
        try:
            if meta.get("mimeType") == SHORTCUT_MIME:
                meta, _ = _resolve_ref(app, meta["id"])
            if meta.get("mimeType") == FOLDER_MIME:
                rc = download_folder(app, meta)
            else:
                rc = download_file(app, meta, None)
        except Interrupted:
            return 130
        except GDSDError as exc:
            if exc.exit_code == 3:
                raise
            err(f"Erro: {exc}")
            rc = 1
        if rc == 130:
            return 130
        code = max(code, rc)
    return code


def cmd_download(app: App) -> int:
    app.offer_resume()
    app.ensure_lock()
    meta, shortcut_name = _resolve_ref(app, app.args.target)
    return download_file(app, meta, app.args.format, name_override=shortcut_name)


def cmd_folder(app: App) -> int:
    if not app.args.dry_run:
        app.offer_resume()
        app.ensure_lock()
    meta, _ = _resolve_ref(app, app.args.target)
    return download_folder(app, meta)


def _print_synced(records: list[dict[str, Any]]) -> None:
    rows = [[r["id"], r.get("remote_name") or r["folder_id"],
             str(Path(r["dest_root"]) / r["local_root"]),
             (r.get("last_sync_at") or "")[:19].replace("T", " "), r.get("last_summary") or "-"]
            for r in records]
    print_table(["#", "Pasta no Drive", "Pasta local", "Última sincronização", "Resultado"], rows,
                [5, 35, 60, 19, 60])


def cmd_sync(app: App) -> int:
    """Baixa apenas o que é novo (ou alterado) em pastas já baixadas anteriormente."""
    args = app.args
    if args.list:
        records = app.history.list_synced_folders(app.account)
        if not records:
            echo("Nenhuma pasta registrada. Baixe uma pasta com: python main.py folder \"LINK\"")
            return 0
        _print_synced(records)
        echo("\nSincronizar todas: python main.py sync   |   uma: python main.py sync LINK_OU_ID")
        return 0
    if args.remove:
        record = app.history.get_synced_folder(args.remove)
        if not record or record["account_email"] != app.account:
            echo(f"Registro #{args.remove} não encontrado.")
            return 1
        app.history.remove_synced_folder(args.remove)
        echo(f"A pasta '{record.get('remote_name')}' não será mais sincronizada. "
             "Nenhum arquivo local foi apagado.")
        return 0

    if not args.dry_run:
        app.ensure_lock()

    if args.target:
        meta, _ = _resolve_ref(app, args.target)
        if meta.get("mimeType") != FOLDER_MIME:
            raise GDSDError("A sincronização funciona com pastas. Para um arquivo use: python main.py download")
        records = app.history.list_synced_folders(app.account, meta["id"])
        if args.dest:
            dest = str(Path(args.dest).expanduser().resolve())
            records = [r for r in records if os.path.normcase(r["dest_root"]) == os.path.normcase(dest)]
        if not records:
            echo("Esta pasta ainda não foi baixada para este destino; será baixada por completo.")
            code, _ = run_folder(app, meta, app.dest(), _folder_options_from_args(args))
            return code
        targets = [(r, meta) for r in records]
    else:
        records = app.history.list_synced_folders(app.account)
        if not records:
            echo("Nenhuma pasta registrada para sincronizar. Baixe uma pasta primeiro com: "
                 "python main.py folder \"LINK\"")
            return 0
        targets = [(r, None) for r in records]

    worst = 0
    for record, meta in targets:
        echo(f"\n=== Sincronizando: {record.get('remote_name') or record['folder_id']} ===")
        local_root = Path(record["dest_root"]) / record["local_root"]
        if not local_root.is_dir():
            # Nunca rebaixar tudo silenciosamente: a pasta pode ter sido movida.
            err(f"A pasta local não foi encontrada: {local_root}\n"
                f"  Se você a moveu: python main.py relocate {record['id']} \"NOVO\\CAMINHO\\{record['local_root']}\"\n"
                f"  Se a apagou e quer baixar tudo de novo: python main.py folder {record['folder_id']} "
                f"--dest \"{record['dest_root']}\"\n"
                f"  Para parar de sincronizá-la: python main.py sync --remove {record['id']}")
            if not args.dry_run:
                app.history.mark_synced(record["id"], "pasta local não encontrada")
            worst = max(worst, 1)
            continue
        try:
            if meta is None:
                app.client.register_resource_key(record["folder_id"], record.get("resource_key"))
                meta = app.client.get_file(record["folder_id"])
            dest = Path(record["dest_root"])
            dest.mkdir(parents=True, exist_ok=True)
            options = _folder_options_from_args(args, record["options"])
            options.root_dir_name = record["local_root"]
            code, summary = run_folder(app, meta, dest, options, record.get("resource_key"))
            if not options.dry_run:
                app.history.mark_synced(record["id"], summary)
        except Interrupted:
            raise
        except GDSDError as exc:
            if exc.exit_code == 3:
                raise
            err(f"Erro ao sincronizar '{record.get('remote_name')}': {exc}")
            app.history.mark_synced(record["id"], f"erro: {exc}")
            code = 1
        if code == 130:
            return 130
        worst = max(worst, code)
    return worst


def _find_synced_record(app: App, ref: str) -> dict[str, Any]:
    """Localiza um registro de sincronização pelo número (``sync --list``) ou pelo link/ID da pasta."""
    if ref.isdigit():
        record = app.history.get_synced_folder(int(ref))
        if record and record["account_email"] == app.account:
            return record
        raise GDSDError(f"Registro #{ref} não encontrado. Veja os números com: python main.py sync --list")
    folder_id = parse_drive_reference(ref).file_id
    records = app.history.list_synced_folders(app.account, folder_id)
    if not records:
        raise GDSDError("Esta pasta do Drive não está registrada. Veja: python main.py sync --list")
    if len(records) > 1:
        ids = ", ".join(f"#{r['id']}" for r in records)
        raise GDSDError(f"Esta pasta foi baixada em mais de um lugar ({ids}). Informe o número do registro.")
    return records[0]


def cmd_relocate(app: App) -> int:
    """Informa o novo local de uma pasta já baixada que foi movida no computador."""
    app.ensure_lock()
    args = app.args
    record = _find_synced_record(app, args.record)
    new_root = Path(args.new_path).expanduser().resolve()
    if not new_root.is_dir():
        raise GDSDError(f"A pasta informada não existe: {new_root}")
    old_root = Path(os.path.abspath(Path(record["dest_root"]) / record["local_root"]))
    if os.path.normcase(str(old_root)) == os.path.normcase(str(new_root)):
        echo("Esse já é o local registrado para esta pasta. Nada a fazer.")
        return 0

    done = [t for t in app.history.tasks_under(app.account, old_root) if t["status"] == H.COMPLETED]
    found = 0
    for t in done:
        candidate = new_root / os.path.relpath(t["local_path"], old_root)
        if exists(candidate) and os.path.getsize(candidate) == t["downloaded_bytes"]:
            found += 1
    echo(f"Pasta no Drive: {record.get('remote_name') or record['folder_id']}")
    echo(f"Local antigo:   {old_root}")
    echo(f"Novo local:     {new_root}")
    echo(f"Arquivos já baixados encontrados no novo local: {found} de {len(done)}.")
    if done and found == 0 and not args.force:
        raise GDSDError("Nenhum dos arquivos baixados foi encontrado no novo local. Confira se o caminho "
                        "aponta para a própria pasta (ex.: ...\\workshop) ou use --force.")
    if old_root.is_dir():
        echo("Aviso: a pasta antiga ainda existe. A partir de agora, só a nova será sincronizada.")
    if app.interactive and not args.yes and not confirm("Confirmar a mudança de local?", default=True):
        return 0
    try:
        updated = app.history.relocate_synced_folder(record["id"], new_root.parent, new_root.name)
    except ValueError as exc:
        raise GDSDError(str(exc)) from exc
    echo(f"Local atualizado ({updated} registro(s) do histórico ajustados). Nenhum arquivo foi baixado ou movido.")
    if found < len(done):
        echo(f"{len(done) - found} arquivo(s) não estão no novo local e serão baixados no próximo sync.")
    echo("Para conferir as novidades: python main.py sync --dry-run")
    return 0


def cmd_resume(app: App, assume_yes: bool | None = None) -> int:
    app.ensure_lock()
    ids = prepare_resume(app.history, app.account, getattr(app.args, "task_ids", None) or None)
    if not ids:
        echo("Nenhum download interrompido ou pendente para esta conta.")
        return 0
    tasks = [app.history.get_task(i) for i in ids]
    echo(f"{len(ids)} download(s) a retomar:")
    for t in tasks[:20]:
        part = f"{human_size(t['downloaded_bytes'])} de {human_size(t['expected_size'])}" if t["expected_size"] else ""
        echo(f"  #{t['id']} {shorten(t['file_name'], 60)} [{H.STATUS_LABELS[t['status']]}] {part}")
    if len(tasks) > 20:
        echo(f"  ... e mais {len(tasks) - 20}")
    yes = assume_yes if assume_yes is not None else getattr(app.args, "yes", False)
    if not yes and app.interactive and not confirm("Retomar agora?", default=True):
        return 0
    echo("Antes de continuar cada arquivo parcial, o programa confirma que o arquivo remoto não mudou.")
    reporter = app.reporter()
    reporter.set_total(len(ids))
    try:
        result = app.downloader(reporter).run_tasks(ids)
    finally:
        reporter.close()
    return _finish(result.outcomes, result.interrupted)


def cmd_retry(app: App) -> int:
    app.ensure_lock()
    args = app.args
    if not args.task_ids and not args.all:
        errors = app.history.list_tasks(statuses=[H.ERROR], account_email=app.account)
        if not errors:
            echo("Nenhum download com erro.")
            return 0
        echo(f"{len(errors)} download(s) com erro. Use 'retry --all' ou 'retry ID [ID ...]'.")
        for t in errors[:20]:
            echo(f"  #{t['id']} {shorten(t['file_name'], 50)}: {shorten(t['error'] or '', 80)}")
        return 0
    ids, messages = prepare_retry(app.history, app.account, args.task_ids, include_all_errors=args.all)
    for m in messages:
        echo(m)
    if not ids:
        echo("Nada a tentar novamente.")
        return 0
    reporter = app.reporter()
    reporter.set_total(len(ids))
    try:
        result = app.downloader(reporter).run_tasks(ids)
    finally:
        reporter.close()
    return _finish(result.outcomes, result.interrupted)


def cmd_cancel(app: App) -> int:
    app.ensure_lock()
    for m in cancel_tasks(app.history, app.account, app.args.task_ids, app.args.delete_partial):
        echo(m)
    return 0


def _account_filter(app: App) -> str | None:
    if getattr(app.args, "all_accounts", False):
        return None
    return app.auth.stored_account()


def cmd_history(app: App) -> int:
    args = app.args
    if args.id:
        task = app.history.get_task(args.id)
        if not task:
            echo(f"Tarefa #{args.id} não encontrada.")
            return 1
        labels = {
            "id": "Tarefa", "status": "Estado", "account_email": "Conta", "file_name": "Nome remoto",
            "file_id": "ID remoto", "parent_id": "ID da pasta de origem", "drive_id": "Drive compartilhado",
            "mime_type": "Tipo MIME", "remote_path": "Caminho remoto", "local_path": "Destino local",
            "temp_path": "Arquivo parcial", "expected_size": "Tamanho esperado (bytes)",
            "downloaded_bytes": "Bytes baixados", "sha256": "SHA-256 local", "google_md5": "MD5 do Google",
            "google_sha256": "SHA-256 do Google", "integrity_status": "Integridade",
            "integrity_detail": "Detalhe da integridade", "export_format": "Formato de exportação",
            "remote_modified_time": "Modificado no Drive", "remote_revision": "Revisão remota",
            "attempts": "Tentativas", "created_at": "Criada em", "updated_at": "Atualizada em",
            "last_attempt_at": "Última tentativa", "error": "Mensagem/erro", "job_id": "Execução",
        }
        for key, label in labels.items():
            value = task.get(key)
            if key == "status":
                value = H.STATUS_LABELS.get(value, value)
            if key == "integrity_status" and value:
                value = integrity.LABELS.get(value, value)
            echo(f"{label:28}: {value if value not in (None, '') else '-'}")
        return 0
    statuses = [STATUS_ARG[args.status]] if args.status else None
    tasks = app.history.list_tasks(statuses=statuses, account_email=_account_filter(app),
                                   name_like=args.name, limit=args.limit)
    if not tasks:
        echo("Histórico vazio para os filtros informados.")
        return 0
    rows = [[t["id"], (t["updated_at"] or "")[:19].replace("T", " "), H.STATUS_LABELS.get(t["status"], t["status"]),
             t["file_name"], human_size(t["downloaded_bytes"]) if t["downloaded_bytes"] else "-",
             {integrity.VERIFIED_SHA256: "sha256 ok", integrity.VERIFIED_MD5: "md5 ok",
              integrity.SIZE_ONLY: "tamanho ok", integrity.UNVERIFIABLE: "não verificável",
              integrity.FAILED: "FALHOU"}.get(t["integrity_status"] or "", "-"),
             t["error"] if t["status"] in (H.ERROR, H.SKIPPED, H.INTERRUPTED) else t["local_path"]]
            for t in tasks]
    print_table(["#", "Atualizado", "Estado", "Nome", "Baixado", "Integridade", "Destino / mensagem"], rows,
                [6, 19, 13, 40, 10, 15, 70])
    echo("\nDetalhes de uma tarefa: python main.py history --id N")
    return 0


def cmd_status(app: App) -> int:
    locked = app.try_lock()
    if not locked:
        echo("Outra execução do programa está em andamento neste momento.")
    tasks = app.history.list_tasks(statuses=H.RESUMABLE, account_email=_account_filter(app), newest_first=False)
    if not tasks:
        echo("Nenhum download em andamento, pendente ou interrompido.")
    else:
        rows = []
        for t in tasks:
            pct = (f"{100 * t['downloaded_bytes'] / t['expected_size']:.0f}%"
                   if t["expected_size"] else human_size(t["downloaded_bytes"]))
            rows.append([t["id"], H.STATUS_LABELS[t["status"]], t["file_name"], pct,
                         (t["last_attempt_at"] or "")[:19].replace("T", " "), t["local_path"]])
        print_table(["#", "Estado", "Nome", "Progresso", "Última tentativa", "Destino"], rows,
                    [6, 13, 40, 10, 19, 70])
        echo("\nPara retomar: python main.py resume")
    errors = app.history.list_tasks(statuses=[H.ERROR], account_email=_account_filter(app))
    if errors:
        echo(f"{len(errors)} download(s) com erro. Veja: python main.py retry")
    return 0


def cmd_verify(app: App) -> int:
    args = app.args
    if args.task_ids:
        tasks = [t for t in (app.history.get_task(i) for i in args.task_ids) if t]
    elif args.all:
        tasks = app.history.list_tasks(statuses=[H.COMPLETED], account_email=_account_filter(app))
    else:
        echo("Informe IDs de tarefas ou use --all.")
        return 2
    bad = 0
    for t in tasks:
        path = Path(t["local_path"])
        if t["status"] != H.COMPLETED or not t["sha256"]:
            echo(f"#{t['id']} {t['file_name']}: não concluída; nada a verificar.")
            continue
        if not exists(path):
            echo(f"[AUSENTE] #{t['id']} {path}")
            bad += 1
            continue
        ok, _ = integrity.check_against_record(path, t["sha256"])
        echo(f"{'[OK]' if ok else '[ALTERADO]'} #{t['id']} {path}")
        bad += 0 if ok else 1
    echo("\nObservação: esta verificação compara com o SHA-256 registrado no download; "
         "ela detecta alteração/corrupção local posterior.")
    return 1 if bad else 0


def cmd_formats(app: App) -> int:
    for kind, options in EXPORT_FORMATS.items():
        default = app.settings.export_formats.get(kind)
        items = ", ".join(f"{f.key}{'*' if f.key == default else ''}" for f in options)
        echo(f"{KIND_LABEL[kind]:22} {items}")
    echo("\n* = padrão atual. Altere em config.toml (seção [exportacao]) ou use --format / --doc-format etc.")
    echo("Não exportáveis pela API: Formulários, Sites, My Maps, Jamboard, Vids.")
    return 0


def cmd_config(app: App) -> int:
    path = app.settings.config_path
    if app.args.init:
        if path.exists():
            echo(f"Já existe: {path}")
            return 1
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(EXAMPLE_CONFIG, encoding="utf-8")
        echo(f"Arquivo de configuração criado: {path}")
        return 0
    s = app.settings
    echo(f"Arquivo de configuração: {path} {'(existe)' if path.exists() else '(não criado; use config --init)'}")
    echo(f"Dados do programa:       {s.home}")
    echo(f"Histórico (SQLite):      {s.db_path}")
    echo(f"Logs:                    {s.log_dir}")
    echo(f"Pasta de downloads:      {s.download_dir.resolve()}")
    echo(f"Downloads simultâneos:   {s.max_workers}")
    echo(f"Tentativas por arquivo:  {s.max_retries}")
    echo(f"Conflito de nomes:       {s.on_conflict}")
    echo(f"Formatos de exportação:  {s.export_formats}")
    return 0


# ================================================================= parser
HELP_EPILOG = """\
Exemplos:
  python main.py login --client-secret C:\\caminho\\client_secret.json
  python main.py account
  python main.py search "relatórios"
  python main.py download "https://drive.google.com/file/d/ID/view" --dest "D:\\Arquivos"
  python main.py download ID_DO_DOC --format pdf
  python main.py folder "https://drive.google.com/drive/folders/ID" --dest "D:\\Arquivos"
  python main.py sync                      (baixa só o que é novo nas pastas já baixadas)
  python main.py sync --dry-run            (apenas mostra as novidades)
  python main.py resume
  python main.py retry --all
  python main.py history --status erro
"""


def _localize(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser._positionals.title = "argumentos"
    parser._optionals.title = "opções"
    parser.add_argument("-h", "--help", action="help", help="mostra esta ajuda e sai")
    return parser


def build_parser() -> argparse.ArgumentParser:
    frozen = getattr(sys, "frozen", False)
    p = _localize(argparse.ArgumentParser(
        prog=command_name() if frozen else "main.py",
        description="Download seguro de arquivos e pastas do Google Drive (somente leitura).",
        epilog=HELP_EPILOG.replace("python main.py", command_name()) if frozen else HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=False,
    ))
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}",
                   help="mostra a versão e sai")
    p.add_argument("-v", "--verbose", action="store_true", help="mostra logs detalhados no terminal")
    p.add_argument("--no-input", action="store_true", help="nunca faz perguntas (usa os padrões)")
    sub = p.add_subparsers(dest="command", metavar="COMANDO")

    def add(name: str, help_text: str) -> argparse.ArgumentParser:
        return _localize(sub.add_parser(name, help=help_text, description=help_text, add_help=False,
                                        formatter_class=argparse.RawDescriptionHelpFormatter))

    def add_conflict(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--on-conflict", choices=CONFLICT_POLICIES, default=None,
                        help="se o arquivo local já existir: rename (padrão) cria 'nome (2).ext'; "
                             "skip ignora; overwrite substitui (autorização explícita)")

    def add_on_update(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--on-update", choices=UPDATE_POLICIES, default=None,
                        help="arquivo já baixado que mudou no Drive: keep-both (padrão) salva a nova "
                             "versão como 'nome (2).ext'; replace substitui a cópia local se ela não foi "
                             "alterada no computador; skip mantém a versão local")

    def add_dest(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--dest", help="pasta local de destino (padrão: configuração download_dir)")

    sp = add("login", "autentica (ou confirma) a conta Google")
    sp.add_argument("--client-secret", metavar="ARQUIVO", help="caminho do client_secret.json (copiado para os dados do programa)")
    sp.add_argument("--switch", action="store_true", help="força novo login para trocar de conta")
    sp.add_argument("--no-browser", action="store_true", help="não abre o navegador automaticamente; exibe o link")

    add("account", "exibe a conta conectada e as pastas usadas pelo programa")

    sp = add("logout", "desconecta a conta e revoga a autorização no Google")
    sp.add_argument("--local-only", action="store_true", help="apenas remove a autorização deste computador")

    sp = add("search", "pesquisa arquivos e pastas pelo nome")
    sp.add_argument("term", help="texto a pesquisar no nome")
    sp.add_argument("--type", choices=["all", "file", "folder"], default="all", help="tipo de item (padrão: all)")
    sp.add_argument("--scope", choices=["all", "mydrive", "shared", "drives"], default="all",
                    help="onde pesquisar: all, mydrive (Meu Drive), shared (compartilhados comigo), drives (drives compartilhados)")
    sp.add_argument("--limit", type=int, default=50, help="máximo de resultados (padrão: 50)")
    sp.add_argument("--no-select", action="store_true", help="apenas lista, sem perguntar o que baixar")
    add_dest(sp)
    add_conflict(sp)

    sp = add("download", "baixa um arquivo por link ou ID")
    sp.add_argument("target", metavar="LINK_OU_ID")
    add_dest(sp)
    sp.add_argument("--format", help="formato de exportação para arquivos Google (ex.: docx, pdf, xlsx, csv, pptx)")
    add_conflict(sp)

    sp = add("folder", "baixa uma pasta inteira (com subpastas) por link ou ID")
    sp.add_argument("target", metavar="LINK_OU_ID")
    add_dest(sp)
    for attr, kind in KIND_ARGS.items():
        sp.add_argument("--" + attr.replace("_", "-"), choices=[f.key for f in EXPORT_FORMATS[kind]],
                        help=f"formato para {KIND_LABEL[kind]}")
    sp.add_argument("--workers", type=int, help="downloads simultâneos (1 a 8)")
    sp.add_argument("--dry-run", action="store_true", help="apenas mostra o que seria baixado")
    add_conflict(sp)
    add_on_update(sp)

    sp = add("sync", "baixa somente arquivos novos ou alterados de pastas já baixadas")
    sp.add_argument("target", metavar="LINK_OU_ID", nargs="?",
                    help="pasta a sincronizar (padrão: todas as pastas já baixadas)")
    sp.add_argument("--dest", help="pasta local onde a pasta foi baixada (quando há mais de uma)")
    sp.add_argument("--list", action="store_true", help="lista as pastas registradas para sincronização")
    sp.add_argument("--remove", type=int, metavar="ID",
                    help="deixa de sincronizar o registro ID (não apaga arquivos)")
    sp.add_argument("--dry-run", action="store_true", help="apenas mostra as novidades, sem baixar")
    sp.add_argument("--workers", type=int, help="downloads simultâneos (1 a 8)")
    for attr, kind in KIND_ARGS.items():
        sp.add_argument("--" + attr.replace("_", "-"), choices=[f.key for f in EXPORT_FORMATS[kind]],
                        help=f"formato para {KIND_LABEL[kind]} (padrão: o usado no download original)")
    add_conflict(sp)
    add_on_update(sp)

    sp = add("relocate", "informa o novo local de uma pasta já baixada que você moveu (sem baixar de novo)")
    sp.add_argument("record", metavar="REGISTRO",
                    help="número mostrado em 'sync --list', ou o link/ID da pasta no Drive")
    sp.add_argument("new_path", metavar="NOVO_CAMINHO",
                    help="caminho da própria pasta no novo local, ex.: \"E:\\Backup\\workshop\"")
    sp.add_argument("--force", action="store_true",
                    help="aceita mesmo que nenhum arquivo baixado seja encontrado no novo local")
    sp.add_argument("-y", "--yes", action="store_true", help="não pede confirmação")

    sp = add("resume", "retoma downloads interrompidos ou pendentes")
    sp.add_argument("task_ids", nargs="*", type=int, metavar="ID", help="tarefas específicas (padrão: todas)")
    sp.add_argument("-y", "--yes", action="store_true", help="não pede confirmação")
    sp.add_argument("--workers", type=int, help="downloads simultâneos (1 a 8)")

    sp = add("retry", "tenta novamente downloads com erro")
    sp.add_argument("task_ids", nargs="*", type=int, metavar="ID")
    sp.add_argument("--all", action="store_true", help="todas as tarefas com erro")
    sp.add_argument("--workers", type=int, help="downloads simultâneos (1 a 8)")

    sp = add("cancel", "cancela tarefas pendentes/interrompidas/com erro")
    sp.add_argument("task_ids", nargs="+", type=int, metavar="ID")
    sp.add_argument("--delete-partial", action="store_true", help="apaga também o arquivo parcial (.gdpart)")

    sp = add("history", "consulta o histórico de downloads")
    sp.add_argument("--limit", type=int, default=30, help="quantidade de linhas (padrão: 30)")
    sp.add_argument("--status", choices=list(STATUS_ARG), help="filtra por estado")
    sp.add_argument("--name", help="filtra por parte do nome")
    sp.add_argument("--id", type=int, help="exibe todos os detalhes de uma tarefa")
    sp.add_argument("--all-accounts", action="store_true", help="inclui tarefas de outras contas")

    sp = add("status", "lista downloads em andamento, pendentes ou interrompidos")
    sp.add_argument("--all-accounts", action="store_true", help="inclui tarefas de outras contas")

    sp = add("verify", "confere arquivos já baixados contra o SHA-256 registrado")
    sp.add_argument("task_ids", nargs="*", type=int, metavar="ID")
    sp.add_argument("--all", action="store_true", help="todas as tarefas concluídas")
    sp.add_argument("--all-accounts", action="store_true", help="inclui tarefas de outras contas")

    add("formats", "lista os formatos de exportação de arquivos Google")

    sp = add("config", "mostra a configuração efetiva")
    sp.add_argument("--init", action="store_true", help="cria um config.toml de exemplo")
    return p


COMMANDS = {
    "login": cmd_login, "account": cmd_account, "logout": cmd_logout, "search": cmd_search,
    "download": cmd_download, "folder": cmd_folder, "sync": cmd_sync, "relocate": cmd_relocate, "resume": cmd_resume, "retry": cmd_retry,
    "cancel": cmd_cancel, "history": cmd_history, "status": cmd_status, "verify": cmd_verify,
    "formats": cmd_formats, "config": cmd_config,
}


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    try:
        settings = load_settings()
        settings.ensure_dirs()
    except GDSDError as exc:
        err(f"Erro de configuração: {exc}")
        return exc.exit_code
    setup_logging(settings.log_dir, settings.log_level, args.verbose)
    log.info("comando iniciado", extra={"ctx": {"command": args.command}})

    app: App | None = None
    try:
        app = App(args, settings)
        _apply_runtime_overrides(app)
        return COMMANDS[args.command](app)
    except KeyboardInterrupt:
        if app:
            app.stop_event.set()
        err("\nInterrompido pelo usuário. Downloads incompletos podem ser retomados com: python main.py resume")
        return 130
    except Interrupted:
        err("\nInterrompido. Downloads incompletos podem ser retomados com: python main.py resume")
        return 130
    except GDSDError as exc:
        err(f"Erro: {exc}")
        log.warning("erro", extra={"ctx": {"type": type(exc).__name__, "msg": str(exc)}})
        return exc.exit_code
    except Exception as exc:  # noqa: BLE001
        log.exception("erro inesperado")
        err(f"Erro inesperado: {type(exc).__name__}: {exc}\nDetalhes no log: {settings.log_dir / 'gdsd.log'}")
        return 1
    finally:
        if app:
            app.close()
