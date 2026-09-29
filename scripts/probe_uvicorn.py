import logging, sys

import litellm
from fastapi import FastAPI

app = FastAPI()

for name in ["", "uvicorn", "uvicorn.error", "uvicorn.access", "LiteLLM", "litellm", "LiteLLM Proxy", "LiteLLM Router"]:
    lg = logging.getLogger(name or None) if name else logging.getLogger()
    print(
        f"{name or 'root':16s} handlers={[(type(h).__module__, type(h).__name__) for h in lg.handlers]} "
        f"filters={[type(f).__name__ for f in lg.filters]} "
        f"level={logging.getLevelName(lg.level)} propagate={lg.propagate}",
        file=sys.stderr,
    )