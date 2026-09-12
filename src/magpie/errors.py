"""The error a failed remote call raises, and the one the API maps to a 503.

Both clients raise it -- ``clients.http`` for HTTP, ``clients.clickhouse`` for the
database -- as do the services that inspect a response. Which service failed is in
the message ("clickhouse query failed: ...", "github returned HTTP 404"), which is
all the handler in ``main`` needs.
"""


class RemoteError(RuntimeError):
    """A call to a remote service failed in a way the API should surface.

    Either the request kept failing after every retry, or the response carried a
    status the caller refuses to forward.
    """
