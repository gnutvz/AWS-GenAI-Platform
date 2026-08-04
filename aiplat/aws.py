"""How this platform talks to AWS: retries and timeouts, decided once.

Bedrock throttles on tokens per minute, not on requests, so a single long answer
can trip a limit that a hundred short ones would not. Throttling is therefore
not an edge case to handle if it shows up — it is the normal steady state of a
busy tenant, and boto3's default of two attempts with no client-side pacing
turns it into a 500 the user sees.

`adaptive` mode over `standard` because the failure is rate, not chance.
Standard mode retries with exponential backoff, which helps when a request
failed on its own; adaptive adds a client-side rate limiter that slows the whole
client down once throttling starts, which is what actually helps when the
account is over its quota.

On the timeouts: the numbers are chosen against the Lambda timeout of five
minutes. A model call is bounded at 3 x 45s and a retrieval at 3 x 20s, so
neither can burn the whole budget alone. A long enough tool loop still can —
the Lambda timeout stays the real backstop, and this only makes sure no single
call is what runs it out.
"""

from __future__ import annotations

from botocore.config import Config

# Total attempts, not retries: 3 means the initial call plus two more.
MAX_ATTEMPTS = 3


def boto_config(**overrides) -> Config:
    """Client configuration for every AWS call this platform makes.

    Args:
        **overrides: Per-client adjustments, e.g. a shorter `read_timeout` for a
            call that should be fast and is better retried than waited on.
    """
    params: dict = {
        "retries": {"max_attempts": MAX_ATTEMPTS, "mode": "adaptive"},
        "connect_timeout": 5,
        "read_timeout": 45,
    }
    params.update(overrides)
    return Config(**params)
