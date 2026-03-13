import os

class Chunking:

    def __init__(self, storage_dir="./chunks", chunk_size=1024*1024):
        self.storage_dir = storage_dir
        self.chunk_size = chunk_size
        # print(f"Chunking initialized with chunk size: {self.chunk_size} bytes and storage directory: '{self.storage_dir}'")

    def get_chunk(self, file_path):
        
        file_id = os.path.basename(file_path)
        chunk_index = 0

        with open(file_path, "rb") as f:
            
            while True:
                data = f.read(self.chunk_size)

                if not data:
                    break
                
                self.save_chunk(file_id, chunk_index, data)

                chunk_index += 1
        
        return chunk_index
    
    def save_chunk(self, file_id, chunk_index, data):

        path = f"{self.storage_dir}/{file_id}/chunk_{chunk_index}"

        os.makedirs(os.path.dirname(path), exist_ok = True)

        with open(path, "wb") as f:
            f.write(data)
    
    def reconstruct(self, file_id, output_path):

        chunk_dir = f"{self.storage_dir}/{file_id}"

        chunks = sorted(os.listdir(chunk_dir))

        with open(output_path, "wb") as output:

            for chunk in chunks:
                chunk_path = f"{chunk_dir}/{chunk}"

                with open(chunk_path, "rb") as f:
                    output.write(f.read())

