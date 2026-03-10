"""
=============================================================================
peer/peer_server.py  —  TCP Peer Server
=============================================================================

PURPOSE
-------
Listens on a TCP port for incoming chunk requests from other peers.
For every accepted connection it spawns one ConnectionHandler daemon thread
and immediately goes back to accepting the next connection.

This module contains:
  - PeerServer          : the server class (manages socket lifecycle + accept loop)
  - start_peer_server() : the public function the Integration layer calls

RESPONSIBILITY BOUNDARY
-----------------------
This module:
  - Binds and listens on a TCP port
  - Accepts incoming connections
  - Delegates all per-connection logic to ConnectionHandler

INTEGRATION INTERFACE
---------------------
The Integration layer (main.py) starts the server with one call:

    from peer.peer_server import start_peer_server

    server = start_peer_server(port=8888, chunk_storage=my_chunk_storage)
    # server is now running in the background

    # ... later, for clean shutdown:
    server.stop()

THREADING MODEL
---------------
The accept loop runs inside a single daemon thread (PeerServer._run).
Each accepted connection is handed to a separate ConnectionHandler daemon
thread.  All threads are daemon threads, so they die automatically when the
main process exits — the caller never needs to clean them up manually.

SHUTDOWN
--------
Calling server.stop() sets an internal flag and closes the listening socket.
That causes the blocking accept() call to raise an OSError, which the loop
detects and exits cleanly.

=============================================================================
"""

import socket
import threading

from peer.connection_handler import ConnectionHandler
from peer.utilities import get_logger


log = get_logger(__name__)


# ---------------------------------------------------------------------------
# PeerServer
# ---------------------------------------------------------------------------

class PeerServer:
    """
    TCP server that accepts peer connections and delegates to ConnectionHandler.

    Purpose
    -------
    Manages the full lifecycle of the listening socket: bind → listen →
    accept loop → shutdown.  Runs entirely in a background daemon thread so
    it never blocks the caller.

    Parameters
    ----------
    port : int
        TCP port to listen on.  Use 0 to let the OS pick a free port.
    chunk_storage : object
        Any object implementing get_chunk(file_id, chunk_index).
        Passed through to each ConnectionHandler unchanged.

    Attributes
    ----------
    port : int
        The actual port the server is bound to.  

    """

    def __init__(self, port: int, chunk_storage):
        self._port          = port
        self._chunk_storage = chunk_storage
        self._server_sock   = None          # set in start()
        self._stop_event    = threading.Event()
        self._thread        = None          # the accept-loop thread

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    @property
    def port(self) -> int:
        """The port the server is currently bound to."""
        return self._port

    def start(self) -> None:
        """
        Bind the socket, start listening, and launch the accept-loop thread.

        Purpose
        -------
        Creates and configures the server socket, then starts the accept loop
        in a background daemon thread.  Returns as soon as the socket is bound
        and the thread is alive — the caller does not block.

        Raises
        ------
        OSError
            If the port is already in use or the bind fails for any reason.
        """
        self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

        # SO_REUSEADDR lets us re-bind immediately after a restart
        # without waiting for the OS TIME_WAIT period to expire.
        self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        self._server_sock.bind(("0.0.0.0", self._port))

        # If port=0 was requested, read back the OS-assigned port.
        self._port = self._server_sock.getsockname()[1]

        # Allow up to 10 connections to queue while we're processing accept().
        self._server_sock.listen(10)

        log.info("PeerServer listening on port %d", self._port)

        self._thread = threading.Thread(
            target=self._run,
            name=f"PeerServer-{self._port}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """
        Signal the accept loop to stop and close the listening socket.

        Purpose
        -------
        Sets the stop flag, then closes the server socket.  Closing the socket
        causes the blocking accept() call inside _run() to raise an OSError,
        which the loop recognises as a shutdown signal and exits cleanly.

        This method blocks briefly (up to 2 s) to wait for the thread to exit.
        """
        log.info("PeerServer stopping on port %d", self._port)
        self._stop_event.set()
        if self._server_sock:
            try:
                self._server_sock.close()
            except OSError:
                pass
        if self._thread:
            self._thread.join(timeout=2.0)

    def is_running(self) -> bool:
        """
        Return True if the accept-loop thread is alive.

        """
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------------
    # Accept loop (runs in background thread)
    # ------------------------------------------------------------------

    def _run(self) -> None:
        """
        Accept-loop: wait for connections, spawn a handler for each one.

        Runs in the background daemon thread started by start().
        Exits when stop() is called (socket.close() unblocks accept()).
        """
        log.debug("Accept loop started on port %d", self._port)

        while not self._stop_event.is_set():
            try:
                conn, addr = self._server_sock.accept()
            except OSError:
                # Socket was closed by stop() — exit the loop cleanly.
                if not self._stop_event.is_set():
                    log.error("Unexpected socket error in accept loop")
                break

            log.info("Accepted connection from %s:%d", addr[0], addr[1])

            handler = ConnectionHandler(conn, addr, self._chunk_storage)
            handler.start()

        log.debug("Accept loop exited on port %d", self._port)


# ---------------------------------------------------------------------------
# Public integration interface
# ---------------------------------------------------------------------------

def start_peer_server(port: int, chunk_storage) -> PeerServer:
    """
    Create, start, and return a running PeerServer.

    Purpose
    -------
    This is the single entry point the Integration layer uses to bring up
    the local peer server.  It hides the PeerServer class so the caller
    only needs to know about this one function.

    Parameters
    ----------
    port : int
        TCP port to listen on (e.g. 8888).  Pass 0 for OS-assigned port.
    chunk_storage : object
        The Chunking module's storage object.  Must implement:
            get_chunk(file_id: str, chunk_index: int) -> bytes | None

    Returns
    -------
    PeerServer
        The running server instance.  Keep a reference so you can call
        server.stop() during shutdown.

    Example
    -------
    >>> from peer.peer_server import start_peer_server
    >>> server = start_peer_server(port=8888, chunk_storage=chunk_mgr)
    >>> print("Peer server running on port", server.port)
    >>> # on shutdown:
    >>> server.stop()
    """
    server = PeerServer(port=port, chunk_storage=chunk_storage)
    server.start()
    return server
