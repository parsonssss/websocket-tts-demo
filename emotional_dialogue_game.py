import os
import asyncio
import websockets
import requests
import pyaudio
import threading
import json
import base64
import wave
from datetime import datetime
from openai import OpenAI
from dotenv import load_dotenv

# --- Configuration ---
load_dotenv()
API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:3000/api")
AUTH_TOKEN = os.getenv("AUTH_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
IFLYTEK_APP_ID = os.getenv("IFLYTEK_APP_ID")
AUDIO_OUTPUT_DIR = "wavecraft/audio_logs"

if not all([AUTH_TOKEN, OPENAI_API_KEY, IFLYTEK_APP_ID]):
    print("Error: Please ensure AUTH_TOKEN, OPENAI_API_KEY, and IFLYTEK_APP_ID are set in your .env file")
    exit(1)

# --- WebSocket and Audio Handling ---
class TTSWebSocketClient:
    def __init__(self, websocket_url, session_id, tts_params):
        self.websocket_url = websocket_url
        self.session_id = session_id
        self.tts_params = tts_params
        self.ws = None
        self.p = pyaudio.PyAudio()
        self.stream = self.p.open(format=pyaudio.paInt16, channels=1, rate=16000, output=True)
        self.audio_queue = asyncio.Queue()
        self.is_playing = False
        self.play_thread = None
        self.audio_frames = []
        self._setup_audio_directory()

    def _setup_audio_directory(self):
        """Ensures the audio output directory exists."""
        os.makedirs(AUDIO_OUTPUT_DIR, exist_ok=True)


    async def connect(self):
        print(f"Connecting to WebSocket: {self.websocket_url}")
        self.ws = await websockets.connect(self.websocket_url)
        print("WebSocket connection successful!")
        asyncio.create_task(self.receive_audio())

    async def receive_audio(self):
        try:
            while True:
                message = await self.ws.recv()
                # iFlytek always sends JSON strings, not raw bytes.
                data = json.loads(message)

                # Log the server message for debugging if needed
                # print(f"[WebSocket Server]: {json.dumps(data, indent=2)}")

                # Check for errors from the server
                if data.get('header', {}).get('code') != 0:
                    print(f"Server Error: {data.get('header', {}).get('message')}")
                    continue

                # Extract audio data
                payload = data.get('payload', {})
                if payload:
                    audio_data_b64 = payload.get('audio', {}).get('audio')
                    if audio_data_b64:
                        # Decode the base64 audio data
                        raw_audio_bytes = base64.b64decode(audio_data_b64)
                        self.audio_frames.append(raw_audio_bytes)
                        await self.audio_queue.put(raw_audio_bytes)

                # Check for the end of the stream
                if data.get('header', {}).get('status') == 2:
                    print("Audio stream finished.")
                    await self.audio_queue.put(None)  # Signal end of audio playback
                    break

        except websockets.exceptions.ConnectionClosed as e:
            print(f"WebSocket connection closed. Details:")
            print(f"  - Code: {e.code}")
            print(f"  - Reason: {e.reason}")
            await self.audio_queue.put(None)  # Ensure player stops
        except json.JSONDecodeError:
            print(f"[WebSocket Server Raw Non-JSON]: {message}")
        except Exception as e:
            print(f"Error receiving audio: {e}")
            await self.audio_queue.put(None)


    async def send_text_stream(self, text_generator):
        print("\n--- Starting to send text stream to TTS ---")
        full_text = ""
        seq = 0
        is_first = True

        try:
            # Create an iterator from the generator to allow lookahead
            iterator = iter(text_generator)
            current_chunk = next(iterator, None)  # Get the first chunk

            if current_chunk is None:
                print("Text generator is empty, sending nothing.")
                return ""

            while current_chunk is not None:
                # Clean up the chunk
                chunk_to_send = current_chunk.strip()
                if not chunk_to_send:
                    current_chunk = next(iterator, None)
                    continue

                # Look ahead to the next chunk to determine if this one is the last
                next_chunk = next(iterator, None)

                # Determine the status for the iFlytek protocol
                if is_first and next_chunk is None:
                    status = 2  # This is the first and only chunk
                elif is_first:
                    status = 0  # This is the first of many chunks
                elif next_chunk is None:
                    status = 2  # This is the last chunk
                else:
                    status = 1  # This is an intermediate chunk

                # Build the frame payload
                frame = {
                    "header": {"app_id": IFLYTEK_APP_ID, "status": status},
                    "payload": {
                        "text": {
                            "encoding": "utf8",
                            "compress": "raw",
                            "format": "plain",
                            "status": status,
                            "seq": seq,
                            "text": base64.b64encode(chunk_to_send.encode('utf-8')).decode('utf-8')
                        }
                    }
                }

                # The 'parameter' block is only sent with the very first frame
                if is_first:
                    frame["parameter"] = {
                        "oral": {"oral_level": "mid"},
                        "tts": {
                            "vcn": self.tts_params['voice'],
                            "speed": self.tts_params['speed'],
                            "volume": self.tts_params['volume'],
                            "pitch": self.tts_params['pitch'],
                            "audio": {
                                "encoding": "raw", "sample_rate": 16000,
                                "channels": 1, "bit_depth": 16,
                            }
                        }
                    }
                    is_first = False

                print(f"  [Sending chunk {seq}]: {chunk_to_send}")
                await self.ws.send(json.dumps(frame, ensure_ascii=False))

                full_text += chunk_to_send
                seq += 1
                current_chunk = next_chunk  # Move to the next chunk
                await asyncio.sleep(0.05)  # Small delay to prevent flooding

            print("--- Text stream sent ---\n")
            return full_text
        except StopIteration:
            # This handles the case of an empty generator gracefully
            print("Text stream ended normally.")
            return ""
        except websockets.exceptions.ConnectionClosed as e:
            print(f"Connection closed while sending text: {e}")
            return full_text
        except Exception as e:
            print(f"Error sending text: {e}")
            return full_text


    def _play_audio_sync(self):
        """Synchronous function to play audio from the queue."""
        self.is_playing = True
        print("\n--- Starting audio playback ---")
        while True:
            try:
                # Use a timeout to prevent blocking indefinitely
                chunk = self.audio_queue.get_nowait()
                if chunk is None: # End of stream signal
                    self.audio_queue.task_done()
                    break
                self.stream.write(chunk)
                self.audio_queue.task_done()
            except asyncio.QueueEmpty:
                # Wait a bit before trying again if the queue is empty
                asyncio.run(asyncio.sleep(0.01))
            except Exception as e:
                print(f"Error playing audio: {e}")
                break
        print("--- Audio playback finished ---\n")
        self.is_playing = False

    async def play_audio_async(self):
        """Asynchronous wrapper to run the synchronous play function in a thread."""
        if not self.is_playing:
            loop = asyncio.get_event_loop()
            # The play_thread runs the synchronous _play_audio_sync
            self.play_thread = threading.Thread(target=self._play_audio_sync, daemon=True)
            self.play_thread.start()

    async def wait_for_playback_to_finish(self):
        """Waits for the playback thread to complete."""
        if self.play_thread and self.play_thread.is_alive():
           # This is tricky because we are mixing async and threads.
           # A better approach for production would be a fully async audio library.
           while self.is_playing:
               await asyncio.sleep(0.1)


    def _save_audio_to_file(self):
        """Saves the collected audio frames to a WAV file."""
        if not self.audio_frames:
            print("No audio to save.")
            return

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = os.path.join(AUDIO_OUTPUT_DIR, f"session_{self.session_id}_{timestamp}.wav")
        
        print(f"\nSaving audio to: {filename}")
        
        with wave.open(filename, 'wb') as wf:
            wf.setnchannels(1)
            wf.setsampwidth(self.p.get_sample_size(pyaudio.paInt16))
            wf.setframerate(16000)
            wf.writeframes(b''.join(self.audio_frames))
        
        print("Audio saved successfully.")

    async def close(self):
        if self.ws:
            await self.ws.close()
            print("WebSocket connection closed.")

        # Save audio before terminating PyAudio
        self._save_audio_to_file()

        self.stream.stop_stream()
        self.stream.close()
        self.p.terminate()
        print("Audio resources released.")


# --- API Interaction ---
def get_tts_websocket_url(text_to_initiate, voice="x5_lingfeiyi_flow"):
    """
    获取 TTS WebSocket URL。
    """
    url = f"{API_BASE_URL}/tts-stream"
    headers = {"Authorization": f"Bearer {AUTH_TOKEN}"}
    params = {
        "text": text_to_initiate,
        "voice": voice,
        "speed": 50,
        "volume": 50,
        "pitch": 50
    }
    print(f"Getting WebSocket URL from {url}...")
    try:
        response = requests.get(url, headers=headers, params=params)
        response.raise_for_status()
        data = response.json()
        print("Successfully retrieved WebSocket URL and Session ID.")
        print(f"  - Session ID: {data['sessionId']}")
        print(f"  - WebSocket URL: {data['websocketUrl'][:50]}...") # 截断显示
        return data
    except requests.exceptions.RequestException as e:
        print(f"Failed to get WebSocket URL: {e}")
        if e.response is not None:
            print(f"Response content: {e.response.text}")
        return None

def confirm_session(session_id, success=True):
    """
    确认或取消会话（用于计费）。
    """
    url = f"{API_BASE_URL}/tts-stream"
    headers = {"Authorization": f"Bearer {AUTH_TOKEN}"}
    payload = {
        "sessionId": session_id,
        "success": success,
        "actualTextLength": 0 # 示例值，可以根据需要调整
    }
    print(f"\nConfirming session {session_id}...")
    try:
        response = requests.post(url, headers=headers, json=payload)
        response.raise_for_status()
        print(f"Session confirmed successfully: {response.json()}")
    except requests.exceptions.RequestException as e:
        print(f"Session confirmation failed: {e}")
        if e.response is not None:
            print(f"Response content: {e.response.text}")

def cancel_session(session_id):
    """
    取消会话。
    """
    url = f"{API_BASE_URL}/tts-stream?sessionId={session_id}"
    headers = {"Authorization": f"Bearer {AUTH_TOKEN}"}
    print(f"\nCancelling session {session_id}...")
    try:
        response = requests.delete(url, headers=headers)
        response.raise_for_status()
        print(f"Session cancelled successfully: {response.json()}")
    except requests.exceptions.RequestException as e:
        print(f"Session cancellation failed: {e}")
        if e.response is not None:
            print(f"Response content: {e.response.text}")

# --- OpenAI Integration ---
def get_openai_stream(messages):
    """
    从 OpenAI 获取流式响应。
    """
    client = OpenAI(api_key=OPENAI_API_KEY,base_url="https://kjp.bt6.top/v1/")
    print("\n--- Getting response stream from OpenAI ---")
    try:
        stream = client.chat.completions.create(
            model="gemini-2.5-flash-lite-preview-06-17",
            messages=messages,
            stream=True,
        )
        for chunk in stream:
            content = chunk.choices[0].delta.content or ""
            if content:
                print(f"  [OpenAI chunk]: {content.strip()}")
                yield content
    except Exception as e:
        print(f"Error getting stream from OpenAI: {e}")
        yield "Sorry, I ran into a problem."


# --- Game Logic ---
async def emotional_dialogue_game():
    """
    情感对话游戏主逻辑。
    """
    print("="*50)
    print("Welcome to the Emotional Dialogue Game")
    print("="*50)
    
    character_setting = input("Please enter the character setting you want to talk to (e.g., 'A wise but slightly sad ancient robot'):\n> ")
    
    messages = [
        {"role": "system", "content": f"You are now playing the following character. Engage in a short, emotional conversation with the user. Fully immerse yourself in the character. Your character setting is: '{character_setting}'"},
        {"role": "user", "content": "Hello, let's start chatting."}
    ]

    current_turn_session_id = None

    try:
        # Initial greeting from the AI
        await handle_ai_turn(messages, character_setting)

        # Main conversation loop
        while True:
            user_input = input("You: ")
            if user_input.lower() in ['退出', 'exit', 'quit']:
                print("Thank you for playing. Game over.")
                # The last session is confirmed inside handle_ai_turn
                break
            
            messages.append({"role": "user", "content": user_input})
            
            await handle_ai_turn(messages, character_setting)

    except (KeyboardInterrupt, EOFError):
        print("\nInterrupt detected. Game over.")
        # If a session was active during interruption, you might want to cancel it.
        # For simplicity, we'll just exit here.
    except Exception as e:
        print(f"An unexpected error occurred in the game loop: {e}")
    finally:
        print("Game client closed.")

async def handle_ai_turn(messages, character_setting):
    """
    Handles a single turn of the AI speaking, including getting a new TTS session.
    """
    # 1. Get a new WebSocket URL for this specific turn.
    # The initial text is just for API validation; it won't be spoken.
    response_data = get_tts_websocket_url("start turn")
    if not response_data:
        print("Could not get TTS session for this turn, please try again.")
        return

    session_id = response_data['sessionId']
    tts_client = TTSWebSocketClient(
        websocket_url=response_data['websocketUrl'],
        session_id=session_id,
        tts_params=response_data['parameters']
    )

    try:
        await tts_client.connect()
        
        # Start the audio player task
        asyncio.create_task(tts_client.play_audio_async())

        # Get OpenAI stream and send it to the new TTS connection
        response_stream = get_openai_stream(messages)
        full_response = await tts_client.send_text_stream(response_stream)
        
        # Update conversation history
        if full_response:
            messages.append({"role": "assistant", "content": full_response})

        # Wait for this turn's audio to finish playing
        await tts_client.wait_for_playback_to_finish()
        
        if full_response:
            print(f"\nAI ({character_setting[:10]}...): {full_response}\n")

        # This turn was successful
        confirm_session(session_id, success=True)

    except Exception as e:
        print(f"Error handling AI turn: {e}")
        # Mark this session as failed for billing purposes
        confirm_session(session_id, success=False)
    finally:
        # Crucially, close the connection and save the audio for THIS turn.
        await tts_client.close()

if __name__ == "__main__":
    # 检查 .env 文件是否存在
    if not os.path.exists('.env'):
        with open('.env', 'w') as f:
            f.write('API_BASE_URL="http://localhost:3000/api"\n')
            f.write('AUTH_TOKEN="YOUR_API_KEY_HERE"\n')
            f.write('OPENAI_API_KEY="YOUR_OPENAI_KEY_HERE"\n')
            f.write('IFLYTEK_APP_ID="YOUR_IFLYTEK_APPID_HERE"\n')
        print("Created .env file. Please fill in your API_BASE_URL, AUTH_TOKEN, OPENAI_API_KEY, and IFLYTEK_APP_ID.")
        exit(0)

    try:
        asyncio.run(emotional_dialogue_game())
    except KeyboardInterrupt:
        print("\nProgram terminated.") 