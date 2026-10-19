#!/usr/bin/env bash
# End-to-end demo against a deployed environment.
# Needs: aws CLI (logged in to the same account), terraform, curl, jq.
# Run from the repo root after `terraform apply`:  ./scripts/demo.sh
set -euo pipefail

cd "$(dirname "$0")/../infra"
API=$(terraform output -raw api_url)
POOL=$(terraform output -raw user_pool_id)
CLIENT=$(terraform output -raw user_pool_client_id)
REGION=$(terraform output -raw region)
PASSWORD='Demo-Passw0rd!2026'

step() { printf '\n\033[1;34m== %s\033[0m\n' "$1"; }

# Creates a confirmed test user (no email sent) and prints an access token.
login() {
  local email=$1
  aws cognito-idp admin-create-user --region "$REGION" --user-pool-id "$POOL" \
    --username "$email" --message-action SUPPRESS \
    --user-attributes Name=email,Value="$email" Name=email_verified,Value=true >/dev/null 2>&1 || true
  aws cognito-idp admin-set-user-password --region "$REGION" --user-pool-id "$POOL" \
    --username "$email" --password "$PASSWORD" --permanent >/dev/null
  aws cognito-idp initiate-auth --region "$REGION" --client-id "$CLIENT" \
    --auth-flow USER_PASSWORD_AUTH \
    --auth-parameters USERNAME="$email",PASSWORD="$PASSWORD" \
    --query AuthenticationResult.AccessToken --output text
}

# api METHOD PATH TOKEN [JSON_BODY] [IDEMPOTENCY_KEY]
api() {
  local method=$1 path=$2 token=$3 body=${4:-} idem=${5:-}
  local args=(-sS -X "$method" "$API$path" -H "Authorization: Bearer $token"
              -H "Content-Type: application/json")
  [[ -n $idem ]] && args+=(-H "Idempotency-Key: $idem")
  [[ -n $body ]] && args+=(-d "$body")
  curl "${args[@]}"
}

step "Logging in two demo users"
ALICE=$(login alice@example.com)
BOB=$(login bob@example.com)

step "Without a token the API refuses the request"
curl -sS -o /dev/null -w "HTTP %{http_code}\n" "$API/accounts"

step "Alice and Bob open GBP accounts"
ALICE_ACC=$(api POST /accounts "$ALICE" '{"currency":"GBP"}' | jq -r .account_id)
BOB_ACC=$(api POST /accounts "$BOB" '{"currency":"GBP"}' | jq -r .account_id)
echo "Alice: $ALICE_ACC"
echo "Bob:   $BOB_ACC"

step "Alice deposits 5,000.00 (amounts are in pence)"
api POST "/accounts/$ALICE_ACC/deposits" "$ALICE" '{"amount":500000}' | jq .

TRANSFER=$(jq -n --arg f "$ALICE_ACC" --arg t "$BOB_ACC" \
  '{from_account_id:$f, to_account_id:$t, amount:12500, currency:"GBP", reference:"Dinner"}')
KEY=$(uuidgen 2>/dev/null || date +%s%N)

step "Alice sends Bob 125.00"
api POST /transfers "$ALICE" "$TRANSFER" "$KEY" | jq .

step "Network blip: the client retries with the same Idempotency-Key (no double charge)"
api POST /transfers "$ALICE" "$TRANSFER" "$KEY" | jq .

step "Balances"
api GET "/accounts/$ALICE_ACC" "$ALICE" | jq '{account: "alice", balance}'
api GET "/accounts/$BOB_ACC" "$BOB" | jq '{account: "bob", balance}'

step "Bob tries to spend from Alice's account (should be 404)"
STEAL=$(jq -n --arg f "$ALICE_ACC" --arg t "$BOB_ACC" \
  '{from_account_id:$f, to_account_id:$t, amount:100, currency:"GBP"}')
api POST /transfers "$BOB" "$STEAL" "steal-1" | jq .

step "Alice overspends (should be insufficient_funds)"
OVER=$(jq -n --arg f "$ALICE_ACC" --arg t "$BOB_ACC" \
  '{from_account_id:$f, to_account_id:$t, amount:99900000, currency:"GBP"}')
api POST /transfers "$ALICE" "$OVER" "over-1" | jq .

step "Alice sends 1,500.00: above the fraud threshold, triggers an alert email"
BIG=$(jq -n --arg f "$ALICE_ACC" --arg t "$BOB_ACC" \
  '{from_account_id:$f, to_account_id:$t, amount:150000, currency:"GBP", reference:"Car deposit"}')
api POST /transfers "$ALICE" "$BIG" "big-$(date +%s)" | jq .

step "Bob's transaction history"
api GET "/accounts/$BOB_ACC/transactions?limit=10" "$BOB" | jq .

printf '\nDone. Check your inbox for the fraud alert and the CloudWatch dashboard:\n%s\n' \
  "$(terraform output -raw dashboard_url)"
