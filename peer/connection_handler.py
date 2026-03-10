"""
=============================================================================
peer/connection_handler.py  —  Per-Connection Server Logic
=============================================================================

PURPOSE
-------
Handles one peer connection from start to finish on the SERVER side.

When the Peer Server accepts a new TCP connection it spawns one
ConnectionHandler thread per client.  That thread owns the socket for the
lifetime of the conversation, then closes it and exits.

SUPPORTED MESSAGE FLOWS
-----------------------
The handler dispatches on the first frame received:

  REQUEST_CHUNK  (action field)
      Pull mode — the remote peer wants to download a chunk from us.
      Response: READY + binary bytes  OR  CHUNK_NOT_FOUND

  NOTIFY_STORAGE_REQ  (type field)
      Push mode — the remote peer (Alice for example) wants to upload chunks to us.
      Response: READY_TO_RECEIVE, then receive each chunk and save it.

RESPONSIBILITY BOUNDARY
-----------------------
This module:
  - Reads the incoming CMD frame and dispatches to the correct handler
  - Pull path: validates request, looks up chunk, streams it
  - Push path: ACKs the notification, receives chunks, saves via chunk_storage

-----------------------
The handler depends on a `chunk_storage` object provided by the caller.
That object must implement:

    chunk_storage.get_chunk(file_id: str, chunk_index: int) -> bytes | None
    chunk_storage.save_chunk(file_id: str, chunk_index: int, data: bytes) -> None

THREADING MODEL
---------------
Each ConnectionHandler instance is a daemon thread.  Daemon threads are
automatically killed when the main process exits.

=============================================================================
"""

import socket
import threading

from peer.protocol import (
    ACTION_NOTIFY_STORAGE,
    ACTION_REQUEST_CHUNK,
    DEFAULT_TIMEOUT,
    compute_sha256,
    make_ready_to_receive,
    make_response_error,
    make_response_not_found,
    make_response_ready,
    validate_request,
)
from peer.utilities import (
    get_logger,
    recv_chunk_payload,
    recv_frame,
    send_frame,
)


log = get_logger(__name__)


# ---------------------------------------------------------------------------
# ConnectionHandler
# ---------------------------------------------------------------------------

class ConnectionHandler(threading.Thread):
    """
    Daemon thread that serves one incoming peer connection.

    Purpose
    -------
    Instantiate and start() this thread whenever the Peer Server accepts a
    new connection.  The thread dispatches to a pull handler (REQUEST_CHUNK)
    or a push handler (NOTIFY_STORAGE_REQ) based on the first frame received.

    Parameters
    ----------
    conn : socket.socket
        The accepted client socket.
    addr : tuple
        (ip, port) of the remote peer — used only for log messages.
    chunk_storage : object
        Any object that implements get_chunk() and save_chunk()

    """

    def __init__(self, conn: socket.socket, addr: tuple, chunk_storage):
        super().__init__(daemon=True)
        self._conn          = conn
        self._addr          = addr
        self._chunk_storage = chunk_storage

    # ------------------------------------------------------------------
    # Thread entry point
    # ------------------------------------------------------------------

    def run(self):
        """
        Full lifecycle of one peer connection.

        Sets a socket timeout, delegates to _handle(), then closes the
        socket regardless of outcome.
        """
        peer = f"{self._addr[0]}:{self._addr[1]}"
        log.info("Connection opened from %s", peer)

        try:
            self._conn.settimeout(DEFAULT_TIMEOUT)
            self._handle()
        except TimeoutError:
            log.warning("Connection from %s timed out", peer)
            self._try_send(make_response_error("Request timed out"))
        except ConnectionError as exc:
            log.warning("Connection error with %s: %s", peer, exc)
        except Exception as exc:
            log.error("Unexpected error handling %s: %s", peer, exc, exc_info=True)
            self._try_send(make_response_error("Internal server error"))
        finally:
            self._conn.close()
            log.info("Connection closed for %s", peer)

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def _handle(self):
        """
        Read the first frame and dispatch to the appropriate handler.

        Pull mode  (action == REQUEST_CHUNK):       → _handle_pull()
        Push mode  (type   == NOTIFY_STORAGE_REQ):  → _handle_push()
        Unknown:                                    → ERROR response
        """
        peer = f"{self._addr[0]}:{self._addr[1]}"

        metadata, _ = recv_frame(self._conn)
        log.debug("First frame from %s: %s", peer, metadata)

        action   = metadata.get("action")
        msg_type = metadata.get("type")

        if action == ACTION_REQUEST_CHUNK:
            self._handle_pull(metadata)
        elif msg_type == ACTION_NOTIFY_STORAGE:
            self._handle_push(metadata)
        else:
            log.warning("Unknown message from %s: action=%r type=%r", peer, action, msg_type)
            send_frame(
                self._conn,
                make_response_error(
                    f"Unknown message: action={action!r} type={msg_type!r}"
                ),
            )

    # ------------------------------------------------------------------
    # Pull path  (remote peer requests a chunk from us)
    # ------------------------------------------------------------------

    def _handle_pull(self, metadata: dict):
        """
        Serve a REQUEST_CHUNK command.

        Steps
        -----
        1. Validate the request fields.
        2. Look up the chunk via chunk_storage.get_chunk().
        3. Send READY + binary payload  OR  CHUNK_NOT_FOUND.

        Parameters
        ----------
        metadata : dict
            The already-decoded first frame from the client.
        """
        peer = f"{self._addr[0]}:{self._addr[1]}"

        # Validate
        error = validate_request(metadata)
        if error:
            log.warning("Invalid pull request from %s: %s", peer, error)
            send_frame(self._conn, make_response_error(error))
            return

        file_id     = metadata["file_id"]
        chunk_index = metadata["index"]

        # Look up chunk
        chunk_data = self._chunk_storage.get_chunk(file_id, chunk_index)
        if chunk_data is None:
            log.info("Chunk not found: file=%r index=%d — notifying %s",
                     file_id, chunk_index, peer)
            send_frame(self._conn, make_response_not_found(file_id, chunk_index))
            return

        # Send READY then stream bytes
        chunk_hash = compute_sha256(chunk_data)
        log.info("Serving chunk file=%r index=%d size=%d to %s",
                 file_id, chunk_index, len(chunk_data), peer)
        send_frame(self._conn, make_response_ready(len(chunk_data), chunk_hash))
        self._conn.sendall(chunk_data)
        log.info("Chunk transfer complete to %s", peer)

    # ------------------------------------------------------------------
    # Push path  (remote peer is uploading chunks to other peers)
    # ------------------------------------------------------------------

    def _handle_push(self, metadata: dict):
        """
        Receive a NOTIFY_STORAGE_REQ and accept all listed chunks.

        Protocol
        --------
        1. ACK with READY_TO_RECEIVE.
        2. For each chunk index in chunks_to_be_sent:
             a. Receive [READY frame (size + hash)] + [binary bytes].
             b. Verify SHA-256.
             c. Save via chunk_storage.save_chunk().

        Parameters
        ----------
        metadata : dict
            The already-decoded NOTIFY_STORAGE_REQ frame from Alice.

        Raises
        ------
        ValueError
            If a received chunk's hash does not match the announced hash.
        KeyError
            If required fields are missing from the notification.
        """
        peer = f"{self._addr[0]}:{self._addr[1]}"

        file_id         = metadata["file_id"]
        chunks_expected = metadata["chunks_to_be_sent"]       # list[int]
        hashes_expected = metadata["integrity_hashes"]         # list[str]

        # Map chunk_index → expected hash for quick lookup
        hash_map = dict(zip(chunks_expected, hashes_expected))

        log.info("Push notification from %s: file=%r chunks=%s",
                 peer, file_id, chunks_expected)

        # Step 1 — ACK the notification
        send_frame(self._conn, make_ready_to_receive())

        # Step 2 — receive each chunk in the order seeder sends them
        for chunk_index in chunks_expected:
            chunk_meta, _ = recv_frame(self._conn)
            announced_size = chunk_meta["size"]
            announced_hash = chunk_meta["hash"]

            log.debug("Receiving pushed chunk file=%r index=%d size=%d",
                      file_id, chunk_index, announced_size)

            chunk_data  = recv_chunk_payload(self._conn, announced_size)
            actual_hash = compute_sha256(chunk_data)

            # Verify against the hash pre-announced in NOTIFY_STORAGE_REQ
            if actual_hash != hash_map[chunk_index]:
                raise ValueError(
                    f"Integrity failure for pushed chunk index={chunk_index}: "
                    f"expected={hash_map[chunk_index]} actual={actual_hash}"
                )

            # Verify against the per-chunk READY frame hash (double-check)
            if actual_hash != announced_hash:
                raise ValueError(
                    f"Integrity failure (READY hash mismatch) index={chunk_index}"
                )

            self._chunk_storage.save_chunk(file_id, chunk_index, chunk_data)
            log.info("Saved pushed chunk file=%r index=%d from %s",
                     file_id, chunk_index, peer)

        log.info("Push complete from %s: %d chunks saved", peer, len(chunks_expected))

    # ------------------------------------------------------------------
    # Helper
    # ------------------------------------------------------------------

    def _try_send(self, frame: bytes):
        """
        Best-effort frame send — used only in error handlers inside run().
        Silently swallows failures so the finally block can close cleanly.
        """
        try:
            self._conn.sendall(frame)
        except OSError:
            pass
