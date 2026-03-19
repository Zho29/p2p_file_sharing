"""
=============================================================================
tracker_client.py  —  Socket-based Tracker Client
=============================================================================

PURPOSE
-------
Provides a clean, high-level API for the integration layer to communicate
with the socket-based Tracker server (Tracker/sockettracker.py).

Each method:
  1. Opens a fresh TCP connection (stateless — simple and robust)
  2. Sends the appropriate JSON message
  3. Reads the response and returns parsed data
  4. Closes the connection

The Tracker protocol (Tracker/protocol.py) uses raw JSON over TCP with
no length-prefix framing — each message is a single JSON object terminated
by the socket close OR received as a single recv() call.

USAGE
-----
    from tracker_client import TrackerClient

    tracker = TrackerClient(host="127.0.0.1", port=5000)

    tracker.register(peer_id="alice", port=8888)
    tracker.announce_file(
        peer_id="alice",
        file_name="movie.mp4",
        file_id="movie.mp4",
        chunk_indices=[0, 1, 2, 3],
    )
    chunk_map = tracker.get_peers(file_id="movie.mp4", chunk_ids=[0, 1, 2, 3])
    # chunk_map: {0: [{"ip": "...", "port": 8888}], 1: [...], ...}

    tracker.keep_alive(peer_id="alice")

=============================================================================
"""

import json
import socket
import logging

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_RECV_BUF   = 65_536      # bytes — plenty for any tracker response
_TIMEOUT    = 10.0        # seconds


# ---------------------------------------------------------------------------
# TrackerClient
# ---------------------------------------------------------------------------

class TrackerClient:
    """
    Communicates with the socket-based Tracker server.

    Parameters
    ----------
    host : str
        Hostname or IP address of the Tracker server.
    port : int
        Port the Tracker is listening on (default 5000).
    timeout : float
        Socket read/write timeout in seconds.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 5000,
                 timeout: float = _TIMEOUT):
        self._host    = host
        self._port    = port
        self._timeout = timeout

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def register(self, peer_id: str, port: int) -> dict:
        """
        Register this peer with the Tracker.

        Must be called once at startup before any other operations.

        Parameters
        ----------
        peer_id : str
            Unique identifier for this peer (e.g. "alice" or a UUID).
        port : int
            The TCP port this peer's server is listening on.

        Returns
        -------
        dict
            Tracker response data (includes a confirmation message).

        Raises
        ------
        ConnectionRefusedError
            Tracker is not running.
        RuntimeError
            Tracker returned an ERROR response.
        """
        log.info("Registering peer_id=%r on port %d", peer_id, port)
        response = self._send("REGISTER_PEER", {
            "peer_id": peer_id,
            "port":    port,
        })
        return response.get("data", {})

    def announce_file(
        self,
        peer_id:       str,
        file_name:     str,
        file_id:       str,
        chunk_indices: list[int],
    ) -> dict:
        """
        Tell the Tracker which chunks of a file this peer holds.

        Parameters
        ----------
        peer_id : str
            This peer's identifier (must match what was used in register()).
        file_name : str
            Human-readable file name (e.g. "movie.mp4").
        file_id : str
            Logical file identifier used in chunk_storage (same as file_name).
        chunk_indices : list[int]
            List of zero-based chunk indices this peer holds.

        Returns
        -------
        dict
            Tracker confirmation data.

        Raises
        ------
        RuntimeError
            Tracker returned an ERROR response.
        """
        chunks = [{"chunk_id": i} for i in chunk_indices]
        log.info("Announcing file=%r with %d chunks", file_id, len(chunks))
        response = self._send("ANNOUNCE_FILE", {
            "peer_id":   peer_id,
            "file_hash": file_id,     # using file_id as the hash key
            "file_name": file_name,
            "chunks":    chunks,
        })
        return response.get("data", {})

    def get_peers(
        self,
        file_id:   str,
        chunk_ids: list[int],
    ) -> dict[int, list[dict]]:
        """
        Ask the Tracker which peers hold which chunks.

        Parameters
        ----------
        file_id : str
            Logical file identifier.
        chunk_ids : list[int]
            Chunk indices to query.

        Returns
        -------
        dict[int, list[dict]]
            Maps each chunk index to a list of peer dicts:
            {0: [{"ip": "192.168.1.5", "port": 8888}], 1: [...], ...}
            Chunks with no available peers are omitted.

        Raises
        ------
        RuntimeError
            Tracker returned ERROR (e.g. file not found).
        """
        log.info("Querying peers for file=%r chunks=%s", file_id, chunk_ids)
        response = self._send("GET_PEERS", {
            "file_hash": file_id,
            "chunk_ids": chunk_ids,
        })
        data = response.get("data", {})
        raw_map = data.get("chunk_peers", {})

        # Normalise keys from str (JSON) → int
        return {int(k): v for k, v in raw_map.items()}

    def keep_alive(self, peer_id: str) -> None:
        """
        Send a keep-alive ping so the Tracker knows this peer is still online.

        The Tracker removes peers that haven't pinged in 30 seconds.
        Call this every ~15 seconds from a background thread.

        Parameters
        ----------
        peer_id : str
            This peer's identifier.
        """
        log.debug("keep_alive peer_id=%r", peer_id)
        try:
            self._send("KEEP_ALIVE", {"peer_id": peer_id})
        except Exception as exc:
            log.warning("keep_alive failed: %s", exc)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _send(self, msg_type: str, data: dict) -> dict:
        """
        Open a connection, send one message, receive the response, close.

        Parameters
        ----------
        msg_type : str
            One of the MessageType constants (e.g. "REGISTER_PEER").
        data : dict
            The payload dict to include under the "data" key.

        Returns
        -------
        dict
            Parsed JSON response from the Tracker.

        Raises
        ------
        ConnectionRefusedError
            Tracker server is not running.
        RuntimeError
            Tracker responded with type "ERROR".
        ConnectionError
            Network error or unexpected empty response.
        """
        message = json.dumps({"type": msg_type, "data": data})

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(self._timeout)

        try:
            sock.connect((self._host, self._port))
            sock.sendall(message.encode("utf-8"))

            # Read until the server closes the connection or we have the data
            raw = b""
            while True:
                chunk = sock.recv(_RECV_BUF)
                if not chunk:
                    break
                raw += chunk
                # The Tracker sends one JSON response per request — try to
                # parse as soon as we have valid JSON (no delimiter needed).
                try:
                    response = json.loads(raw.decode("utf-8"))
                    break   # got a complete JSON object
                except json.JSONDecodeError:
                    continue

        except ConnectionRefusedError:
            log.error("Tracker at %s:%d is not reachable", self._host, self._port)
            raise
        finally:
            sock.close()

        if not raw:
            raise ConnectionError("Tracker returned an empty response")

        if response.get("type") == "ERROR":
            msg = response.get("data", {}).get("message", "unknown error")
            raise RuntimeError(f"Tracker error: {msg}")

        log.debug("Tracker response type=%r data=%s",
                  response.get("type"), response.get("data"))
        return response
