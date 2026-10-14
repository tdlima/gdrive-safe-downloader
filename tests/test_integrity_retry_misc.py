import hashlib
import json
import logging
import random
import threading

import pytest
import requests

from gdsd import integrity
from gdsd.errors import Interrupted, TransientError
from gdsd.logging_setup import JsonFormatter, redact
from gdsd.retry import RetryPolicy, call_with_retry


# ---------------------------------------------------------------- integridade
def test_niveis_de_verificacao(tmp_path):
    data = b"conteudo de teste"
    f = tmp_path / "a.bin"
    f.write_bytes(data)
    md5, sha = hashlib.md5(data).hexdigest(), hashlib.sha256(data).hexdigest()
    assert integrity.verify_file(f, len(data), md5, sha).status == integrity.VERIFIED_SHA256
    assert integrity.verify_file(f, len(data), md5, None).status == integrity.VERIFIED_MD5
    assert integrity.verify_file(f, len(data), None, None).status == integrity.SIZE_ONLY
    assert integrity.verify_file(f, None, None, None).status == integrity.UNVERIFIABLE
    assert integrity.verify_file(f, len(data) + 1, md5, sha).status == integrity.FAILED
    assert integrity.verify_file(f, len(data), "0" * 32, None).status == integrity.FAILED
    assert integrity.verify_file(f, len(data), md5, "0" * 64).status == integrity.FAILED
    r = integrity.verify_file(f, None, None, None)
    assert r.hashes.sha256 == sha and r.hashes.md5 == md5 and r.ok


def test_check_against_record(tmp_path):
    f = tmp_path / "a.bin"
    f.write_bytes(b"abc")
    sha = hashlib.sha256(b"abc").hexdigest()
    assert integrity.check_against_record(f, sha)[0]
    f.write_bytes(b"abd")
    assert not integrity.check_against_record(f, sha)[0]


# ---------------------------------------------------------------- retentativas
def test_backoff_exponencial_com_teto_e_retry_after():
    p = RetryPolicy(base=1, cap=8, rng=random.Random(1))
    for attempt, upper in [(1, 1), (2, 2), (3, 4), (4, 8), (10, 8)]:
        d = p.delay(attempt)
        assert upper / 2 <= d <= upper
    assert p.delay(1, retry_after=30) == 30


def test_call_with_retry_rede_e_status():
    calls = []
    waits = []
    p = RetryPolicy(max_retries=4, sleep=lambda s, e: waits.append(s))

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise requests.ConnectionError("rede caiu")
        if len(calls) == 2:
            raise TransientError("503", 503)
        return "ok"

    assert call_with_retry(flaky, p) == "ok"
    assert len(calls) == 3 and len(waits) == 2


def test_call_with_retry_nao_repete_erro_permanente():
    calls = []
    p = RetryPolicy(max_retries=4, sleep=lambda s, e: None)

    def bad():
        calls.append(1)
        raise ValueError("permanente")

    with pytest.raises(ValueError):
        call_with_retry(bad, p)
    assert len(calls) == 1


def test_call_with_retry_rede_persistente_vira_transient():
    p = RetryPolicy(max_retries=1, sleep=lambda s, e: None)
    with pytest.raises(TransientError, match="rede persistente"):
        call_with_retry(lambda: (_ for _ in ()).throw(requests.Timeout()), p)


def test_espera_interrompivel():
    stop = threading.Event()
    stop.set()
    with pytest.raises(Interrupted):
        RetryPolicy().wait(10, stop)


# ---------------------------------------------------------------- logs
def test_redacao_de_credenciais():
    text = ("Authorization: Bearer ya29.a0AfB_secret token ya29.xyz refresh_token=1//0gABCDEFGHIJKLMNOPQRSTUV "
            '{"client_secret": "GOCSPX-abc123"} url?code=4/0Aabc&state=x')
    out = redact(text)
    for secret in ("ya29.a0AfB_secret", "ya29.xyz", "1//0gABCDEFGHIJKLMNOPQRSTUV", "GOCSPX-abc123", "4/0Aabc"):
        assert secret not in out


def test_log_json_estruturado():
    rec = logging.LogRecord("x", logging.INFO, __file__, 1, "Bearer ya29.segredo", None, None)
    rec.ctx = {"task": 1}
    data = json.loads(JsonFormatter().format(rec))
    assert data["ctx"] == {"task": 1} and "ya29.segredo" not in data["msg"]
