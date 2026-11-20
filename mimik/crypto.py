"""Capture-store encryption primitives (v0.8, audit item G1).

The design Joshua decided 2026-11-13 ("most secure way possible"):

- The key is derived from a passphrase with Argon2id (the OWASP
  top-recommended password KDF), with a random per-store salt.
- The key lives in memory only, never on disk. MIMIK asks for the
  passphrase each time it starts and forgets it when it exits.
- Captures are sealed with AES-256-GCM (authenticated encryption):
  a tampered or truncated file fails to open instead of parsing
  into garbage.
- A forgotten passphrase means the captures are unrecoverable.
  That is the point of the design, not a bug in it.

This module is only the primitives + the marker/envelope formats.
mimik.store owns the files and the unlock state. The two crypto
packages (cryptography, argon2-cffi) are core dependencies; if
they are somehow missing, available() is False and every entry
point refuses in plain words rather than half-encrypting.
"""
import base64
import json
import os

ENVELOPE_KIND = "mimik-enc-v1"
MARKER_KIND = "mimik-store-encryption"
_VERIFIER_PLAINTEXT = b"mimik store verifier v1"

# Argon2id profile: OWASP's recommended parameters for interactive
# use, sized for an 8 GB PC (64 MiB is nothing there, painful for a
# cracking rig doing millions of guesses).
TIME_COST = 3
MEMORY_KIB = 64 * 1024
PARALLELISM = 4
KEY_LEN = 32
SALT_LEN = 16
NONCE_LEN = 12


class CryptoError(Exception):
    """Encryption could not be done (missing packages, bad data)."""


def available():
    """True when both crypto packages import."""
    try:
        import argon2.low_level  # noqa: F401
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401
        return True
    except ImportError:
        return False


def _require():
    if not available():
        raise CryptoError(
            "capture encryption needs the 'cryptography' and "
            "'argon2-cffi' packages, which are not installed. "
            "Reinstall MIMIK to get them.")


def _b64(raw):
    return base64.b64encode(raw).decode("ascii")


def _unb64(text):
    return base64.b64decode(text.encode("ascii"))


def derive_key(passphrase, salt, time_cost=TIME_COST,
               memory_kib=MEMORY_KIB, parallelism=PARALLELISM):
    """Argon2id(passphrase, salt) -> 32-byte key."""
    _require()
    from argon2.low_level import Type, hash_secret_raw
    return hash_secret_raw(
        passphrase.encode("utf-8"), salt,
        time_cost=time_cost, memory_cost=memory_kib,
        parallelism=parallelism, hash_len=KEY_LEN, type=Type.ID)


def _seal(key, plaintext):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = os.urandom(NONCE_LEN)
    ct = AESGCM(key).encrypt(nonce, plaintext, None)
    return nonce, ct


def _open(key, nonce, ct):
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    try:
        return AESGCM(key).decrypt(nonce, ct, None)
    except InvalidTag:
        return None


def make_marker(passphrase):
    """(marker dict, key) for a NEW encrypted store: a random salt
    plus a sealed verifier the unlock check can test a passphrase
    against without touching a single capture."""
    _require()
    salt = os.urandom(SALT_LEN)
    key = derive_key(passphrase, salt)
    nonce, ct = _seal(key, _VERIFIER_PLAINTEXT)
    marker = {
        "version": 1,
        "kind": MARKER_KIND,
        "kdf": "argon2id",
        "time_cost": TIME_COST,
        "memory_kib": MEMORY_KIB,
        "parallelism": PARALLELISM,
        "salt": _b64(salt),
        "verifier_nonce": _b64(nonce),
        "verifier_ct": _b64(ct),
    }
    return marker, key


def check_marker(marker, passphrase):
    """The derived key if `passphrase` opens `marker`'s verifier,
    else None. A wrong passphrase and a damaged marker both read as
    None — callers say "that passphrase did not unlock" either way."""
    _require()
    try:
        salt = _unb64(marker["salt"])
        nonce = _unb64(marker["verifier_nonce"])
        ct = _unb64(marker["verifier_ct"])
        key = derive_key(
            passphrase, salt,
            time_cost=int(marker.get("time_cost", TIME_COST)),
            memory_kib=int(marker.get("memory_kib", MEMORY_KIB)),
            parallelism=int(marker.get("parallelism", PARALLELISM)))
    except (KeyError, ValueError, TypeError):
        return None
    if _open(key, nonce, ct) != _VERIFIER_PLAINTEXT:
        return None
    return key


def encrypt_json(key, obj):
    """Seal a JSON-able object into an envelope dict (itself JSON,
    so an encrypted capture file still ends in .json and carries no
    plaintext at all)."""
    _require()
    raw = json.dumps(obj).encode("utf-8")
    nonce, ct = _seal(key, raw)
    return {"enc": ENVELOPE_KIND, "nonce": _b64(nonce), "ct": _b64(ct)}


def decrypt_json(key, envelope):
    """Open an envelope dict back into the object. Raises
    CryptoError when the key is wrong or the data was tampered
    with — AES-GCM authentication failing is a hard stop, never a
    best-effort parse."""
    _require()
    try:
        nonce = _unb64(envelope["nonce"])
        ct = _unb64(envelope["ct"])
    except (KeyError, ValueError, TypeError) as e:
        raise CryptoError(f"damaged encrypted capture: {e}") from e
    raw = _open(key, nonce, ct)
    if raw is None:
        raise CryptoError(
            "this capture did not open with the current key "
            "(wrong passphrase or tampered file)")
    return json.loads(raw.decode("utf-8"))


def is_envelope(obj):
    """True when a parsed JSON value is an encryption envelope."""
    return (isinstance(obj, dict)
            and obj.get("enc") == ENVELOPE_KIND
            and "ct" in obj and "nonce" in obj)
