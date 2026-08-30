#!/usr/bin/env python3
"""Minimal Zabbix 7.x JSON-RPC client.

Zabbix 7.0 changed how API calls are authenticated, and it is the single most
common reason a script written against 6.x fails after an upgrade:

    Zabbix <= 6.4   {"jsonrpc":"2.0", "method":..., "auth":"<token>", ...}
    Zabbix >= 7.0   Authorization: Bearer <token>   (the body "auth" field is
                    ignored, and every call fails with a permission error)

This client always uses the header form.

Usage as a library::

    from zbx import Zabbix
    z = Zabbix.from_env()
    for h in z.call("host.get", {"output": ["host", "status"]}):
        print(h["host"])

Configuration comes from the environment (see ``.env.example``):

    ZBX_URL      full path to api_jsonrpc.php
    ZBX_USER     API user
    ZBX_PASS     API password
    ZBX_TOKEN    a pre-created API token; used instead of user/pass if set
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request


class ZabbixError(RuntimeError):
    """An error returned by the Zabbix API itself (not a transport failure)."""


class Zabbix:
    def __init__(self, url: str, user: str = "", password: str = "",
                 token: str = "", timeout: int = 30):
        if not url:
            raise ZabbixError("ZBX_URL is not set.")
        self.url = url
        self.timeout = timeout
        self._id = 0
        # A pre-created API token (Users -> API tokens) avoids storing a
        # password and can be scoped to a read-only role. Preferred.
        self.token = token or ""
        if not self.token:
            if not (user and password):
                raise ZabbixError(
                    "Set ZBX_TOKEN, or both ZBX_USER and ZBX_PASS."
                )
            self.token = self._login(user, password)

    @classmethod
    def from_env(cls) -> "Zabbix":
        return cls(
            url=os.environ.get("ZBX_URL", "").strip(),
            user=os.environ.get("ZBX_USER", "").strip(),
            password=os.environ.get("ZBX_PASS", "").strip(),
            token=os.environ.get("ZBX_TOKEN", "").strip(),
        )

    # -- transport ---------------------------------------------------------
    def _post(self, payload: dict, authed: bool) -> dict:
        self._id += 1
        payload["id"] = self._id
        payload["jsonrpc"] = "2.0"
        headers = {"Content-Type": "application/json-rpc"}
        if authed:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(
            self.url, data=json.dumps(payload).encode(), headers=headers
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                body = json.load(r)
        except urllib.error.HTTPError as e:
            raise ZabbixError(f"HTTP {e.code} from {self.url}: {e.reason}") from e
        except urllib.error.URLError as e:
            raise ZabbixError(f"Cannot reach {self.url}: {e.reason}") from e
        if "error" in body:
            err = body["error"]
            raise ZabbixError(
                f"{payload.get('method')}: {err.get('message')} {err.get('data', '')}".strip()
            )
        return body["result"]

    def _login(self, user: str, password: str) -> str:
        return self._post(
            {"method": "user.login", "params": {"username": user, "password": password}},
            authed=False,
        )

    # -- public ------------------------------------------------------------
    def call(self, method: str, params=None):
        """Invoke any API method. ``params`` defaults to an empty object."""
        return self._post({"method": method, "params": params or {}}, authed=True)

    def version(self) -> str:
        # apiinfo.version is the one method that must NOT be authenticated.
        return self._post({"method": "apiinfo.version", "params": {}}, authed=False)

    def count(self, method: str, params=None) -> int:
        p = dict(params or {})
        p["countOutput"] = True
        return int(self.call(method, p))


def connect_or_exit() -> Zabbix:
    """Build a client from the environment, or print a clear error and exit.

    Every script in this repo uses this so a misconfigured environment produces
    one readable line instead of a traceback.
    """
    try:
        z = Zabbix.from_env()
    except ZabbixError as e:
        print(f"error: {e}", file=sys.stderr)
        print("\nCopy .env.example to .env, fill it in, then:", file=sys.stderr)
        print("    set -a; . ./.env; set +a", file=sys.stderr)
        sys.exit(2)
    return z


if __name__ == "__main__":
    z = connect_or_exit()
    print(f"Connected to {z.url}")
    print(f"  API version : {z.version()}")
    print(f"  Hosts       : {z.count('host.get', {'filter': {'status': 0}})} enabled")
    print(f"  Items       : {z.count('item.get')}")
    print(f"  Triggers    : {z.count('trigger.get')}")
    print(f"  Problems    : {z.count('problem.get')} active")
