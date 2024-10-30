import aiohttp
import os
import argparse
import subprocess
import logging
from dotenv import load_dotenv
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse

from pipecat.transports.services.helpers.daily_rest import DailyRESTHelper, DailyRoomParams

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load environment variables from .env file
load_dotenv()

# Verify API key is available
DAILY_API_KEY = os.getenv("DAILY_API_KEY")
if not DAILY_API_KEY:
    raise ValueError("DAILY_API_KEY environment variable is not set")

# Configure Daily.co API URL
DAILY_API_URL = "https://api.daily.co/v1"

MAX_BOTS_PER_ROOM = 1

# Bot sub-process dict for status reporting and concurrency control
bot_procs = {}

daily_helpers = {}

def cleanup():
    # Clean up function, just to be extra safe
    for entry in bot_procs.values():
        proc = entry[0]
        proc.terminate()
        proc.wait()

@asynccontextmanager
async def lifespan(app: FastAPI):
    async with aiohttp.ClientSession() as aiohttp_session:
        daily_helpers["rest"] = DailyRESTHelper(
            daily_api_key=DAILY_API_KEY,
            daily_api_url=DAILY_API_URL,
            aiohttp_session=aiohttp_session,
        )
        yield
        cleanup()

app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
async def start_agent(request: Request):
    try:
        logger.info("Creating new Daily.co room")
        
        # Configure room parameters
        room_params = DailyRoomParams(
            privacy="public",  # Make room public
            max_participants=10,  # Set max participants
            enable_chat=True,    # Enable chat
        )
        
        # Create the room
        room = await daily_helpers["rest"].create_room(room_params)
        logger.info(f"Room created successfully: {room.url}")

        if not room.url:
            raise HTTPException(
                status_code=500,
                detail="Failed to get room URL from Daily.co API",
            )

        # Check bot limits
        num_bots_in_room = sum(
            1 for proc in bot_procs.values() if proc[1] == room.url and proc[0].poll() is None
        )
        if num_bots_in_room >= MAX_BOTS_PER_ROOM:
            raise HTTPException(
                status_code=500,
                detail=f"Max bot limit reached for room: {room.url}"
            )

        # Get the token for the room
        token = await daily_helpers["rest"].get_token(room.url)
        if not token:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to get token for room: {room.url}"
            )

        # Start the bot process
        try:
            proc = subprocess.Popen(
                [f"python3 -m bot -u {room.url} -t {token}"],
                shell=True,
                bufsize=1,
                cwd=os.path.dirname(os.path.abspath(__file__)),
            )
            bot_procs[proc.pid] = (proc, room.url)
            logger.info(f"Bot process started with PID: {proc.pid}")
        except Exception as e:
            logger.error(f"Failed to start bot process: {e}")
            raise HTTPException(
                status_code=500,
                detail=f"Failed to start bot process: {str(e)}"
            )

        return RedirectResponse(room.url)

    except Exception as e:
        logger.error(f"Error in start_agent: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to create room: {str(e)}"
        )

@app.get("/status/{pid}")
def get_status(pid: int):
    proc = bot_procs.get(pid)
    if not proc:
        raise HTTPException(
            status_code=404,
            detail=f"Bot with process id: {pid} not found"
        )

    status = "running" if proc[0].poll() is None else "finished"
    return JSONResponse({"bot_id": pid, "status": status})

if __name__ == "__main__":
    import uvicorn

    default_host = os.getenv("HOST", "0.0.0.0")
    default_port = int(os.getenv("FAST_API_PORT", "7860"))

    parser = argparse.ArgumentParser(description="Daily Storyteller FastAPI server")
    parser.add_argument("--host", type=str, default=default_host, help="Host address")
    parser.add_argument("--port", type=int, default=default_port, help="Port number")
    parser.add_argument("--reload", action="store_true", help="Reload code on change")

    config = parser.parse_args()

    uvicorn.run(
        "server:app",
        host=config.host,
        port=config.port,
        reload=config.reload,
    )
