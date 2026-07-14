#!/bin/bash
export OPAQUE_HUMAN_ID=$(echo -n "$USER-$(date +%F)" | shasum -a 256 | head -c 16)
RUN_ID=$(uuidgen)
export WORKSPACE_ROOT="/tmp/fleet_workspaces/$RUN_ID"

HOOK_SHA=$(shasum -a 256 ~/.claude/hooks/pre_tool.sh | awk '{print $1}')
CONFIG_SHA=$(shasum -a 256 ~/.claude/settings.json | awk '{print $1}')

LEDGER_DIR="/var/log/fleet_audits/$RUN_ID"
sudo mkdir -p "$LEDGER_DIR"
sudo touch "$LEDGER_DIR/a2a_ledger.jsonl"
sudo chown _fleet_maker "$LEDGER_DIR/a2a_ledger.jsonl"
sudo chmod 600 "$LEDGER_DIR/a2a_ledger.jsonl"

sudo -u _fleet_maker bash -c "echo '{\"event\": \"RunStarted\", \"run_id\": \"$RUN_ID\", \"human_hmac\": \"$OPAQUE_HUMAN_ID\", \"hooks_sha\": \"$HOOK_SHA\", \"config_sha\": \"$CONFIG_SHA\"}' >> $LEDGER_DIR/a2a_ledger.jsonl"

sudo mkdir -p "$WORKSPACE_ROOT"
sudo chown -R _fleet_maker "$WORKSPACE_ROOT"

sudo -u _fleet_maker -i \
     env -i \
     PATH="/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin" \
     HOME="$WORKSPACE_ROOT" \
     USER="_fleet_maker" \
     LOGNAME="_fleet_maker" \
     WORKSPACE_ROOT="$WORKSPACE_ROOT" \
     HUMAN_UID="$OPAQUE_HUMAN_ID" \
     CLAUDE_API_KEY="$CLAUDE_API_KEY" \
     LEDGER_PATH="$LEDGER_DIR/a2a_ledger.jsonl" \
     claude
