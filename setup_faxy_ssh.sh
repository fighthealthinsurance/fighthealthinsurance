#!/usr/bin/env bash
set -euo pipefail

NAMESPACE="totallylegitco"
SECRET_NAME="faxymcfaxface-ssh"
KEY_NAME="faxy-id_ed25519"
KEY_PATH="$HOME/.ssh/$KEY_NAME"
PUB_PATH="${KEY_PATH}.pub"
FAXY_HOST="turo.local.pigscanfly.ca"
# Both fax identities need this key: the Ray fax actor connects as "ray"
# (secret mounted at /home/ray/.ssh, see k8s/ray/cluster.yaml) and the
# Temporal worker connects as "root" (mounted at /root/.ssh, see
# k8s/temporal/worker.yaml).
FAXY_USERS=("ray" "root")

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

echo "==> Installing public key on $FAXY_HOST for users: ${FAXY_USERS[*]}"

# Copy pubkey to temp location on remote
scp "$PUB_PATH" "$FAXY_HOST:/tmp/faxy_tmp_key.pub"

for FAXY_USER in "${FAXY_USERS[@]}"; do
    # Resolve the user's home remotely (root is /root, not /home/root).
    # shellcheck disable=SC2029
    FAXY_HOME=$(ssh "$FAXY_HOST" "getent passwd '$FAXY_USER' | cut -d: -f6")
    if [[ -z "$FAXY_HOME" ]]; then
        echo "ERROR: user $FAXY_USER does not exist on $FAXY_HOST (run the fax-setup playbook first?)"
        exit 1
    fi
    # shellcheck disable=SC2029
    ssh "$FAXY_HOST" "sudo -S mkdir -p \"$FAXY_HOME/.ssh\" && sudo -S touch \"$FAXY_HOME/.ssh/authorized_keys\" && sudo -S chmod 700 \"$FAXY_HOME/.ssh\""
    # Append the pubkey only if absent so re-runs stay idempotent
    # shellcheck disable=SC2029
    ssh "$FAXY_HOST" "sudo -S sh -c 'grep -qxF -f /tmp/faxy_tmp_key.pub \"$FAXY_HOME/.ssh/authorized_keys\" || cat /tmp/faxy_tmp_key.pub >> \"$FAXY_HOME/.ssh/authorized_keys\"'"
    # shellcheck disable=SC2029
    ssh "$FAXY_HOST" "sudo -S chmod 600 \"$FAXY_HOME/.ssh/authorized_keys\""
    # shellcheck disable=SC2029
    ssh "$FAXY_HOST" "sudo -S chown -R \"$FAXY_USER:$FAXY_USER\" \"$FAXY_HOME/.ssh\""
    echo "==> Public key installed for user $FAXY_USER on $FAXY_HOST."
done

# Cleanup temp
ssh "$FAXY_HOST" "rm -f /tmp/faxy_tmp_key.pub"

echo "==> Done!"
