"""Run the server so it binds the address the SEC-1 guard actually checks.

    python -m server

Launching via ``uvicorn server.main:app --host X`` is UNSAFE: uvicorn's --host
flag decides the real bind, while the SEC-1 guard (check_bind_security) only
sees ``settings.host`` — the two decouple, so a --host 0.0.0.0 service passes a
guard that thinks it's on loopback. This entrypoint binds ``settings.host``
itself, so guard-input == real-bind. All service definitions use it.
"""
import uvicorn

from server.config import settings

if __name__ == "__main__":
    uvicorn.run(
        "server.main:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level,
    )
