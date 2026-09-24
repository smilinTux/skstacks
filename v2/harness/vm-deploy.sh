#!/usr/bin/env bash
# skstacks deploy harness — FULL closed loop on a fresh disposable VM (QEMU/KVM via
# libvirt). Provisions an Ubuntu cloud VM with cloud-init (installs k3s), deploys our
# skrender'd workload onto that REAL node, verifies it serves live HTTP, then destroys
# the VM. This is the bare-VM-node path (vs k3d-in-docker / host swarm).
#
# Needs: sudo (libvirt), virt-install, qemu-img, genisoimage, an Ubuntu cloud image.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
V2="${1:-$(cd "$HERE/.." && pwd)}"
PY="${PYTHON:-python3}"

VM="skstacks-vm"
WORK="$HOME/.cache/skstacks-harness"     # user-writable: keys + cloud-init files
LVDIR="/var/lib/libvirt/images"          # libvirt-accessible: base + disk + seed
IMG_URL="https://cloud-images.ubuntu.com/releases/24.04/release/ubuntu-24.04-server-cloudimg-amd64.img"
BASE="$LVDIR/skstacks-ubuntu-24.04-base.img"
DISK="$LVDIR/${VM}.qcow2"
SEED="$LVDIR/${VM}-seed.iso"
KEY="$WORK/id_harness"
RAM_MB=2560; VCPU=2

log(){ printf '\033[1;36m[vm]\033[0m %s\n' "$*"; }
ok(){  printf '  \033[1;32m✓\033[0m %s\n'  "$*"; }
bad(){ printf '  \033[1;31m✗\033[0m %s\n'  "$*"; FAILED=1; }
FAILED=0

cleanup(){ log "destroying VM…"; sudo virsh destroy "$VM" >/dev/null 2>&1 || true
           sudo virsh undefine "$VM" --remove-all-storage >/dev/null 2>&1 || true; }
trap cleanup EXIT

mkdir -p "$WORK"
log "1) base image + ssh key"
sudo test -f "$BASE" || { log "downloading Ubuntu 24.04 cloud image (~600MB, one-time)…"; sudo wget -q -O "$BASE" "$IMG_URL"; }
ok "base image present ($(sudo du -h "$BASE" | cut -f1))"
[ -f "$KEY" ] || ssh-keygen -t ed25519 -N "" -f "$KEY" -q
PUB="$(cat "$KEY.pub")"

log "2) overlay disk + cloud-init seed (installs k3s)"
sudo virsh destroy "$VM" >/dev/null 2>&1 || true; sudo virsh undefine "$VM" --remove-all-storage >/dev/null 2>&1 || true
sudo qemu-img create -f qcow2 -F qcow2 -b "$BASE" "$DISK" 12G >/dev/null
cat > "$WORK/user-data" <<EOF
#cloud-config
hostname: $VM
users:
  - name: ubuntu
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/bash
    ssh_authorized_keys: [ "$PUB" ]
runcmd:
  - [ bash, -c, "curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC='--write-kubeconfig-mode 644' sh -" ]
  - [ bash, -c, "touch /tmp/k3s-ready" ]
EOF
echo "instance-id: $VM" > "$WORK/meta-data"
genisoimage -quiet -output "$WORK/seed.iso" -volid cidata -joliet -rock "$WORK/user-data" "$WORK/meta-data"
sudo cp "$WORK/seed.iso" "$SEED"
sudo chown libvirt-qemu:kvm "$DISK" "$SEED" 2>/dev/null || true
ok "seed built"

log "3) virt-install the VM (KVM, ${RAM_MB}MB, ${VCPU} vCPU)"
if sudo virt-install --name "$VM" --memory "$RAM_MB" --vcpus "$VCPU" --cpu host-passthrough \
  --disk "path=$DISK,format=qcow2,bus=virtio" --disk "path=$SEED,device=cdrom" \
  --osinfo detect=on,require=off --network network=default,model=virtio \
  --graphics none --import --noautoconsole 2>/tmp/vi-err; then
  ok "VM defined + booting"
else
  bad "virt-install failed:"; sed 's/^/      /' /tmp/vi-err; exit 1
fi

log "4) wait for the VM IP (DHCP lease)…"
IP=""
for i in $(seq 1 60); do
  IP="$(sudo virsh domifaddr "$VM" --source lease 2>/dev/null | awk '/ipv4/{print $4}' | cut -d/ -f1 | head -1)"
  [ -n "$IP" ] && break; sleep 5
done
[ -n "$IP" ] && ok "VM IP = $IP" || { bad "no IP after 5min"; exit 1; }

SSH="ssh -i $KEY -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=5 ubuntu@$IP"
log "5) wait for SSH + k3s ready…"
for i in $(seq 1 60); do $SSH true >/dev/null 2>&1 && break; sleep 5; done
$SSH true >/dev/null 2>&1 && ok "SSH up" || { bad "SSH never came up"; exit 1; }
for i in $(seq 1 60); do $SSH "test -f /tmp/k3s-ready && sudo k3s kubectl get node | grep -q Ready" >/dev/null 2>&1 && break; sleep 5; done
$SSH "sudo k3s kubectl get node 2>/dev/null | grep -q Ready" && ok "k3s node Ready" || { bad "k3s not ready"; exit 1; }

log "6) deploy skwhoami onto the VM node (no ESO — secret pre-created)"
# render workload only (drop the ExternalSecret); the secret is created directly here
PYTHONPATH="$V2" "$PY" -m skrender.cli "$V2/harness/fixtures/skwhoami" --platform k8s \
  | "$PY" -c "import sys,yaml; docs=[d for d in yaml.safe_load_all(sys.stdin) if d and d['kind']!='ExternalSecret']; print(yaml.safe_dump_all(docs))" > /tmp/vm-skwhoami.yaml
cat /tmp/vm-skwhoami.yaml | $SSH "cat > /tmp/wl.yaml"
$SSH "sudo k3s kubectl create namespace skwhoami --dry-run=client -o yaml | sudo k3s kubectl apply -f - ;
      sudo k3s kubectl create secret generic skwhoami-secrets -n skwhoami --from-literal=DUMMY_TOKEN=test-token-123 --dry-run=client -o yaml | sudo k3s kubectl apply -f - ;
      sudo k3s kubectl apply -f /tmp/wl.yaml" >/dev/null 2>&1 && ok "applied workload + secret"

log "7) VALIDATE THE SERVICE WORKS (on the VM node)"
$SSH "sudo k3s kubectl rollout status deploy/skwhoami -n skwhoami --timeout=120s" >/dev/null 2>&1 \
  && ok "deployment Ready on the VM" || bad "rollout failed on VM"
# curl the pod IP directly from the VM host (k3s node reaches pod net — no image pull / DNS)
podip="$($SSH "sudo k3s kubectl get pod -n skwhoami -l app=skwhoami -o jsonpath='{.items[0].status.podIP}'" 2>/dev/null)"
body="$($SSH "curl -s -m 8 http://${podip}/" 2>/dev/null)"
echo "$body" | grep -q "Hostname:" && ok "HTTP 200 on VM node (pod $podip) — whoami: $(echo "$body" | grep Hostname: | tr -d '\r')" \
                                   || bad "no valid HTTP from the VM service (podip=$podip)"

echo
[ "$FAILED" -eq 0 ] && log "✅ VM DEPLOY LOOP PASSED — provisioned a fresh node, deployed, verified, destroying." \
                     || { log "❌ VM loop had failures."; exit 1; }
