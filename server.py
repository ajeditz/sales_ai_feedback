#
# Copyright (c) 2024, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

import argparse
import os
import json
import base64
import subprocess
from contextlib import asynccontextmanager
from urllib.parse import urlparse
from pydantic import BaseModel, Field
from typing import Optional
import time

import aiohttp
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse

from pipecat.transports.services.helpers.daily_rest import (
    DailyRESTHelper,
    DailyRoomObject,
    DailyRoomProperties,
    DailyRoomParams,
)

from pipecat.transports.services.helpers.daily_rest import DailyRESTHelper, DailyRoomParams

MAX_BOTS_PER_ROOM = 1

# Bot sub-process dict for status reporting and concurrency control
bot_procs = {}

daily_helpers = {}

load_dotenv(override=True)

# ------------ Configuration ------------ #

MAX_SESSION_TIME = 5 * 60  # 5 minutes
REQUIRED_ENV_VARS = [
    "DAILY_API_KEY",
    "OPENAI_API_KEY",
    "FLY_API_KEY",
    "FLY_APP_NAME",
]

FLY_API_HOST = os.getenv("FLY_API_HOST", "https://api.machines.dev/v1")
FLY_APP_NAME = os.getenv("FLY_APP_NAME", "pipecat-fly-example")
FLY_API_KEY = os.getenv("FLY_API_KEY", "")
FLY_HEADERS = {"Authorization": f"Bearer {FLY_API_KEY}", "Content-Type": "application/json"}

daily_helpers = {}

class BotConfig(BaseModel):
    speed: str = Field("normal", description="Voice speed (slow/normal/fast)")
    emotion: list[str] = Field(["positivity:high", "curiosity"], description="List of emotions for the voice")
    prompt: str = Field("You are a friendly customer service agent...", description="System prompt for the bot")
    voice_id: str = Field("tmXu3zSmE1qTdNsiLHv0", description="Voice ID for TTS")
    difficulty_level:  str = Field("You are a friendly customer service agent...", description="System prompt for the bot")
    session_time: Optional[float] = Field(3600, description="Session expiry time in seconds")
    avatar_name: str = Field("John",description="The name of the avatar")
    user_id: str = Field("aldfalgna", description="User Id")



def cleanup():
    # Clean up function, just to be extra safe
    for entry in bot_procs.values():
        proc = entry[0]
        proc.terminate()
        proc.wait()


@asynccontextmanager
async def lifespan(app: FastAPI):
    aiohttp_session = aiohttp.ClientSession()
    daily_helpers["rest"] = DailyRESTHelper(
        daily_api_key=os.getenv("DAILY_API_KEY", ""),
        daily_api_url=os.getenv("DAILY_API_URL", "https://api.daily.co/v1"),
        aiohttp_session=aiohttp_session,
    )
    yield
    await aiohttp_session.close()
    cleanup()


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --------------------- Main ----------------------------- #



async def spawn_fly_machine(room_url: str, token: str, config:BotConfig):
    async with aiohttp.ClientSession() as session:
        # Use the same image as the bot runner
        async with session.get(
            f"{FLY_API_HOST}/apps/{FLY_APP_NAME}/machines", headers=FLY_HEADERS
        ) as r:
            if r.status != 200:
                text = await r.text()
                raise Exception(f"Unable to get machine info from Fly: {text}")

            data = await r.json()
            image = data[0]["config"]["image"]

        config_str = json.dumps(config.model_dump())
        config_b64 = base64.b64encode(config_str.encode()).decode()

        # Machine configuration
        cmd = f"python3 bot.py -u {room_url} -t {token} --config {config_b64}"
        cmd = cmd.split()
        worker_props = {
            "config": {
                "image": image,
                "auto_destroy": True,
                "init": {"cmd": cmd},
                "restart": {"policy": "no"},
                "guest": {"cpu_kind": "shared", "cpus": 1, "memory_mb": 1024},
            },
        }

        # Spawn a new machine instance
        async with session.post(
            f"{FLY_API_HOST}/apps/{FLY_APP_NAME}/machines", headers=FLY_HEADERS, json=worker_props
        ) as r:
            if r.status != 200:
                text = await r.text()
                raise Exception(f"Problem starting a bot worker: {text}")

            data = await r.json()
            # Wait for the machine to enter the started state
            vm_id = data["id"]

        async with session.get(
            f"{FLY_API_HOST}/apps/{FLY_APP_NAME}/machines/{vm_id}/wait?state=started",
            headers=FLY_HEADERS,
        ) as r:
            if r.status != 200:
                text = await r.text()
                raise Exception(f"Bot was unable to enter started state: {text}")

    print(f"Machine joined room: {room_url}")



@app.post("/")
async def start_agent(config: BotConfig):
    try:
        data = await config.model_dump_json()
        # Is this a webhook creation request?
        if "test" in data:
            return JSONResponse({"test": True})
    except Exception as e:
        pass

    # Use specified room URL, or create a new one if not specified
    room_url = os.getenv("DAILY_SAMPLE_ROOM_URL", "")

    if not room_url:
        params = DailyRoomParams(properties=DailyRoomProperties(exp=time.time() + 10 * 60))
        try:
            room: DailyRoomObject = await daily_helpers["rest"].create_room(params=params)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Unable to provision room {e}")
    else:
        # Check passed room URL exists, we should assume that it already has a sip set up
        try:
            room: DailyRoomObject = await daily_helpers["rest"].get_room_from_url(room_url)
        except Exception:
            raise HTTPException(status_code=500, detail=f"Room not found: {room_url}")

    # Give the agent a token to join the session
    token = await daily_helpers["rest"].get_token(room.url, MAX_SESSION_TIME)

    if not room or not token:
        raise HTTPException(status_code=500, detail=f"Failed to get token for room: {room_url}")

    # Launch a new fly.io machine, or run as a shell process (not recommended)
    run_as_process = os.getenv("RUN_AS_PROCESS", False)
    config_str = json.dumps(config.model_dump())
    config_b64 = base64.b64encode(config_str.encode()).decode()


    if run_as_process:
        try:
            subprocess.Popen(
                [f"python3 -m bot -u {room.url} -t {token} --config {config_b64}"],
                shell=True,
                bufsize=1,
                cwd=os.path.dirname(os.path.abspath(__file__)),
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to start subprocess: {e}")
    else:
        try:
            await spawn_fly_machine(room.url, token, config)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to spawn VM: {e}")

    # Grab a token for the user to join with
    user_token = await daily_helpers["rest"].get_token(room.url, MAX_SESSION_TIME)

    return JSONResponse(
        {
            "room_url": room.url,
            "token": user_token,
            "room_id":(urlparse(room.url).path).removeprefix('/')
        }
    )

    # return RedirectResponse(room.url)


@app.get("/status/{pid}")
def get_status(pid: int):
    # Look up the subprocess
    proc = bot_procs.get(pid)

    # If the subprocess doesn't exist, return an error
    if not proc:
        raise HTTPException(status_code=404, detail=f"Bot with process id: {pid} not found")

    # Check the status of the subprocess
    if proc[0].poll() is None:
        status = "running"
    else:
        status = "finished"

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

    try:
        import uvicorn
        uvicorn.run(
            "server:app",
            host=config.host,
            port=config.port,
            reload=config.reload,
        )
    except KeyboardInterrupt:
        print("Pipecat bot  shutting down...")
