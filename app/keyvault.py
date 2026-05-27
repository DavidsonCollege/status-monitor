"""Key Vault secret access for the status monitor.

Secrets are fetched once per process and cached in memory. The Container App's
system-assigned managed identity authenticates via DefaultAzureCredential and
must have the "Key Vault Secrets User" role on the vault.
"""

import os
from functools import lru_cache

from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient

# Canonical secret names in Key Vault (kebab-case per the migration plan).
SECRET_NAMES = [
    "slack-bot-token",
    "zoom-account-id",
    "zoom-client-id",
    "zoom-client-secret",
    "zoom-chatbot-client-id",
    "zoom-chatbot-client-secret",
    "zoom-bot-jid",
    "smtp-username",
    "smtp-password",
]


def _vault_url() -> str:
    name = os.environ["KEY_VAULT_NAME"]
    return f"https://{name}.vault.azure.net"


@lru_cache(maxsize=1)
def _client() -> SecretClient:
    return SecretClient(vault_url=_vault_url(), credential=DefaultAzureCredential())


@lru_cache(maxsize=1)
def load_secrets() -> dict[str, str]:
    """Load all known secrets into a dict keyed by their Key Vault name.

    Missing secrets resolve to "" so a partially-populated vault doesn't crash
    the process; the individual notifiers skip themselves when their required
    values are empty.
    """
    client = _client()
    secrets: dict[str, str] = {}
    for name in SECRET_NAMES:
        try:
            secrets[name] = client.get_secret(name).value or ""
        except Exception as exc:  # noqa: BLE001 - tolerate missing secrets
            print(f"  keyvault: could not read '{name}': {exc}")
            secrets[name] = ""
    return secrets
