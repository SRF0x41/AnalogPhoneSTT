"""Deployment constants for the phone machine.

Every value here describes *this LAN*, not the design, so it is the one file you should
expect to edit when moving the project to different hardware. Overridable from the
environment so a second instance (or a test) doesn't have to patch the module.
"""

from __future__ import annotations

import os

# Where Asterisk's AudioSocket() connects to. Localhost by default: Asterisk runs on this
# same machine, so the call audio never touches the network on this hop.
AUDIOSOCKET_HOST = os.environ.get("ANALOGPHONE_AUDIOSOCKET_HOST", "127.0.0.1")
AUDIOSOCKET_PORT = int(os.environ.get("ANALOGPHONE_AUDIOSOCKET_PORT", "9092"))

# The speech-to-text machine (the `stt_port` package). The path is ignored by that server;
# one connection is one call, and the call is identified in the call_start message.
STT_URL = os.environ.get("ANALOGPHONE_STT_URL", "ws://192.168.50.120:9099/")

# Asterisk ARI, for originating outbound calls. Enable in http.conf and ari.conf -- see
# asterisk/ari.conf.sample.
ARI_URL = os.environ.get("ANALOGPHONE_ARI_URL", "http://127.0.0.1:8088/ari")
ARI_USER = os.environ.get("ANALOGPHONE_ARI_USER", "analogphone")
ARI_PASSWORD = os.environ.get("ANALOGPHONE_ARI_PASSWORD", "")

# The PJSIP endpoint for the HT801, and the dialplan context an originated call is dropped
# into once it answers. Must match asterisk/extensions.conf.sample.
PHONE_ENDPOINT = os.environ.get("ANALOGPHONE_PHONE_ENDPOINT", "PJSIP/phone")
OUTBOUND_CONTEXT = os.environ.get("ANALOGPHONE_OUTBOUND_CONTEXT", "analogphone-outbound")
OUTBOUND_EXTENSION = os.environ.get("ANALOGPHONE_OUTBOUND_EXTENSION", "s")
