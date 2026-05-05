API_PREFIX = "/v1/api"


def api_url(path: str) -> str:
    if not path.startswith("/"):
        path = f"/{path}"
    return f"{API_PREFIX}{path}"
