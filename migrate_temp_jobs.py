import sqlalchemy
from sqlalchemy import create_engine, text
import os

# Inside container, use 'db' hostname
db_url = "postgresql://user:password@db:5432/moti_db"

try:
    engine = create_engine(db_url)
    with engine.connect() as connection:
        print("Connected to DB.")
        
        # Check columns in video_jobs
        result = connection.execute(text("SELECT column_name FROM information_schema.columns WHERE table_name = 'video_jobs';"))
        columns = [row[0] for row in result]
        print(f"Columns in video_jobs: {columns}")
        
        if 'is_temporary' not in columns:
            print("Column 'is_temporary' missing. Adding it...")
            connection.execute(text("ALTER TABLE video_jobs ADD COLUMN is_temporary BOOLEAN DEFAULT FALSE;"))
            connection.commit()
            print("Column 'is_temporary' added successfully.")
        else:
            print("Column 'is_temporary' already exists.")
            
except Exception as e:
    print(f"Error: {e}")
