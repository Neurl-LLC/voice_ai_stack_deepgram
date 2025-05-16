# ⚡ Real-Time Voice Agent – <3 s Round-Trip

Mic ▶ **Deepgram Nova-3 (STT)** ▶ **GPT-4o-mini (LLM)** ▶ **Deepgram Aura-2 (TTS)** ▶ Speaker

<p align="center">
<img src="https://raw.githubusercontent.com/Neurl-LLC/voice_ai_stack_deepgram/refs/heads/main/neurl_creators_community.png" width="640">
</p>

> **Goal:** Keep the 95-th percentile end-to-end latency **below 3 second** – from the moment a user finishes speaking until the first audio sample of the assistant’s reply is heard.

---

## ✨ Features

| Component | Target P95 | How we hit it |
|-----------|------------|---------------|
| STT | ≤ 300 ms | 48 kHz WebSocket + `endpointing=200` |
| LLM | ≤ 200 ms (first token) | GPT-4o-mini, streaming, max 60 tokens |
| TTS | ≤ 250 ms TTFB | Aura-2 WebSocket, 20-char micro-batches |
| Playback watchdog | 100–250 ms | Queue-drain + silence detection |

* **Full-duplex** – mic continues in the background (optionally even while Aura speaks).  
* **Live RTT metric** – every turn is timed and printed (`⏱ End-to-end RTT: xxx ms`).  
* **No SDK lock-in** – pure WebSockets, <300 LOC of Python.

---

## 📦 Requirements

* Python ≥ 3.9  
* PortAudio / PyAudio (for mic and playback)  
* A **Deepgram** API key with Nova-3 and Aura-2 access  
* An **OpenAI** API key (GPT-4o-mini)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## **🔧 Configuration**

Create a `.env` next to the script:

```bash
DEEPGRAM_API_KEY=...
OPENAI_API_KEY=sk-...
```

Optional tweaks (inside the script):

| Constant | Default | Notes |
| ----- | ----- | ----- |
| `RATE` | 48000 | Use 16000 for lower bandwidth |
| `CHUNK` | 8000 | Audio frames sent per WS message (\~42 ms) |
| `SEND_EVERY` | 180 | Characters before we flush to Aura |
| `ALLOW_INTERRUPT` | `False` | Set `True` to capture mic during TTS |

---

## **▶ Running**

```python
python realtime_voice_agent.py
```

CLI output (example):

```bash
🔗  Mic → Nova-3 → GPT-4o → Aura-2 – starting …
[ 0.16s] 🎙  Mic streaming …  Ctrl-C to stop
[ 1.20s] 🟢 STT WebSocket open
[ 1.31s] 🟢 TTS WebSocket open
[ 4.85s] 📝 User: Hi, what can you do?
[ 5.33s] ⚡ GPT first token 484 ms
[ 5.58s] 🎧 Aura audio started
[ 5.81s] 🌊 Aura finishing playback...
[ 5.81s] ⏱  End-to-end RTT: 962 ms
[ 5.81s] 🎤  You can speak now …
```

---

## **🛠 How it works**

1. **Microphone → STT**  
    `PyAudio` captures 48 kHz 16-bit mono PCM. Every 42 ms we push a frame to Deepgram’s `/listen` WS.

2. **STT → LLM**  
    When `is_final=true` is received, we drop the transcript into an async queue for GPT-4o.

3. **LLM streaming**  
    GPT-4o streams tokens. Every ≈ 180 chars we send a `{"type":"Speak","text":…}` to Aura-2.

4. **TTS → Speaker**  
    Aura responds with 48 kHz linear16 PCM chunks which we play immediately in a background thread.

5. **Watch-dog** ends playback when either `PlaybackFinished` arrives, the audio queue stays empty ≥ 100 ms, or 3 s of total silence elapse – then we print the RTT and cue the user.

---

## **🧪 Benchmarks**

| Test | RTT (mean) | Notes |
| ----- | ----- | ----- |
| Local MacBook M2 \+ AirPods | **\< 2100 ms** | Wi-Fi, OpenAI/Deepgram |
| GCP e2-medium (us-central) | **\~3100 ms** | Low-variance, wired |

---

## **📚 Docs & Links**

* Deepgram live STT: [https://developers.deepgram.com/reference/speech-to-text-api/listen-streaming](https://developers.deepgram.com/reference/speech-to-text-api/listen-streaming)

* Deepgram Aura-2 TTS: [https://developers.deepgram.com/reference/text-to-speech-api/speak-streaming](https://developers.deepgram.com/reference/text-to-speech-api/speak-streaming)

* OpenAI Chat completions (stream): [https://platform.openai.com/docs/guides/text-generation](https://platform.openai.com/docs/guides/text-generation)

---

## **🤝 Contributing**

PRs welcome\! Open an issue if you spot latency regressions.
