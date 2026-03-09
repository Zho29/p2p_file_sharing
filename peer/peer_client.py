"""
=============================================================================
peer/peer_client.py  —  Outbound Peer Client
=============================================================================

PURPOSE
-------
Implements the outbound (pull) side of a peer connection.

Given a remote peer's address and a chunk identifier, this module:
  1. Opens a TCP connection to the remote peer
  2. Sends a REQUEST_CHUNK command frame
  3. Receives the READY response (containing size + SHA-256 hash)
  4. Streams the raw binary payload
  5. Verifies the SHA-256 digest
  6. Returns the verified chunk bytes to the caller

RESPONSIBILITY BOUNDARY
-----------------------
This module:
  - Makes outbound connections
  - Sends chunk requests and receives responses
  - Verifies data integrity via SHA-256

This module does NOT:
  - Store received chunks          (Chunking module's job via save_chunk())
  - Decide which peer to contact   (Integration layer's job)
  - Retry on a different peer      (Integration layer's job)
  - Contact the Tracker            (Integration layer's job)

INTEGRATION INTERFACE
---------------------
The Integration layer (or the p2p_download wrapper) calls:

    from peer.peer_client import request_chunk

    chunk_bytes = request_chunk(
        peer_ip    = "192.168.1.5",
        peer_port  = 8888,
        file_id    = "movie.mp4",
        chunk_index = 3,
    )
    chunk_storage.save_chunk("movie.mp4", 3, chunk_bytes)

EXCEPTIONS
----------
request_chunk() raises descriptive exceptions so the Integration layer can
decide what to do next (try a different peer, log the error, etc.):

  ConnectionRefusedError  — peer is offline or port is not listening
  ChunkNotFoundError      — peer responded CHUNK_NOT_FOUND
  IntegrityError          — downloaded bytes don't match the announced hash
  ConnectionError         — transfer dropped mid-stream
  TimeoutError            — peer did not respond within DEFAULT_TIMEOUT seconds

CONCURRENCY LIMIT
-----------------
The spec requires a maximum of 4 simultaneous outbound pull requests to
avoid saturating the local network.  This is enforced by a module-level
threading.Semaphore.  Any call to request_chunk() that would exceed the
limit blocks until an in-flight download completes.

=============================================================================
"""

import socket
import threading

from peer.protocol import (
    DEFAULT_TIMEOUT,
    STATUS_READY,
    STATUS_CHUNK_NOT_FOUND,
    STATUS_READY_TO_RECEIVE,
    compute_sha256,
    make_request_chunk,
    make_notify_storage_req,
    make_response_ready,
)
from peer.utilities import get_logger, send_frame, recv_frame, recv_chunk_payload


log = get_logger(__name__)

# Maximum number of simultaneous outbound chunk downloads (spec requirement).
MAX_CONCURRENT_DOWNLOADS: int = 4

# Module-level semaphore — shared across all threads in this process.
_download_semaphore = threading.Semaphore(MAX_CONCURRENT_DOWNLOADS)


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------

class ChunkNotFoundError(Exception):
    """
    Raised when the remote peer responds with CHUNK_NOT_FOUND.

    Attributes
    ----------
    file_id : str
    chunk_index : int

    Example
    -------
    >>> try:
    ...     request_chunk("1.2.3.4", 8888, "f.zip", 5)
    ... except ChunkNotFoundError as e:
    ...     print(f"Peer does not have chunk {e.chunk_index} of {e.file_id}")
    """

    def __init__(self, file_id: str, chunk_index: int):
        self.file_id     = file_id
        self.chunk_index = chunk_index
        super().__init__(
            f"CHUNK_NOT_FOUND: file={file_id!r} index={chunk_index}"
        )


class IntegrityError(Exception):
    """
    Raised when the SHA-256 digest of received data does not match
    the hash announced by the server in its READY response.

    The caller should discard the data and retry on a different peer.

    Attributes
    ----------
    expected : str   hex digest announced by the server
    actual   : str   hex digest of the bytes that arrived

    Example
    -------
    >>> try:
    ...     request_chunk("1.2.3.4", 8888, "f.zip", 5)
    ... except IntegrityError as e:
    ...     print(f"Hash mismatch — expected {e.expected}, got {e.actual}")
    """

    def __init__(self, expected: str, actual: str):
        self.expected = expected
        self.actual   = actual
        super().__init__(
            f"Integrity check failed: expected={expected} actual={actual}"
        )


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def request_chunk(
    peer_ip:     str,
    peer_port:   int,
    file_id:     str,
    chunk_index: int,
) -> bytes:
    """
    Connect to a remote peer and download one chunk.

    Purpose
    -------
    This is the single function the Integration layer calls to pull a chunk
    from another peer.  It handles the full CMD → RES → DATA exchange,
    verifies integrity, and returns the raw bytes.

    Parameters
    ----------
    peer_ip : str
        IP address of the remote peer (e.g. "192.168.1.5").
    peer_port : int
        TCP port the remote peer's server is listening on.
    file_id : str
        Logical file identifier (must match what the server's chunk_storage
        uses, e.g. "movie.mp4").
    chunk_index : int
        Zero-based index of the chunk to fetch.

    Returns
    -------
    bytes
        The raw, verified chunk data.

    Raises
    ------
    ConnectionRefusedError
        Peer is offline or not listening on that port.
    ChunkNotFoundError
        Peer responded CHUNK_NOT_FOUND — it does not hold this chunk.
    IntegrityError
        Downloaded bytes do not match the SHA-256 hash in the READY response.
    ConnectionError
        Connection dropped during transfer.
    TimeoutError
        Peer did not respond within DEFAULT_TIMEOUT seconds.

    Example
    -------
    >>> data = request_chunk("192.168.1.5", 8888, "report.pdf", 2)
    >>> chunk_storage.save_chunk("report.pdf", 2, data)
    """
    peer = f"{peer_ip}:{peer_port}"
    log.info(
        "Requesting chunk file=%r index=%d from %s", file_id, chunk_index, peer
    )

    # Acquire semaphore — blocks if MAX_CONCURRENT_DOWNLOADS are in flight.
    acquired = _download_semaphore.acquire(timeout=DEFAULT_TIMEOUT)
    if not acquired:
        raise TimeoutError(
            f"Could not start download: {MAX_CONCURRENT_DOWNLOADS} downloads "
            f"already in progress (timeout)"
        )

    try:
        return _do_request(peer_ip, peer_port, file_id, chunk_index)
    finally:
        # Always release so the next waiting download can proceed.
        _download_semaphore.release()


def _do_request(
    peer_ip:     str,
    peer_port:   int,
    file_id:     str,
    chunk_index: int,
) -> bytes:
    """
    Internal: perform the actual TCP connection and protocol exchange.

    Separated from request_chunk() so the semaphore logic in the public
    function stays clean and this function can focus purely on I/O.

    Parameters / Returns / Raises: same as request_chunk().
    """
    peer = f"{peer_ip}:{peer_port}"
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    try:
        # ── Connect ──────────────────────────────────────────────────────
        sock.settimeout(DEFAULT_TIMEOUT)
        try:
            sock.connect((peer_ip, peer_port))
        except ConnectionRefusedError:
            log.warning("Peer %s refused connection", peer)
            raise   # let caller decide to try a different peer

        log.debug("Connected to %s", peer)

        # ── Send REQUEST_CHUNK command ────────────────────────────────────
        cmd_frame = make_request_chunk(file_id, chunk_index)
        send_frame(sock, cmd_frame)
        log.debug("Sent REQUEST_CHUNK to %s", peer)

        # ── Receive response header ───────────────────────────────────────
        meta, _ = recv_frame(sock)
        status  = meta.get("status")
        log.debug("Response from %s: %s", peer, meta)

        # ── Handle CHUNK_NOT_FOUND ────────────────────────────────────────
        if status == STATUS_CHUNK_NOT_FOUND:
            log.info("Peer %s does not have file=%r index=%d", peer, file_id, chunk_index)
            raise ChunkNotFoundError(file_id, chunk_index)

        # ── Handle unexpected status ──────────────────────────────────────
        if status != STATUS_READY:
            reason = meta.get("reason", "unknown")
            raise ConnectionError(
                f"Unexpected response from {peer}: status={status!r} reason={reason!r}"
            )

        # ── Receive binary payload ────────────────────────────────────────
        announced_size = meta["size"]
        announced_hash = meta["hash"]

        log.info(
            "Peer %s ready to send %d bytes (hash=%s…)",
            peer, announced_size, announced_hash[:12]
        )

        chunk_data = recv_chunk_payload(sock, announced_size)

        # ── Verify integrity ──────────────────────────────────────────────
        actual_hash = compute_sha256(chunk_data)
        if actual_hash != announced_hash:
            log.error(
                "Integrity failure from %s: expected=%s actual=%s",
                peer, announced_hash, actual_hash
            )
            raise IntegrityError(announced_hash, actual_hash)

        log.info(
            "Chunk received and verified: file=%r index=%d size=%d from %s",
            file_id, chunk_index, len(chunk_data), peer
        )
        return chunk_data

    finally:
        sock.close()


# ---------------------------------------------------------------------------
# Push  (Alice uploads a chunk to a target peer)
# ---------------------------------------------------------------------------

def push_chunk(
    target_ip:   str,
    target_port: int,
    sender_id:   str,
    file_id:     str,
    chunk_index: int,
    chunk_data:  bytes,
) -> None:
    """
    Connect to a remote peer and push (upload) one chunk to it.

    Purpose
    -------
    This is Alice's side of the push distribution flow.  She opens a
    connection, sends a NOTIFY_STORAGE_REQ, waits for READY_TO_RECEIVE,
    then streams the chunk.  The remote peer's ConnectionHandler saves it.

    Protocol sequence
    -----------------
      Alice → Peer:  NOTIFY_STORAGE_REQ  (lists chunk + hash)
      Peer  → Alice: READY_TO_RECEIVE
      Alice → Peer:  READY frame         (size + hash)
      Alice → Peer:  binary chunk bytes

    Parameters
    ----------
    target_ip : str
        IP address of the peer that will receive the chunk.
    target_port : int
        TCP port of the peer's server.
    sender_id : str
        Human-readable name for Alice in the notification (e.g. "Alice").
    file_id : str
        Logical file identifier (must match what chunk_storage uses).
    chunk_index : int
        Zero-based index of the chunk being pushed.
    chunk_data : bytes
        Raw chunk bytes to transfer.

    Returns
    -------
    None

    Raises
    ------
    ConnectionRefusedError
        Target peer is offline.
    ConnectionError
        Peer rejected the notification or transfer failed mid-stream.

    Example
    -------
    >>> push_chunk("192.168.1.5", 8888, "Alice", "report.pdf", 1, chunk_bytes)
    """
    target = f"{target_ip}:{target_port}"
    log.info("Pushing chunk file=%r index=%d to %s", file_id, chunk_index, target)

    chunk_hash = compute_sha256(chunk_data)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    try:
        sock.settimeout(DEFAULT_TIMEOUT)
        sock.connect((target_ip, target_port))

        # Step 1: send notification
        notify = make_notify_storage_req(
            sender_id=sender_id,
            file_id=file_id,
            chunks=[chunk_index],
            total_size_bytes=len(chunk_data),
            integrity_hashes=[chunk_hash],
        )
        send_frame(sock, notify)

        # Step 2: wait for READY_TO_RECEIVE
        meta, _ = recv_frame(sock)
        if meta.get("status") != STATUS_READY_TO_RECEIVE:
            raise ConnectionError(
                f"Expected READY_TO_RECEIVE from {target}, got {meta}"
            )

        # Step 3: send READY frame (size + hash) then raw bytes
        send_frame(sock, make_response_ready(len(chunk_data), chunk_hash))
        sock.sendall(chunk_data)

        log.info("Push complete: file=%r index=%d to %s", file_id, chunk_index, target)

    finally:
        sock.close()
