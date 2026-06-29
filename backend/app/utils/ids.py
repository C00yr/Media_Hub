from datetime import datetime
from itertools import count

_counter = count(1)


def trace_id(prefix: str = "REQ") -> str:
    stamp = datetime.utcnow().strftime("%Y%m%d")
    return f"{prefix}-{stamp}-{next(_counter):06d}"

