"""Shared pairing fixture for the #279 adversarial suites: one real, parseable certificate.

The pairing save step parses every certificate as X.509 (OpenSSL), so a placeholder body such as
``AAAA`` is rejected as unreadable before any anchor or save-step check runs. Tests that need an
answer to reach the save step, or to match an operator-confirmed anchor, use this certificate and
its SHA-256 fingerprint. It is a throwaway self-signed certificate with no chain and no private
key in the repository; nothing trusts it beyond the test that pins it.
"""

from __future__ import annotations

REAL_CERT_PEM = """-----BEGIN CERTIFICATE-----
MIICHTCCAcQCCQCkin2iSozkPTAKBggqhkjOPQQDAjAcMRowGAYDVQQDDBFpc3N1
ZS01Nzgtb2ZmZXJlZDAgFw0yNjEwMjEyMDU3MjNaGA8yMTI2MDkyNzIwNTcyM1ow
HDEaMBgGA1UEAwwRaXNzdWUtNTc4LW9mZmVyZWQwggFLMIIBAwYHKoZIzj0CATCB
9wIBATAsBgcqhkjOPQEBAiEA/////wAAAAEAAAAAAAAAAAAAAAD/////////////
//8wWwQg/////wAAAAEAAAAAAAAAAAAAAAD///////////////wEIFrGNdiqOpPn
s+u9VXaYhrxlHQawzFOw9jvOPD4n0mBLAxUAxJ02CIbnBJNqZnjhE50mt4GffpAE
QQRrF9Hy4SxCR/i85uVjpEDydwN9gS3rM6D0oTlF2JjClk/jQuL+Gn+bjufrSnwP
nhYrzjNXazFezsu2QGg3v1H1AiEA/////wAAAAD//////////7zm+q2nF56E87nK
wvxjJVECAQEDQgAE6yw+AAuaugVxT1Yy+3zEF/SLyW4FzDgKWjQDnYYBZQ4FRZGA
9xjeGvJex/KPMsrNM9GkUgSpjS7WncbKIZ3dfzAKBggqhkjOPQQDAgNHADBEAiBF
09SIUT99frHOshDpRwW139NxHP3b1evj9fjlIOrIBQIgTKRjXbbtPCjW/tzm+dgG
7F9wLxq6viAX86QUBbRiAL0=
-----END CERTIFICATE-----
"""

# SHA-256 of the certificate's DER encoding, as ``_certificate_pem_fingerprint`` reports it.
REAL_CERT_FINGERPRINT = "c2dd4cd635059e7fdb2a69b118fbcd20e7673109baadf90b0405753c33e7de61"

# A well-formed operator-confirmed anchor that does NOT match REAL_CERT_PEM (for mismatch cases).
OTHER_ANCHOR = "ee" * 32
