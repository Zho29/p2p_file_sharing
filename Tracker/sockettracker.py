# tracker.py
import socket
import threading
import json
import time
from protocol import parse_message, create_message, MessageType

class Tracker:
    def __init__(self, host='0.0.0.0', port=5000):
        self.host = host
        self.port = port
        self.file_index = {}
        self.peers = {}
        self.lock = threading.Lock()
        
    def start(self):
        """Start the tracker server"""
        server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_socket.bind((self.host, self.port))
        server_socket.listen(10)
        print(f"Tracker started on {self.host}:{self.port}")
        
        
        cleanup_thread = threading.Thread(target=self.cleanup_inactive_peers)
        cleanup_thread.daemon = True
        cleanup_thread.start()
        
        while True:
            client_socket, address = server_socket.accept()
            print(f"New connection from {address}")
            
            client_thread = threading.Thread(
                target=self.handle_client, 
                args=(client_socket, address)
            )
            client_thread.start()

    def handle_client(self, client_socket, address):
        """Handle incoming client requests"""
        try:
            while True:
                # Receive message (assuming max 4096 bytes)
                data = client_socket.recv(4096).decode('utf-8')
                if not data:
                    break
                    
                message = parse_message(data)
                if not message:
                    self.send_error(client_socket, "Invalid message format")
                    continue
                    
                msg_type = message.get('type')
                msg_data = message.get('data', {})
                
                # Process different message types
                if msg_type == MessageType.REGISTER_PEER:
                    self.handle_register_peer(client_socket, address, msg_data)
                elif msg_type == MessageType.ANNOUNCE_FILE:
                    self.handle_announce_file(client_socket, msg_data)
                elif msg_type == MessageType.GET_PEERS:
                    self.handle_get_peers(client_socket, msg_data)
                elif msg_type == MessageType.KEEP_ALIVE:
                    self.handle_keep_alive(client_socket, address, msg_data)
                else:
                    self.send_error(client_socket, f"Unknown message type: {msg_type}")
                    
        except Exception as e:
            print(f"Error handling client {address}: {e}")
        finally:
            client_socket.close()
            # Remove peer from active list
            self.remove_peer(address)

    def handle_register_peer(self, client_socket, address, data):
        """Register a new peer in the network"""
        peer_id = data.get('peer_id')
        peer_port = data.get('port')
        
        with self.lock:
            self.peers[peer_id] = {
                'ip': address[0],
                'port': peer_port,
                'last_seen': time.time()
            }
        
        response = create_message(
            MessageType.SUCCESS,
            {"message": "Peer registered successfully", "peer_id": peer_id}
        )
        client_socket.send(response.encode('utf-8'))
        print(f"Peer {peer_id} registered from {address[0]}:{peer_port}")

    def handle_announce_file(self, client_socket, data):
        """Handle file announcement from peers"""
        file_hash = data.get('file_hash')
        file_name = data.get('file_name')
        chunks = data.get('chunks', [])
        peer_id = data.get('peer_id')
        
        with self.lock:
            if file_hash not in self.file_index:
                self.file_index[file_hash] = {
                    'file_name': file_name,
                    'chunks': {}
                }
            
            # Register each chunk with the announcing peer
            for chunk_info in chunks:
                chunk_id = chunk_info['chunk_id']
                if chunk_id not in self.file_index[file_hash]['chunks']:
                    self.file_index[file_hash]['chunks'][chunk_id] = []
                
                if peer_id not in self.file_index[file_hash]['chunks'][chunk_id]:
                    self.file_index[file_hash]['chunks'][chunk_id].append(peer_id)
        
        response = create_message(
            MessageType.SUCCESS,
            {"message": f"File {file_name} registered with {len(chunks)} chunks"}
        )
        client_socket.send(response.encode('utf-8'))

    def handle_get_peers(self, client_socket, data):
        """Provide list of peers that have requested chunks"""
        file_hash = data.get('file_hash')
        chunk_ids = data.get('chunk_ids', [])
        
        with self.lock:
            if file_hash not in self.file_index:
                response = create_message(
                    MessageType.ERROR,
                    {"message": "File not found"}
                )
            else:
                # Get peers for requested chunks
                chunk_peers = {}
                file_info = self.file_index[file_hash]
                
                for chunk_id in chunk_ids:
                    if chunk_id in file_info['chunks']:
                        # Filter only active peers
                        active_peers = [
                            peer_id for peer_id in file_info['chunks'][chunk_id]
                            if peer_id in self.peers
                        ]
                        if active_peers:
                            # Get peer connection info
                            peer_info = []
                            for peer_id in active_peers:
                                if peer_id in self.peers:
                                    peer_info.append({
                                        # 'peer_id': peer_id,    peer id not mentioned in usage reference.md
                                        'ip': self.peers[peer_id]['ip'],
                                        'port': self.peers[peer_id]['port']
                                    })
                            chunk_peers[chunk_id] = peer_info
                
                response = create_message(
                    MessageType.SUCCESS,
                    {
                        'file_name': file_info['file_name'],
                        'chunk_peers': chunk_peers
                    }
                )
        
        client_socket.send(response.encode('utf-8'))
    def handle_keep_alive(self, client_socket, address, data):
        """Update peer's last seen timestamp"""
        peer_id = data.get('peer_id')
        
        with self.lock:
            if peer_id in self.peers:
                self.peers[peer_id]['last_seen'] = time.time()
                response = create_message(MessageType.SUCCESS, {"status": "alive"})
            else:
                response = create_message(MessageType.ERROR, {"message": "Peer not registered"})
        
        client_socket.send(response.encode('utf-8'))

    def cleanup_inactive_peers(self):
        """Remove peers that haven't sent keep-alive for 30 seconds"""
        while True:
            time.sleep(10)  # Check every 10 seconds
            current_time = time.time()
            with self.lock:
                inactive_peers = []
                for peer_id, peer_info in self.peers.items():
                    if current_time - peer_info['last_seen'] > 30:  # 30 seconds timeout
                        inactive_peers.append(peer_id)
                
                for peer_id in inactive_peers:
                    del self.peers[peer_id]
                    print(f"Removed inactive peer: {peer_id}")

    def remove_peer(self, address):
        """Remove peer when it disconnects"""
        with self.lock:
            for peer_id, peer_info in list(self.peers.items()):
                if peer_info['ip'] == address[0]:
                    del self.peers[peer_id]
                    print(f"Peer {peer_id} removed due to disconnection")
                    break
    def send_error(self, client_socket, message):
        """Send error message to client"""
        response = create_message(MessageType.ERROR, {"message": message})
        try:
            client_socket.send(response.encode('utf-8'))
        except:
            pass