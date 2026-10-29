import os

# Set dummy/mock environment variables before any application module is imported
os.environ["SUPABASE_URL"] = os.environ.get("SUPABASE_URL") or "https://mock-test-agency.supabase.co"
os.environ["SUPABASE_SECRET_KEY"] = os.environ.get("SUPABASE_SECRET_KEY") or "mock-test-secret-key-12345"
os.environ["GEMINI_API_KEY"] = os.environ.get("GEMINI_API_KEY") or "mock-test-gemini-key"
os.environ["TELEGRAM_BOT_TOKEN"] = os.environ.get("TELEGRAM_BOT_TOKEN") or "123456789:ABCDEF_mock_telegram_token"
os.environ["SUPER_ADMIN_CHAT_ID"] = os.environ.get("SUPER_ADMIN_CHAT_ID") or "12345678"
os.environ["BOT_USERNAME"] = os.environ.get("BOT_USERNAME") or "ORIK_1_bot"
os.environ["META_APP_SECRET"] = os.environ.get("META_APP_SECRET") or "mock_meta_app_secret"
os.environ["META_VERIFY_TOKEN"] = os.environ.get("META_VERIFY_TOKEN") or "mock_verify_token"
os.environ["TIKTOK_APP_SECRET"] = os.environ.get("TIKTOK_APP_SECRET") or "mock_tiktok_secret"

