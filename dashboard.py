import streamlit as st
import uuid
import requests
import os
import glob
import hashlib
import subprocess
import shutil
from datetime import datetime, time
from sqlalchemy import create_engine, Column, String, Text, DateTime, Boolean
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

# --- CONFIGURATION ---
STORAGE_ROOT = os.getenv("STORAGE_ROOT", "/app/storage")
USERS_ROOT = os.getenv("USERS_ROOT", "/app/users_data")
MUSIC_DIR = os.getenv("MUSIC_DIR", "/app/music")
N8N_WEBHOOK_URL = os.getenv("N8N_WEBHOOK_URL", "http://n8n:5678/webhook/generate-video")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://user:password@db:5432/moti_db")
MAX_VIDEOS_PER_USER = 5

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
    id = Column(String, primary_key=True)
    user_id = Column(String, index=True)
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

# Ensure tables exist
Base.metadata.create_all(bind=engine)

# --- AUTH HELPERS ---
SALT = "moti_secure_salt_v1"

def make_hash(password):
    return hashlib.sha256((password + SALT).encode()).hexdigest()

def check_hash(password, hash_val):
    return make_hash(password) == hash_val

def init_db():
    db = SessionLocal()
    try:
        # Check if users exist. If not, create default admin.
        if db.query(User).count() == 0:
            admin = User(
                username="admin",
                password_hash=make_hash("admin123"),
                is_admin=True
            )
            db.add(admin)
            db.commit()
            print("Initialized default admin account.")
    except Exception as e:
        print(f"DB Init Error: {e}")
    finally:
        db.close()

# Initialize DB on start
init_db()

# --- STREAMLIT CONFIG ---
st.set_page_config(page_title="Video Generator", layout="wide")

# --- HELPER FUNCTIONS ---

def cleanup_user_videos(user_id):
    """Enforces storage limits: 5 Persistent Videos, 1 Temporary Video."""
    db = SessionLocal()
    try:
        # 1. Clean up Persistent Videos (Limit 5)
        persistent_jobs = db.query(VideoJob).filter(
            VideoJob.user_id == user_id, 
            VideoJob.is_temporary == False
        ).order_by(VideoJob.created_at.desc()).all()
        
        if len(persistent_jobs) > MAX_VIDEOS_PER_USER:
            for job in persistent_jobs[MAX_VIDEOS_PER_USER:]:
                _delete_job_files(db, job)

        # 2. Clean up Temporary Videos (Limit 1)
        temp_jobs = db.query(VideoJob).filter(
            VideoJob.user_id == user_id, 
            VideoJob.is_temporary == True
        ).order_by(VideoJob.created_at.desc()).all()
        
        # Keep only the newest temp job (index 0), delete the rest
        if len(temp_jobs) > 1:
            for job in temp_jobs[1:]:
                _delete_job_files(db, job)
            
        db.commit()
    except Exception as e:
        print(f"Cleanup Error: {e}")
    finally:
        db.close()

def _delete_job_files(db, job):
    """Helper to delete files and DB entry for a job."""
    session_dir = os.path.join(USERS_ROOT, job.user_id, job.session_id)
    if os.path.exists(session_dir):
        try:
            shutil.rmtree(session_dir)
            print(f"Deleted old session: {job.session_id}")
        except Exception as e:
            print(f"Error deleting directory {session_dir}: {e}")
    db.delete(job)

def generate_thumbnail(video_path, output_path):
    """Generates a thumbnail for a video using ffmpeg."""
    if os.path.exists(output_path):
        return True
    
    try:
        cmd = [
            "ffmpeg", "-y",
            "-i", video_path,
            "-ss", "00:00:01",
            "-vframes", "1",
            output_path
        ]
        subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        return True
    except Exception as e:
        print(f"Thumbnail generation failed: {e}")
        return False

# --- AUTH LOGIC ---
if 'user_id' not in st.session_state:
    st.session_state.user_id = None
if 'is_admin' not in st.session_state:
    st.session_state.is_admin = False
if 'selected_video' not in st.session_state:
    st.session_state.selected_video = None
if 'current_generation_id' not in st.session_state:
    st.session_state.current_generation_id = None

def login_user(username, password):
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == username).first()
        if user and check_hash(password, user.password_hash):
            if user.is_suspended:
                return False, "Account suspended."
            return True, user
        return False, "Invalid username or password."
    finally:
        db.close()

def logout():
    st.session_state.user_id = None
    st.session_state.is_admin = False
    st.session_state.selected_video = None
    st.rerun()

# --- PAGES ---

def page_login():
    st.title("🔐 Login")
    with st.form("login_form"):
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Login")
        
        if submitted:
            success, result = login_user(username, password)
            if success:
                st.session_state.user_id = result.id
                st.session_state.is_admin = result.is_admin
                st.success("Logged in!")
                st.rerun()
            else:
                st.error(result)

def page_admin_panel():
    st.header("🛡️ Admin Panel")
    
    # Create User
    with st.expander("Create New User"):
        with st.form("create_user"):
            new_user = st.text_input("Username")
            new_pass = st.text_input("Password", type="password")
            is_admin = st.checkbox("Is Admin?")
            if st.form_submit_button("Create"):
                if new_user and new_pass:
                    db = SessionLocal()
                    try:
                        if db.query(User).filter(User.username == new_user).first():
                            st.error("Username already exists.")
                        else:
                            u = User(
                                username=new_user,
                                password_hash=make_hash(new_pass),
                                is_admin=is_admin
                            )
                            db.add(u)
                            db.commit()
                            st.success(f"User {new_user} created.")
                    except Exception as e:
                        st.error(str(e))
                    finally:
                        db.close()
                else:
                    st.error("Username and Password required.")

    # List Users
    st.subheader("User Management")
    db = SessionLocal()
    users = db.query(User).all()
    db.close()
    
    for u in users:
        col1, col2, col3, col4, col5 = st.columns([1, 2, 1, 1, 1])
        col1.write(f"**{u.username}**")
        col2.write(f"ID: {u.id[:8]}...")
        col3.write("Admin" if u.is_admin else "User")
        col4.write("Suspended" if u.is_suspended else "Active")
        
        with col5:
            if u.username != "admin": # Protect main admin slightly
                if u.is_suspended:
                    if st.button("Unsuspend", key=f"unsus_{u.id}"):
                        db = SessionLocal()
                        user = db.query(User).get(u.id)
                        user.is_suspended = False
                        db.commit()
                        db.close()
                        st.rerun()
                else:
                    if st.button("Suspend", key=f"sus_{u.id}"):
                        db = SessionLocal()
                        user = db.query(User).get(u.id)
                        user.is_suspended = True
                        db.commit()
                        db.close()
                        st.rerun()
                
                if st.button("Delete", key=f"del_{u.id}"):
                    db = SessionLocal()
                    user = db.query(User).get(u.id)
                    db.delete(user)
                    db.commit()
                    db.close()
                    st.rerun()

def page_create_video():
    # Enforce cleanup on load
    cleanup_user_videos(st.session_state.user_id)

    # --- GALLERY DATA FETCH ---
    if os.path.exists(STORAGE_ROOT):
        db = SessionLocal()
        try:
            # Only show persistent jobs (is_temporary=False)
            user_jobs = db.query(VideoJob).filter(
                VideoJob.user_id == st.session_state.user_id,
                VideoJob.is_temporary == False
            ).order_by(VideoJob.created_at.desc()).all()
        except Exception:
            user_jobs = []
        finally:
            db.close()

        found_videos = []
        for job in user_jobs:
            # Check persistent storage: users_data/user_id/session_id
            vid_path = os.path.join(USERS_ROOT, st.session_state.user_id, job.session_id, "final_video.mp4")
            
            if os.path.exists(vid_path):
                # Ensure thumbnail exists
                thumb_path = os.path.join(os.path.dirname(vid_path), "thumb.jpg")
                generate_thumbnail(vid_path, thumb_path)
                found_videos.append({
                    "session_id": job.session_id,
                    "video_path": vid_path,
                    "thumb_path": thumb_path,
                    "date": job.created_at
                })
    else:
        found_videos = []

    # --- MAIN: Input ---
    st.header("✨ Create New Video")

    # Upload Custom Music
    with st.expander("Upload Custom Music"):
        uploaded_file = st.file_uploader("Choose an audio file", type=['mp3', 'wav', 'm4a'])
        if uploaded_file is not None:
            if not os.path.exists(MUSIC_DIR):
                os.makedirs(MUSIC_DIR)
            file_path = os.path.join(MUSIC_DIR, uploaded_file.name)
            with open(file_path, "wb") as f:
                f.write(uploaded_file.getbuffer())
            st.success(f"Uploaded: {uploaded_file.name}")

    # Music Selection
    music_options = ["None", "Random"]
    if os.path.exists(MUSIC_DIR):
        music_files = [f for f in os.listdir(MUSIC_DIR) if f.endswith(('.mp3', '.wav', '.m4a'))]
        music_options.extend(music_files)

    col1, col2 = st.columns([2, 1])
    with col1:
        music_choice = st.selectbox("Select Background Music", music_options)

    if music_choice and music_choice not in ["None", "Random"]:
        audio_path = os.path.join(MUSIC_DIR, music_choice)
        if os.path.exists(audio_path):
            st.write(f"🎧 **Audio Preview:** {music_choice}")
            st.info("💡 Note: Please ensure your device volume is turned up for the best experience.")
            try:
                with open(audio_path, "rb") as f:
                    audio_bytes = f.read()
                st.audio(audio_bytes)
            except Exception as e:
                st.error(f"Error loading audio: {e}")

    with st.form("video_form"):
        script = st.text_area("Enter Video Script", height=200, placeholder="Type your script here...")
        submitted = st.form_submit_button("Generate Video")

    if submitted:
        if not script:
            st.error("Please enter a script.")
        else:
            session_id = str(uuid.uuid4())
            payload = {
                "type": "video_generation",
                "session_id": session_id,
                "script": script,
                "music": music_choice
            }
            try:
                # Save job ownership (Temporary Job)
                db = SessionLocal()
                try:
                    job = VideoJob(
                        session_id=session_id, 
                        user_id=st.session_state.user_id,
                        is_temporary=True
                    )
                    db.add(job)
                    db.commit()
                except Exception as e:
                    print(f"DB Error saving job: {e}")
                finally:
                    db.close()
                
                # Set tracking ID for UI
                st.session_state.current_generation_id = session_id
                
                st.info(f"Starting job {session_id}...")
                response = requests.post(N8N_WEBHOOK_URL, json=payload)
                if response.status_code == 200:
                    st.success("Job submitted! Please wait for the result below.")
                    st.rerun() 
                else:
                    st.error(f"Error submitting job: {response.status_code} - {response.text}")
            except Exception as e:
                st.error(f"Connection error: {e}")

    # --- LATEST GENERATION STATUS (Under the button) ---
    if st.session_state.current_generation_id:
        gen_id = st.session_state.current_generation_id
        temp_path = os.path.join(USERS_ROOT, st.session_state.user_id, gen_id, "final_video.mp4")
        
        st.markdown("### 📽️ Generation Status")
        
        if os.path.exists(temp_path):
            st.success("✅ Video Ready!")
            st.warning("⚠️ This is a temporary preview. Download it now.")
            
            c_v1, c_v2, c_v3 = st.columns([1, 1, 1])
            with c_v2:
                st.video(temp_path)
                with open(temp_path, "rb") as file:
                     st.download_button(
                        label="⬇️ Download Video",
                        data=file,
                        file_name=f"video_{gen_id[:8]}.mp4",
                        mime="video/mp4"
                    )
                if st.button("✨ Generate Another"):
                    # Explicitly clean up this temp job immediately
                    db = SessionLocal()
                    try:
                        job = db.query(VideoJob).filter(VideoJob.session_id == st.session_state.current_generation_id).first()
                        if job:
                            _delete_job_files(db, job)
                            db.commit()
                    except Exception as e:
                        print(f"Error deleting temp job: {e}")
                    finally:
                        db.close()
                        
                    st.session_state.current_generation_id = None
                    st.rerun()
        else:
            col_status, col_ref = st.columns([3, 1])
            col_status.info("🔄 Checking for video result... Please wait (this can take 1-2 minutes).")
            if col_ref.button("🔄 Refresh Status"):
                st.rerun()

    st.markdown("---")
    page_saved_videos_section(found_videos)

def page_saved_videos_section(found_videos):
    # 1. Video Player Area (Top - Permanent)
    st.header("🎬 Your Saved Videos")
    
    # Check if a video is selected (do not default to first one)
    if st.session_state.selected_video:
        current_vid = next((v for v in found_videos if v['session_id'] == st.session_state.selected_video), None)
        
        if current_vid:
            # Use columns to center and constrain width (medium size)
            c1, c2, c3 = st.columns([1, 1, 1])
            
            with c2:
                # Close button aligned to right of the center column
                col_title, col_btn = st.columns([3, 1])
                with col_title:
                    st.subheader("Now Playing")
                with col_btn:
                     if st.button("❌ Close", key="close_player_top"):
                        st.session_state.selected_video = None
                        st.rerun()

                st.video(current_vid['video_path'])
                
                with open(current_vid['video_path'], "rb") as file:
                    st.download_button(
                        label="⬇️ Download Video",
                        data=file,
                        file_name=f"video_{current_vid['session_id']}.mp4",
                        mime="video/mp4",
                        key=f"dl_player_{current_vid['session_id']}"
                    )

                st.caption(f"Session ID: {current_vid['session_id']} | Created: {current_vid['date'].strftime('%Y-%m-%d %H:%M')}")
            
            st.markdown("---")

    elif not found_videos:
        st.info("No videos generated yet. Create one below!")
        st.markdown("---")

    # 2. Thumbnail Grid
    if found_videos:
        st.subheader("Gallery")
        cols = st.columns(5) # Grid of 5
        for idx, vid in enumerate(found_videos):
            col = cols[idx % 5]
            with col:
                if os.path.exists(vid['thumb_path']):
                    st.image(vid['thumb_path'], use_container_width=True)
                else:
                    st.write("🎬") # Fallback icon
                
                # Button to select video
                if st.button(f"Watch #{idx+1}", key=f"btn_{vid['session_id']}"):
                    st.session_state.selected_video = vid['session_id']
                    st.rerun()

                # Download Button for Gallery
                with open(vid['video_path'], "rb") as file:
                    st.download_button(
                        label="⬇️ Download",
                        data=file,
                        file_name=f"video_{vid['session_id']}.mp4",
                        mime="video/mp4",
                        key=f"dl_gallery_{vid['session_id']}"
                    )

def page_content_calendar():
    st.header("📅 Content Calendar")
    st.markdown("Plan and view your upcoming video posts.")
    
    # Music Options
    music_options = ["Random", "None"]
    if os.path.exists(MUSIC_DIR):
        music_files = [f for f in os.listdir(MUSIC_DIR) if f.endswith(('.mp3', '.wav', '.m4a'))]
        music_options.extend(music_files)
    
    with st.expander("➕ Schedule New Post", expanded=False):
        with st.form("calendar_form"):
            c_title = st.text_input("Video Title")
            c_script = st.text_area("Video Script snippet (or full script)", height=100)
            c_music = st.selectbox("Select Music", music_options)
            
            col_d, col_t = st.columns(2)
            with col_d:
                c_date = st.date_input("Upload Date", min_value=datetime.today())
            with col_t:
                c_time = st.time_input("Upload Time", value=time(12, 0))
            submitted_cal = st.form_submit_button("Add to Calendar")
            if submitted_cal:
                if c_title and c_date:
                    db = SessionLocal()
                    try:
                        new_post = Post(
                            id=str(uuid.uuid4()),
                            user_id=st.session_state.user_id,
                            title=c_title,
                            script=c_script,
                            music=c_music,
                            status="pending",
                            scheduled_time=datetime.combine(c_date, c_time)
                        )
                        db.add(new_post)
                        db.commit()
                        st.success("Scheduled successfully!")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Database Error: {e}")
                    finally:
                        db.close()
                else:
                    st.error("Title and Date are required.")

    # Fetch User Posts
    db = SessionLocal()
    pending_posts = []
    history_posts = []
    try:
        all_posts = db.query(Post).filter(Post.user_id == st.session_state.user_id).order_by(Post.scheduled_time).all()
        for p in all_posts:
            if p.status == "published":
                history_posts.append(p)
            else:
                pending_posts.append(p)
    except Exception as e:
        st.error(f"Error connecting to database: {e}")
    finally:
        db.close()
    
    # TABS
    tab1, tab2 = st.tabs(["🕒 Upcoming", "✅ History"])

    with tab1:
        if not pending_posts:
            st.info("No upcoming posts scheduled.")
        else:
            for item in pending_posts:
                with st.container():
                    with st.expander(f"📝 {item.title} ({item.scheduled_time.strftime('%Y-%m-%d %H:%M')})"):
                        with st.form(f"edit_{item.id}"):
                            e_title = st.text_input("Title", value=item.title)
                            e_script = st.text_area("Script", value=item.script)
                            
                            # Handle existing music value safely
                            current_music = item.music if item.music in music_options else "Random"
                            e_music = st.selectbox("Music", music_options, index=music_options.index(current_music))
                            
                            e_date = st.date_input("Date", value=item.scheduled_time.date())
                            e_time = st.time_input("Time", value=item.scheduled_time.time())
                            c1, c2, c3 = st.columns(3)
                            with c1:
                                if st.form_submit_button("Save Changes"):
                                    db_update = SessionLocal()
                                    try:
                                        p = db_update.query(Post).filter(Post.id == item.id).first()
                                        p.title = e_title
                                        p.script = e_script
                                        p.music = e_music
                                        p.scheduled_time = datetime.combine(e_date, e_time)
                                        db_update.commit()
                                        st.success("Updated!")
                                        st.rerun()
                                    except Exception as e:
                                        st.error(f"Update failed: {e}")
                                    finally:
                                        db_update.close()
                            with c2:
                                if st.form_submit_button("🚀 Post Now"):
                                    db_now = SessionLocal()
                                    try:
                                        # 1. Update/Save current edits first
                                        p = db_now.query(Post).filter(Post.id == item.id).first()
                                        p.title = e_title
                                        p.script = e_script
                                        p.music = e_music
                                        p.scheduled_time = datetime.combine(e_date, e_time)
                                        db_now.commit()

                                        # 2. Generate Session & Job
                                        session_id = str(uuid.uuid4())
                                        job = VideoJob(session_id=session_id, user_id=p.user_id)
                                        db_now.add(job)
                                        db_now.commit()

                                        # 3. Send Webhook
                                        payload = {
                                            "type": "calendar_post",
                                            "session_id": session_id,
                                            "post_id": p.id,
                                            "user_id": p.user_id,
                                            "title": p.title,
                                            "script": p.script,
                                            "music": p.music,
                                            "scheduled_time": p.scheduled_time.isoformat()
                                        }
                                        
                                        st.info("Sending to n8n...")
                                        response = requests.post(N8N_WEBHOOK_URL, json=payload, timeout=10)
                                        
                                        if response.status_code == 200:
                                            # 3. Mark as published
                                            p.status = "published"
                                            db_now.commit()
                                            st.success("Posted successfully!")
                                            st.rerun()
                                        else:
                                            st.error(f"Webhook failed: {response.status_code} - {response.text}")
                                            
                                    except Exception as e:
                                        st.error(f"Error: {e}")
                                    finally:
                                        db_now.close()
                            with c3:
                                if st.form_submit_button("Delete Post", type="primary"):
                                    db_del = SessionLocal()
                                    try:
                                        p = db_del.query(Post).filter(Post.id == item.id).first()
                                        db_del.delete(p)
                                        db_del.commit()
                                        st.success("Deleted!")
                                        st.rerun()
                                    except:
                                        pass
                                    finally:
                                        db_del.close()

    with tab2:
        if not history_posts:
            st.info("No history yet.")
        else:
            for item in history_posts:
                st.markdown(f"**{item.title}**")
                st.caption(f"Published: {item.scheduled_time.strftime('%Y-%m-%d %H:%M')}")
                with st.expander("View Details"):
                     st.write(f"**Script:** {item.script}")
                     st.write(f"**Music:** {item.music}")
                st.markdown("---")
if st.session_state.user_id is None:
    page_login()
else:
    # Sidebar Navigation
    nav_options = ["Create Video", "Content Calendar"]
    if st.session_state.is_admin:
        nav_options.append("Admin Panel")
    
    st.sidebar.title("Moti Generator")
    selected_page = st.sidebar.radio("Menu", nav_options)
    
    if st.sidebar.button("Logout"):
        logout()
    
    if selected_page == "Create Video":
        page_create_video()
    elif selected_page == "Content Calendar":
        page_content_calendar()
    elif selected_page == "Admin Panel":
        page_admin_panel()

st.markdown("---")
st.markdown("Powered by Docker, Python, n8n, and PostgreSQL.")