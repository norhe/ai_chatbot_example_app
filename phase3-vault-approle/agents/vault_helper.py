"""
vault_helper.py — Vault AppRole authentication and token renewal for Voyager microservices.

Provides two public functions:

  vault_approle_login(role_id, secret_id) -> hvac.Client
      Authenticates to Vault using AppRole and returns a client with the token set.

  start_token_renewal(client, logger)
      Starts a background daemon thread that renews the Vault token at half-TTL
      intervals.  When renewal is no longer possible (max_ttl reached or token
      revoked), logs an error and calls os._exit(1) to force a clean restart.
"""

import os
import threading
import time
import hvac


def vault_approle_login(role_id: str, secret_id: str) -> hvac.Client:
    """Authenticate to Vault via AppRole and return an authenticated hvac.Client.

    Args:
        role_id:   The AppRole role_id for this service.
        secret_id: A single-use AppRole secret_id.

    Returns:
        An hvac.Client with client.token already set to the issued client token.

    Raises:
        KeyError:  If VAULT_ADDR is not in the environment.
        hvac.exceptions.VaultError: If the AppRole login fails.
    """
    client = hvac.Client(url=os.environ["VAULT_ADDR"])
    result = client.auth.approle.login(role_id=role_id, secret_id=secret_id)
    client.token = result["auth"]["client_token"]
    return client


def start_token_renewal(client: hvac.Client, logger) -> None:
    """Start a background daemon thread that keeps the Vault token alive.

    The thread sleeps for half the token's current remaining TTL, then calls
    renew_self().  It loops until renewal raises an exception, at which point
    it logs the failure and calls os._exit(1) so the process manager can
    restart the service with a fresh AppRole login.

    Args:
        client: An authenticated hvac.Client (token already set).
        logger: A stdlib logging.Logger instance for structured log output.
    """
    def _renew_loop():
        while True:
            try:
                lookup = client.auth.token.lookup_self()
                ttl = lookup["data"]["ttl"]
                if ttl <= 0:
                    logger.error(
                        '{"event": "vault_token_expired", "action": "exit"}'
                    )
                    os._exit(1)
                sleep_for = ttl // 2 or 1
                logger.info(
                    '{"event": "vault_token_renew_scheduled", "sleep_seconds": %d}',
                    sleep_for,
                )
                time.sleep(sleep_for)
                client.auth.token.renew_self()
                logger.info('{"event": "vault_token_renewed"}')
            except Exception as exc:  # noqa: BLE001 — intentional catch-all in daemon thread
                logger.error(
                    '{"event": "vault_token_renewal_failed", "error": "%s", "action": "exit"}',
                    type(exc).__name__,
                )
                os._exit(1)

    thread = threading.Thread(target=_renew_loop, daemon=True, name="vault-token-renewer")
    thread.start()
