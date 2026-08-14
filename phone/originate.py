"""Place an outbound call to the handset, through Asterisk's REST interface (ARI).

Asterisk rings the HT801 and, once it is answered, drops the channel into the
`analogphone-outbound` dialplan context -- which runs the same `AudioSocket()` application an
inbound call does. So an originated call arrives at `audiosocket.serve` indistinguishable
from one the caller placed, and nothing downstream needs an "is this outbound?" branch.

ARI rather than a second SIP endpoint: Asterisk is already running on this machine and
already knows how to reach the HT801, so origination is a local HTTP request rather than a
SIP stack. It also reports real channel state, where the previous pyVoIP implementation had
to poll a call object waiting for it to stop saying DIALING.

Requires ARI enabled -- see `asterisk/ari.conf.sample`. It is off by default in Asterisk.
"""

from __future__ import annotations

import asyncio
import sys

from . import config


class OriginateError(RuntimeError):
    """Asterisk refused to place the call, or ARI is not reachable/enabled."""


def _post_channel(endpoint: str, context: str, extension: str, timeout: float) -> dict:
    """Blocking ARI call. Runs on a worker thread so it can't stall the event loop."""
    import requests

    try:
        response = requests.post(
            f"{config.ARI_URL}/channels",
            params={
                "endpoint": endpoint,
                "context": context,
                "extension": extension,
                "priority": 1,
                "timeout": int(timeout),
            },
            auth=(config.ARI_USER, config.ARI_PASSWORD),
            timeout=timeout + 5,
        )
    except requests.RequestException as exc:
        raise OriginateError(
            f"could not reach ARI at {config.ARI_URL}: {exc}. Is it enabled in "
            "http.conf and ari.conf? See asterisk/ari.conf.sample."
        ) from exc

    if response.status_code == 401:
        raise OriginateError(
            f"ARI rejected the credentials for user {config.ARI_USER!r}. "
            "Set ANALOGPHONE_ARI_USER / ANALOGPHONE_ARI_PASSWORD to match ari.conf."
        )
    if response.status_code >= 400:
        raise OriginateError(
            f"ARI refused to originate to {endpoint}: {response.status_code} {response.text.strip()}"
        )
    return response.json()


async def call_phone(
    endpoint: str | None = None,
    context: str | None = None,
    extension: str | None = None,
    timeout: float = 30.0,
    verbose: bool = False,
) -> dict:
    """Ring the handset. Returns ARI's channel record as soon as Asterisk accepts the request.

    Returning early is deliberate: the call's *audio* arrives on the AudioSocket server when
    the handset is picked up, so that -- not this function -- is where a call becomes real.
    Waiting here for an answer would just be a second place tracking the same thing.
    """
    endpoint = endpoint or config.PHONE_ENDPOINT
    context = context or config.OUTBOUND_CONTEXT
    extension = extension or config.OUTBOUND_EXTENSION

    print(f"[originate] ringing {endpoint} -> {context},{extension}", file=sys.stderr)
    channel = await asyncio.to_thread(_post_channel, endpoint, context, extension, timeout)
    if verbose:
        print(
            f"[originate] channel {channel.get('name')} ({channel.get('id')}) "
            f"state={channel.get('state')}",
            file=sys.stderr,
        )
    return channel
