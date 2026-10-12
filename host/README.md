# Host kit: reaching the A1580 from the LXC container

## Why
The A1580 is an Ethernet instrument (SCPI on TCP 5025, A-scan stream on TCP 2758). Out of the
box it sits at `192.168.200.18` on `192.168.200.0/24`, a network the container (one NIC on the
office LAN) cannot reach: the office gateway has no route to it. The sibling projects pass USB
device nodes into the container; the equivalent here is a second container NIC on the lab network.
Nothing on the device is changed, in line with the rule never to alter its network settings.

## Design
On the Proxmox host a bridge (`vmbr1` by default) is created on the NIC the A1580 is cabled to,
with no host address, so only the container is on the lab network. The container gets
`net1: name=eth1,bridge=vmbr1,ip=192.168.200.2/24` (static, no gateway, hot-plugged by `pct`).
If the A1580 instead hangs off a switch VLAN, the same NIC is tagged on an existing bridge
(`--bridge vmbr0 --vlan N`). Inside the container `/etc/profile.d/a1580.sh` exports
`A1580_HOST=192.168.200.18`, which `tools/hw_probe.py` and `examples/example_test.py` read.

## Files
- `install-ct-net.sh` - host installer (idempotent): bridge, `pct set -net1`, then a ping from
  inside the container. `--help` lists the options.

## Host steps (Proxmox host, as root)
```
scp -r root@<container-ip>:/opt/acs-a1580-openhtf/host /root/ct-net
cd /root/ct-net
ip -br link                                   # find the NIC the A1580 is cabled to (carrier UP, not in a bridge)
./install-ct-net.sh 101 --nic <that NIC>      # or: ./install-ct-net.sh 101 --bridge vmbr0 --vlan <N>
```
A running container picks the NIC up without a reboot. If `eth1` does not appear: `pct reboot 101`.

## Verify (inside the container)
```
ip -4 -br addr show eth1                      # 192.168.200.2/24
ping -c 3 192.168.200.18
A1580_HOST=192.168.200.18 .venv/bin/python tools/hw_probe.py --dry-run
```
Stop at the dry run. Anything beyond it opens a SCPI session and follows `HARDWARE-SESSION.md`:
the human confirms the preconditions first (what is connected, voltage ceiling, exclusive use;
a second SCPI client closes the first).

## Alternatives that need no kit

- A static route on the office gateway to `192.168.200.0/24`, if the A1580 hangs off a switch or
  router that the gateway can reach. Then every LAN machine, this container included, reaches the
  device and nothing changes on the Proxmox host.
- Moving the device onto the office LAN: a human gives it a free static address on the office
  subnet through its web UI from a laptop on the vendor subnet (never over the network from a
  tool, see `AGENTS.md`), then plugs it into the office switch. `PROTOCOL.md` leaves DHCP, netmask
  and gateway behaviour unknown, so a static address is the safe choice. Then only `A1580_HOST`
  changes.

The kit is only needed when the device is cabled straight into a NIC of the Proxmox host or sits
on a VLAN that the office gateway does not route.

## Notes
- The device address is the vendor default; if the unit was given another address, pass
  `--device-ip` to the installer and change `/etc/profile.d/a1580.sh`.
- Pick `--ip` outside anything else on that cable; only the device and the container are expected there.
- `192.168.200.18` is the vendor default and may appear in docs. Any other real site address stays
  out of git (`AGENTS.md`).
