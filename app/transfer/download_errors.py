"""Download/transfer error types.

Extracted from ``drone_api.py``. ``DownloadCancelled`` unwinds an in-flight asset
transfer when the active download is cancelled; the download manager, the direct-peer
download path, and the Edge relay path all raise/catch it.
"""


class DownloadCancelled(RuntimeError):
    pass
