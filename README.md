# P2P File Sharing Project

A peer-to-peer (P2P) file sharing system built with Python that enables direct file transfers between peers with a central Tracker server coordinating peer discovery.

## System Architecture

```
Tracker Server (port 5000)
  ├── Maintains peer registry
  ├── Tracks which chunks each peer has
  └── Responds to peer discovery queries

Peer A (Seeder)
  ├── Splits file into chunks
  ├── Registers with Tracker
  └── Serves chunks to downloaders

Peer B (Downloader)
  ├── Queries Tracker for chunk locations
  ├── Downloads chunks in parallel
  └── Reconstructs file locally
```

## Prerequisites

- Python 3.x
- No external dependencies required

## Project Structure

```
p2p_file_sharing/
├── README.md                          # This file
├── chunking.py                        # File chunking/reassembly logic
├── alice.txt.txt                      # Example file to share
├── integration/
│   ├── main.py                        # Main CLI entry point
│   ├── tracker_client.py              # Tracker communication client
│   └── chunk_storage_adapter.py       # Storage abstraction layer
├── peer/
│   ├── peer_server.py                 # P2P server (accepts incoming connections)
│   ├── peer_client.py                 # P2P client (connects to other peers)
│   ├── connection_handler.py          # Handles peer-to-peer transfers
│   ├── protocol.py                    # Wire protocol (4-byte header + JSON + binary)
│   ├── utilities.py                   # Helper functions
│   └── docs/usage_references.md       # Detailed API reference
└── Tracker/
    ├── sockettracker.py               # Tracker server (manages peer registry)
    └── protocol.py                    # Tracker protocol (JSON over TCP)
```

## Quick Start

### Step 1: Start the Tracker Server

Open a terminal and run:

```powershell
cd ~\p2p_file_sharing
python Tracker/sockettracker.py
```

Expected output:
```
Tracker started on 0.0.0.0:5000
```

The Tracker will now listen for peer registrations and queries.

### Step 2: Start a Seeder (Upload File)

Open a second terminal and run:

```powershell
cd ~\p2p_file_sharing
python integration/main.py seed alice.txt.txt --peer-id alice --port 8888
```

Expected output:
```
=== SEEDER starting ===
File      : .../alice.txt.txt
File ID   : alice.txt.txt
Peer port : 8888
Tracker   : 127.0.0.1:5000
Splitting file into chunks (size=1048576 bytes each)…
Split complete: 1 chunks created in ./chunks/alice.txt.txt/
Peer server listening on port 8888
Registered with Tracker as peer_id='alice'
Announced 1 chunks for file='alice.txt.txt' to Tracker
Seeder is ready. Press Ctrl-C to stop.
```

The seeder will now hold the file chunks and serve them to downloading peers.

### Step 3: Start a Downloader (Download File)

Open a third terminal and run:

```powershell
cd ~\p2p_file_sharing
python integration/main.py download alice.txt.txt --peer-id bob --port 8889 --out ./downloads
```

Expected output:
```
=== DOWNLOADER starting ===
File ID   : alice.txt.txt
Output    : .../downloads/alice.txt.txt
Peer port : 8889
Tracker   : 127.0.0.1:5000
Peer server listening on port 8889
Registered with Tracker as peer_id='bob'
Querying Tracker for chunk locations…
Tracker reports 1 chunks for file='alice.txt.txt'
Download context set for 1 chunks
Downloading 1 chunks (up to 4 in parallel)…
✓ chunk 0  (152136 bytes)
All 1 chunks downloaded successfully.
Reconstructing file → .../downloads/alice.txt.txt
File reconstructed successfully: .../downloads/alice.txt.txt
=== Download complete ===
```

The downloaded file will be saved to `./downloads/alice.txt.txt`.

## Command-Line Options

### seed (Seeding a File)

```powershell
python integration/main.py seed <file_path> [options]
```

**Required:**
- `<file_path>` — Path to the file to seed

**Options:**
- `--peer-id <name>` — Unique peer identifier (default: your hostname)
- `--port <number>` — TCP port for this peer's server (default: 8888)
- `--tracker-host <ip>` — Tracker hostname/IP (default: 127.0.0.1)
- `--tracker-port <number>` — Tracker port (default: 5000)
- `--chunk-size <bytes>` — Bytes per chunk (default: 1048576 = 1 MiB)
- `--storage-dir <path>` — Directory for storing chunks (default: ./chunks)


### download (Downloading a File)

```powershell
python integration/main.py download <file_id> [options]
```

**Required:**
- `<file_id>` — File identifier to download (must match what was seeded, e.g. "alice.txt.txt")

**Options:**
- Same as seed options, plus:
- `--out <path>` — Output directory for reconstructed file (default: ./downloads)

**Example:**
```powershell
python integration/main.py download alice.txt.txt --peer-id bob --port 8889 --out ./downloads
```

### Chunk Storage

When you seed a file, chunks are stored in:
```
./chunks/<file_id>/
├── chunk_0
├── chunk_1
├── chunk_2
└── ...
```

Example: `./chunks/alice.txt.txt/chunk_0`, `./chunks/alice.txt.txt/chunk_1`

### Downloaded Files

Downloaded files are placed in:
```
./downloads/<file_id>
```

Example: `./downloads/alice.txt.txt`

## Concurrency

- **Download parallelism:** Downloads up to 4 chunks simultaneously from all available seeders
- **Tracker registration:** Each peer registers once and sends keep-alive pings every 15 seconds
- **Peer timeout:** Peers are removed from registry if no keep-alive received for 30 seconds

## Troubleshooting

### "Tracker is not running" Error

**Solution:** Start the Tracker in a separate terminal:
```powershell
python Tracker/sockettracker.py
```

## Key Design Patterns

1. **Stateless TrackerClient:** Each message opens a fresh connection, enabling simple recovery
2. **Keep-alive based cleanup:** Peers stay registered unless they timeout (30 sec)
3. **Parallel chunk downloads:** Up to 4 simultaneous downloads for faster transfers
4. **SHA-256 integrity:** All peer-to-peer transfers are verified with checksums
5. **Modular architecture:** Chunking, P2P networking, and Tracker are separate concerns

