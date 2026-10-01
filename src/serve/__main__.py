from __future__ import annotations

import os


def main() -> None:
    import uvicorn

    host = os.environ.get("GITCRAWL_HOST", "127.0.0.1")
    port = int(os.environ.get("GITCRAWL_PORT", "8000"))
    uvicorn.run("serve.app:app", host=host, port=port)


if __name__ == "__main__":
    main()
