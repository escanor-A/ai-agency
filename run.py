import os
import subprocess
import sys
import time

def main():
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    print("🚀 بدء تشغيل نظام AI Agency (السيرفر + بوت تليجرام)...")

    # 1. إطلاق بوت تليجرام في عملية منفصلة في الخلفية
    bot_process = subprocess.Popen([sys.executable, "telegram_bot.py"])
    print("🤖 تم إطلاق بوت تليجرام بنجاح...")

    # 2. إطلاق سيرفر FastAPI / Uvicorn للاستماع على البورت المحدد من الاستضافة (Render / Railway)
    port = os.environ.get("PORT", "8000")
    server_cmd = [
        sys.executable, "-m", "uvicorn", "server:app",
        "--host", "0.0.0.0",
        "--port", str(port),
    ]

    server_process = None
    try:
        server_process = subprocess.Popen(server_cmd)
        print(f"🌐 تم تشغيل سيرفر الويب على البورت: {port}")

        # مراقبة مستمرة للعمليتين
        while True:
            time.sleep(3)
            # إعادة تشغيل البوت تلقائياً لو حدث أي انقطاع في الشبكة
            if bot_process.poll() is not None:
                print("⚠️ بوت تليجرام توقف، جاري إعادة تشغيله فوراً...")
                bot_process = subprocess.Popen([sys.executable, "telegram_bot.py"])

            # إذا توقف سيرفر الويب، ننهي البرنامج ليقوم Render بإعادة الإقلاع
            if server_process.poll() is not None:
                print("❌ توقف سيرفر الويب!")
                break
    except KeyboardInterrupt:
        print("\n🛑 جاري إيقاف الخدمات بنظافة...")
    finally:
        if bot_process and bot_process.poll() is None:
            bot_process.terminate()
        if server_process and server_process.poll() is None:
            server_process.terminate()

if __name__ == "__main__":
    main()
