"""Container Apps Job entry point — one status-check pass against Azure stores.

Invoked as a script by the dc-status-monitor-cron job:
    python /app/cron/run.py
(direct script invocation rather than -m, because az containerapp job update
argparse rejects dash-prefixed values in --command — see the deploy workflow).
Wires the Azure-backed stores and Key Vault-sourced notifier clients into the
shared checker logic in scripts.check_status.run_once().
"""

import os
import sys

# Direct script invocation puts /app/cron on sys.path, not /app — add /app so
# the `from app.* import …` and `from scripts import …` imports resolve.
sys.path.insert(0, "/app")

from app.config_store import ConfigStore
from app.feed_store import FeedStore
from app.keyvault import load_secrets
from app.state_store import StateStore
from scripts import check_status, gchat_notify, slack_notify, zoom_notify


def main() -> None:
    config = ConfigStore().read()
    secrets = load_secrets()

    notifiers = {
        "Slack": slack_notify.make_client(
            secrets.get("slack-bot-token", ""),
            os.environ.get("SLACK_DEFAULT_CHANNEL", ""),
        ),
        "Zoom": zoom_notify.make_client(secrets),
        "Google Chat": gchat_notify.make_client(),
    }

    check_status.run_once(
        config=config,
        secrets=secrets,
        state_store=StateStore(),
        feed_store=FeedStore(),
        notifiers=notifiers,
        base_url=os.environ.get("BASE_URL"),
    )


if __name__ == "__main__":
    main()
