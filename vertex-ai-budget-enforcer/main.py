"""
Cloud Run service to disable service account keys when budget is exceeded.
Triggered by Pub/Sub messages from GCP Billing Budget alerts.
"""
import base64
import json
import os
from google.cloud import iam_admin_v1
from flask import Flask, request


app = Flask(__name__)


# Service account email to disable
SERVICE_ACCOUNT_EMAIL = os.environ.get("SERVICE_ACCOUNT_EMAIL")
PROJECT_ID = os.environ.get("GCP_PROJECT_ID")


@app.route("/", methods=["POST"])
def handle_budget_alert():
    """Handle budget alert from Pub/Sub."""
    envelope = request.get_json()
    if not envelope:
        return "No Pub/Sub message received", 400

    # Decode Pub/Sub message
    if "message" not in envelope:
        return "Invalid Pub/Sub message format", 400

    pubsub_message = envelope["message"]

    # Decode data
    if "data" in pubsub_message:
        data = base64.b64decode(pubsub_message["data"]).decode("utf-8")
        budget_notification = json.loads(data)

        # Check if budget exceeded
        cost_amount = budget_notification.get("costAmount", 0)
        budget_amount = budget_notification.get("budgetAmount", 0)

        print(f"Budget notification received: cost=${cost_amount}, budget=${budget_amount}")

        if cost_amount >= budget_amount:
            # Budget exceeded - disable service account key
            print(f"BUDGET EXCEEDED! Disabling service account keys...")
            disable_service_account_keys()
            return f"Budget exceeded (${cost_amount} >= ${budget_amount}). Service account keys disabled.", 200

    return "Budget alert received but threshold not met", 200


def disable_service_account_keys():
    """Disable all keys for the configured service account."""
    client = iam_admin_v1.IAMClient()

    # List keys for service account
    request = iam_admin_v1.ListServiceAccountKeysRequest(
        name=f"projects/{PROJECT_ID}/serviceAccounts/{SERVICE_ACCOUNT_EMAIL}"
    )

    keys = client.list_service_account_keys(request=request)

    # Disable each key
    for key in keys.keys:
        # Skip Google-managed keys (only disable user-managed keys)
        if key.key_type == iam_admin_v1.types.ListServiceAccountKeysRequest.KeyType.USER_MANAGED:
            disable_request = iam_admin_v1.DisableServiceAccountKeyRequest(
                name=key.name
            )
            client.disable_service_account_key(request=disable_request)
            print(f"Disabled key: {key.name}")


if __name__ == "__main__":
    # Cloud Run sets PORT environment variable
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
