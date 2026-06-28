from supabase import create_client
import config

supabase = create_client(
    config.SUPABASE_URL,
    config.SUPABASE_KEY
)
