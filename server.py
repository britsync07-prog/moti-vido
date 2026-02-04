import os
import json
import uuid
import shutil
import asyncio
import subprocess
import uvicorn
import re
import math
import random
import requests
from fastapi import FastAPI, HTTPException, UploadFile, File, Form, BackgroundTasks
from pydantic import BaseModel
from typing import List, Optional
from fastapi.responses import FileResponse
from faster_whisper import WhisperModel
from playwright.async_api import async_playwright
from sqlalchemy import create_engine, Column, String, Text, DateTime, Integer, Boolean
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import datetime

app = FastAPI()

# --- CONFIGURATION ---
FPS = 30
WORKERS_COUNT = 1  
BASE_DIR = os.getcwd()
STORAGE_ROOT = os.path.join(BASE_DIR, "storage") # Root for temporary processing
USERS_ROOT = os.path.join(BASE_DIR, "users_data") # Root for persistent user videos
MUSIC_DIR = os.path.join(BASE_DIR, "music")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://user:password@db:5432/moti_db")
N8N_WEBHOOK_URL = os.getenv("N8N_WEBHOOK_URL", "http://n8n:5678/webhook/generate-video")

# Windows Path Fix
template_abs_path = os.path.join(BASE_DIR, 'template.html').replace("\\", "/")
TEMPLATE_PATH = f"file:///{template_abs_path}"

# Create directories
os.makedirs(STORAGE_ROOT, exist_ok=True)
os.makedirs(USERS_ROOT, exist_ok=True)
os.makedirs(MUSIC_DIR, exist_ok=True)

print("Loading Whisper Model...")
model = WhisperModel("small", device="cpu", compute_type="int8")
print("Whisper Model Loaded!")

# --- DATABASE SETUP ---
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    username = Column(String, unique=True, index=True)
    password_hash = Column(String)
    is_admin = Column(Boolean, default=False)
    is_suspended = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

class Post(Base):
    __tablename__ = "posts"
    id = Column(String, primary_key=True, index=True)
    user_id = Column(String, index=True, nullable=True)
    title = Column(String)
    script = Column(Text)
    music = Column(String, default="Random")
    status = Column(String, default="pending") # pending, published
    scheduled_time = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)

class VideoJob(Base):
    __tablename__ = "video_jobs"
    session_id = Column(String, primary_key=True)
    user_id = Column(String, index=True)
    is_temporary = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

# Create tables
Base.metadata.create_all(bind=engine)

# --- BACKGROUND SCHEDULER ---
async def check_scheduled_posts():
    """Checks for due posts every 60 seconds and triggers webhook."""
    while True:
        try:
            db = SessionLocal()
            now = datetime.utcnow()
            # Find pending posts that are due
            due_posts = db.query(Post).filter(
                Post.status == "pending",
                Post.scheduled_time <= now
            ).all()

            for post in due_posts:
                print(f"Triggering scheduled post: {post.title} ({post.id})")
                
                # Generate Session ID for this job
                session_id = str(uuid.uuid4())
                
                # Save VideoJob so we know who this session belongs to
                try:
                    job = VideoJob(session_id=session_id, user_id=post.user_id)
                    db.add(job)
                    db.commit()
                except Exception as e:
                    print(f"Error creating VideoJob for scheduled post: {e}")
                    continue

                payload = {
                    "type": "calendar_post",
                    "session_id": session_id,
                    "post_id": post.id,
                    "user_id": post.user_id,
                    "title": post.title,
                    "script": post.script,
                    "music": post.music,
                    "scheduled_time": post.scheduled_time.isoformat()
                }
                
                try:
                    # Send to N8N
                    response = requests.post(N8N_WEBHOOK_URL, json=payload, timeout=10)
                    if response.status_code == 200:
                        print(f"Webhook sent for {post.id}")
                        post.status = "published"
                        db.commit()
                    else:
                        print(f"Webhook failed for {post.id}: {response.status_code}")
                except Exception as e:
                    print(f"Error sending webhook for {post.id}: {e}")

            db.close()
        except Exception as e:
            print(f"Scheduler Error: {e}")
        
        await asyncio.sleep(60) # Check every minute

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(check_scheduled_posts())

# --- DATA MODELS ---
class GenerateRequest(BaseModel):
    session_id: str
    audio_filename: str
    image_filenames: List[str]
    video_mode: Optional[str] = "vertical"

class MergeRequest(BaseModel):
    session_id: str
    music_filename: Optional[str] = None 

class WebhookPost(BaseModel):
    title: str
    script: str
    upload_time: str  # ISO format string expected
    username: Optional[str] = None

# --- HELPER: Get Session Directory ---
def get_session_dir(session_id: str):
    # Always use temporary storage for processing
    session_dir = os.path.join(STORAGE_ROOT, session_id)
    os.makedirs(session_dir, exist_ok=True)
    return session_dir

# --- HELPER: Wipe Storage ---
def cleanup_session_storage(session_id: str, exclude_files: List[str] = []):
    print(f"Starting auto-cleanup for session {session_id} (Excluding: {exclude_files})...")
    session_dir = os.path.join(STORAGE_ROOT, session_id)
    try:
        if os.path.exists(session_dir):
            for filename in os.listdir(session_dir):
                if filename in exclude_files: continue
                
                file_path = os.path.join(session_dir, filename)
                try:
                    if os.path.isfile(file_path) or os.path.islink(file_path):
                        os.unlink(file_path)
                    elif os.path.isdir(file_path):
                        shutil.rmtree(file_path)
                except Exception as e:
                    print(f"Failed to delete {file_path}: {e}")
            print(f"Session {session_id} storage cleared.")
    except Exception as e:
        print(f"Error during storage cleanup: {e}")

# --- HELPER: Get Next Segment Number ---
def get_next_segment_filename(session_dir: str):
    files = os.listdir(session_dir)
    max_num = 0
    pattern = re.compile(r"^(\d+)\.mp4$")
    for f in files:
        match = pattern.match(f)
        if match:
            num = int(match.group(1))
            if num > max_num: max_num = num
    return f"{max_num + 1}.mp4"

# --- ENDPOINTS ---

@app.post("/webhook/calendar")
async def webhook_calendar(post: WebhookPost):
    db = SessionLocal()
    try:
        if not post.username:
            raise HTTPException(status_code=400, detail="Field 'username' is required")

        user = db.query(User).filter(User.username == post.username).first()
        if not user:
            raise HTTPException(status_code=404, detail=f"User '{post.username}' not found")
        
        user_id = user.id
        
        try:
            scheduled = datetime.fromisoformat(post.upload_time.replace('Z', '+00:00'))
        except:
            scheduled = datetime.now() # Fallback

        new_post = Post(
            id=str(uuid.uuid4()),
            user_id=user_id,
            title=post.title,
            script=post.script,
            scheduled_time=scheduled
        )
        db.add(new_post)
        db.commit()
        db.refresh(new_post)
        return {"status": "success", "id": new_post.id}
    except HTTPException as he:
        raise he
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

@app.get("/webhook/calendar")
async def get_calendar_posts():
    db = SessionLocal()
    try:
        posts = db.query(Post).order_by(Post.scheduled_time).all()
        return {
            "status": "success",
            "data": [
                {
                    "id": p.id,
                    "title": p.title,
                    "script": p.script,
                    "scheduled_time": p.scheduled_time.isoformat(),
                    "created_at": p.created_at.isoformat()
                } for p in posts
            ]
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

@app.post("/upload-final")
async def upload_final_video(
    session_id: str = Form(...), 
    filename: str = Form("final_video.mp4"),
    file: UploadFile = File(None)
):
    try:
        print(f"Finalizing session: {session_id}")
        
        # 1. Find User ID
        db = SessionLocal()
        user_id = None
        try:
            job = db.query(VideoJob).filter(VideoJob.session_id == session_id).first()
            if job:
                user_id = job.user_id
        except Exception as e:
            print(f"DB Lookup Error: {e}")
        finally:
            db.close()

        if not user_id:
            print(f"User not found for session {session_id}")
            # We can't save to a user folder if we don't know the user. 
            # But we can leave it in temp or save to an 'orphan' folder.
            # For now, let's proceed with a fallback or error.
            # Choosing to try and save to users_data/unknown for debugging
            user_id = "unknown_user"

        # 2. Prepare Destination
        dest_dir = os.path.join(USERS_ROOT, user_id, session_id)
        os.makedirs(dest_dir, exist_ok=True)
        dest_path = os.path.join(dest_dir, "final_video.mp4") # Always rename to standard name

        # 3. Handle Source
        if file:
            # Case A: File was uploaded via form-data
            print("Receiving file upload...")
            with open(dest_path, "wb") as buffer:
                shutil.copyfileobj(file.file, buffer)
        else:
            # Case B: File is already in temp storage (server-side copy)
            source_path = os.path.join(STORAGE_ROOT, session_id, filename)
            print(f"Looking for local file: {source_path}")
            
            if not os.path.exists(source_path):
                raise HTTPException(status_code=404, detail=f"Video file not found in temp storage: {filename}")
            
            shutil.copy2(source_path, dest_path)
            print(f"Copied local file to {dest_path}")

        return {
            "status": "success", 
            "message": "Final video saved to user profile", 
            "session_id": session_id, 
            "path": dest_path
        }

    except Exception as e:
        print(f"Upload-final Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/upload")
async def upload_file(session_id: str = Form(...), file: UploadFile = File(...)):
    try:
        session_dir = get_session_dir(session_id)
        file_ext = os.path.splitext(file.filename)[1]
        unique_name = f"{uuid.uuid4()}{file_ext}"
        file_path = os.path.join(session_dir, unique_name)
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        return {"status": "success", "filename": unique_name, "type": "storage", "session_id": session_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/upload-music")
async def upload_music(file: UploadFile = File(...)):
    try:
        file_path = os.path.join(MUSIC_DIR, file.filename)
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        return {"status": "success", "filename": file.filename, "type": "music"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/delete/{session_id}")
@app.post("/delete/{session_id}")
async def delete_session_files(session_id: str):
    # Strictly target the temporary processing storage
    session_dir = os.path.join(STORAGE_ROOT, session_id)
    
    if os.path.exists(session_dir):
        try:
            shutil.rmtree(session_dir)
            return {"status": "success", "message": f"Temporary processing files for session {session_id} deleted"}
        except Exception as e:
            print(f"Error deleting session dir: {e}")
            raise HTTPException(status_code=500, detail=f"Failed to delete session files: {e}")
    else:
         return {"status": "success", "message": f"Session {session_id} not found in temp storage (already cleaned?)"}

@app.get("/list-files/{session_id}")
async def list_files(session_id: str):
    session_dir = os.path.join(STORAGE_ROOT, session_id)
    storage_files = os.listdir(session_dir) if os.path.exists(session_dir) else []
    music_files = os.listdir(MUSIC_DIR) if os.path.exists(MUSIC_DIR) else []
    return {"status": "success", "storage": storage_files, "music": music_files}

# --- PARALLEL RENDER LOGIC (No Music Here) ---
async def render_chunk(browser, chunk_id, start_frame, end_frame, word_data, image_paths, total_frames, frames_dir, width, height, mode):
    page = await browser.new_page(viewport={"width": width, "height": height}, device_scale_factor=1)
    await page.goto(TEMPLATE_PATH)
    await page.evaluate(f"loadWords({json.dumps(word_data)})")
    await page.evaluate(f"setMode('{mode}')")

    num_images = len(image_paths)
    frames_per_image = total_frames / num_images 

    for frame in range(start_frame, end_frame):
        time = frame / FPS
        img_index = int(frame / frames_per_image)
        if img_index >= num_images: img_index = num_images - 1
        
        img_abs_path = image_paths[img_index].replace("\\", "/")
        abs_img_url = f"file:///{img_abs_path}"
        
        await page.evaluate(f"setBg('{abs_img_url}')")
        await page.evaluate(f"updateFrame({time})")
        
        await page.screenshot(
            path=os.path.join(frames_dir, f"frame_{frame:04d}.jpg"), 
            type="jpeg", quality=80
        )
    await page.close()

async def render_logic(audio_path, image_paths, output_path, unique_id, session_dir, mode="vertical"):
    # 1. TRANSCRIBE
    print("Transcribing audio...")
    segments, info = model.transcribe(audio_path, word_timestamps=True)
    duration = info.duration
    word_data = [{"word": w.word.strip(), "start": w.start, "end": w.end} for s in segments for w in s.words]

    # 2. RENDER FRAMES
    frames_dir = os.path.join(session_dir, f"{unique_id}_frames")
    os.makedirs(frames_dir, exist_ok=True)
    total_frames = int(duration * FPS)

    if mode == "horizontal":
        width, height = 1920, 1080
    else:
        width, height = 1080, 1920
    
    if len(image_paths) == 0: raise Exception("No images provided")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        chunk_size = math.ceil(total_frames / WORKERS_COUNT)
        tasks = []
        for i in range(WORKERS_COUNT):
            start = i * chunk_size
            end = min((i + 1) * chunk_size, total_frames)
            if start >= end: break
            tasks.append(render_chunk(browser, i, start, end, word_data, image_paths, total_frames, frames_dir, width, height, mode))
        
        await asyncio.gather(*tasks)
        await browser.close()

    # 3. MERGE VIDEO ONLY (No Music yet)
    print("Merging frames with Audio...")
    ffmpeg_cmd = [
        "ffmpeg", "-y", "-framerate", str(FPS),
        "-i", os.path.join(frames_dir, "frame_%04d.jpg"), 
        "-i", audio_path,
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-b:v", "1000k", "-maxrate", "1000k", "-bufsize", "2000k", "-preset", "veryfast",
        "-c:a", "aac", "-ac", "2", "-ar", "44100", "-b:a", "64k", 
        "-movflags", "+faststart",
        "-shortest", output_path
    ]
    
    startupinfo = None
    if os.name == 'nt':
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

    process = await asyncio.create_subprocess_exec(
        *ffmpeg_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, startupinfo=startupinfo
    )
    stdout, stderr = await process.communicate()
    
    if process.returncode != 0:
        print(f"FFmpeg Error: {stderr.decode()}")
        raise Exception("FFmpeg generation failed")

    try: shutil.rmtree(frames_dir)
    except: pass

@app.post("/create-segment")
async def create_segment(req: GenerateRequest):
    unique_id = str(uuid.uuid4())
    session_dir = get_session_dir(req.session_id)
    
    audio_full_path = os.path.join(session_dir, req.audio_filename)
    image_full_paths = [os.path.join(session_dir, img) for img in req.image_filenames]
    
    output_filename = get_next_segment_filename(session_dir)
    output_full_path = os.path.join(session_dir, output_filename)

    if not os.path.exists(audio_full_path):
        raise HTTPException(404, detail=f"Audio file not found")

    try:
        await render_logic(audio_full_path, image_full_paths, output_full_path, unique_id, session_dir, req.video_mode)
        return {"status": "segment_saved", "filename": output_filename, "session_id": req.session_id}
    except Exception as e:
        print(f"Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/merge-all")
async def merge_all_videos(req: MergeRequest, background_tasks: BackgroundTasks):
    try:
        session_dir = get_session_dir(req.session_id)
        
        # 1. IDENTIFY SEGMENTS
        files = os.listdir(session_dir)
        pattern = re.compile(r"^(\d+)\.mp4$")
        segments = sorted([int(pattern.match(f).group(1)) for f in files if pattern.match(f)])
        
        if not segments: raise HTTPException(404, detail="No segments found.")
            
        list_path = os.path.join(session_dir, "merge_list.txt")
        # Temporary video-only file
        temp_merged = os.path.join(session_dir, "temp_concat.mp4")
        # Final output
        final_output = "final_video.mp4"
        final_full_path = os.path.join(session_dir, final_output)

        # 2. CREATE CONCAT LIST
        with open(list_path, "w") as f:
            for num in segments:
                safe_path = os.path.join(session_dir, f"{num}.mp4").replace("\\", "/")
                f.write(f"file '{safe_path}'\n")

        # 3. CONCAT SEGMENTS (Video + Voice only)
        startupinfo = None
        if os.name == 'nt':
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

        concat_cmd = [
            "ffmpeg", "-y", "-f", "concat", "-safe", "0",
            "-i", list_path, "-c", "copy", temp_merged
        ]
        
        print("Concatenating segments...")
        process = await asyncio.create_subprocess_exec(
            *concat_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, startupinfo=startupinfo
        )
        await process.communicate()
        if process.returncode != 0: raise Exception("Concatenation failed")

        # 4. HANDLE MUSIC
        music_file = None
        
        # Determine Music Choice
        if req.music_filename and req.music_filename.lower() in ["none", "skip", "off"]:
            music_file = None
        elif req.music_filename and req.music_filename.lower() != "random":
             specific = os.path.join(MUSIC_DIR, req.music_filename)
             if os.path.exists(specific): music_file = specific
        else:
            # Random logic (Default if not specified or 'random')
            available = [f for f in os.listdir(MUSIC_DIR) if f.endswith((".mp3", ".wav", ".m4a"))]
            if available:
                music_file = os.path.join(MUSIC_DIR, random.choice(available))

        # 5. FINAL MERGE
        if music_file:
            print(f"Adding music: {os.path.basename(music_file)}")
            mix_cmd = [
                "ffmpeg", "-y",
                "-i", temp_merged,                    # [0] Video + Voice
                "-stream_loop", "-1", "-i", music_file, # [1] Music (Looping)
                "-filter_complex", 
                "[1:a]volume=0.12[bg];[0:a][bg]amix=inputs=2:duration=first[a_out]",
                "-map", "0:v", "-map", "[a_out]",
                "-c:v", "copy", "-c:a", "aac", 
                "-movflags", "+faststart",
                "-shortest",
                final_full_path
            ]
            
            process = await asyncio.create_subprocess_exec(
                *mix_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, startupinfo=startupinfo
            )
            await process.communicate()
            if process.returncode != 0: raise Exception("Music mixing failed")
            
        else:
            # No music, just rename temp to final
            if os.path.exists(final_full_path): os.remove(final_full_path)
            os.rename(temp_merged, final_full_path)

        # Cleanup helpers
        if os.path.exists(list_path): os.remove(list_path)
        if os.path.exists(temp_merged): os.remove(temp_merged) # Clean temp concat

        # Schedule full cleanup (except the result)
        background_tasks.add_task(cleanup_session_storage, req.session_id, exclude_files=[final_output])
        
        return FileResponse(final_full_path, media_type="video/mp4", filename="final_video.mp4")

    except Exception as e:
        print(f"Merge Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)