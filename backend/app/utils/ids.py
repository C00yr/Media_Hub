from datetime import datetime
from itertools import count

from app.utils.time import system_now

_counter = count(1)


def trace_id(prefix: str = "REQ") -> str:
    stamp = system_now().strftime("%Y%m%d")
    return f"{prefix}-{stamp}-{next(_counter):06d}"

