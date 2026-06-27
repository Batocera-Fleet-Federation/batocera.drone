"""Verify the Drone's outbound Overmind payloads still satisfy Overmind's contracts.

Overmind was recently updated to add response_model typing to every endpoint, but its
drone-facing *request* models (e.g. DroneHeartbeatRequest, DeviceRegister) were left unchanged
and its responses stayed byte-compatible. This test pins that: the payloads the Drone builds
must validate against the (mirrored) Overmind request models, and the response fields the Drone
reads must exist on the response models. If Overmind ever tightens a request contract, the
strict-model assertions here fail and tell us the Drone needs updating.
"""

import unittest

from pydantic import ValidationError

from app.overmind_contract import (
    BatoceraInfoContract,
    DeviceRegisterContract,
    DeviceRegisterResponseContract,
    DroneHeartbeatContract,
    HeartbeatResponseContract,
)


# The exact key set drone_api.py puts on a heartbeat (see the heartbeat loop, ~drone_api.py:12932).
HEARTBEAT_KEYS = {
    "device_id", "device_name", "network", "api_port", "scheme", "reachable_url",
    "certificate", "system_info", "downloads", "rom_inventory_fingerprint",
    "rom_inventory_fingerprint_algorithm", "romset_files_thumbprint",
    "bios_files_thumbprint", "saves_files_thumbprint",
}


def _representative_heartbeat() -> dict:
    return {
        "device_id": "drone-123",
        "device_name": "Living Room Cab",
        "network": {"public_ip": "8.8.8.8"},
        "api_port": 443,
        "scheme": "https",
        "reachable_url": "https://drone.example/",
        "certificate": {"fingerprint": "ab:cd"},
        "system_info": {"model": "Batocera"},
        "downloads": {"active": [], "queued": [], "recent": []},
        "rom_inventory_fingerprint": "fp-1",
        "rom_inventory_fingerprint_algorithm": "sample-fp-v1",
        "romset_files_thumbprint": "rt-1",
        "bios_files_thumbprint": "bt-1",
        "saves_files_thumbprint": "st-1",
    }


def _representative_register() -> dict:
    return {
        "device_id": "drone-123",
        "device_name": "Living Room Cab",
        "api_port": 443,
        "scheme": "https",
        "reachable_url": "https://drone.example/",
        "batocera_info": {
            "model": "Batocera Drone",
            "system": "linux",
            "architecture": "x86_64",
            "cpu_model": "unknown",
            "cpu_cores": 4,
            "cpu_threads": 4,
            "cpu_max_frequency": "unknown",
            "memory_available": "unknown",
            "memory_total": "unknown",
            "ip_address": "192.168.1.50",
            "network": {},
            "certificate": {"fingerprint": "ab:cd"},
            "system_info": {},
        },
        "email": "owner@example.com",
        "authorization_token": "tok-1",
    }


class OvermindContractCompatTest(unittest.TestCase):
    def test_heartbeat_payload_keys_are_all_allowed_by_strict_contract(self):
        allowed = set(DroneHeartbeatContract.model_fields)
        self.assertTrue(
            HEARTBEAT_KEYS.issubset(allowed),
            f"Drone sends keys Overmind's strict heartbeat would reject: {HEARTBEAT_KEYS - allowed}",
        )

    def test_representative_heartbeat_validates(self):
        # Strict model: this passing proves the Drone sends no extra keys.
        DroneHeartbeatContract.model_validate(_representative_heartbeat())

    def test_strict_heartbeat_rejects_unknown_key(self):
        # Guard: if the Drone ever adds an unmodelled key, Overmind (extra=forbid) would 422.
        with self.assertRaises(ValidationError):
            DroneHeartbeatContract.model_validate({**_representative_heartbeat(), "surprise": 1})

    def test_representative_register_validates(self):
        model = DeviceRegisterContract.model_validate(_representative_register())
        self.assertIsInstance(model.batocera_info, BatoceraInfoContract)

    def test_response_models_cover_fields_the_drone_reads(self):
        hb = set(HeartbeatResponseContract.model_fields)
        for field in ("actions", "swarm", "romset_files_thumbprint", "bios_files_thumbprint", "saves_files_thumbprint"):
            self.assertIn(field, hb)
        reg = set(DeviceRegisterResponseContract.model_fields)
        for field in ("message", "status", "device_id", "drone_token"):
            self.assertIn(field, reg)


if __name__ == "__main__":
    unittest.main()
