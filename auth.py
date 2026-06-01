"""Autenticação: hash de senha (PBKDF2, biblioteca padrão) e código de amigo.

Usa PBKDF2-HMAC-SHA256 da stdlib — sem dependências que exigem compilação
(diferente de bcrypt/argon2), seguro para o build do Railway.
"""
import hashlib
import hmac
import os
import secrets

_ITERATIONS = 200_000
_ALGO = "pbkdf2_sha256"

# Sem caracteres ambíguos (0/O, 1/I) para o código ditado por voz/leitura.
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def hash_password(password: str) -> str:
    """Retorna 'pbkdf2_sha256$iterações$salt_hex$hash_hex'."""
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    return f"{_ALGO}${_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Confere a senha contra o hash armazenado (comparação constante)."""
    if not stored or "$" not in stored:
        return False
    try:
        algo, iters, salt_hex, hash_hex = stored.split("$")
        if algo != _ALGO:
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iters))
        return hmac.compare_digest(dk, expected)
    except (ValueError, TypeError):
        return False


def gen_friend_code() -> str:
    """Código curto tipo 'MF-7F3K2' para amigos se acharem."""
    body = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(5))
    return f"MF-{body}"


def gen_temp_password() -> str:
    """Senha temporária legível (sem caracteres ambíguos) — para o admin
    ajudar quem perdeu a senha, enquanto não há recuperação por e-mail."""
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(8)).lower()


def normalize_username(username: str) -> str:
    return (username or "").strip().lower()


def normalize_code(code: str) -> str:
    return (code or "").strip().upper().replace(" ", "")
