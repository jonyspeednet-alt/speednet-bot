# Python 3.9 Slim ইমেজ ব্যবহার করছি (হালকা এবং দ্রুত)
FROM python:3.9-slim

# কাজের ডিরেক্টরি সেট করা
WORKDIR /app

# প্রয়োজনীয় সিস্টেম ডিপেন্ডেন্সি ইনস্টল করা (PostgreSQL ড্রাইভারের জন্য)
RUN apt-get update && apt-get install -y \
    libpq-dev \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# requirements.txt কপি এবং ইনস্টল করা
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# বাকি সব ফাইল কপি করা
COPY . .

# ডাটাবেস ইনিশিয়ালাইজ করে অ্যাপ রান করা (Gunicorn ব্যবহার করে)
CMD ["sh", "-c", "python -c 'from app import init_db, seed_db; init_db(); seed_db()' && gunicorn -b 0.0.0.0:5000 app:app"]