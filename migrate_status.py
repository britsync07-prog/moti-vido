import sqlalchemy
from sqlalchemy import create_engine, text
import os

# Inside container, use 'db' hostname
db_url = "postgresql://user:password@db:5432/moti_db"

try:
    engine = create_engine(db_url)
    with engine.connect() as connection:
        print("Connected to DB.")
        
        # Check columns
        result = connection.execute(text("SELECT column_name FROM information_schema.columns WHERE table_name = 'posts';"))
        columns = [row[0] for row in result]
        print(f"Columns in posts: {columns}")
        
        if 'status' not in columns:
            print("Column 'status' missing. Adding it...")
            connection.execute(text("ALTER TABLE posts ADD COLUMN status VARCHAR DEFAULT 'pending';"))
            connection.execute(text("UPDATE posts SET status = 'pending' WHERE status IS NULL;"))
            connection.commit()
            print("Column 'status' added successfully.")
        else:
            print("Column 'status' already exists.")
            
except Exception as e:
    print(f"Error: {e}")
