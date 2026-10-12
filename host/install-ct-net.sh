#!/bin/bash
# Run as root ON THE PROXMOX HOST, from the directory holding these files.
#
#   ./install-ct-net.sh [CTID] --nic <host NIC>        # A1580 cabled to a spare host NIC: new bridge
#   ./install-ct-net.sh [CTID] --bridge <bridge>       # A1580 reachable through an existing bridge
#   ./install-ct-net.sh [CTID] --bridge vmbr0 --vlan N # A1580 on VLAN N of an existing bridge
#
# Options (defaults):
#   CTID                 101
#   --nic IFACE          host NIC the A1580 is plugged into; a bridge is created on it (--bridge names it, default vmbr1)
#   --bridge NAME        bridge to attach the container NIC to (with --nic: the bridge to create)
#   --vlan N             VLAN tag for the container NIC
#   --ip CIDR            container address on the lab network (192.168.200.2/24; must not be the device's)
#   --device-ip ADDR     address pinged from inside the container at the end (192.168.200.18, vendor default)
#   --name IFNAME        interface name inside the container (eth1)
#   --index N            netN slot in the container config (1)
#
# Idempotent: an existing bridge or netN entry is reported, not duplicated. Nothing is sent to the
# device except ICMP echo from inside the container.
set -euo pipefail

CTID=101; NIC=""; BRIDGE=""; VLAN=""; IP="192.168.200.2/24"; DEVIP="192.168.200.18"; IFNAME="eth1"; IDX=1
while [ $# -gt 0 ]; do
  case "$1" in
    --nic) NIC="$2"; shift 2;;
    --bridge) BRIDGE="$2"; shift 2;;
    --vlan) VLAN="$2"; shift 2;;
    --ip) IP="$2"; shift 2;;
    --device-ip) DEVIP="$2"; shift 2;;
    --name) IFNAME="$2"; shift 2;;
    --index) IDX="$2"; shift 2;;
    -h|--help) sed -n '2,22p' "$0"; exit 0;;
    -*) echo "unknown option $1"; exit 2;;
    *) CTID="$1"; shift;;
  esac
done
CONF="/etc/pve/lxc/${CTID}.conf"
[ -f "$CONF" ] || { echo "no container config $CONF"; exit 1; }
[ -n "$NIC" ] || [ -n "$BRIDGE" ] || { echo "need --nic <host NIC> or --bridge <bridge>"; exit 2; }
[ "${IP%%/*}" != "$DEVIP" ] || { echo "--ip must differ from the device address $DEVIP"; exit 2; }

# ── bridge ──
if [ -n "$NIC" ]; then
  BRIDGE="${BRIDGE:-vmbr1}"
  ip link show "$NIC" >/dev/null 2>&1 || { echo "host NIC $NIC does not exist (ip -br link)"; exit 1; }
  if [ "$(cat "/sys/class/net/$NIC/carrier" 2>/dev/null)" != 1 ]; then
    echo "!!!! $NIC has no carrier: is the A1580 cabled to it and powered?"
  fi
  if [ -e "/sys/class/net/$NIC/master" ]; then
    MASTER="$(basename "$(readlink "/sys/class/net/$NIC/master")")"
    if [ "$MASTER" != "$BRIDGE" ]; then
      echo "$NIC is already a port of $MASTER; use --bridge $MASTER or free the NIC first"; exit 1
    fi
  fi
  if ip link show "$BRIDGE" >/dev/null 2>&1; then
    echo "+ bridge $BRIDGE already exists"
  else
    echo "+ adding bridge $BRIDGE on $NIC to /etc/network/interfaces (no host address: the host stays off the lab network)"
    cat >> /etc/network/interfaces <<IF

auto $BRIDGE
iface $BRIDGE inet manual
	bridge-ports $NIC
	bridge-stp off
	bridge-fd 0
#A1580 lab network, container $CTID only
IF
    if command -v ifreload >/dev/null 2>&1; then ifreload -a; else ifup "$BRIDGE"; fi
  fi
else
  ip link show "$BRIDGE" >/dev/null 2>&1 || { echo "bridge $BRIDGE does not exist"; exit 1; }
fi

# ── container NIC ──
NETSPEC="name=$IFNAME,bridge=$BRIDGE,ip=$IP,firewall=0"
[ -n "$VLAN" ] && NETSPEC="$NETSPEC,tag=$VLAN"
CURRENT="$(grep -E "^net${IDX}:" "$CONF" || true)"
if [ -n "$CURRENT" ]; then
  echo "+ $CONF already has: $CURRENT"
  if [[ "$CURRENT" == *"bridge=$BRIDGE"* && "$CURRENT" == *"ip=$IP"* ]]; then
    echo "  bridge and address match, left as is"
  else
    echo "  differs from the requested $NETSPEC; updating"
    pct set "$CTID" "-net${IDX}" "$NETSPEC"
  fi
else
  echo "+ pct set $CTID -net$IDX $NETSPEC"
  pct set "$CTID" "-net${IDX}" "$NETSPEC"
fi

# ── verify from inside the container ──
if pct status "$CTID" | grep -q running; then
  sleep 2
  echo "+ container $IFNAME:"
  pct exec "$CTID" -- ip -4 -br addr show "$IFNAME" || echo "  not up yet; pct reboot $CTID"
  echo "+ ping $DEVIP from the container:"
  if pct exec "$CTID" -- ping -c 3 -W 2 "$DEVIP"; then
    echo "+ the A1580 answers; inside the container run: A1580_HOST=$DEVIP tools/hw_probe.py --dry-run"
  else
    echo "!!!! no answer from $DEVIP. Check the cable and link LED, the device address (vendor default"
    echo "!!!! 192.168.200.18; the device's own web UI shows it), and that $BRIDGE sits on the right NIC/VLAN."
  fi
else
  echo "+ container not running; start it, then ping $DEVIP from inside"
fi
