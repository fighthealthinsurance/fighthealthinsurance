import React, { useEffect, useRef, useState } from "react";
import { Alert, Box, Button, Group, Text, Textarea } from "@mantine/core";
import { createMoonshineClient } from "./moonshine_local_stt";

interface VoiceIntakeProps {
  enabledLocalSTT: boolean;
  onSubmitTranscript: (transcript: string) => void;
  onSwitchToTyping: () => void;
}

const VoiceIntake: React.FC<VoiceIntakeProps> = ({ enabledLocalSTT, onSubmitTranscript, onSwitchToTyping }) => {
  const [isInitializing, setIsInitializing] = useState(false);
  const [isRecording, setIsRecording] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [transcript, setTranscript] = useState("");
  const streamRef = useRef<MediaStream | null>(null);
  const recorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const supportsVoiceCapture = Boolean(navigator.mediaDevices?.getUserMedia) && typeof MediaRecorder !== "undefined";

  const stopActiveStreamTracks = () => {
    streamRef.current?.getTracks().forEach((track) => track.stop());
    streamRef.current = null;
  };

  useEffect(() => () => {
    if (recorderRef.current && recorderRef.current.state !== "inactive") {
      recorderRef.current.stop();
    }
    recorderRef.current = null;
    stopActiveStreamTracks();
  }, []);

  const startRecording = async () => {
    setError(null);
    if (!enabledLocalSTT || !supportsVoiceCapture) {
      setError("Voice capture is unavailable in this browser. Please switch to typing.");
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      streamRef.current = stream;
      chunksRef.current = [];
      const rec = new MediaRecorder(stream);
      rec.ondataavailable = (e) => chunksRef.current.push(e.data);
      rec.start();
      recorderRef.current = rec;
      setIsRecording(true);
    } catch {
      setError("Microphone permission was denied or unavailable. Please switch to typing.");
      stopActiveStreamTracks();
    }
  };

  const stopRecording = async () => {
    if (!recorderRef.current) return;
    setIsRecording(false);
    const recorder = recorderRef.current;
    const recorderMimeType = recorder.mimeType || "audio/webm";
    const stopPromise = new Promise<void>((resolve) => {
      recorder.onstop = () => resolve();
    });
    recorder.stop();
    stopActiveStreamTracks();

    await stopPromise;
    const blobChunks = chunksRef.current.filter((chunk) => chunk.size > 0);
    const audioBlob = new Blob(blobChunks, { type: recorderMimeType });
    setIsInitializing(true);
    try {
      const client = await createMoonshineClient();
      const nextTranscript = await client.transcribe(audioBlob);
      setTranscript(nextTranscript);
    } catch {
      setError("Local transcription unavailable. You can switch to typing.");
    } finally {
      chunksRef.current = [];
      recorderRef.current = null;
      setIsInitializing(false);
    }
  };

  return (
    <Box mt="sm" p="sm" style={{ border: "1px solid #ddd", borderRadius: 8 }}>
      <Text size="sm" mb="xs">Voice transcription runs in your browser when supported. Your audio is not uploaded; only the transcript you choose to submit is sent.</Text>
      {!enabledLocalSTT && <Alert color="yellow">Local browser transcription is currently disabled. You can continue by typing.</Alert>}
      {!supportsVoiceCapture && <Alert color="yellow">Your browser does not support microphone capture for this feature. Please continue by typing.</Alert>}
      {error && <Alert color="yellow">{error}</Alert>}
      <Group mt="xs">
        {!isRecording ? <Button size="xs" disabled={isInitializing || !enabledLocalSTT || !supportsVoiceCapture} onClick={startRecording}>Start recording</Button> : <Button size="xs" color="red" onClick={stopRecording}>Stop recording</Button>}
        <Button size="xs" variant="default" onClick={onSwitchToTyping}>Switch to typing</Button>
        <Button size="xs" variant="light" onClick={() => setTranscript("")}>Clear transcript</Button>
      </Group>
      {isInitializing && <Text size="xs" mt="xs">Loading local speech model and transcribing…</Text>}
      <Textarea
        mt="xs"
        minRows={4}
        value={transcript}
        onChange={(e) => setTranscript(e.currentTarget.value)}
        placeholder="Review and edit your transcript before sending. Voice transcripts can contain mistakes."
      />
      <Button mt="xs" size="xs" disabled={!transcript.trim()} onClick={() => onSubmitTranscript(transcript.trim())}>Submit transcript</Button>
    </Box>
  );
};

export default VoiceIntake;
