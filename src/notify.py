"""Alerts for newly discovered jobs: Windows toast notification and/or a Discord webhook."""

from __future__ import annotations

import json
import os
import subprocess
import urllib.request

from src.tools import NO_WINDOW

# PowerShell's own AppUserModelID: lets a script raise a toast without registering an app
_PS_APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"

_TOAST_PS = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml($env:AUTOAPPLY_TOAST_XML)
$toast = New-Object Windows.UI.Notifications.ToastNotification $xml
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($env:AUTOAPPLY_TOAST_APP).Show($toast)
"""


def _xml_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def toast_xml(title: str, lines: list[str]) -> str:
    texts = "".join(f"<text>{_xml_escape(line)}</text>" for line in [title, *lines[:2]])
    return f'<toast duration="long"><visual><binding template="ToastGeneric">{texts}</binding></visual></toast>'


def toast(title: str, lines: list[str]) -> None:
    """Windows desktop notification (title + up to two lines)."""
    env ={**os.environ, "AUTOAPPLY_TOAST_XML": toast_xml(title, lines), "AUTOAPPLY_TOAST_APP": _PS_APP_ID}
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _TOAST_PS],
                   env=env, capture_output=True, timeout=30, creationflags=NO_WINDOW, check=True)


def discord(webhook_url: str, content: str) -> None:
    body = json.dumps({"content": content[:1900]}).encode("utf-8")
    req = urllib.request.Request(webhook_url, data=body,
                                 headers={"Content-Type": "application/json", "User-Agent": "autoapply"})
    urllib.request.urlopen(req, timeout=15).close()


def send(cfg: dict, title: str, lines: list[str]) -> list[str]:
    """Send through every enabled channel; returns errors (alerts must never crash the watcher)."""
    ncfg = cfg.get("notify") or {}
    errors = []
    if ncfg.get("toast", True):
        try:
            toast(title, lines)
        except Exception as e:
            errors.append(f"toast: {e}")
    if ncfg.get("discord_webhook"):
        try:
            discord(ncfg["discord_webhook"], "**" + title + "**\n" + "\n".join(lines))
        except Exception as e:
            errors.append(f"discord: {e}")
    return errors
