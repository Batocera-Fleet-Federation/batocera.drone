import socket
import sys
import unittest
from unittest import mock

from app.transfer import local_network


_SO_BINDTODEVICE = 25  # Linux value; patched in so the test also runs off Linux


def _ifreq_with_address(address: str) -> bytes:
    # struct ifreq as returned by SIOCGIFADDR: 16-byte name, then sockaddr_in
    # whose IPv4 address sits at offset 20.
    return bytes(20) + socket.inet_aton(address) + bytes(236)


class _FakeSocket:
    def __init__(self) -> None:
        self.sockopts: list[tuple[int, int, object]] = []
        self.sent: list[tuple[bytes, tuple]] = []

    def setsockopt(self, level: int, option: int, value: object) -> None:
        self.sockopts.append((level, option, value))

    def sendto(self, data: bytes, target: tuple) -> int:
        self.sent.append((data, target))
        return len(data)

    def close(self) -> None:
        pass


class LanInterfaceDiscoveryTests(unittest.TestCase):
    def test_lan_interfaces_skip_tunnel_devices_and_loopback(self) -> None:
        names = [(1, "lo"), (2, "tun0"), (3, "tailscale0"), (4, "wlan0"), (5, "eth1")]
        addresses = {"wlan0": "192.168.0.189", "tun0": "10.96.0.51", "tailscale0": "100.91.1.2"}

        def fake_ioctl(_fd: int, _request: int, buf: bytes) -> bytes:
            name = buf[:16].rstrip(b"\0").decode()
            if name not in addresses:
                raise OSError("no IPv4 address")
            return _ifreq_with_address(addresses[name])

        with mock.patch.object(sys, "platform", "linux"), \
                mock.patch.object(socket, "if_nameindex", return_value=names), \
                mock.patch("fcntl.ioctl", side_effect=fake_ioctl):
            found = local_network._lan_ipv4_interfaces()

        self.assertEqual(found, [("wlan0", "192.168.0.189")])

    def test_lan_interfaces_empty_off_linux(self) -> None:
        with mock.patch.object(sys, "platform", "darwin"):
            self.assertEqual(local_network._lan_ipv4_interfaces(), [])

    def test_join_uses_only_lan_addresses(self) -> None:
        sock = _FakeSocket()
        with mock.patch.object(local_network, "_lan_ipv4_interfaces", return_value=[("wlan0", "192.168.0.189")]):
            local_network._join_multicast_group(sock)

        memberships = [value for _, option, value in sock.sockopts if option == socket.IP_ADD_MEMBERSHIP]
        self.assertEqual(
            memberships,
            [socket.inet_aton(local_network.DISCOVERY_GROUP) + socket.inet_aton("192.168.0.189")],
        )

    def test_announce_pins_multicast_and_broadcast_to_lan_interface(self) -> None:
        fake = _FakeSocket()
        settings = mock.Mock()
        lan = [("wlan0", "192.168.0.189")]
        with mock.patch.object(local_network, "is_local_mode", return_value=True), \
                mock.patch.object(local_network, "discovery_payload", return_value={"drone_id": "d1"}), \
                mock.patch.object(local_network, "_lan_ipv4_interfaces", return_value=lan), \
                mock.patch.object(socket, "SO_BINDTODEVICE", _SO_BINDTODEVICE, create=True), \
                mock.patch.object(local_network.socket, "socket", return_value=fake):
            self.assertTrue(local_network.announce(settings, "ab" * 32))

        multicast_if = [value for _, option, value in fake.sockopts if option == socket.IP_MULTICAST_IF]
        self.assertEqual(multicast_if, [socket.inet_aton("192.168.0.189")])
        group_sends = [target for _, target in fake.sent if target[0] == local_network.DISCOVERY_GROUP]
        self.assertEqual(group_sends, [(local_network.DISCOVERY_GROUP, local_network.DISCOVERY_PORT)])
        # Broadcast is bound to the LAN device, never sent through the default route.
        bound = [value for _, option, value in fake.sockopts if option == _SO_BINDTODEVICE]
        self.assertEqual(bound, [b"wlan0"])
        broadcast_sends = [target for _, target in fake.sent if target[0] != local_network.DISCOVERY_GROUP]
        self.assertTrue(broadcast_sends)


if __name__ == "__main__":
    unittest.main()
