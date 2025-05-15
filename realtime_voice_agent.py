"""
Real-Time Voice Agent
---------------------

Mic ► Deepgram STT ► GPT-4o streaming ► Deepgram Aura-2 streaming ► Speaker

Round-trip P95 target  ≤ 1,000 ms
  • STT  ≤ 300 ms        (Deepgram Nova-3 streaming)
  • GPT  ≤ 200 ms        (first token latency – we overlap synthesis)
  • TTS  ≤ 250 ms TTFB   (Aura-2 WebSocket, 44.1 kHz linear16)

Author: <you>
"""

import os, sys, json, queue, asyncio, threading, time
from datetime import datetime
import pyaudio, websockets, openai
from dotenv import load_dotenv

from deepgram import (
    DeepgramClient,
    PrerecordedOptions,
    FileSource,
)


# ------------ 0. Config - ENV / KEYS -----------------------------------------------------------------

load_dotenv()

DG_API = os.getenv("DEEPGRAM_API_KEY")
OA_API = os.getenv("OPENAI_API_KEY")
if not (DG_API and OA_API):
    print("❌  Set DEEPGRAM_API_KEY & OPENAI_API_KEY in .env"); sys.exit(1)

# ─────────────── CONSTANTS ───────────────────────────────────────────────────────────────
STT_MODEL   = "nova-3"
TTS_MODEL   = "aura-2-thalia-en"
GPT_MODEL   = "gpt-4o-mini"          # streaming
SYS_PROMPT =  "You are a succinct, helpful assistant. Reply conversationally." # system prompt keeps output short / ready for TTS

RATE        = 48_000                 # matches most laptop mics; Aura-2 optimal rate (also works at 16 k)
CHUNK       = 8_000                  # 8000 / 48000 = ~167 ms chunks
AUDIO_FMT   = pyaudio.paInt16
SEND_EVERY = 180                     # chars per Speak  ≈ one sentence
SILENCE     = b"\x00" * CHUNK * 2    # ➡ two bytes per sample (16-bit mono)
LAT_BUDGET  = {"stt":300, "gpt":200, "tts":250}  # ms

# ------------ 1. GLOBAL CO-ORDINATION PRIMITIVES / helpers ------------------------------------------------------

audio_q   : asyncio.Queue[bytes] = asyncio.Queue(maxsize=200)
utter_q   : asyncio.Queue[str]   = asyncio.Queue()   # STT → GPT
token_q   : asyncio.Queue[str]   = asyncio.Queue()   # GPT → TTS

p         = pyaudio.PyAudio()
start_ts  = datetime.now()
speaking  = threading.Event()          # set ↔ TTS audio is playing
last_tts_audio = asyncio.Queue(maxsize=1)  # ← timestamp bucket

def log(msg:str):
    print(f"[{(datetime.now()-start_ts).total_seconds():6.2f}s] {msg}")

# ------------ 2. Microphone task (drops audio while speaker is talking) --------------------------------------------------------

def mic_cb(indata, frame_count, time_info, status):
    """
    Called by PyAudio every `CHUNK` frames.
    While Aura is speaking we still push *digital silence* into the STT queue
    so Deepgram’s watchdog never fires.
    """
    if speaking.is_set():                       # avoid feedback → infinite loop
        try:
            audio_q.put_nowait(SILENCE)         # ➡ keep-alive
        except asyncio.QueueFull:
            pass
        return (indata, pyaudio.paContinue)

    # Non-blocking put
    try:
        audio_q.put_nowait(indata)
    except asyncio.QueueFull:
        pass
    return (indata, pyaudio.paContinue)

async def mic_task():
    stream = p.open(format=AUDIO_FMT, channels=1, rate=RATE,
                    input=True, frames_per_buffer=CHUNK,
                    stream_callback=mic_cb)
    stream.start_stream()
    log("🎙  Mic streaming …  Ctrl-C to stop")
    try:
        while stream.is_active():
            await asyncio.sleep(0.1)
    finally:
        stream.stop_stream(); stream.close(); p.terminate()

# ------------ 3. Deepgram STT tasks -----------------------------------------------------
def extract_final(msg: dict) -> str | None:
    if not msg.get("is_final"):
        return None
    alt = msg.get("channel", {}).get("alternatives", [{}])[0]
    return alt.get("transcript", "").strip()

# ---------------------------------------------------------------------
# 3.1  STT sender     (audio_q ➜ Nova-3)
# ---------------------------------------------------------------------

async def stt_sender(ws):
    """Send mic PCM -> Deepgram"""
    while True:
        chunk = await audio_q.get()
        await ws.send(chunk)

# ---------------------------------------------------------------------
# 3.2  STT receiver   (Nova-3 ➜ utter_q)
# ---------------------------------------------------------------------

async def stt_receiver(ws):
    """Receive transcripts; push completed utterances to GPT queue"""
    async for raw in ws:
        text = extract_final(json.loads(raw))
        if text:
            await utter_q.put(text)

async def run_stt():
    url =(f"wss://api.deepgram.com/v1/listen?"
          f"model={STT_MODEL}&encoding=linear16&sample_rate={RATE}"
          f"&punctuate=true&interim_results=false")
    async with websockets.connect(url,
             extra_headers={"Authorization": f"Token {DG_API}"}) as ws:
        log("🟢 STT WebSocket open")
        await asyncio.gather(stt_sender(ws), stt_receiver(ws))

# ------------ 4. GPT-4o streaming task/worker (utter_q ➜ token_q  [+ [[FLUSH]]) -----------

oa_client = openai.OpenAI(api_key=OA_API)

async def gpt_worker():
    """For each utterance -> stream GPT response tokens -> token_q"""
    while True:
        user_utt = await utter_q.get()
        log(f"📝 User: {user_utt}")
        # Stream completion
        t0 = time.perf_counter()
        stream = oa_client.chat.completions.create(
            model=GPT_MODEL,
            messages=[{"role":"system","content":SYS_PROMPT},
                      {"role":"user","content":user_utt}],
            stream=True,
            temperature=0.4
        )

        first_tok = True
        for chunk in stream:
            tok = chunk.choices[0].delta.content
            if tok is None:
                continue
            if first_tok:
                log(f"⚡ GPT first token {int((time.perf_counter()-t0)*1000)} ms")
                first_tok = False
            await token_q.put(tok)
        # mark end
        await token_q.put("[[FLUSH]]")          # sentinel

# ------------ 5. Deepgram Aura-2 TTS and playback / Speaker (WebSocket helper – runs in thread) ------

# One async task handling *both* directions
class Speaker:
    """Plays PCM bytes from a Queue in a background thread."""
    def __init__(self, rate=RATE, chunk=CHUNK):
        self.q: queue.Queue[bytes] = queue.Queue()
        self.exit = threading.Event()
        self.stream = p.open(format=AUDIO_FMT, channels=1, rate=rate,
                             output=True, frames_per_buffer=chunk)
        self.th = threading.Thread(target=self.run, daemon=True)

    def start(self): self.th.start()
    def stop(self):
        self.exit.set(); self.th.join(); self.stream.close()

    def play(self, data: bytes):
        self.q.put(data)

    def run(self):
        while not self.exit.is_set():
            try:
                self.stream.write(self.q.get(timeout=0.1))
            except queue.Empty:
                pass

async def tts_sender(ws):
    """Read tokens from GPT and send Speak messages"""
    buffer: list[str] = []
    while True:
        tok = await token_q.get()

        # Hold off sending new requests while Aura is still speaking
        while speaking.is_set():
            await asyncio.sleep(0.05)

        if tok == "[[FLUSH]]":
            if buffer:                                       # send last batch
                await ws.send(json.dumps({"type": "Speak",
                                           "text": "".join(buffer)}))
                buffer.clear()
            await ws.send(json.dumps({"type": "Flush"}))
            speaking.set()                                   # block mic & GPT
        else:
            buffer.append(tok)
            if sum(len(t) for t in buffer) >= SEND_EVERY:
                await ws.send(json.dumps({"type": "Speak",
                                           "text": "".join(buffer)}))
                buffer.clear()

async def tts_receiver(ws):
    """
    • Plays PCM chunks to the Speaker/user as they arrive
    • Logs when the first audio chunk of each turn arrives
    • Updates last-audio timestamp
    • Clears `speaking` if we haven’t heard audio for 600 ms - `silence_timeout`
    """
    spk = Speaker(); spk.start()

    silence_timeout = 0.6                               # seconds of silence (without audio) ⇒ playback finished
    last_audio_ts   = time.perf_counter()
    first_audio     = False                             # will become True once we log the start

    # --- watchdog that fires when playback seems to be over -------------
    async def watchdog():
        nonlocal first_audio
        while True:
            await asyncio.sleep(0.1)
            if speaking.is_set() and time.perf_counter() - last_audio_ts > silence_timeout:
                speaking.clear()
                first_audio = False               # reset for next turn
                log("🌊 Aura finished playback (watch-dog)")

    wd_task = asyncio.create_task(watchdog())

    try:
        async for msg in ws:
            # ───────── control frames (JSON) ─────────
            if isinstance(msg, str):
                try:
                    evt = json.loads(msg)
                except json.JSONDecodeError:
                    continue

                if evt.get("type") == "PlaybackFinished":   # some voices still send it
                    speaking.clear()
                    first_audio = False                     # reset for next turn
                    log("🌊 Aura finished playback")
                elif evt.get("type") == "Error":
                    log(f"🔴 Aura error: {evt}")
                continue

            # ───────── audio frames (bytes) ──────────
            elif isinstance(msg, bytes):                    # audio payload
                if not first_audio:
                    log("🎧 Aura audio started")
                    first_audio = True
                last_audio_ts = time.perf_counter()        # ❤  update timestamp
                spk.play(msg)                             # first audio byte → we already log elsewhere

    finally:
        wd_task.cancel()
        spk.stop()

async def run_tts():
    url=(f"wss://api.deepgram.com/v1/speak?"
         f"model={TTS_MODEL}&encoding=linear16&sample_rate={RATE}")
    async with websockets.connect(url,
             extra_headers={"Authorization": f"Token {DG_API}"}) as ws:
        log("🟢 TTS WebSocket open")
        await asyncio.gather(tts_sender(ws), tts_receiver(ws))

# ------------ 6. Main Orchestrator -----------------------------------------------------------
async def main():
    tasks = [
        asyncio.create_task(mic_task()),
        asyncio.create_task(run_stt()),
        asyncio.create_task(gpt_worker()),
        asyncio.create_task(run_tts()),
    ]
    try:
        await asyncio.gather(*tasks)
    except KeyboardInterrupt:
        log("🛑 Ctrl-C, shutting down…")
        for t in tasks:
            t.cancel()

if __name__=="__main__":
    print("🔗  Mic → Nova-3 → GPT-4o → Aura-2 – starting …")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    print("👋  Goodbye")
