#!/usr/bin/env python3
"""Envelope v2 — one message, many devices (#347).

    python3 e2ee_v2.py --vectors     # deterministic test vectors, JSON

v1 sealed to ONE device: static-static X25519 between the agent key and that
device's key. A second device could only be served by sealing twice, and a
device added later could read nothing at all — which is the wall Vladimir keeps
photographing.

v2 is the standard multi-recipient shape and nothing cleverer:

    CEK        random 32 bytes, per message
    payload    AES-256-GCM(CEK, iv, plaintext, aad=header)
    per device AES-256-GCM(wrap_key, iv, CEK, aad=header)
    wrap_key   HKDF-SHA256(X25519(eph_priv, device_pub),
                           salt="voicebridge-e2ee-v2",
                           info="voicebridge/v2/" || dir || 0 || key_id || 0 || epk)

Three properties worth naming, because each is a decision rather than a detail:

  * THE SENDER KEY IS EPHEMERAL, one per message. v1 was static-static, so a
    device key recovered next year opened everything ever sent to it; here it
    opens nothing but the messages whose ephemeral half is already gone.
  * EVERY WRAP IS BOUND TO ITS RECIPIENT AND DIRECTION through the HKDF info,
    and the payload's AAD is the header — so a wrap cannot be lifted into
    another envelope, replayed at a different device, or re-mixed with wraps
    from a different message.
  * KEY_ID SELECTS, THE PUBLIC KEY DECIDES. The id is eight bytes of
    SHA-256(pubkey) and exists only so a reader can find its own wrap quickly;
    opening is what proves ownership, and a reader must never trust the id
    alone.
"""
import argparse
import base64
import hashlib
import json
import os
import sys

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey, X25519PublicKey)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

VERSION = 2
SALT = b"voicebridge-e2ee-v2"
DIR_TO_AGENT = "phone->agent"
DIR_TO_PHONE = "agent->phone"


def b64(raw):
    return base64.b64encode(raw).decode()


def unb64(s):
    return base64.b64decode(s)


def key_id(pub_raw):
    """Eight bytes of SHA-256(pubkey), hex. Selects a wrap; proves nothing."""
    return hashlib.sha256(pub_raw).hexdigest()[:16]


def _raw(pub):
    return pub.public_bytes(serialization.Encoding.Raw,
                            serialization.PublicFormat.Raw)


def _wrap_key(eph_priv, device_pub_raw, direction, epk_raw):
    shared = eph_priv.exchange(X25519PublicKey.from_public_bytes(device_pub_raw))
    info = (b"voicebridge/v2/" + direction.encode() + b"\x00"
            + key_id(device_pub_raw).encode() + b"\x00" + epk_raw)
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=SALT,
                info=info).derive(shared)


def _header(direction, epk_raw, key_ids):
    """What the AAD covers: version, direction, sender key, recipient set.

    Sorted, compact, and separators fixed — the header is authenticated, so
    both ends must serialise it identically or every open fails.
    """
    return json.dumps({"v": VERSION, "dir": direction, "epk": b64(epk_raw),
                       "key_ids": sorted(key_ids)},
                      separators=(",", ":"), sort_keys=True).encode()


def seal(plaintext, device_pubs, direction, *, eph_priv=None, cek=None,
         iv=None, wrap_ivs=None):
    """Seal once, wrap per device. The keyword arguments exist for vectors."""
    if not device_pubs:
        raise ValueError("no devices to seal to — refusing to send in clear")
    eph_priv = eph_priv or X25519PrivateKey.generate()
    epk_raw = _raw(eph_priv.public_key())
    cek = cek or os.urandom(32)
    iv = iv or os.urandom(12)
    ids = [key_id(p) for p in device_pubs]
    header = _header(direction, epk_raw, ids)
    body = AESGCM(cek).encrypt(iv, plaintext.encode()
                               if isinstance(plaintext, str) else plaintext,
                               header)
    wraps = []
    for n, pub_raw in enumerate(device_pubs):
        wiv = (wrap_ivs or [None] * len(device_pubs))[n] or os.urandom(12)
        wk = _wrap_key(eph_priv, pub_raw, direction, epk_raw)
        wraps.append({"key_id": key_id(pub_raw), "iv": b64(wiv),
                      "ct": b64(AESGCM(wk).encrypt(wiv, cek, header))})
    return {"v": VERSION, "dir": direction, "epk": b64(epk_raw),
            "key_ids": sorted(ids), "iv": b64(iv), "ct": b64(body),
            "wraps": wraps}


def open_envelope(env, device_priv, device_pub_raw):
    """Open with THIS device's key, or raise. The id finds the wrap; the key
    opens it — a wrong id simply means no candidate, and a right id with the
    wrong key still fails, which is the property that matters."""
    if int(env.get("v", 0)) != VERSION:
        raise ValueError(f"not a v{VERSION} envelope")
    direction, epk_raw = env["dir"], unb64(env["epk"])
    header = _header(direction, epk_raw, env["key_ids"])
    mine = key_id(device_pub_raw)
    wrap = next((w for w in env["wraps"] if w["key_id"] == mine), None)
    if not wrap:
        raise ValueError("no wrap for this device")
    shared = device_priv.exchange(X25519PublicKey.from_public_bytes(epk_raw))
    info = (b"voicebridge/v2/" + direction.encode() + b"\x00"
            + mine.encode() + b"\x00" + epk_raw)
    wk = HKDF(algorithm=hashes.SHA256(), length=32, salt=SALT,
              info=info).derive(shared)
    cek = AESGCM(wk).decrypt(unb64(wrap["iv"]), unb64(wrap["ct"]), header)
    return AESGCM(cek).decrypt(unb64(env["iv"]), unb64(env["ct"]),
                               header).decode()


def vectors():
    """Fixed keys and nonces, so a reader in another language can check bytes.

    Nothing here is secret and nothing here is ever used live: the private
    keys are literal constants precisely so the vectors are reproducible.
    """
    def priv(seed):
        return X25519PrivateKey.from_private_bytes(bytes([seed]) * 32)

    agent, phone, ipad, other = priv(1), priv(2), priv(3), priv(4)
    eph = priv(9)
    devices = [_raw(phone.public_key()), _raw(ipad.public_key())]
    env = seal("the drill template is on its way", devices, DIR_TO_PHONE,
               eph_priv=eph, cek=bytes([0xAB]) * 32, iv=bytes([0xCD]) * 12,
               wrap_ivs=[bytes([0x01]) * 12, bytes([0x02]) * 12])
    return {
        "note": "v2 envelope test vectors — private keys are constants on "
                "purpose; never used live",
        "private_keys": {"phone": b64(bytes([2]) * 32),
                         "ipad": b64(bytes([3]) * 32),
                         "stranger": b64(bytes([4]) * 32),
                         "ephemeral_sender": b64(bytes([9]) * 32)},
        "public_keys": {"phone": b64(devices[0]), "ipad": b64(devices[1]),
                        "stranger": b64(_raw(other.public_key()))},
        "key_ids": {"phone": key_id(devices[0]), "ipad": key_id(devices[1]),
                    "stranger": key_id(_raw(other.public_key()))},
        "plaintext": "the drill template is on its way",
        "envelope": env,
        "expectations": [
            "phone opens it", "ipad opens it",
            "stranger finds no wrap for its key_id",
            "phone's key against ipad's wrap fails the GCM tag",
            "any edit to v/dir/epk/key_ids fails the tag (header is the AAD)",
        ],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vectors", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.vectors:
        print(json.dumps(vectors(), indent=1))
        return 0
    if a.selftest:
        def priv(seed):
            return X25519PrivateKey.from_private_bytes(bytes([seed]) * 32)
        phone, ipad, stranger = priv(2), priv(3), priv(4)
        pubs = [_raw(phone.public_key()), _raw(ipad.public_key())]
        env = seal("hello devices", pubs, DIR_TO_PHONE)
        print("phone   :", open_envelope(env, phone, pubs[0]))
        print("ipad    :", open_envelope(env, ipad, pubs[1]))
        for label, fn in (
                ("stranger", lambda: open_envelope(
                    env, stranger, _raw(stranger.public_key()))),
                ("wrong key for a real id", lambda: open_envelope(
                    env, stranger, pubs[0])),
                ("tampered header", lambda: open_envelope(
                    {**env, "dir": DIR_TO_AGENT}, phone, pubs[0]))):
            try:
                fn()
                print(f"{label:24}: OPENED — THAT IS A BUG")
                return 1
            except Exception as e:
                print(f"{label:24}: refused ({type(e).__name__})")
        return 0
    sys.exit(__doc__.strip())


if __name__ == "__main__":
    sys.exit(main())
