#!/usr/bin/env bash
set -euo pipefail

NAMESPACE="totallylegitco"
SECRET_NAME="faxymcfaxface-ssh"
KEY_NAME="faxy-id_ed25519"
KEY_PATH="$HOME/.ssh/$KEY_NAME"
PUB_PATH="${KEY_PATH}.pub"
FAXY_HOST="faxymcfaxface"
FAXY_USER="ray"

echo "==> Generating SSH keypair (if missing)"
if [[ ! -f "$KEY_PATH" ]]; then
    ssh-keygen -t ed25519 -f "$KEY_PATH" -N "" -C "k3s-faxy"
    echo "Created new keypair at $KEY_PATH"
else
    echo "Keypair already exists, not overwriting."
fi

echo "==> Ensuring public key exists"
if [[ ! -f "$PUB_PATH" ]]; then
    echo "ERROR: Public key missing at $PUB_PATH"
    exit 1
fi

echo "==> Creating/Updating Kubernetes secret: $SECRET_NAME in $NAMESPACE"

# Delete existing secret if present (k8s has no built-in "replace" for registry secrets)
kubectl delete secret "$SECRET_NAME" -n "$NAMESPACE" --ignore-not-found

kubectl create secret generic "$SECRET_NAME" \
  -n "$NAMESPACE" \
  --type=kubernetes.io/ssh-auth \
  --from-file=ssh-privatekey="$KEY_PATH"

echo "==> Secret created."

echo "==> Installing public key on $FAXY_HOST for user $FAXY_USER"

# Copy pubkey to temp location on remote
scp "$PUB_PATH" "$FAXY_HOST:/tmp/faxy_tmp_key.pub"

# Append pubkey into authorized_keys for ray with sudo
ssh "$FAXY_HOST" "sudo -S mkdir -p /home/$FAXY_USER/.ssh && sudo -S touch /home/$FAXY_USER/.ssh/authorized_keys && sudo -S chmod 700 /home/$FAXY_USER/.ssh"
ssh "$FAXY_HOST" "sudo -S sh -c 'cat /tmp/faxy_tmp_key.pub >> /home/$FAXY_USER/.ssh/authorized_keys'"
ssh "$FAXY_HOST" "sudo -S chmod 600 /home/$FAXY_USER/.ssh/authorized_keys"
ssh "$FAXY_HOST" "sudo -S chown -R $FAXY_USER:$FAXY_USER /home/$FAXY_USER/.ssh"

# Cleanup temp
ssh "$FAXY_HOST" "rm -f /tmp/faxy_tmp_key.pub"

echo "==> Public key installed for user $FAXY_USER on $FAXY_HOST."
echo "==> Done!"
