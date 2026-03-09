"""
=============================================================================
peer/__init__.py  —  P2P Module Public Integration API
=============================================================================

PURPOSE
-------
This is the single entry point for the Integration layer (main.py).
It exposes four functions that cover the full peer lifecycle and hides all
internal implementation details.

USAGE
-----
Step 1 — Initialise with external dependencies:

    from peer import init_p2p
    init_p2p(
        chunk_storage        = chunk_mgr,        # from Chunking module
        tracker_get_peers_fn = tracker.get_peers  # optional callable
    )

Step 2 — Start the local server (call once at startup):

    server = p2p_serve(port=8888)

Step 3 — Discover who is online (optional; requires tracker_get_peers_fn):

    peers = p2p_discover()

Step 4 — Before downloading, tell the module where each chunk lives:

    set_download_context(file_id="movie.mp4", chunk_peer_map={
        0: [("192.168.1.5", 8888), ("192.168.1.6", 8888)],
        1: [("192.168.1.7", 8888)],
    })

Step 5 — Upload (push) a chunk to a peer:

    p2p_upload(
        target_ip   = "192.168.1.5",
        chunk_obj   = {
            "port":      8888,
            "sender_id": "Alice",
            "file_id":   "movie.mp4",
            "index":     0,
            "data":      chunk_bytes,
        }
    )

Step 6 — Download a chunk (tries peers in order, skips offline ones):

    chunk_bytes = p2p_download(chunk_index=0)

DEPENDENCY INJECTION
--------------------
The module never imports the Chunking or Tracker modules directly.
External dependencies are provided at init time via init_p2p(), keeping
the P2P module fully decoupled.

chunk_storage must implement:
    get_chunk(file_id: str, chunk_index: int)  -> bytes | None
    save_chunk(file_id: str, chunk_index: int, data: bytes) -> None

tracker_get_peers_fn (optional) must be:
    callable() -> list[dict]   e.g. [{"ip": "1.2.3.4", "port": 8888}, ...]

=============================================================================
"""

from peer.peer_server import start_peer_server, PeerServer
from peer.peer_client import request_chunk, push_chunk, ChunkNotFoundError
from peer.utilities  import get_logger


log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------

# All mutable state lives in one dict so it is easy to inspect and reset.
_state: dict = {
    # Injected by init_p2p()
    "chunk_storage":        None,
    "tracker_get_peers_fn": None,   # callable() -> list[{"ip":..,"port":..}]

    # Set by p2p_serve()
    "server":               None,

    # Set by p2p_discover() — cached list of online peers
    "available_peers":      [],

    # Set by set_download_context() — maps chunk_index → [(ip, port), ...]
    "current_file_id":      None,
    "chunk_peer_map":       {},     # {chunk_index: [(ip, port), ...]}
}


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------

def init_p2p(chunk_storage, tracker_get_peers_fn=None) -> None:
    """
    Inject external dependencies into the P2P module.

    Purpose
    -------
    Must be called once before any other function in this module.
    Separates object creation from dependency wiring so the module has
    no hard imports of the Chunking or Tracker modules.

    Parameters
    ----------
    chunk_storage : object
        The Chunking module's storage object.
        Must implement get_chunk() and save_chunk().
    tracker_get_peers_fn : callable, optional
        A zero-argument callable that returns a list of active-peer dicts:
            [{"ip": "192.168.1.5", "port": 8888}, ...]
        If omitted, p2p_discover() returns an empty list.

    Returns
    -------
    None

    Example
    -------
    >>> init_p2p(chunk_storage=chunk_mgr, tracker_get_peers_fn=tracker.get_peers)
    """
    _state["chunk_storage"]        = chunk_storage
    _state["tracker_get_peers_fn"] = tracker_get_peers_fn
    log.info("P2P module initialised (tracker=%s)",
             "yes" if tracker_get_peers_fn else "no")


# ---------------------------------------------------------------------------
# p2p_serve
# ---------------------------------------------------------------------------

def p2p_serve(port: int) -> PeerServer:
    """
    Start the local peer server and return the running instance.

    Purpose
    -------
    Binds a TCP port and begins accepting incoming chunk requests (pull)
    and incoming chunk uploads (push) from other peers in the background.

    Parameters
    ----------
    port : int
        TCP port to listen on.  Pass 0 to let the OS assign a free port.

    Returns
    -------
    PeerServer
        The running server.  Store the reference to call server.stop()
        on shutdown.

    Raises
    ------
    RuntimeError
        If init_p2p() has not been called first.

    Example
    -------
    >>> server = p2p_serve(port=8888)
    >>> print("Listening on", server.port)
    """
    _require_init("p2p_serve")
    server = start_peer_server(port=port, chunk_storage=_state["chunk_storage"])
    _state["server"] = server
    log.info("p2p_serve: server started on port %d", server.port)
    return server


# ---------------------------------------------------------------------------
# p2p_discover
# ---------------------------------------------------------------------------

def p2p_discover() -> list:
    """
    Fetch the current list of active peers from the Tracker and cache it.

    Purpose
    -------
    Calls the tracker_get_peers_fn injected at init time, stores the result
    in an internal cache, and returns it.  The Integration layer uses this
    list to decide which peers to contact for push or pull operations.

    Returns
    -------
    list[dict]
        Each dict has at least {"ip": str, "port": int}.
        Returns [] if no tracker function was provided at init time.

    Example
    -------
    >>> peers = p2p_discover()
    >>> for p in peers:
    ...     print(p["ip"], p["port"])
    """
    fn = _state["tracker_get_peers_fn"]
    if fn is None:
        log.warning("p2p_discover: no tracker function configured — returning []")
        return []

    peers = fn()
    _state["available_peers"] = peers
    log.info("p2p_discover: %d peers found", len(peers))
    return peers


# ---------------------------------------------------------------------------
# set_download_context  (helper for p2p_download)
# ---------------------------------------------------------------------------

def set_download_context(file_id: str, chunk_peer_map: dict) -> None:
    """
    Tell the module which peers hold which chunks before calling p2p_download.

    Purpose
    -------
    The Integration layer obtains the chunk-to-peer map from the Tracker
    (e.g. GET /file/location?file_id=XYZ) and passes it here so that
    p2p_download() knows which peers to contact.

    Parameters
    ----------
    file_id : str
        The file being downloaded (e.g. "movie.mp4").
    chunk_peer_map : dict
        Maps each chunk index to a list of (ip, port) tuples:
            {0: [("192.168.1.5", 8888), ("192.168.1.6", 8888)],
             1: [("192.168.1.7", 8888)], ...}

    Returns
    -------
    None

    Example
    -------
    >>> set_download_context("movie.mp4", {
    ...     0: [("192.168.1.5", 8888)],
    ...     1: [("192.168.1.6", 8888)],
    ... })
    """
    _state["current_file_id"] = file_id
    _state["chunk_peer_map"]  = chunk_peer_map
    log.info("Download context set: file=%r chunks=%d",
             file_id, len(chunk_peer_map))


# ---------------------------------------------------------------------------
# p2p_upload
# ---------------------------------------------------------------------------

def p2p_upload(target_ip: str, chunk_obj: dict) -> None:
    """
    Push one chunk to a target peer (Alice's distribution flow).

    Purpose
    -------
    Opens a connection to target_ip, performs the NOTIFY_STORAGE_REQ
    handshake, then streams the chunk.  The remote peer saves it via its
    own chunk_storage.

    Parameters
    ----------
    target_ip : str
        IP address of the receiving peer.
    chunk_obj : dict
        Bundle of everything needed for the push:
            {
              "port":      int,   # target peer's server port
              "sender_id": str,   # e.g. "Alice" (optional, default "peer")
              "file_id":   str,   # e.g. "movie.mp4"
              "index":     int,   # zero-based chunk index
              "data":      bytes, # raw chunk bytes
            }

    Returns
    -------
    None

    Raises
    ------
    RuntimeError
        If init_p2p() has not been called first.
    ConnectionRefusedError
        Target peer is offline.
    ConnectionError
        Push handshake or transfer failed.

    Example
    -------
    >>> p2p_upload("192.168.1.5", {
    ...     "port": 8888, "sender_id": "Alice",
    ...     "file_id": "report.pdf", "index": 1,
    ...     "data": chunk_bytes,
    ... })
    """
    _require_init("p2p_upload")

    target_port = chunk_obj["port"]
    sender_id   = chunk_obj.get("sender_id", "peer")
    file_id     = chunk_obj["file_id"]
    chunk_index = chunk_obj["index"]
    chunk_data  = chunk_obj["data"]

    log.info("p2p_upload: pushing file=%r index=%d to %s:%d",
             file_id, chunk_index, target_ip, target_port)

    push_chunk(
        target_ip   = target_ip,
        target_port = target_port,
        sender_id   = sender_id,
        file_id     = file_id,
        chunk_index = chunk_index,
        chunk_data  = chunk_data,
    )


# ---------------------------------------------------------------------------
# p2p_download
# ---------------------------------------------------------------------------

def p2p_download(chunk_index: int) -> bytes:
    """
    Pull a chunk from the best available peer and save it locally.

    Purpose
    -------
    Looks up the peer list for chunk_index from the context set by
    set_download_context(), then tries each peer in order until one
    succeeds.  On success it saves the chunk via chunk_storage.save_chunk()
    and returns the bytes.

    Fault tolerance
    ---------------
    - If a peer is offline (ConnectionRefusedError), the next peer is tried.
    - If a peer reports CHUNK_NOT_FOUND, the next peer is tried.
    - If all peers fail, RuntimeError is raised.

    Parameters
    ----------
    chunk_index : int
        Zero-based index of the chunk to download.

    Returns
    -------
    bytes
        The verified, saved chunk data.

    Raises
    ------
    RuntimeError
        No peer in the chunk map could serve the chunk, or
        set_download_context() was never called.

    Example
    -------
    >>> set_download_context("movie.mp4", {0: [("192.168.1.5", 8888)]})
    >>> data = p2p_download(0)
    """
    _require_init("p2p_download")

    file_id = _state["current_file_id"]
    if file_id is None:
        raise RuntimeError("Call set_download_context() before p2p_download()")

    peers = _state["chunk_peer_map"].get(chunk_index, [])
    if not peers:
        raise RuntimeError(
            f"No peers registered for file={file_id!r} chunk_index={chunk_index}"
        )

    last_error = None
    for ip, port in peers:
        try:
            log.info("p2p_download: trying %s:%d for file=%r index=%d",
                     ip, port, file_id, chunk_index)
            data = request_chunk(ip, port, file_id, chunk_index)
            _state["chunk_storage"].save_chunk(file_id, chunk_index, data)
            log.info("p2p_download: chunk %d saved (%d bytes)", chunk_index, len(data))
            return data
        except (ConnectionRefusedError, ChunkNotFoundError) as exc:
            log.warning("p2p_download: peer %s:%d failed (%s), trying next",
                        ip, port, exc)
            last_error = exc

    raise RuntimeError(
        f"All peers failed for file={file_id!r} chunk_index={chunk_index}. "
        f"Last error: {last_error}"
    )


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------

def _require_init(fn_name: str) -> None:
    """Raise RuntimeError if init_p2p() was not called."""
    if _state["chunk_storage"] is None:
        raise RuntimeError(
            f"{fn_name}() called before init_p2p() — "
            f"call init_p2p(chunk_storage=...) first"
        )
