"""
Real-Time Voice Agent
---------------------

Mic ► Deepgram STT ► GPT-4o streaming ► Deepgram Aura-2 streaming ► Speaker

Round-trip P95 target  ≤ 800 ms
  • STT  ≤ 300 ms        (Deepgram Nova-3 streaming)
  • GPT  ≤ 200 ms        (first token latency – we overlap synthesis)
  • TTS  ≤ 250 ms TTFB   (Aura-2 WebSocket, 48 kHz linear16)

Author: <you>
"""

import asyncio, json, os, queue, sys, threading, time
from datetime import datetime

import websockets
import pyaudio
from openai import OpenAI
from dotenv import load_dotenv

# ------------ 0. Config -----------------------------------------------------------------

load_dotenv()

DG_API = os.getenv("DEEPGRAM_API_KEY")
OA_API = os.getenv("OPENAI_API_KEY")
if not (DG_API and OA_API):
    print("❌  Set DEEPGRAM_API_KEY & OPENAI_API_KEY in .env"); sys.exit(1)

STT_MODEL   = "nova-3-medical"
TTS_MODEL   = "aura-2-thalia-en"
GPT_MODEL   = "gpt-4o-mini"          # streaming
RATE        = 48_000                 # Aura-2 optimal rate (also works at 16 k)
CHUNK       = 8_000                  # 8000 / 48000 = 166 ms chunks
AUDIO_FMT   = pyaudio.paInt16
LAT_BUDGET  = {"stt":300, "gpt":200, "tts":250}  # ms

# ------------ 1. Globals / helpers ------------------------------------------------------

audio_q   : asyncio.Queue[bytes] = asyncio.Queue()
utter_q   : asyncio.Queue[str]   = asyncio.Queue()   # STT → GPT
token_q   : asyncio.Queue[str]   = asyncio.Queue()   # GPT → TTS
p         = pyaudio.PyAudio()
start_ts  = datetime.now()

def log(msg:str): print(f"[{(datetime.now()-start_ts).total_seconds():6.2f}s] {msg}")

# ------------ 2. Microphone task --------------------------------------------------------

def mic_cb(indata, frame_count, time_info, status):
    # Non-blocking put
    try: audio_q.put_nowait(indata)
    except asyncio.QueueFull: pass
    return (indata, pyaudio.paContinue)

async def mic_task():
    stream = p.open(format=AUDIO_FMT, channels=1, rate=RATE,
                    input=True, frames_per_buffer=CHUNK,
                    stream_callback=mic_cb)
    stream.start_stream()
    log("🎙  Mic streaming …  Ctrl-C to stop")
    try:
        while stream.is_active(): await asyncio.sleep(0.1)
    finally:
        stream.stop_stream(); stream.close(); p.terminate()

# ------------ 3. Deepgram STT tasks -----------------------------------------------------

async def stt_sender(ws):
    """Send mic PCM -> Deepgram"""
    while True:
        chunk = await audio_q.get()
        await ws.send(chunk)

def extract_final_transcript(msg:dict) -> str|None:
    if not msg.get("is_final"): return None
    alt = msg.get("channel",{}).get("alternatives",[{}])[0]
    return alt.get("transcript","").strip()

async def stt_receiver(ws):
    """Receive transcripts; push completed utterances to GPT queue"""
    async for raw in ws:
        res = json.loads(raw)
        text = extract_final_transcript(res)
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

# ------------ 4. GPT-4o streaming task --------------------------------------------------

oa_client = OpenAI(api_key=OA_API)

async def gpt_worker():
    """For each utterance -> stream GPT response tokens -> token_q"""
    while True:
        user_utt = await utter_q.get()
        log(f"📝 User: {user_utt}")
        # Stream completion
        t0 = time.perf_counter()
        stream = oa_client.chat.completions.create(
            model=GPT_MODEL,
            messages=[{"role":"user","content":user_utt}],
            stream=True,
        )
        first_tok = True
        for chunk in stream:
            if chunk.choices[0].delta.content is None: continue
            tok = chunk.choices[0].delta.content
            if first_tok:
                dt=int((time.perf_counter()-t0)*1000)
                log(f"⚡ GPT first token {dt} ms")
                first_tok=False
            await token_q.put(tok)
        # mark end
        await token_q.put("[[FLUSH]]")

# ------------ 5. Deepgram Aura-2 TTS / Speaker -----------------------------------------

class Speaker:
    """Small blocking playback thread with a queue of PCM bytes"""
    def __init__(self, rate=RATE, chunk=CHUNK):
        self.q:queue.Queue[bytes]=queue.Queue()
        self.exit=threading.Event()
        self.stream=p.open(format=AUDIO_FMT, channels=1, rate=rate,
                           output=True, frames_per_buffer=chunk)
        self.th=threading.Thread(target=self.run,daemon=True)
    def start(self): self.th.start()
    def stop(self): self.exit.set(); self.th.join(); self.stream.close()
    def play(self,data:bytes): self.q.put(data)
    def run(self):
        while not self.exit.is_set():
            try: self.stream.write(self.q.get(timeout=0.1))
            except queue.Empty: pass

async def tts_sender(ws):
    """Read tokens from GPT and send Speak messages"""
    buffer=[]
    while True:
        tok=await token_q.get()
        if tok=="[[FLUSH]]":
            if buffer:
                await ws.send(json.dumps({"type":"Speak","text":"".join(buffer)}))
                buffer=[]
            await ws.send(json.dumps({"type":"Flush"}))
        else:
            buffer.append(tok)
            # micro-batching: send every ~20 chars
            if sum(len(t) for t in buffer)>=20:
                await ws.send(json.dumps({"type":"Speak","text":"".join(buffer)}))
                buffer=[]

async def tts_receiver(ws):
    speaker=Speaker(); speaker.start()
    first=False; t0=0
    try:
        async for msg in ws:
            if isinstance(msg,str):
                continue   # JSON control; ignore
            elif isinstance(msg,bytes):
                if not first:
                    t0=time.perf_counter(); first=True
                    log("🎧 Aura audio started")
                speaker.play(msg)
    finally:
        speaker.stop()

async def run_tts():
    url=(f"wss://api.deepgram.com/v1/speak?"
         f"model={TTS_MODEL}&encoding=linear16&sample_rate={RATE}")
    async with websockets.connect(url,
             extra_headers={"Authorization": f"Token {DG_API}"}) as ws:
        log("🟢 TTS WebSocket open")
        await asyncio.gather(tts_sender(ws), tts_receiver(ws))

# ------------ 6. Orchestrator -----------------------------------------------------------

async def main():
    tasks=[
        asyncio.create_task(mic_task()),
        asyncio.create_task(run_stt()),
        asyncio.create_task(gpt_worker()),
        asyncio.create_task(run_tts()),
    ]
    try:
        await asyncio.gather(*tasks)
    except KeyboardInterrupt:
        log("🛑 Ctrl-C, shutting down…")
        for t in tasks: t.cancel()

if __name__=="__main__":
    asyncio.run(main())
