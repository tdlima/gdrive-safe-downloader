"""Hierarquia de exceções com mensagens em português destinadas ao usuário."""

from __future__ import annotations


class GDSDError(Exception):
    """Erro base da aplicação. ``str(exc)`` é uma mensagem segura para exibição."""

    exit_code = 1


class ConfigError(GDSDError):
    exit_code = 2


class AuthError(GDSDError):
    exit_code = 3


class InvalidLinkError(GDSDError):
    exit_code = 2


class UnsafePathError(GDSDError):
    """Caminho de destino tentaria sair do diretório escolhido."""


class AnotherInstanceRunning(GDSDError):
    exit_code = 4


class DriveApiError(GDSDError):
    """Erro retornado pela Google Drive API."""

    def __init__(self, message: str, status: int | None = None, reason: str | None = None):
        super().__init__(message)
        self.status = status
        self.reason = reason


class NotFoundOrNoAccess(DriveApiError):
    """HTTP 404: o recurso não existe ou a conta não tem acesso a ele."""


class PermissionDenied(DriveApiError):
    """HTTP 403 não relacionado a cotas: permissão ou política impede a operação."""


class ExportSizeLimitExceeded(PermissionDenied):
    """O endpoint ``files.export`` recusou o arquivo por exceder 10 MB."""


class TransientError(DriveApiError):
    """Erro temporário (429, 5xx, limite de taxa, falha de rede) que pode ser repetido."""

    def __init__(
        self,
        message: str,
        status: int | None = None,
        reason: str | None = None,
        retry_after: float | None = None,
    ):
        super().__init__(message, status, reason)
        self.retry_after = retry_after


class RangeNotSatisfiable(DriveApiError):
    """HTTP 416 em requisição com intervalo de bytes."""


class DownloadNotAllowed(GDSDError):
    """O proprietário/administrador desativou o download para esta conta."""


class ExportNotSupported(GDSDError):
    """O tipo de arquivo Google não pode ser exportado no formato pedido (ou em nenhum)."""


class IntegrityError(GDSDError):
    """A verificação de tamanho ou checksum falhou."""


class RemoteChangedError(GDSDError):
    """O arquivo remoto mudou durante a transferência."""


class InsufficientSpaceError(GDSDError):
    """Espaço livre em disco insuficiente."""


class Interrupted(GDSDError):
    """Operação interrompida pelo usuário (Ctrl+C) ou encerramento do programa."""

    exit_code = 130
