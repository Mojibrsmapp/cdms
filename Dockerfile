FROM python:3.10-slim

# ক্রোম এবং প্রয়োজনীয় ডিপেন্ডেন্সি ইনস্টল করা
RUN apt-get update && apt-get install -y \
    wget \
    gnupg \
    unzip \
    curl \
    chromium \
    chromium-driver \
    && rm -rf /lib/apt/lists/*

# পাইথন এনভায়রনমেন্ট ভেরিয়েবল সেট করা
ENV PYTHONUNBUFFERED=1
ENV DISPLAY=:99

WORKDIR /app

# রিকোয়ারমেন্টস কপি এবং ইনস্টল করা
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# সম্পূর্ণ প্রজেক্ট কপি করা
COPY . .

# এপিআই পোর্ট এক্সপোজ করা
EXPOSE 5000

# সার্ভার রান করার কমান্ড
CMD ["python", "app.py"]