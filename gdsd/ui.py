"""Saída no terminal: barras de progresso, tabelas, resumos e perguntas."""

from __future__ import annotations

import shutil
import sys
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from tqdm import tqdm

from . import history as H
from .downloader import Outcome, human_size

TAGS = {
    H.COMPLETED: "[OK]",
    H.SKIPPED: "[IGNORADO]",
    H.ERROR: "[ERRO]",
    H.INTERRUPTED: "[INTERROMPIDO]",
    H.CANCELLED: "[CANCELADO]",
}


def shorten(text: str, width: int) -> str:
    text = text or ""
    return text if len(text) <= width else text[: max(1, width - 1)] + "…"


def command_name() -> str:
    """Como o usuário invoca o programa (script Python ou executável PyInstaller)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).name
    return "python main.py"


def _adapt(message: str) -> str:
    return message if not getattr(sys, "frozen", False) else message.replace("python main.py", command_name())


def echo(message: str = "") -> None:
    tqdm.write(_adapt(message), file=sys.stdout)


def err(message: str) -> None:
    tqdm.write(_adapt(message), file=sys.stderr)


class ConsoleReporter:
    """Progresso por arquivo (até ``slots`` barras simultâneas) + barra geral."""

    def __init__(self, slots: int = 3, enabled: bool = True):
        self.enabled = enabled
        self._lock = threading.Lock()
        self._bars: dict[int, tuple[tqdm, int]] = {}
        self._free = list(range(1, slots + 1))
        self._overall: tqdm | None = None

    def set_total(self, files: int) -> None:
        if self._overall is not None:
            self._overall.close()
        self._overall = tqdm(total=files, desc="Total", unit="arq", position=0, leave=True,
                             dynamic_ncols=True, disable=not self.enabled or files <= 1)

    def file_started(self, task_id: int, name: str, total: int | None, offset: int, converting: bool) -> None:
        if converting:
            echo(f"  Convertendo (exportação do Google): {name}")
        with self._lock:
            old = self._bars.pop(task_id, None)
            if old:
                old[0].close()
                pos = old[1]
            else:
                pos = self._free.pop(0) if self._free else 0
            bar = tqdm(total=total, initial=offset, unit="B", unit_scale=True, unit_divisor=1024,
                       desc=shorten(name, 32), position=pos, leave=False, dynamic_ncols=True,
                       disable=not self.enabled)
            self._bars[task_id] = (bar, pos)

    def file_progress(self, task_id: int, nbytes: int) -> None:
        entry = self._bars.get(task_id)
        if entry:
            entry[0].update(nbytes)

    def file_retry(self, task_id: int, name: str, attempt: int, delay: float, error: str) -> None:
        echo(f"  [nova tentativa {attempt}] {name}: {error} — aguardando {delay:.0f}s")

    def file_finished(self, outcome: Outcome) -> None:
        with self._lock:
            entry = self._bars.pop(outcome.task_id or -1, None)
            if entry:
                entry[0].close()
                if entry[1]:
                    self._free.append(entry[1])
                    self._free.sort()
        if self._overall is not None:
            self._overall.update(1)
        if outcome.unchanged:
            return  # já atualizado: não polui a tela (aparece só no resumo)
        size = f" ({human_size(outcome.size)})" if outcome.size is not None else ""
        echo(f"{TAGS.get(outcome.status, '[?]')} {outcome.name}{size} — {outcome.message}")

    def info(self, message: str) -> None:
        echo(message)

    def close(self) -> None:
        with self._lock:
            for bar, _ in self._bars.values():
                bar.close()
            self._bars.clear()
        if self._overall is not None:
            self._overall.close()
            self._overall = None


def print_summary(outcomes: Sequence[Outcome], interrupted: bool = False) -> str:
    counts = {s: 0 for s in TAGS}
    unchanged = 0
    for o in outcomes:
        if o.unchanged:
            unchanged += 1
            continue
        counts[o.status] = counts.get(o.status, 0) + 1
    lines = [
        "",
        "Resumo:",
        f"  Concluídos:    {counts[H.COMPLETED]}",
    ]
    if unchanged:
        lines.append(f"  Já atualizados: {unchanged} (nada a fazer)")
    lines += [
        f"  Ignorados:     {counts[H.SKIPPED]}",
        f"  Com erro:      {counts[H.ERROR]}",
    ]
    if counts[H.INTERRUPTED]:
        lines.append(f"  Interrompidos: {counts[H.INTERRUPTED]}")
    errors = [o for o in outcomes if o.status == H.ERROR]
    if errors:
        lines.append("  Erros:")
        for o in errors[:30]:
            lines.append(f"    #{o.task_id} {shorten(o.name, 50)}: {o.message}")
        if len(errors) > 30:
            lines.append(f"    ... e mais {len(errors) - 30}. Veja: python main.py history --status erro")
        lines.append("  Para tentar novamente: python main.py retry --all")
    if interrupted or counts[H.INTERRUPTED]:
        lines.append("  Downloads interrompidos podem ser retomados com: python main.py resume")
    text = "\n".join(lines)
    echo(text)
    return (f"concluídos={counts[H.COMPLETED]} já_atualizados={unchanged} ignorados={counts[H.SKIPPED]} "
            f"erros={counts[H.ERROR]} interrompidos={counts[H.INTERRUPTED]}")


def print_table(headers: Sequence[str], rows: Sequence[Sequence[Any]], max_widths: Sequence[int] | None = None) -> None:
    cols = len(headers)
    str_rows = [[str(c) if c is not None else "" for c in row] for row in rows]
    widths = [len(h) for h in headers]
    for row in str_rows:
        for i in range(cols):
            widths[i] = max(widths[i], len(row[i]))
    if max_widths:
        widths = [min(w, m) for w, m in zip(widths, max_widths)]
    term = shutil.get_terminal_size((120, 20)).columns
    while sum(widths) + 2 * (cols - 1) > term and max(widths) > 12:
        idx = widths.index(max(widths))
        widths[idx] -= 1
    def fmt(row: Sequence[str]) -> str:
        return "  ".join(shorten(row[i], widths[i]).ljust(widths[i]) for i in range(cols)).rstrip()
    echo(fmt(list(headers)))
    echo("  ".join("-" * w for w in widths))
    for row in str_rows:
        echo(fmt(row))


def ask(prompt: str, default: str = "") -> str:
    try:
        answer = input(prompt)
    except EOFError:
        return default
    return answer.strip() or default


def confirm(prompt: str, default: bool = False) -> bool:
    suffix = " [S/n] " if default else " [s/N] "
    answer = ask(prompt + suffix, "s" if default else "n").lower()
    return answer in ("s", "sim", "y", "yes")


def parse_selection(text: str, maximum: int) -> list[int]:
    """Converte ``"1,3-5"`` em ``[1, 3, 4, 5]`` (1-based), validando limites."""
    result: list[int] = []
    for part in text.replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            start, end = int(a), int(b)
            if start > end:
                start, end = end, start
            values = range(start, end + 1)
        else:
            values = range(int(part), int(part) + 1)
        for v in values:
            if not 1 <= v <= maximum:
                raise ValueError(f"Número fora da lista: {v}")
            if v not in result:
                result.append(v)
    return result
