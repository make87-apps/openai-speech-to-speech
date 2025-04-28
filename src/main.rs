use base64::engine::general_purpose;
use base64::Engine;
use futures_util::StreamExt;
use make87_messages::audio::frame_pcm_s16le::Fraction;
use make87_messages::audio::FramePcmS16le;
use make87_messages::core::Header;
use make87_messages::google::protobuf::Timestamp;
use openai_api_rs::realtime::api::RealtimeClient;
use openai_api_rs::realtime::client_event::InputAudioBufferAppend;
use openai_api_rs::realtime::client_event::SessionUpdate;
use openai_api_rs::realtime::server_event::ServerEvent;
use openai_api_rs::realtime::types::{Session, TurnDetection};
use std::env;
use std::process::exit;
use tokio_tungstenite::tungstenite::protocol::Message;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    make87::initialize();

    let api_key = env::var("OPENAI_API_KEY").unwrap().to_string();
    let model = "gpt-4o-realtime-preview".to_string();

    let (stdin_tx, stdin_rx) = futures_channel::mpsc::unbounded();
    tokio::spawn(read_user_audio(stdin_tx));

    let realtime_client = RealtimeClient::new(api_key, model);

    let (write, read) = realtime_client.connect().await.unwrap();
    println!("WebSocket handshake complete");

    let stdin_to_ws = stdin_rx.map(Ok).forward(write);

    let topic_key = make87::resolve_topic_name("OPENAI_AUDIO").unwrap();
    let publisher = make87::get_publisher::<FramePcmS16le>(topic_key).unwrap();

    let ws_to_stdout = {
        read.for_each(|message| async {
            let message = message.unwrap();
            match message {
                Message::Text(_) => {
                    let data = message.clone().into_data();
                    let server_event: ServerEvent = serde_json::from_slice(&data).unwrap();
                    match server_event {
                        ServerEvent::ResponseAudioDelta(_event) => {
                            let output_message = FramePcmS16le {
                                header: Some(Header {
                                    timestamp: Some(Timestamp::get_current_time()),
                                    reference_id: 0,
                                    entity_path: String::new(),
                                }),
                                data: general_purpose::STANDARD.decode(_event.delta).unwrap(),
                                pts: 0,
                                time_base: Some(Fraction { num: 1, den: 24000 }),
                                channels: 1,
                            };
                            let _ = publisher.publish(&output_message);
                        }
                        _ => {}
                    }
                }
                Message::Close(_) => {
                    eprintln!("Close");
                    exit(0);
                }
                _ => {}
            }
        })
    };

    make87::keep_running();

    Ok(())
}

async fn read_user_audio(tx: futures_channel::mpsc::UnboundedSender<Message>) {
    // Subscribe to AUDIO_OUT topic and forward to broadcast channel
    if let Some(topic_key) = make87::resolve_topic_name("USER_AUDIO") {
        if let Some(sub) = make87::get_subscriber::<FramePcmS16le>(topic_key) {
            let init_event = SessionUpdate {
                session: Session {
                    modalities: Some(vec!["audio".to_string()]),
                    turn_detection: Some(TurnDetection::ServerVAD {
                        threshold: 0.5,
                        prefix_padding_ms: 300,
                        silence_duration_ms: 300,
                    }),
                    ..Default::default()
                },
                ..Default::default()
            };
            let init_message: Message = init_event.into();
            tx.unbounded_send(init_message).unwrap();

            sub.subscribe(move |message| {
                let event = InputAudioBufferAppend {
                    audio: general_purpose::STANDARD.encode(message.data),
                    ..Default::default()
                };
                let message: Message = event.into();
                tx.unbounded_send(message).unwrap();
            })
                .unwrap();
        }
    }
}