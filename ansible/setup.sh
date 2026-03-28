#!/bin/bash
# Pull shared Ansible roles from the local bare repo.
# Run this before ansible-playbook if shared roles are missing or need updating.
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SHARED_DIR="$SCRIPT_DIR/roles/shared"
REPO="file:///path/to/ansible-roles.git"

if [ -d "$SHARED_DIR/.git" ]; then
    echo "Updating shared roles..."
    git -C "$SHARED_DIR" pull --ff-only
else
    echo "Cloning shared roles..."
    git clone "$REPO" "$SHARED_DIR"
fi

echo "Shared roles ready."
