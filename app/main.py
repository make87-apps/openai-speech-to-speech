import os
import json
import asyncio
import threading
import base64
import queue
from typing import Callable, Dict

from make87_messages.core.header_pb2 import Header
from websockets.asyncio.client import connect
import make87
from make87_messages.audio.frame_pcm_s16le_pb2 import FramePcmS16le


# A global queue fed by your subscriber callback
pcm_queue = queue.Queue()


async def handle_connection(publish_fn: Callable[[FramePcmS16le], None], ws_url: str, headers: Dict[str, str]):
    print("🔌 [handle_connection] Connecting to OpenAI Realtime API…")
    async with connect(ws_url, additional_headers=headers) as ws:
        print("✅ [handle_connection] WebSocket connected")

        # 1) Send session.update with your Jarvis instructions
        print("✨ [handle_connection] Sending session.update")
        await ws.send(
            json.dumps(
                {
                    "type": "session.update",
                    "session": {
                        "modalities": ["audio", "text"],
                        "input_audio_format": "pcm16",
                        "output_audio_format": "pcm16",
                        "voice": "echo",  # deep, resonant
                        "temperature": 0.6,
                        "instructions": (
                            "Speak with a calm, British butler tone, " "articulate clearly like Jarvis from Iron Man."
                        ),
                        "turn_detection": {
                            "type": "server_vad",
                            "threshold": 0.5,
                            "prefix_padding_ms": 300,
                            "silence_duration_ms": 200,
                            "create_response": True,
                            "interrupt_response": True,
                        },
                    },
                }
            )
        )
        print("✅ [handle_connection] session.update sent")

        # 2) **Wait** for the server to acknowledge the update
        print("⌛️ [handle_connection] Waiting for session.updated…")
        while True:
            msg_text = await ws.recv()
            msg = json.loads(msg_text)
            mtype = msg.get("type")
            print(f"📥 [handle_connection] {mtype}")
            if mtype == "session.updated":
                print("✅ [handle_connection] session.updated received — now streaming audio")
                break
            # you can optionally handle other early events here,
            # e.g. session.created, error, etc.

        # --- PTS setup ------------------------------------------------
        # 24 kHz, mono, s16le: 2 bytes/sample × 1 ch → 2 bytes per frame
        bytes_per_frame = 2 * 1
        total_frames = 0

        # 3a) Continuously send whatever arrives in pcm_queue
        async def send_audio():
            print("▶️ [send_audio] Started")
            while True:
                pcm = await asyncio.get_event_loop().run_in_executor(None, pcm_queue.get)
                print(f"🔊 [send_audio] Sending audio chunk ({len(pcm)} bytes)")
                await ws.send(
                    json.dumps(
                        {
                            "type": "input_audio_buffer.append",
                            "audio": base64.b64encode(pcm).decode("utf-8"),
                        }
                    )
                )

        # 3b) Receive responses and publish
        async def recv_and_publish():
            nonlocal total_frames
            print("👂 [recv_and_publish] Started")
            async for msg_text in ws:
                msg = json.loads(msg_text)
                mtype = msg.get("type")
                print(f"📥 [recv] {mtype}")
                if mtype == "response.audio.delta":
                    # decode
                    pcm_out = base64.b64decode(msg["delta"])
                    n_frames = len(pcm_out) // bytes_per_frame

                    # compute PTS (in ticks of 1/24000s)
                    pts = total_frames
                    total_frames += n_frames

                    print(f"🔉 [recv_and_publish] Received {n_frames} frames, pts={pts}")

                    # build and publish
                    header = Header()
                    header.timestamp.GetCurrentTime()
                    header.entity_path = "/openai/audio"
                    message = FramePcmS16le(
                        header=header,
                        data=pcm_out,
                        pts=pts,
                        time_base=FramePcmS16le.Fraction(num=1, den=24000),
                        channels=1,
                    )
                    publish_fn(message)

                elif mtype == "response.done":
                    print("🏁 [recv_and_publish] response.done received — ending turn")

        print("🚀 [handle_connection] Launching send/recv tasks")
        await asyncio.gather(send_audio(), recv_and_publish())
        print("🔒 [handle_connection] Connection handler exiting")


async def connect_forever(publish_fn: Callable[[FramePcmS16le], None], ws_url: str, headers: Dict[str, str]):
    backoff = 0.5
    while True:
        print(f"🔄 [connect_forever] Attempting connection (backoff: {backoff}s)")
        try:
            await handle_connection(publish_fn=publish_fn, ws_url=ws_url, headers=headers)
            print("✅ [connect_forever] Turn complete, reconnecting immediately")
            backoff = 0.5  # reset after clean turn
        except Exception as e:
            print(f"⚠️ [connect_forever] Connection lost: {e!r}. Retrying in {backoff}s…")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 5)


def main():
    print("🚀 [main] Initializing make87")
    make87.initialize()

    ws_url = "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview"
    headers = {
        "Authorization": f"Bearer {make87.get_config_value('OPENAI_API_KEY')}",
        "OpenAI-Beta": "realtime=v1",
    }

    user_sub = make87.get_subscriber(name="USER_AUDIO", message_type=FramePcmS16le)
    ai_pub = make87.get_publisher(name="OPENAI_AUDIO", message_type=FramePcmS16le)
    print("🎧 [main] Subscribed to USER_AUDIO, ready to receive PCM frames")

    user_sub.subscribe(lambda frame: pcm_queue.put(frame.data))
    print("✅ [main] Callback wired: USER_AUDIO → pcm_queue")

    print("🧵 [main] Spawning WebSocket streamer thread")
    threading.Thread(
        target=lambda: asyncio.run(connect_forever(publish_fn=ai_pub.publish, ws_url=ws_url, headers=headers)),
        daemon=True,
    ).start()

    print("🔄 [main] Entering make87.loop()")
    make87.loop()


if __name__ == "__main__":
    main()
