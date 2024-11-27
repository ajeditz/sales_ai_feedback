import asyncio
import os
import sys
from typing import Dict
import json

import aiohttp
import datetime
import wave
from dotenv import load_dotenv
from loguru import logger
from runner import configure

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import EndFrame, LLMMessagesFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
from pipecat.processors.audio.audio_buffer_processor import AudioBufferProcessor
from pipecat.services.elevenlabs import ElevenLabsTTSService
from pipecat.services.cartesia import CartesiaTTSService
from pipecat.services.openai import OpenAILLMService
from pipecat.transports.services.daily import DailyParams, DailyTransport, DailyTranscriptionSettings
import uuid
import firebase_admin
from firebase_admin import firestore, credentials


load_dotenv(override=True)

logger.remove(0)
logger.add(sys.stderr, level="DEBUG")

FILES_DIR = "saved_files"


cred = credentials.Certificate(os.getenv("CRED_PATH"))
firebase_admin.initialize_app(cred)

db = firestore.client()


async def create_agent(room_id, transcript):
    doc_ref = db.collection("Transcription").document(room_id)
    data={
        "prompt":transcript
    }
    doc_ref.set(data)
    print(f"Agent {room_id} created succesfully.")


async def save_audio(audiobuffer, room_url):
    """Save the audio buffer to a WAV file"""
    if audiobuffer.has_audio():
        merged_audio = audiobuffer.merge_audio_buffers()
        filename = os.path.join(FILES_DIR, f"audio_{room_url.removeprefix("https://applicationsquare.daily.co/")}.wav")
        with wave.open(filename, "wb") as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(audiobuffer._sample_rate)
            wf.writeframes(merged_audio)
        logger.info(f"Merged audio saved to {filename}")
    else:
        logger.warning("No audio data to save")

async def save_transcription(transcriptions: Dict, participant_id: str, room_url: str):
    """Save transcriptions to a JSON file"""
    if participant_id in transcriptions:
        filename = os.path.join(FILES_DIR, f"transcription_{room_url.removeprefix("https://applicationsquare.daily.co/")}.json")        
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(transcriptions[participant_id], f, ensure_ascii=False, indent=2)
        logger.info(f"Transcription saved to {filename}")

async def save_message_log(context, participant_id: str, room_url: str):
    """Save the latest message log to a JSON file"""
    if context and context.get_messages():
        filename = os.path.join(FILES_DIR, f"message_logs_{room_url.removeprefix("https://applicationsquare.daily.co/")}.json")
        full_path = os.path.abspath(filename)
        
        # Convert messages to a format that can be easily serialized
        messages_to_save = context.get_messages()
        
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(messages_to_save, f, ensure_ascii=False, indent=2)
        
        logger.info(f"Message log saved to full path: {full_path}")

async def main():
    async with aiohttp.ClientSession() as session:
        # Get room configuration
        (room_url, token) = await configure(session)

        # Store transcriptions
        transcriptions: Dict[str, list] = {}

        # Initialize Daily transport
        transport = DailyTransport(
            room_url,
            token,
            "Chatbot",
            DailyParams(
                audio_out_enabled=True,
                audio_in_enabled=True,
                camera_out_enabled=False,
                vad_enabled=True,
                vad_audio_passthrough=True,
                vad_analyzer=SileroVADAnalyzer(),
                transcription_enabled=True,
                transcription_settings=DailyTranscriptionSettings(
                    language="en",  # Change to "es" for Spanish
                    tier="nova",
                    model="2-general"
                )
            ),
        )

        # Initialize TTS service
        tts = CartesiaTTSService(
            api_key=os.getenv("CARTESIA_API_KEY"),
            voice_id="a0e99841-438c-4a64-b679-ae501e7d6091",  # English voice
        )

        # Initialize LLM service
        llm = OpenAILLMService(api_key=os.getenv("OPENAI_API_KEY"), model="gpt-4o")

        # Initial messages for the chatbot
        messages = [
            {
                "role": "system",
                "content": "You are Chatbot, a friendly, helpful robot. Your goal is to demonstrate your capabilities in a succinct way. Your output will be converted to audio so don't include special characters in your answers. Respond to what the user said in a creative and helpful way, but keep your responses brief. Start by introducing yourself. Keep all your response to 12 words or fewer.",
            },
        ]

        # Initialize context and pipeline components
        context = OpenAILLMContext(messages)
        context_aggregator = llm.create_context_aggregator(context)
        audiobuffer = AudioBufferProcessor()

        # Create pipeline
        pipeline = Pipeline(
            [
                transport.input(),
                context_aggregator.user(),
                llm,
                tts,
                transport.output(),
                audiobuffer,
                context_aggregator.assistant(),
            ]
        )

        # Initialize pipeline task
        task = PipelineTask(pipeline, PipelineParams(allow_interruptions=True))

        @transport.event_handler("on_transcription_message")
        async def on_transcription_message(transport, message):
            """Handle incoming transcriptions"""
            participant_id = message.get("participantId", "")
            if not participant_id:
                return

            if participant_id not in transcriptions:
                transcriptions[participant_id] = []
            
            # Store transcription with metadata
            transcriptions[participant_id].append({
                'text': message.get('text', ''),
                'timestamp': message.get('timestamp', datetime.datetime.now().isoformat()),
                'is_final': message.get('rawResponse', {}).get('is_final', False),
                'confidence': message.get('rawResponse', {}).get('confidence', 0.0)
            })
            
            # Print real-time transcription
            logger.info(f"Transcription from {participant_id}: {message.get('text', '')}")
            if message.get('rawResponse', {}).get('is_final'):
                logger.info(f"Final transcription confidence: {message.get('rawResponse', {}).get('confidence', 0.0)}")

        @transport.event_handler("on_first_participant_joined")
        async def on_first_participant_joined(transport, participant):
            """Handle first participant joining"""
            await transport.capture_participant_transcription(participant["id"])
            await task.queue_frames([LLMMessagesFrame(messages)])
            logger.info(f"First participant joined: {participant['id']}")

        @transport.event_handler("on_participant_left")
        async def on_participant_left(transport, participant, reason):
            """Handle participant leaving"""
            participant_id = participant['id']
            logger.info(f"Participant left: {participant_id}")
            
            # Print final transcriptions
            if participant_id in transcriptions:
                logger.info(f"\nFinal transcriptions for participant {participant_id}:")
                for entry in transcriptions[participant_id]:
                    logger.info(f"[{entry['timestamp']}] {entry['text']}")
                
                # Save transcriptions to file
                # await save_transcription(transcriptions, participant_id, room_url)
                
                # Save message log
                await save_message_log(context, participant_id, room_url)
            
            # Save audio and end pipeline
            await save_audio(audiobuffer, room_url)
            await create_agent(room_url.removeprefix("https://applicationsquare.daily.co/"), context.get_messages())
            await task.queue_frame(EndFrame())

        # Run the pipeline
        runner = PipelineRunner()
        await runner.run(task)

if __name__ == "__main__":
    asyncio.run(main())
