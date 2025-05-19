import React, { useState, useEffect, useRef, useCallback } from "react";
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
  ActionIcon,
  FileButton,
  Tooltip,
  TextInput,
  Checkbox,
  Stack,
  Anchor,
  Card,
  Stepper,
} from "@mantine/core";

import { IconPaperclip, IconSend, IconUser, IconArrowRight } from "./icons";
import { recognize } from "./scrub_ocr";

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
  isProcessingFile: boolean;
}

interface UserInfo {
  firstName: string;
  lastName: string;
  email: string;
  address: string;
  city: string;
  state: string;
  zipCode: string;
  acceptedTerms: boolean;
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

// Save user info to local storage
const saveUserInfo = (userInfo: UserInfo): void => {
  localStorage.setItem("fhi_user_info", JSON.stringify(userInfo));
};

// Get user info from local storage
const getUserInfo = (): UserInfo | null => {
  const storedInfo = localStorage.getItem("fhi_user_info");
  if (storedInfo) {
    try {
      return JSON.parse(storedInfo) as UserInfo;
    } catch (e) {
      console.error("Error parsing stored user info:", e);
      return null;
    }
  }
  return null;
};

// Replace personal info in a message with placeholders
const scrubPersonalInfo = (message: string, userInfo: UserInfo): string => {
  if (!userInfo) return message;

  let scrubbedMessage = message;

  // Replace name - use word boundaries to avoid partial matches
  if (userInfo.firstName) {
    scrubbedMessage = scrubbedMessage.replace(
      new RegExp(`\\b${userInfo.firstName}\\b`, "gi"),
      "[FIRST_NAME]",
    );
  }

  if (userInfo.lastName) {
    scrubbedMessage = scrubbedMessage.replace(
      new RegExp(`\\b${userInfo.lastName}\\b`, "gi"),
      "[LAST_NAME]",
    );
  }

  // Replace address
  if (userInfo.address) {
    scrubbedMessage = scrubbedMessage.replace(
      new RegExp(userInfo.address, "gi"),
      "[ADDRESS]",
    );
  }

  // Replace city
  if (userInfo.city) {
    scrubbedMessage = scrubbedMessage.replace(
      new RegExp(`\\b${userInfo.city}\\b`, "gi"),
      "[CITY]",
    );
  }

  // Replace state
  if (userInfo.state) {
    scrubbedMessage = scrubbedMessage.replace(
      new RegExp(`\\b${userInfo.state}\\b`, "gi"),
      "[STATE]",
    );
  }

  // Replace zip code
  if (userInfo.zipCode) {
    scrubbedMessage = scrubbedMessage.replace(
      new RegExp(`\\b${userInfo.zipCode}\\b`, "gi"),
      "[ZIP_CODE]",
    );
  }

  // Replace email
  if (userInfo.email) {
    // Email regex to avoid partial matches
    scrubbedMessage = scrubbedMessage.replace(
      new RegExp(userInfo.email.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "gi"),
      "[EMAIL]",
    );
  }

  return scrubbedMessage;
};

// Replace placeholders in a message with actual personal info
const restorePersonalInfo = (message: string, userInfo: UserInfo): string => {
  if (!userInfo) return message;

  let restoredMessage = message;

  // Restore name
  if (userInfo.firstName) {
    restoredMessage = restoredMessage.replace(
      /\[FIRST_NAME\]/g,
      userInfo.firstName,
    );
  }

  if (userInfo.lastName) {
    restoredMessage = restoredMessage.replace(
      /\[LAST_NAME\]/g,
      userInfo.lastName,
    );
  }

  // Restore address
  if (userInfo.address) {
    restoredMessage = restoredMessage.replace(/\[ADDRESS\]/g, userInfo.address);
  }

  // Restore city
  if (userInfo.city) {
    restoredMessage = restoredMessage.replace(/\[CITY\]/g, userInfo.city);
  }

  // Restore state
  if (userInfo.state) {
    restoredMessage = restoredMessage.replace(/\[STATE\]/g, userInfo.state);
  }

  // Restore zip code
  if (userInfo.zipCode) {
    restoredMessage = restoredMessage.replace(
      /\[ZIP_CODE\]/g,
      userInfo.zipCode,
    );
  }

  // Restore email
  if (userInfo.email) {
    restoredMessage = restoredMessage.replace(/\[EMAIL\]/g, userInfo.email);
  }

  return restoredMessage;
};

const ChatInterface: React.FC = () => {
  // State for our chat interface
  const [state, setState] = useState<ChatState>({
    messages: [],
    isLoading: false,
    input: "",
    chatId: localStorage.getItem("fhi_chat_id"),
    error: null,
    isProcessingFile: false,
  });

  const wsRef = useRef<WebSocket | null>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);

  // Initialize chat interface on load
  useEffect(() => {
    const userInfo = getUserInfo();

    // Add a welcome message from the assistant
    if (state.messages.length === 0) {
      const welcomeMessage: ChatMessage = {
        role: "assistant",
        content: userInfo
          ? `Welcome to Fight Health Insurance, ${userInfo.firstName}! I'm here to help you with your health insurance questions and appeals. Feel free to ask me anything or upload relevant documents using the paperclip icon.`
          : "Welcome to Fight Health Insurance! I'm here to help you with your health insurance questions and appeals. Feel free to ask me anything or upload relevant documents using the paperclip icon.",
        timestamp: new Date().toISOString(),
        status: "done",
      };

      setState((prev) => ({
        ...prev,
        messages: [welcomeMessage],
      }));
    }
  }, [state.messages.length]);

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

        // Get user info for restoring personal info
        const userInfo = getUserInfo();

        // Handle different types of messages from the server
        if (data.error) {
          // Skip the professional user error message as we're in patient mode
          if (
            data.error.includes("Professional user not found or not active")
          ) {
            return;
          }

          setState((prev) => ({
            ...prev,
            isLoading: false,
            error: data.error,
          }));
        } else if (data.messages) {
          // This is a history replay
          // Restore personal info in the message content if we have user info
          const processedMessages = data.messages.map((msg: ChatMessage) => {
            if (msg.role === "assistant" && userInfo) {
              return {
                ...msg,
                content: restorePersonalInfo(msg.content, userInfo),
              };
            }
            return msg;
          });

          setState((prev) => ({
            ...prev,
            messages: processedMessages,
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
          // This is a new message - restore personal info if it's from the assistant
          const processedContent =
            data.role === "assistant" && userInfo
              ? restorePersonalInfo(data.content, userInfo)
              : data.content;

          setState((prev) => ({
            ...prev,
            messages: [
              ...prev.messages,
              {
                role: data.role,
                content: processedContent,
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

  // Handle file upload
  const handleFileUpload = useCallback(
    async (file: File | null) => {
      if (!file || !wsRef.current) return;

      try {
        setState((prev) => ({ ...prev, isProcessingFile: true }));

        // Process the file with local OCR instead of sending to server
        let fileContent = "";

        // Use a function to collect text from OCR
        const addText = (text: string) => {
          fileContent += text;
        };

        // Use the local OCR implementation
        await recognize(file, addText);

        // Add a user message showing the file was uploaded
        const userMessage: ChatMessage = {
          role: "user",
          content: `I've uploaded a document: ${file.name}`,
          timestamp: new Date().toISOString(),
          status: "done",
        };

        setState((prev) => ({
          ...prev,
          messages: [...prev.messages, userMessage],
          isLoading: true,
        }));

        // Get user info for scrubbing
        const userInfo = getUserInfo();

        // Scrub personal information in the extracted content
        const scrubbedContent = userInfo
          ? scrubPersonalInfo(fileContent, userInfo)
          : fileContent;

        // Send extracted content to the chat
        if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
          const messageToSend = {
            chat_id: state.chatId,
            content: scrubbedContent, // Use scrubbed content
            is_patient: true,
            session_key: getSessionKey(),
            is_document: true,
            document_name: file.name,
          };

          wsRef.current.send(JSON.stringify(messageToSend));
        }
      } catch (error) {
        console.error("Error processing file:", error);
        setState((prev) => ({
          ...prev,
          error: "Error processing the uploaded file. Please try again.",
          isProcessingFile: false,
        }));
      } finally {
        setState((prev) => ({ ...prev, isProcessingFile: false }));
      }
    },
    [state.chatId],
  );

  // Handle sending a new message
  const handleSendMessage = () => {
    if (!state.input.trim() || !wsRef.current || state.isLoading) return;

    // Get user info for scrubbing
    const userInfo = getUserInfo();

    // Add the user message to the UI immediately - show the original (unscrubbed) message to the user
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

    // Scrub personal information before sending
    const scrubbedContent = userInfo
      ? scrubPersonalInfo(state.input, userInfo)
      : state.input;

    // Send the message to the server
    const messageToSend = {
      chat_id: state.chatId, // Can be null if starting a new chat
      email: userInfo?.email, // Include email for server-side processing
      content: scrubbedContent,
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

        <Flex justify="space-between" align="center" mb="md">
          <Box>
            {state.error && (
              <MantineText c="red" size="sm">
                {state.error}
              </MantineText>
            )}
          </Box>
          <Button
            variant="subtle"
            size="xs"
            onClick={() => {
              // Clear user info from local storage and redirect to consent form
              localStorage.removeItem("fhi_user_info");
              window.location.href = "/chat-consent";
            }}
            leftSection={<IconUser size={14} />}
          >
            Update Personal Info
          </Button>
        </Flex>

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

        <Box p="xs" style={{ width: "100%", marginTop: "auto" }}>
          <Paper
            radius="lg"
            p="xs"
            shadow="sm"
            withBorder
            style={{ width: "100%" }}
          >
            <Flex align="flex-end" style={{ width: "100%" }}>
              <Tooltip label="Upload PDF" position="top">
                <FileButton
                  onChange={handleFileUpload}
                  accept="application/pdf"
                  disabled={state.isLoading || state.isProcessingFile}
                >
                  {(props) => (
                    <ActionIcon
                      {...props}
                      size="lg"
                      variant="light"
                      radius="xl"
                      color="gray"
                      loading={state.isProcessingFile}
                      disabled={state.isLoading || state.isProcessingFile}
                      mr="xs"
                    >
                      <IconPaperclip size={18} />
                    </ActionIcon>
                  )}
                </FileButton>
              </Tooltip>

              <ScrollArea.Autosize mah={200} style={{ flex: 1, width: "100%" }}>
                <Textarea
                  placeholder={
                    state.isLoading
                      ? "Assistant is typing..."
                      : "Type your message..."
                  }
                  value={state.input}
                  onChange={(e: React.ChangeEvent<HTMLTextAreaElement>) =>
                    setState({ ...state, input: e.target.value })
                  }
                  onKeyDown={(e: React.KeyboardEvent<HTMLTextAreaElement>) => {
                    if (e.key === "Enter" && !e.shiftKey) {
                      e.preventDefault();
                      handleSendMessage();
                    }
                  }}
                  autosize
                  minRows={3}
                  maxRows={6}
                  variant="unstyled"
                  disabled={state.isLoading || state.isProcessingFile}
                  style={{ flex: 1, width: "100%" }}
                />
              </ScrollArea.Autosize>

              <ActionIcon
                onClick={handleSendMessage}
                size="lg"
                radius="xl"
                color="blue"
                variant="filled"
                disabled={
                  !state.input.trim() ||
                  state.isLoading ||
                  state.isProcessingFile
                }
                ml="xs"
              >
                <IconSend size={18} />
              </ActionIcon>
            </Flex>
          </Paper>
        </Box>
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
