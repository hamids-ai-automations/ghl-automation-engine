"""HMAC signature verification with explicit, configurable framing.

HighLevel signs webhook deliveries with an HMAC over the raw request body. The exact
header names and encoding vary between marketplace apps and API versions, and guessing
is how integrations end up accepting forged events. So the framing is configuration,
not a hard-coded assumption:

* ``header``          â€” where the signature arrives
* ``timestamp_header`` â€” where the timestamp arrives (``None`` means "signed body only")
* ``encoding``        â€” ``"hex"`` or ``"base64"``
* ``prefix``          â€” e.g. ``"sha256="``, or ``""`` when the header is bare
* ``payload_template`` â€” what gets signed: ``"{body}"``, ``"{timestamp}.{body}"``, etc.

:data:`DEFAULT_CONFIG` reflects the most common HighLevel layout. It is a documented
assumption, not a verified fact; :func:`verify` accepts several configs at once so a
deployment can be tolerant during rollout and then narrowed to the one that matches a
captured live payload. See ``docs/adr/ADR-002-webhook-signature-framing.md``.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
from dataclasses import dataclass

from .errors import WebhookSignatureError

_ALGORITHMS = {"sha256": hashlib.sha256, "sha1": hashlib.sha1}


@dataclass(frozen=True)
class SignatureConfig:
    """How to find and check a signature for one webhook source."""

    header: str = "x-ghl-signature"
    timestamp_header: str | None = "x-ghl-timestamp"
    algorithm: str = "sha256"
    encoding: str = "hex"
    prefix: str = ""
    payload_template: str = "{timestamp}.{body}"
    label: str = "default"

    def __post_init__(self) -> None:
        if self.algorithm not in _ALGORITHMS:
            raise ValueError(
                f"unsupported algorithm {self.algorithm!r}; "
                f"choose from {sorted(_ALGORITHMS)}"
            )
        if self.encoding not in {"hex", "base64"}:
            raise ValueError(
                f"unsupported encoding {self.encoding!r}; choose 'hex' or 'base64'"
            )
        if "{body}" not in self.payload_template:
            raise ValueError(
                f"payload_template must contain {{body}}; got {self.payload_template!r}"
            )
        needs_stamp = "{timestamp}" in self.payload_template
        if needs_stamp and not self.timestamp_header:
            raise ValueError(
                "payload_template uses {timestamp} but timestamp_header is None"
            )
        if self.timestamp_header and not needs_stamp:
            raise ValueError(
                f"timestamp_header {self.timestamp_header!r} is configured but "
                "payload_template does not sign it; the timestamp would be unauthenticated"
            )


DEFAULT_CONFIG = SignatureConfig()

#: A layout that signs the timestamp separately and base64-encodes the digest.
ALTERNATE_CONFIG = SignatureConfig(
    header="x-wh-signature",
    timestamp_header="x-wh-timestamp",
    encoding="base64",
    payload_template="{timestamp}.{body}",
    label="alt-timestamped-b64",
)


def _canonical_digest(secret: str, payload: bytes, algorithm: str) -> bytes:
    return hmac.new(secret.encode("utf-8"), payload, _ALGORITHMS[algorithm]).digest()


def _decode(supplied: str, encoding: str) -> bytes | None:
    text = supplied.strip()
    if encoding == "hex":
        if len(text) % 2:
            return None
        try:
            return bytes.fromhex(text)
        except ValueError:
            return None
    try:
        return base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        return None


def _strip_prefix(supplied: str, prefix: str, algorithm: str) -> str:
    """Drop a ``sha256=`` style label.

    Senders commonly prefix the digest with the algorithm name even when the
    documentation does not say so. Stripping it is safe: the digest itself is still
    verified with :func:`hmac.compare_digest`, so a stripped label never weakens the
    check.
    """
    text = supplied.strip()
    for candidate in (prefix, f"{algorithm}="):
        if candidate and text.lower().startswith(candidate.lower()):
            text = text[len(candidate):].strip()
    return text


class SignatureVerifier:
    """Verifies signatures against one or more secrets.

    Several secrets are supported so a rotation can overlap: the new secret is added
    while the old one still verifies, then the old one is removed.
    """

    def __init__(
        self,
        secrets: list[str] | tuple[str, ...],
        *,
        configs: tuple[SignatureConfig, ...] = (DEFAULT_CONFIG,),
    ) -> None:
        cleaned = [s for s in secrets if s]
        if not cleaned:
            # Fail closed at construction: a receiver with no secret must never run.
            raise ValueError(
                "at least one signing secret is required; refusing to build a "
                "verifier that cannot verify"
            )
        if not configs:
            raise ValueError("at least one SignatureConfig is required")
        self._secrets = tuple(cleaned)
        self._configs = tuple(configs)

    @property
    def configs(self) -> tuple[SignatureConfig, ...]:
        return self._configs

    def looks_signed(self, headers: dict[str, str]) -> bool:
        """True when any configured signature header is present."""
        lowered = {k.lower() for k in headers}
        return any(c.header.lower() in lowered for c in self._configs)

    def verify(
        self,
        body: bytes,
        headers: dict[str, str],
        *,
        timestamp: str | None = None,
    ) -> SignatureConfig:
        """Return the matching config, or raise :class:`WebhookSignatureError`.

        ``headers`` keys are matched case-insensitively. The comparison uses
        :func:`hmac.compare_digest`, so it does not leak timing information.
        """
        lowered = {k.lower(): v for k, v in headers.items()}
        attempts: list[str] = []

        for config in self._configs:
            supplied = lowered.get(config.header.lower())
            if not supplied:
                attempts.append(f"{config.label}: header {config.header} absent")
                continue

            stamp = timestamp
            if config.timestamp_header:
                stamp = stamp or lowered.get(config.timestamp_header.lower())
            if config.timestamp_header and stamp is None:
                attempts.append(
                    f"{config.label}: header {config.timestamp_header} absent"
                )
                continue

            try:
                payload = config.payload_template.format(
                    body=body.decode("utf-8"), timestamp=stamp or ""
                ).encode("utf-8")
            except UnicodeDecodeError:
                attempts.append(f"{config.label}: body is not valid UTF-8")
                continue

            candidate = _strip_prefix(supplied, config.prefix, config.algorithm)
            decoded = _decode(candidate, config.encoding)
            if decoded is None:
                attempts.append(
                    f"{config.label}: signature is not valid {config.encoding}"
                )
                continue

            for secret in self._secrets:
                expected = _canonical_digest(secret, payload, config.algorithm)
                if hmac.compare_digest(expected, decoded):
                    return config
            attempts.append(f"{config.label}: digest mismatch")

        raise WebhookSignatureError(
            "webhook signature did not verify",
            attempts="; ".join(attempts) or "no signature header found",
            headers_seen=",".join(sorted(k.lower() for k in headers)),
        )
