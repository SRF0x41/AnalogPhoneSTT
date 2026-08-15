"""Deployment constants for the phone machine.

Every value here describes *this LAN*, not the design, so it is the one file you should
expect to edit when moving the project to different hardware. Edit the values in place;
there is no .env file and nothing here reads one.

Anything in here can still be overridden per-run from the command line -- see
`python -m phone --help`.
"""

from __future__ import annotations

# Where the AudioSocket server binds, and so where Asterisk's AudioSocket() must connect.
# Localhost: Asterisk runs on this same machine, so the call audio never touches the
# network on this hop. Must match the address in asterisk/extensions.conf.sample --
# change one and you must change the other. Do not use 5060 or 10000-20000; Asterisk
# already holds those for SIP and RTP.
AUDIOSOCKET_HOST = "127.0.0.1"
AUDIOSOCKET_PORT = 9092

# The speech-to-text machine (the `stt_port` package). The path is ignored by that server;
# one connection is one call, and the call is identified in the call_start message.
STT_URL = "ws://192.168.50.120:9099/"

# Asterisk ARI, for originating outbound calls. Enable in http.conf and ari.conf -- see
# asterisk/ari.conf.sample.
ARI_URL = "http://127.0.0.1:8088/ari"
ARI_USER = "analogphone"
ARI_PASSWORD = "userpass1"

# The PJSIP endpoint for the HT801, and the dialplan context an originated call is dropped
# into once it answers. Must match asterisk/extensions.conf.sample.
PHONE_ENDPOINT = "PJSIP/phone"
OUTBOUND_CONTEXT = "analogphone-outbound"
OUTBOUND_EXTENSION = "s"
