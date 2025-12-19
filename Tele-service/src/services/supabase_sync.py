import httpx
import logging
import json
from src.config.config import config
from src.database import db
from src.telegram import telegram_manager  

logger = logging.getLogger(__name__)

async def sync_sessions_from_supabase():
    """
    Two-way Sync:
    1. DOWNLOAD: Fetch active sessions from Supabase -> Update Local DB.
    2. PRUNE: Identify sessions in Local DB that are NOT in Supabase -> Delete them.
    """
    if not config.SUPABASE_URL or not config.SUPABASE_SERVICE_KEY:
        logger.warning("⚠️ Supabase credentials missing. Skipping sync.")
        return

    logger.info("🌍 Connecting to Supabase to fetch Telegram sessions...")
    
    headers = {
        "apikey": config.SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {config.SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json"
    }

    # 1. Fetch Remote (Supabase) Sessions
    url = (
        f"{config.SUPABASE_URL}/rest/v1/agent_integrations"
        f"?select=agent_id,config"
        f"&channel=eq.telegram"
        f"&enabled=eq.true"
    )

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, headers=headers)
            
            if response.status_code != 200:
                logger.error(f"Supabase Sync Failed: {response.status_code}")
                return

            remote_rows = response.json()
            
            # Create a set of valid Agent IDs from Supabase
            valid_remote_ids = set()

            # --- STEP 1: UPSERT (Add/Update) ---
            logger.info(f"Processing {len(remote_rows)} remote sessions...")
            for row in remote_rows:
                try:
                    agent_id = row.get('agent_id')
                    config_data = row.get('config', {})
                    
                    api_id = config_data.get('api_id')
                    api_hash = config_data.get('api_hash')
                    session_string = config_data.get('session')

                    if api_id and api_hash and session_string:
                        valid_remote_ids.add(str(agent_id)) # Keep track of what IS valid
                        
                        await db.save_session(
                            account_id=str(agent_id),
                            api_id=str(api_id),
                            api_hash=str(api_hash),
                            session_string=str(session_string)
                        )
                except Exception as inner_e:
                    logger.error(f"Parse error for agent {row.get('agent_id')}: {inner_e}")

            # --- STEP 2: PRUNE (Delete Zombies) ---
            local_sessions = await db.get_all_sessions()
            local_ids = {s['account_id'] for s in local_sessions}
            
            # Find IDs that are Local BUT NOT Remote
            zombies = local_ids - valid_remote_ids
            
            if zombies:
                logger.warning(f"Found {len(zombies)} ZOMBIE sessions (deleted in Supabase). Removing...")
                for zombie_id in zombies:
                    # A. Stop the client in memory (if running)
                    await telegram_manager.remove_client(zombie_id)
                    
                    # B. Delete from Local DB
                    await db.delete_session(zombie_id)
                    logger.info(f"Killed Zombie: {zombie_id}")
            else:
                logger.info("✨ No zombie sessions found. Local DB is clean.")

            logger.info("✅ Supabase Sync & Prune Complete.")

    except Exception as e:
        logger.error(f"❌ Error during Supabase sync: {e}")