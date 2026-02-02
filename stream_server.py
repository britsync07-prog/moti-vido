import os
from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import StreamingResponse
import mimetypes

app = FastAPI()

BASE_DIR = os.getcwd()
ALLOWED_DIRS = [
    os.path.join(BASE_DIR, "storage"),
    os.path.join(BASE_DIR, "users_data")
]

def is_safe_path(path: str):
    absolute_path = os.path.abspath(path)
    return any(absolute_path.startswith(allowed) for allowed in ALLOWED_DIRS) and os.path.exists(absolute_path)

@app.get("/stream")
async def stream_video(path: str, range: str = Header(None)):
    if not is_safe_path(path):
        raise HTTPException(status_code=404, detail="File not found or access denied")

    file_size = os.path.getsize(path)
    chunk_size = 1024 * 1024  # 1MB chunks

    start, end = 0, file_size - 1
    if range:
        try:
            start, end = range.replace("bytes=", "").split("-")
            start = int(start)
            end = int(end) if end else file_size - 1
        except ValueError:
            pass

    if start >= file_size:
         raise HTTPException(status_code=416, detail="Range not satisfiable")

    end = min(end, file_size - 1)
    length = end - start + 1

    def iterfile():
        with open(path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                read_size = min(chunk_size, remaining)
                data = f.read(read_size)
                if not data:
                    break
                remaining -= len(data)
                yield data

    headers = {
        "Content-Range": f"bytes {start}-{end}/{file_size}",
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
        "Content-Type": "video/mp4",
    }

    return StreamingResponse(iterfile(), status_code=206, headers=headers)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8002)
