"""Phase 0 transport-abstraction tests.

These assert the new transport seam is byte-identical to the previous hard-wired
download dispatch: the selector returns the single DirectPublic transport, and
``_directpublic_fetch`` calls each ``_download_*_from_peer`` helper with exactly
the arguments ``DownloadManager._run_job`` used before the refactor.
"""

import unittest
from unittest import mock

import app.drone_api as drone_api
from app.transport import (
    DirectPublicTransport,
    DownloadRequest,
    TransferContext,
    TransportSelector,
)
from app.transport.base import PeerTransport


def _ctx(**overrides) -> TransferContext:
    base = dict(
        settings=mock.sentinel.settings,
        repository=mock.sentinel.repository,
        config={"overmind_url": "https://o", "overmind_token": "t"},
        peer={"drone_id": "peer-1", "public_reachable_url": "https://peer"},
        progress_callback=mock.sentinel.progress,
        cancellation_event=mock.sentinel.cancel,
    )
    base.update(overrides)
    return TransferContext(**base)


class TransportSelectorTests(unittest.TestCase):
    def test_requires_at_least_one_transport(self):
        with self.assertRaises(ValueError):
            TransportSelector([])

    def test_returns_only_transport(self):
        transport = DirectPublicTransport(lambda req, ctx: {"status": "completed"})
        selector = TransportSelector([transport])
        chosen = selector.select(DownloadRequest(asset_type="rom"), _ctx())
        self.assertIs(chosen, transport)
        self.assertEqual([t.name for t in selector.transports], ["direct-public"])

    def test_picks_first_usable_then_falls_back_to_first(self):
        class Never(PeerTransport):
            name = "never"

            def usable(self, request, context):
                return False

            def fetch(self, request, context):
                return {}

        class Always(PeerTransport):
            name = "always"

            def fetch(self, request, context):
                return {}

        never, always = Never(), Always()
        req = DownloadRequest(asset_type="rom")
        # First usable transport wins even when an unusable one precedes it.
        self.assertIs(TransportSelector([never, always]).select(req, _ctx()), always)
        # When none are usable, fall back to the highest-priority transport so
        # the underlying helper can raise its own descriptive error.
        self.assertIs(TransportSelector([never]).select(req, _ctx()), never)


class DirectPublicTransportTests(unittest.TestCase):
    def test_fetch_delegates_to_injected_fn(self):
        captured = {}

        def fake(req, ctx):
            captured["req"], captured["ctx"] = req, ctx
            return {"status": "completed", "ok": True}

        transport = DirectPublicTransport(fake)
        req = DownloadRequest(asset_type="rom", system="snes", relative_path="g.sfc")
        ctx = _ctx()
        result = transport.fetch(req, ctx)
        self.assertEqual(result, {"status": "completed", "ok": True})
        self.assertIs(captured["req"], req)
        self.assertIs(captured["ctx"], ctx)
        self.assertEqual(transport.name, "direct-public")
        self.assertTrue(transport.usable(req, ctx))


class DirectPublicDispatchTests(unittest.TestCase):
    def test_rom_file(self):
        with mock.patch.object(drone_api, "_download_rom_from_peer", return_value={"status": "completed"}) as m:
            req = DownloadRequest(
                asset_type="rom", system="snes", relative_path="g.sfc",
                expected_size=10, expected_fingerprint="fp", entry_type="file",
            )
            ctx = _ctx()
            out = drone_api._directpublic_fetch(req, ctx)
        self.assertEqual(out, {"status": "completed"})
        m.assert_called_once_with(
            ctx.settings, ctx.config, ctx.peer, "snes", "g.sfc",
            expected_size=10, expected_fingerprint="fp",
            progress_callback=ctx.progress_callback, cancellation_event=ctx.cancellation_event,
        )

    def test_rom_folder(self):
        with mock.patch.object(drone_api, "_download_rom_folder_from_peer", return_value={"s": 1}) as m:
            req = DownloadRequest(
                asset_type="rom", system="ps2", relative_path="game",
                expected_size=99, entry_type="folder",
            )
            ctx = _ctx()
            drone_api._directpublic_fetch(req, ctx)
        m.assert_called_once_with(
            ctx.settings, ctx.config, ctx.peer, "ps2", "game",
            expected_size=99, progress_callback=ctx.progress_callback,
            cancellation_event=ctx.cancellation_event,
        )

    def test_bios(self):
        with mock.patch.object(drone_api, "_download_bios_from_peer", return_value={}) as m:
            req = DownloadRequest(
                asset_type="bios", relative_path="scph.bin",
                expected_size=512, expected_fingerprint="md5x",
            )
            ctx = _ctx()
            drone_api._directpublic_fetch(req, ctx)
        m.assert_called_once_with(
            ctx.settings, ctx.config, ctx.peer, "scph.bin",
            expected_size=512, expected_md5="md5x",
            progress_callback=ctx.progress_callback, cancellation_event=ctx.cancellation_event,
        )

    def test_saves(self):
        with mock.patch.object(drone_api, "_download_save_from_peer", return_value={}) as m:
            req = DownloadRequest(
                asset_type="saves", system="snes", relative_path="g.srm",
                expected_size=8, expected_fingerprint="fp",
            )
            ctx = _ctx()
            drone_api._directpublic_fetch(req, ctx)
        m.assert_called_once_with(
            ctx.settings, ctx.config, ctx.peer, "snes", "g.srm",
            expected_size=8, expected_fingerprint="fp",
            cancellation_event=ctx.cancellation_event,
        )

    def test_artwork(self):
        with mock.patch.object(drone_api, "_download_artwork_from_peer", return_value={}) as m:
            req = DownloadRequest(
                asset_type="artwork", system="snes", rom_path="g.sfc",
                artwork_type="boxart", overwrite=True, local_rom_path="/roms/snes/g.sfc",
            )
            ctx = _ctx()
            drone_api._directpublic_fetch(req, ctx)
        m.assert_called_once_with(
            ctx.settings, ctx.repository, ctx.config, ctx.peer, "snes", "g.sfc", "boxart",
            progress_callback=ctx.progress_callback, cancellation_event=ctx.cancellation_event,
            overwrite=True, local_rom_path="/roms/snes/g.sfc",
        )


class EdgeTokenTests(unittest.TestCase):
    """_edge_token_for must read the live (post-claim) token, falling back to env."""

    def _settings(self, token_env=None):
        env = {"OVERMIND_DEVICE_ID": "dev1"}
        if token_env is not None:
            env["OVERMIND_DRONE_TOKEN"] = token_env
        with mock.patch.dict("os.environ", env, clear=True):
            return drone_api.Settings.from_env()

    def test_prefers_live_config_token(self):
        settings = self._settings(token_env="env-token")
        with mock.patch.object(
            drone_api, "overmind_load_config", return_value={"overmind_token": "live-token"}
        ):
            self.assertEqual(drone_api._edge_token_for(settings), "live-token")

    def test_falls_back_to_env_token_when_config_empty(self):
        settings = self._settings(token_env="env-token")
        with mock.patch.object(drone_api, "overmind_load_config", return_value={}):
            self.assertEqual(drone_api._edge_token_for(settings), "env-token")

    def test_empty_when_no_token_anywhere(self):
        settings = self._settings()
        with mock.patch.object(drone_api, "overmind_load_config", return_value={}):
            self.assertEqual(drone_api._edge_token_for(settings), "")

    def test_handles_config_error(self):
        settings = self._settings(token_env="env-token")
        with mock.patch.object(
            drone_api, "overmind_load_config", side_effect=RuntimeError("boom")
        ):
            self.assertEqual(drone_api._edge_token_for(settings), "env-token")


if __name__ == "__main__":
    unittest.main()
