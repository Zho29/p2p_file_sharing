"""
=============================================================================
peer/utilities.py  —  Socket I/O Helpers & Logging
=============================================================================

PURPOSE
-------
Provides two things that every other module in the peer package depends on:

  1. Reliable socket I/O
     TCP is a stream protocol — a single send() on one side may arrive as
     multiple recv() calls on the other.  The helpers here absorb that
     complexity so the rest of the code can think in whole frames and whole
     chunks, not arbitrary byte fragments.

  2. A consistent logger factory
     All peer modules get a named logger through get_logger(), which ensures
     uniform formatting and a single place to change log level or handlers.

PUBLIC API
----------
  recv_exact(sock, n)          -> bytes
  send_frame(sock, frame)      -> None
  recv_frame(sock)             -> tuple[dict, bytes]
  recv_chunk_payload(sock, n)  -> bytes
  get_logger(name)             -> logging.Logger

=============================================================================
"""

import logging
import socket
from typing import Tuple

from peer.protocol import (
    HEADER_SIZE,
    MAX_CHUNK_SIZE,
    decode_frame_header,
    decode_metadata,
)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

# Module-level flag so basicConfig is only called once per process
_logging_configured = False


def get_logger(name: str) -> logging.Logger:
    """
    Return a named logger with a consistent format.

    Purpose
    -------
    All peer modules call this instead of logging.getLogger() directly.
    That gives us one place to change format, level, or add file handlers
    across the entire peer package.

    Parameters
    ----------
    name : str
        Typically __name__ of the calling module
        (e.g. "peer.peer_server", "peer.peer_client").

    Returns
    -------
    logging.Logger

    Example
    -------
    >>> log = get_logger(__name__)
    >>> log.info("Server started on port %d", port)
    """
    global _logging_configured
    if not _logging_configured:
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
            datefmt="%H:%M:%S",
        )
        _logging_configured = True
    return logging.getLogger(name)


# ---------------------------------------------------------------------------
# Reliable receive helpers
# ---------------------------------------------------------------------------

def recv_exact(sock: socket.socket, n: int) -> bytes:
    """
    Read exactly `n` bytes from `sock`, blocking until all arrive.

    Purpose
    -------
    TCP delivers data as a stream.  A single logical message may arrive in
    many small recv() calls.  This function reassembles them into one complete
    chunk of exactly the requested size.

    Parameters
    ----------
    sock : socket.socket
        A connected, blocking TCP socket.
    n : int
        Exact number of bytes to read.

    Returns
    -------
    bytes
        Exactly `n` bytes.

    Raises
    ------
    ConnectionError
        If the connection closes before `n` bytes have arrived
        (the remote peer disconnected mid-transfer).
    ValueError
        If `n` is not a positive integer.

    Example
    -------
    >>> raw_header = recv_exact(sock, HEADER_SIZE)   # always 4 bytes
    >>> json_block = recv_exact(sock, json_length)
    """
    if n <= 0:
        raise ValueError(f"recv_exact requires n > 0, got {n}")

    buf = bytearray(n)          # pre-allocate target buffer
    view = memoryview(buf)      # zero-copy view for recv_into
    received = 0

    while received < n:
        # recv_into writes directly into the buffer — avoids extra copies
        count = sock.recv_into(view[received:], n - received)
        if count == 0:
            raise ConnectionError(
                f"Connection closed after {received}/{n} bytes"
            )
        received += count

    return bytes(buf)


# ---------------------------------------------------------------------------
# Frame-level send / receive
# ---------------------------------------------------------------------------

def send_frame(sock: socket.socket, frame: bytes) -> None:
    """
    Send a complete encoded frame over the socket.

    Purpose
    -------
    Thin wrapper around sock.sendall() that adds a log line and a clear
    exception message so callers never have to handle sendall() directly.

    Parameters
    ----------
    sock : socket.socket
        A connected TCP socket.
    frame : bytes
        A frame produced by any encode_frame() call in protocol.py.

    Returns
    -------
    None

    Raises
    ------
    ConnectionError
        If the socket write fails (remote end closed, network error, etc.).

    Example
    -------
    >>> send_frame(sock, make_request_chunk("movie.mp4", 3))
    """
    log = get_logger(__name__)
    
    try:
        sock.sendall(frame)
        log.debug("Sent %d bytes", len(frame))
    except OSError as exc:
        raise ConnectionError(f"Failed to send frame: {exc}") from exc


def recv_frame(sock: socket.socket) -> Tuple[dict, bytes]:
    """
    Receive one complete frame from the socket and return (metadata, payload).

    Purpose
    -------
    Handles the full three-step receive sequence:
      1. Read 4-byte header  →  learn JSON block size
      2. Read JSON block     →  decode metadata dict
      3. Read remaining bytes as payload
         (payload size = total bytes already in buffer beyond JSON,
          which for command/response frames is 0)

    NOTE: This function reads ONLY the header and metadata.  Binary chunk
    payloads are received separately with recv_chunk_payload() because the
    payload size is announced inside the metadata (the "size" field), and
    streaming large payloads through this function would require holding the
    entire chunk in memory at once.

    Parameters
    ----------
    sock : socket.socket
        A connected TCP socket.

    Returns
    -------
    tuple[dict, bytes]
        (metadata_dict, payload_bytes)
        For command/response frames: payload_bytes will be b"".

    Raises
    ------
    ConnectionError
        If the connection drops during reading.
    ValueError
        If the header or JSON block is malformed.

    Example
    -------
    >>> meta, payload = recv_frame(sock)
    >>> if meta.get("status") == STATUS_READY:
    ...     chunk = recv_chunk_payload(sock, meta["size"])
    """
    log = get_logger(__name__)

    # Step 1: read fixed-size header
    raw_header = recv_exact(sock, HEADER_SIZE)
    json_length = decode_frame_header(raw_header)
    log.debug("Incoming frame: JSON block = %d bytes", json_length)

    # Step 2: read the JSON metadata block
    raw_json = recv_exact(sock, json_length)
    metadata = decode_metadata(raw_json)
    log.debug("Received metadata: %s", metadata)

    return metadata, b""


def recv_chunk_payload(sock: socket.socket, size: int) -> bytes:
    """
    Receive exactly `size` bytes of binary chunk payload.

    Purpose
    -------
    Called after recv_frame() returns a READY response.  The server streams
    raw binary bytes immediately after sending the READY JSON frame — this
    function collects those bytes reliably.

    Parameters
    ----------
    sock : socket.socket
        The same socket used for recv_frame().
    size : int
        Exact number of payload bytes to read, as announced in the READY
        response's "size" field.

    Returns
    -------
    bytes
        The complete raw chunk data.

    Raises
    ------
    ValueError
        If size exceeds MAX_CHUNK_SIZE (sanity guard).
    ConnectionError
        If the connection drops before all bytes arrive.

    Example
    -------
    >>> meta, _ = recv_frame(sock)          # get READY response
    >>> chunk = recv_chunk_payload(sock, meta["size"])
    """
    if size > MAX_CHUNK_SIZE:
        raise ValueError(
            f"Announced payload size {size} exceeds MAX_CHUNK_SIZE {MAX_CHUNK_SIZE}"
        )
    log = get_logger(__name__)
    log.debug("Receiving chunk payload: %d bytes", size)
    data = recv_exact(sock, size)
    log.debug("Chunk payload received: %d bytes", len(data))
    return data
