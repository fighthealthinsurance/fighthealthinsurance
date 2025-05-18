import React, { useState, useEffect, useRef } from "react";
import { createRoot } from "react-dom/client";
import ReactMarkdown from "react-markdown";
import {
  Box,
  Container,
  Textarea,
  Button,
  Paper,
  Text as MantineText,
  ScrollArea,
  Loader,
  Group as MantineGroup,
  MantineProvider,
  Title,
  Image,
  Flex,
} from "@mantine/core";

// Define types for our chat messages
interface ChatMessage {
  role: "user" | "assistant";
  content: string;
  timestamp?: string;
  status?: "done" | "typing" | "error";
  uid?: string;
}

interface ChatState {
  messages: ChatMessage[];
  isLoading: boolean;
  input: string;
  chatId: string | null;
  error: string | null;
}

// Typing animation component for loading state
const TypingAnimation: React.FC = () => {
  const [dots, setDots] = useState(".");

  useEffect(() => {
    const interval = setInterval(() => {
      setDots((prevDots) => {
        if (prevDots.length >= 3) return ".";
        return prevDots + ".";
      });
    }, 500);

    return () => clearInterval(interval);
  }, []);

  return <span style={{ marginLeft: 4 }}>Typing{dots}</span>;
};

// Get a session key or use an existing one from localStorage
const getSessionKey = (): string => {
  const existingKey = localStorage.getItem("fhi_chat_session_key");
  if (existingKey) {
    return existingKey;
  }

  // Generate a new random session key
  const newKey =
    Math.random().toString(36).substring(2, 15) +
    Math.random().toString(36).substring(2, 15);
  localStorage.setItem("fhi_chat_session_key", newKey);
  return newKey;
};

const ChatInterface: React.FC = () => {
  // State for our chat interface
  const [state, setState] = useState<ChatState>({
    messages: [],
    isLoading: false,
    input: "",
    chatId: localStorage.getItem("fhi_chat_id"),
    error: null,
  });
  const wsRef = useRef<WebSocket | null>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);

  // Scroll to the bottom when new messages arrive
  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  };

  useEffect(() => {
    scrollToBottom();
  }, [state.messages]);

  // Connect to the WebSocket when the component mounts
  useEffect(() => {
    const connectWebSocket = () => {
      const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      const wsUrl = `${protocol}//${window.location.host}/ws/ongoing-chat/`;

      const ws = new WebSocket(wsUrl);
      ws.onopen = () => {
        console.log("WebSocket connected");
        wsRef.current = ws;

        // If we have a chat ID, request the chat history
        if (state.chatId) {
          ws.send(
            JSON.stringify({
              chat_id: state.chatId,
              replay: true,
            }),
          );
        }
      };

      ws.onmessage = (event) => {
        const data = JSON.parse(event.data);
        console.log("Received message:", data);

        // Handle different types of messages from the server
        if (data.error) {
          setState((prev) => ({
            ...prev,
            isLoading: false,
            error: data.error,
          }));
        } else if (data.messages) {
          // This is a history replay
          setState((prev) => ({
            ...prev,
            messages: data.messages,
          }));
        } else if (data.chat_id && !state.chatId) {
          // Save the chat ID for future use
          localStorage.setItem("fhi_chat_id", data.chat_id);
          setState((prev) => ({
            ...prev,
            chatId: data.chat_id,
          }));
        }

        if (data.content && data.role) {
          // This is a new message
          setState((prev) => ({
            ...prev,
            messages: [
              ...prev.messages,
              {
                role: data.role,
                content: data.content,
                timestamp: data.timestamp || new Date().toISOString(),
                status: "done",
              },
            ],
            isLoading: false,
          }));
        } else if (data.status) {
          // This is a status update (typing, etc.)
          setState((prev) => ({
            ...prev,
            isLoading: true,
          }));
        }
      };

      ws.onclose = () => {
        console.log("WebSocket disconnected");
        wsRef.current = null;
        // Attempt to reconnect after a short delay
        setTimeout(connectWebSocket, 3000);
      };

      ws.onerror = (error) => {
        console.error("WebSocket error:", error);
      };
    };

    connectWebSocket();

    // Clean up WebSocket connection when component unmounts
    return () => {
      if (wsRef.current) {
        wsRef.current.close();
      }
    };
  }, []);

  // Handle sending a new message
  const handleSendMessage = () => {
    if (!state.input.trim() || !wsRef.current) return;

    // Add the user message to the UI immediately
    const userMessage: ChatMessage = {
      role: "user",
      content: state.input,
      timestamp: new Date().toISOString(),
      status: "done",
    };

    setState((prev) => ({
      ...prev,
      messages: [...prev.messages, userMessage],
      input: "",
      isLoading: true,
    }));

    // Send the message to the server
    const messageToSend = {
      chat_id: state.chatId, // Can be null if starting a new chat
      content: state.input,
      is_patient: true, // This is for the patient-facing version
      session_key: getSessionKey(),
    };

    wsRef.current.send(JSON.stringify(messageToSend));
  };

  // Render each chat message
  const renderMessage = (message: ChatMessage, index: number) => {
    const isUser = message.role === "user";

    return (
      <Paper
        key={index}
        shadow="xs"
        p="md"
        style={{
          backgroundColor: isUser ? "#f0f9ff" : "#f9fafb",
          marginBottom: 10,
          width: "100%",
          marginLeft: isUser ? "auto" : 0,
          marginRight: isUser ? 0 : "auto",
        }}
      >
        <Flex align="center" gap="xs">
          {!isUser && (
            <Image
              src="/static/images/better-logo.png"
              width={24}
              height={24}
              alt="FHI Logo"
            />
          )}
          <MantineText fw={500} size="sm" c={isUser ? "blue" : "dark"}>
            {isUser ? "You" : "FightHealthInsurance Assistant"}
          </MantineText>
        </Flex>{" "}
        <Box mt="xs">
          {message.status === "typing" ? (
            <TypingAnimation />
          ) : (
            <ReactMarkdown>{message.content}</ReactMarkdown>
          )}
        </Box>
      </Paper>
    );
  };

  return (
    <Container size="md" my="lg">
      <Paper
        shadow="md"
        p="md"
        withBorder
        style={{
          height: "calc(100vh - 150px)",
          display: "flex",
          flexDirection: "column",
        }}
      >
        <Title order={2} size="xl" fw={700} mb="md">
          Fight Health Insurance Chat
        </Title>

        {state.error && (
          <MantineText c="red" size="sm" mb="md">
            {state.error}
          </MantineText>
        )}

        <ScrollArea
          flex={1}
          mb="md"
          style={{ display: "flex", flexDirection: "column" }}
        >
          <Box p="xs">
            {state.messages.length === 0 ? (
              <MantineText ta="center" c="dimmed" mt="xl">
                No messages yet. Start a conversation!
              </MantineText>
            ) : (
              state.messages.map(renderMessage)
            )}

            {state.isLoading && (
              <Paper
                shadow="xs"
                p="md"
                style={{ backgroundColor: "#f9fafb", marginBottom: 10 }}
              >
                <Flex align="center" gap="xs">
                  <Image
                    src="/static/images/better-logo.png"
                    width={24}
                    height={24}
                    alt="FHI Logo"
                  />
                  <MantineText fw={500} size="sm" c="dark">
                    FightHealthInsurance Assistant
                  </MantineText>
                </Flex>
                <Box mt="xs">
                  <TypingAnimation />
                </Box>
              </Paper>
            )}
            <div ref={messagesEndRef} />
          </Box>
        </ScrollArea>
        <div style={{ display: "flex", width: "100%" }}>
          <Textarea
            placeholder="Type your message..."
            value={state.input}
            onChange={(e) => setState({ ...state, input: e.target.value })}
            onKeyDown={(e) =>
              e.key === "Enter" && !e.shiftKey && handleSendMessage()
            }
            minRows={4}
            style={{ width: "100%" }}
          />
          <Button
            onClick={handleSendMessage}
            disabled={!state.input.trim() || state.isLoading}
          >
            Send
          </Button>
        </div>
      </Paper>
    </Container>
  );
};

// Initialize the app when the DOM is loaded
document.addEventListener("DOMContentLoaded", () => {
  const chatRoot = document.getElementById("chat-interface-root");
  if (chatRoot) {
    console.log("Chat interface root element found");
    const root = createRoot(chatRoot);
    root.render(
      <MantineProvider>
        <ChatInterface />
      </MantineProvider>,
    );
  } else {
    console.error("Chat interface root element not found");
  }
});

console.log("Chat interface script loaded");
export default ChatInterface;
