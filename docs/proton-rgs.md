# Proton VPN port forwarding for RGS

Drone recognizes Proton OpenVPN profiles and appends Proton's `+pmp` flag to
the saved OpenVPN service username. This enables Proton's NAT-PMP port
forwarding for paid Proton VPN plans that use a port-forwarding-capable P2P
server. Credentials saved before this integration are updated the next time
Drone connects the profile. The VPN status response includes
`proton_port_forwarding: true` when the profile and saved username have the
required settings.

RGS's download service already requests and renews a P2P mapping, then
registers the resulting public IP and port with the RGS backend. Its normal
gateway discovery sees the OpenVPN tunnel's default gateway (`10.96.0.1`),
which does not provide Proton NAT-PMP. When `+pmp` is active, Drone patches
RGS's NAT-PMP helper to use Proton's NAT-PMP gateway (`10.2.0.1`). The helper
continues using ordinary gateway discovery when Proton forwarding is not
enabled. RGS then retains its existing mapping renewal and backend registration
behavior.

The patch is applied when Drone starts and before the RGS service starts. Drone
also adds a post-update hook to RGS's stable upgrade script so an RGS update
reapplies the patch after replacing the download-service files. Original files are backed up under
`/userdata/system/backups/hotfixes/rgs-proton-port-forwarding/`.

This integration requires a Proton OpenVPN profile with Proton port forwarding
available, an active VPN tunnel, and the RGS download-service files at their
standard Batocera paths. The public port is assigned by Proton and may change
when the VPN reconnects; RGS's existing mapping keepalive is responsible for
updating the portal registration.
