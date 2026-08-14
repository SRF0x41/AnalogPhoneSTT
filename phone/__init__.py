"""AnalogPhoneSTT, phone-machine half: Asterisk's call audio in, transcripts out.

Runs on the box with the HT801 and Asterisk attached. Asterisk hands each answered call to
`audiosocket.serve` over a local TCP connection; `session` forwards that audio to the
speech-to-text machine and receives finished transcripts back; `originate` places outbound
calls through Asterisk's REST interface.

No SIP, no RTP, no resampling, and no audio processing of any kind happens here -- see the
module docstring in `audiosocket.py` for why that is Asterisk's job rather than ours.
"""

from .audiosocket import Call, Dtmf, ProtocolError, serve

__all__ = ["Call", "Dtmf", "ProtocolError", "serve"]
