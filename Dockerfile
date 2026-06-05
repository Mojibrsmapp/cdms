FROM python:3.10-slim

# ক্রোম, সেলেনিয়াম ডিপেন্ডেন্সি এবং Tor ইনস্টল করা
RUN apt-get update && apt-get install -y \
    wget \
    gnupg \
    unzip \
    curl \
    chromium \
    chromium-driver \
    tor \
    && rm -rf /var/lib/apt/lists/*

# পাইথন এনভায়রনমেন্ট সেট করা
ENV PYTHONUNBUFFERED=1
ENV DISPLAY=:99

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 5000

# ব্যাকগ্রাউন্ডে Tor সার্ভিস চালু করে তারপর পাইথন অ্যাপ রান করার স্ক্রিপ্ট
CMD tor & python app.py
