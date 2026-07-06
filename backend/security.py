import base64
import os
import re
import secrets

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from backend.config import Config


class Security:
    _encryption_key = None

    @classmethod
    def get_or_create_key(cls):
        if cls._encryption_key is None:
            key_from_env = Config.ENCRYPTION_KEY or os.environ.get("ENCRYPTION_KEY")
            if key_from_env:
                candidate = key_from_env.encode() if isinstance(key_from_env, str) else key_from_env
                try:
                    Fernet(candidate)
                    cls._encryption_key = candidate
                except Exception:
                    if Config.is_production():
                        raise RuntimeError("Invalid ENCRYPTION_KEY")
                    cls._encryption_key = Fernet.generate_key()
            else:
                if Config.is_production():
                    raise RuntimeError("ENCRYPTION_KEY is required")
                cls._encryption_key = Fernet.generate_key()
        return cls._encryption_key

    @classmethod
    def encrypt(cls, data: str) -> str:
        if not data:
            return data
        key = cls.get_or_create_key()
        f = Fernet(key)
        encrypted = f.encrypt(data.encode())
        return base64.urlsafe_b64encode(encrypted).decode()

    @classmethod
    def decrypt(cls, encrypted_data: str) -> str:
        """Decrypt data encrypted with ``encrypt()``.

        If the input does not look like a Fernet token (i.e. does not
        start with ``gAAAAA``) and does not base64-decode to one, it is
        returned as-is — this handles legacy plaintext values that predate
        encryption.

        If decryption fails on a Fernet-shaped input, ``None`` is
        returned instead of the raw ciphertext, so callers can detect
        and handle the error rather than silently propagating garbage.
        """
        if not encrypted_data:
            return encrypted_data
        candidate = encrypted_data
        # `encrypt()` double-encodes (Fernet output is itself base64url,
        # then wrapped in another base64 layer). Unwrap when needed.
        if not candidate.startswith("gAAAAA"):
            try:
                unwrapped = base64.urlsafe_b64decode(candidate.encode()).decode("ascii")
                if unwrapped.startswith("gAAAAA"):
                    candidate = unwrapped
            except Exception:
                # Not valid base64 — treat as plaintext legacy value.
                return encrypted_data
        if not candidate.startswith("gAAAAA"):
            return encrypted_data
        try:
            key = cls.get_or_create_key()
            f = Fernet(key)
            decrypted = f.decrypt(candidate.encode())
            return decrypted.decode()
        except Exception as exc:
            import logging

            logging.getLogger(__name__).warning("Security.decrypt failed: %s", exc)
            return None

    @staticmethod
    def hash_password(password: str) -> str:
        salt = os.urandom(16)
        iterations = 600000  # OWASP 2024 recommendation for PBKDF2-HMAC-SHA256
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=iterations,
        )
        key = base64.urlsafe_b64encode(kdf.derive(password.encode()))
        # New 3-part format: <iterations>$<b64_salt>$<b64_hash>
        # Embedding the iteration count lets verify_password read it
        # back, enabling transparent iteration bumps in the future.
        return f"{iterations}${base64.urlsafe_b64encode(salt).decode()}${key.decode()}"

    @staticmethod
    def verify_password(password: str, hashed: str) -> bool:
        try:
            parts = hashed.split("$")
            if len(parts) == 3:
                # New format: <iterations>$<b64_salt>$<b64_hash>
                iterations = int(parts[0])
                salt = base64.urlsafe_b64decode(parts[1].encode())
                stored_key = parts[2]
            elif len(parts) == 2:
                # Legacy format: <b64_salt>$<b64_hash> (100k iterations)
                iterations = 100000
                salt = base64.urlsafe_b64decode(parts[0].encode())
                stored_key = parts[1]
            else:
                return False
            kdf = PBKDF2HMAC(
                algorithm=hashes.SHA256(),
                length=32,
                salt=salt,
                iterations=iterations,
            )
            key = base64.urlsafe_b64encode(kdf.derive(password.encode())).decode()
            return secrets.compare_digest(key, stored_key)
        except Exception:
            return False

    @staticmethod
    def is_legacy_password_hash(hashed: str) -> bool:
        """Return ``True`` if ``hashed`` predates the 600k-iteration
        3-part format and should be transparently re-hashed on login.

        P2.9: the legacy 2-part ``<b64_salt>$<b64_hash>`` format uses
        only 100k iterations, well below the OWASP 2024 recommendation
        of 600k for PBKDF2-HMAC-SHA256. Successful login is the only
        time we have the plaintext password in hand, so that's the
        natural moment to silently upgrade the stored hash to the
        current format. Callers should re-hash with
        :meth:`hash_password` and ``UPDATE`` the user / admin row in
        the same request — never log the plaintext or the hashes.
        """
        if not hashed:
            return False
        # 3-part format = current (iterations embedded). 2-part =
        # legacy 100k. Anything else is malformed and should be left
        # alone (verify_password already returned False for it).
        return hashed.count("$") == 1

    @staticmethod
    def generate_api_key() -> str:
        return secrets.token_urlsafe(32)

    @staticmethod
    def generate_session_id() -> str:
        return secrets.token_urlsafe(24)

    @staticmethod
    def generate_csrf_token() -> str:
        return secrets.token_urlsafe(32)

    @staticmethod
    def assert_strong_password(password: str, username: str | None = None) -> None:
        if not password or len(password) < 12:
            raise ValueError("密码长度至少 12 位")

        if len(password) > 128:
            raise ValueError("密码长度不能超过 128 位")

        if username and username.strip():
            lowered = password.lower()
            if username.strip().lower() in lowered:
                raise ValueError("密码不能包含用户名")

        classes = 0
        if re.search(r"[a-z]", password):
            classes += 1
        if re.search(r"[A-Z]", password):
            classes += 1
        if re.search(r"[0-9]", password):
            classes += 1
        if re.search(r"[^A-Za-z0-9]", password):
            classes += 1

        if classes < 3:
            raise ValueError("密码需包含大小写字母、数字、符号中的至少三类")

        weak = {"password", "admin", "admin123", "12345678", "qwerty", "iloveyou"}
        if password.lower() in weak:
            raise ValueError("密码过于简单")
