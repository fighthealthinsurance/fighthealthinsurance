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
  Group as MantineGroup,
  MantineProvider,
  Title,
  Image,
  Flex,
  ActionIcon,
  FileButton,
  Tooltip,
} from "@mantine/core";

import { IconPaperclip, IconSend, IconUser, IconRefresh } from "./icons";
import { recognize } from "./scrub_ocr";

const THEME = {
  colors: {
    background: '#f4f6fb',
    buttonBackground: '#e6f4c2',
    buttonText: '#5a6b1b',
  },
  spacing: {
    headerMargin: 16,
  },
  borderRadius: {
    small: 3,
    medium: 'xl', // Changed from 30 to 'xl' for very rounded edges
    large: 24,
    extraLarge: 'xl',
    buttonDefault: '7px', // Added for default button border radius
  },
  buttonSharedStyles: { // Added for common button styles
    background: '#a5c422', // Corresponds to colors.buttonBackground
    color: "#ffffff",
    border: 'none',
    boxShadow: 'none',
    transition: 'background 0.2s',
  },
} as const;

// PWYW Component for the chat interface
const PWYWBanner: React.FC<{ onDismiss: () => void }> = ({ onDismiss }) => {
  const handleSupport = () => {
    // Open Stripe payment page where users can enter their preferred amount
    window.open(`https://buy.stripe.com/5kA03r2ZwbgebyE7ss`, '_blank', 'noopener,noreferrer');
    onDismiss();
  };

  return (
    <Box
      style={{
        background: 'linear-gradient(135deg, #f8f9fa 0%, #e8f5e9 100%)',
        borderRadius: 12,
        padding: '16px',
        margin: '12px 0',
        border: '1px solid #c8e6c9',
      }}
    >
      <Flex justify="space-between" align="flex-start" mb="sm">
        <MantineText fw={600} size="sm" c="dark">
          Help us help others
        </MantineText>
        <ActionIcon
          size="xs"
          variant="subtle"
          onClick={onDismiss}
          aria-label="Dismiss"
          style={{ color: '#666' }}
        >
          ✕
        </ActionIcon>
      </Flex>
      <MantineText size="xs" c="dimmed" mb="sm">
        Fight Health Insurance is free for everyone. If we've helped you, consider supporting our work so we can help more people appeal their denials. Pay what you want on the next page.
      </MantineText>
      <Button
        size="xs"
        fullWidth
        onClick={handleSupport}
        style={{
          background: '#a5c422',
          color: '#fff',
        }}
      >
        Support Us (Pay What You Want)
      </Button>
      <MantineText size="xs" c="dimmed" mt="xs" ta="center">
        No payment required to use the chat
      </MantineText>
    </Box>
  );
};

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
  showPWYW: boolean;
  messageCount: number;
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

// Helper function to escape special regex characters in a string
const escapeRegExp = (string: string): string => {
  // Escapes special characters in a string to safely use it inside a RegExp
  // $& inserts the matched character, and \\ escapes it
  return string.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
};

// Replace personal info in a message with placeholders
const scrubPersonalInfo = (message: string, userInfo: UserInfo): string => {
  if (!userInfo) return message;

  let scrubbedMessage = message;

  // Replace name - use word boundaries to avoid partial matches
  if (userInfo.firstName) {
    scrubbedMessage = scrubbedMessage.replace(
      new RegExp(`\\b${escapeRegExp(userInfo.firstName)}\\b`, "gi"),
      "[FIRST_NAME]",
    );
  }

  if (userInfo.lastName) {
    scrubbedMessage = scrubbedMessage.replace(
      new RegExp(`\\b${escapeRegExp(userInfo.lastName)}\\b`, "gi"),
      "[LAST_NAME]",
    );
  }

  // Replace address
  if (userInfo.address) {
    scrubbedMessage = scrubbedMessage.replace(
      new RegExp(escapeRegExp(userInfo.address), "gi"),
      "[ADDRESS]",
    );
  }

  // Replace city
  if (userInfo.city) {
    scrubbedMessage = scrubbedMessage.replace(
      new RegExp(`\\b${escapeRegExp(userInfo.city)}\\b`, "gi"),
      "[CITY]",
    );
  }

  // Replace state
  if (userInfo.state) {
    scrubbedMessage = scrubbedMessage.replace(
      new RegExp(`\\b${escapeRegExp(userInfo.state)}\\b`, "gi"),
      "[STATE]",
    );
  }

  // Replace zip code
  if (userInfo.zipCode) {
    scrubbedMessage = scrubbedMessage.replace(
      new RegExp(`\\b${escapeRegExp(userInfo.zipCode)}\\b`, "gi"),
      "[ZIP_CODE]",
    );
  }

  // Replace email
  if (userInfo.email) {
    // Email regex to avoid partial matches
    scrubbedMessage = scrubbedMessage.replace(
      new RegExp(escapeRegExp(userInfo.email), "gi"),
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

interface ChatInterfaceProps {
  defaultProcedure?: string;
  defaultCondition?: string;
  medicare?: string;
  micrositeSlug?: string;
}

const ChatInterface: React.FC<ChatInterfaceProps> = ({ defaultProcedure, defaultCondition, medicare, micrositeSlug }) => {
  // State for our chat interface
  const [state, setState] = useState<ChatState>({
    messages: [],
    isLoading: false,
    input: "",
    chatId: localStorage.getItem("fhi_chat_id"),
    error: null,
    isProcessingFile: false,
    showPWYW: false,
    messageCount: 0,
  });

  // Track if we've sent the initial procedure message
  const hasSentInitialMessage = useRef(false);

  // Check if user has dismissed PWYW before
  const hasDismissedPWYW = localStorage.getItem("fhi_pwyw_dismissed") === "true";

  // Show PWYW after a few exchanges (to not be intrusive)
  useEffect(() => {
    const assistantMessages = state.messages.filter(m => m.role === "assistant").length;
    // Show PWYW after 3 assistant messages, if not dismissed before
    if (assistantMessages >= 3 && !hasDismissedPWYW && !state.showPWYW) {
      setState(prev => ({ ...prev, showPWYW: true }));
    }
  }, [state.messages, hasDismissedPWYW, state.showPWYW]);

  const dismissPWYW = () => {
    localStorage.setItem("fhi_pwyw_dismissed", "true");
    setState(prev => ({ ...prev, showPWYW: false }));
  };

  const wsRef = useRef<WebSocket | null>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);

  // Initialize chat interface on load
  useEffect(() => {
    // Add a welcome message from the assistant if no messages exist
    if (state.messages.length === 0) {
      // Use startNewChat to initialize chat with welcome message
      startNewChat(false); // false means don't close websocket
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
      console.log("Connecting to WebSocket...");
      const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      const wsUrl = `${protocol}//${window.location.host}/ws/ongoing-chat/`;

      const ws = new WebSocket(wsUrl);
      ws.onopen = () => {
        console.log("WebSocket connected");
        wsRef.current = ws;

        // Get user info for potential email data
        const userInfo = getUserInfo();
        const messageData = {
          session_key: getSessionKey(),
          email: userInfo?.email, // Send email if available
          is_patient: true, // Indicate this is a patient session
        };

        // If we have a chat ID, request the chat history we explicitily refresh from local storage
        // so reconnect does not capture the old state.
        let chatId = localStorage.getItem("fhi_chat_id");

        if (chatId) {
          console.log("Replaying chat history for chat ID:", chatId);
          ws.send(
            JSON.stringify({
              ...messageData,
              chat_id: chatId,
              replay: true,
            }),
          );
        } else {
          // If we don't have a chat ID no replay is needed
          console.log("Waiting for user input to start new chat");

          // If we have a default procedure from a microsite, send an initial message
          if (defaultProcedure && !hasSentInitialMessage.current) {
            hasSentInitialMessage.current = true;
            console.log("Sending initial message for procedure:", defaultProcedure);
            if (defaultCondition) {
              console.log("Default condition from microsite:", defaultCondition);
            }
            if (medicare) {
              console.log("Medicare flag set:", medicare);
            }
            if (micrositeSlug) {
              console.log("Microsite slug:", micrositeSlug);
            }

            // Small delay to ensure welcome message is displayed first
            setTimeout(() => {
              // Build initial message with procedure and optionally condition
              let initialMessage = "";
              
              // Special message for medicare-work-requirements microsite
              if (micrositeSlug === "medicare-work-requirements") {
                initialMessage = `I need help understanding the new Medicare work requirements. Can you explain what I need to know?`;
              } else {
                // Default message for appeals
                initialMessage = `I'm working on an appeal for ${defaultProcedure}`;
                if (defaultCondition) {
                  initialMessage += ` for ${defaultCondition}`;
                }
                if (medicare === "true") {
                  initialMessage += ` through Medicare`;
                }
                initialMessage += `. Can you help me understand what I need to do?`;
              }

              // Add the user message to the UI
              const userMessage: ChatMessage = {
                role: "user",
                content: initialMessage,
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
              const scrubbedContent = userInfo
                ? scrubPersonalInfo(initialMessage, userInfo)
                : initialMessage;

              // Send to server
              ws.send(
                JSON.stringify({
                  chat_id: null,
                  email: userInfo?.email,
                  content: scrubbedContent,
                  is_patient: true,
                  session_key: getSessionKey(),
                }),
              );
            }, 500);
          }
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
        } else if (data.chat_id) {
          // Always update the chat ID when received from server
          // This handles both new chats and reconnecting to existing ones
          localStorage.setItem("fhi_chat_id", data.chat_id);
          console.log("Setting chat ID:", data.chat_id);
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
            email: userInfo?.email, // Include email for server-side processing
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

  // Handle starting a new chat
  const startNewChat = (resetWebSocket: boolean = true) => {
    console.log("Starting new chat...");
    // Always clear the chat ID from localStorage when starting a new chat
    if (resetWebSocket) {
      console.log("Resetting chat ID in localStorage");
      localStorage.removeItem("fhi_chat_id");
    } else {
      console.log("Keeping existing chat ID in localStorage");
    }

    let chatId = localStorage.getItem("fhi_chat_id");

    console.log("Reseting the chat state");
    // Reset the chat state
    setState({
      messages: [],
      isLoading: false,
      input: "",
      chatId: chatId, // Reset chat ID
      error: null,
      isProcessingFile: false,
      showPWYW: false,
      messageCount: 0,
    });

    // Handle WebSocket for a new chat
    if (resetWebSocket) {
      // If we're requesting a complete reset, close and reconnect WebSocket
      if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
        // Close existing WebSocket - it will reconnect via useEffect
        wsRef.current.close();
      }
    }

    // Add welcome message again
    const userInfo = getUserInfo();
    const welcomeMessage: ChatMessage = {
      role: "assistant",
      content: userInfo
        ? `👋 Hey ${userInfo.firstName}! I'm your AI sidekick for fighting health insurance denials.\n\n**I can help you:**\n• 💬 Answer questions about your denial or policy\n• 📄 Review denial letters (use the 📎 to upload)\n• 🎯 Guide you through the appeal process\n• ✍️ Help craft appeal arguments\n\nJust ask me anything, or upload your denial letter to get started!`
        : "👋 Welcome! I'm your AI sidekick for fighting health insurance denials.\n\n**I can help you:**\n• 💬 Answer questions about denials and appeals\n• 📄 Review your denial letter (use the 📎 to upload)\n• 🎯 Guide you through the appeal process\n• ✍️ Help you craft persuasive arguments\n\n**Quick tips:**\n• Be specific about your situation\n• Upload any relevant documents\n• Ask follow-up questions—I'm here to help!\n\nWhat brings you here today?",
      timestamp: new Date().toISOString(),
      status: "done",
    };

    setState((prev) => ({
      ...prev,
      messages: [welcomeMessage],
    }));
  };

  // Render each chat message
  const renderMessage = (message: ChatMessage, index: number) => {
    const isUser = message.role === "user";

    return (
      <Paper
        key={index}
        shadow="xs"
        style={{
          backgroundColor: isUser ? "#f0f9ff" : "#f9fafb",
          borderRadius: 12,
          maxWidth: '85%',
          marginLeft: isUser ? 'auto' : 0,
          marginRight: isUser ? 0 : 'auto',
          paddingTop: 7, // Added padding
          paddingBottom: 7, // Added padding
          paddingLeft: 14, // Added padding
          paddingRight: 14, // Added padding
          marginTop: 5, // Added margin for better spacing
          marginBottom: 5, // Added margin for better spacing
        }}
      >
        <Flex gap="xs" align="flex-start">
          {!isUser && (
            <Image
              src="/static/images/better-logo.png"
              width={24}
              height={24}
              alt="FHI Logo"
              />
          )}
          <Box flex={1}>
            <MantineText fw={500} size="sm" c={isUser ? "blue" : "dark"} mb="xs">
              {isUser ? "You" : "FightHealthInsurance Assistant"}
            </MantineText>
            {message.status === "typing" ? (
              <TypingAnimation />
            ) : (
              <ReactMarkdown>{message.content}</ReactMarkdown>
            )}
          </Box>
        </Flex>
      </Paper>
    );
  };

  return (
    <Container
      size="lg"
      px="md"
      py={0}
      style={{
        minHeight: '100vh',
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        justifyContent: 'flex-start',
        background: '#f4f6fb',
      }}
    >
      {/* Title, subtitle, and button above the chat container */}
      <Box style={{ width: '100%', maxWidth: 800, margin: '0 auto', textAlign: 'center', marginBottom: THEME.spacing.headerMargin }}>
        <Title
          order={3}
          size="28px"
          style={{ paddingTop: '20px', paddingBottom: '10px' }}
        >
          Fight Health Insurance Chat
        </Title>
        <MantineText size="md" fw={500} c="dimmed" mb={4}>
          This is a chat interface. Use the text box below to talk to the assistant.
        </MantineText>
        <MantineGroup gap="md" justify="center">
          <Button
            fw={500}
            style={{
              ...THEME.buttonSharedStyles,
              borderRadius: THEME.borderRadius.buttonDefault,
              fontWeight: 500,
              fontSize: 14,
              paddingTop: 7,
              paddingBottom: 7,
              paddingLeft: 14,
              paddingRight: 14,
            }}
            onClick={() => startNewChat(true)}
            leftSection={<IconRefresh size={13} />}
          >
            New Chat
          </Button>
          <Button
            fw={500}
            style={{
              ...THEME.buttonSharedStyles,
              borderRadius: THEME.borderRadius.buttonDefault,
              fontWeight: 500,
              fontSize: 14,
              paddingTop: 7,
              paddingBottom: 7,
              paddingLeft: 14,
              paddingRight: 14,
            }}
            onClick={() => {
              localStorage.removeItem("fhi_user_info");
              window.location.href = "/chat-consent";
            }}
            leftSection={<IconUser size={13} />}
          >
            Update Personal Info
          </Button>
        </MantineGroup>
        {state.error && (
          <MantineText c="red" size="sm" mt="xs">
            {state.error}
          </MantineText>
        )}
      </Box>

      <Paper
        shadow="lg"
        p="xl"
        withBorder
        style={{
          height: "80vh", // Fixed height for containment
          maxHeight: "80vh",
          minHeight: 500,
          display: "flex",
          flexDirection: "column",
          maxWidth: 800,
          width: '100%',
          margin: '0 auto',
          borderRadius: 24,
          background: '#fff',
          boxShadow: '0 4px 32px rgba(0,0,0,0.07)',
          overflow: 'hidden', // Prevent children from overflowing
        }}
      >
        {/* Message list area */}
        <ScrollArea
          style={{
            flex: 1,
            minHeight: 0,
            overflowY: 'auto',
            display: 'flex',
            flexDirection: 'column',
          }}
        >
          {/* Messages container with padding and margin for spacing */}
          <Box style={{ marginBottom: 10, marginTop: 10 }}>
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
                style={{ backgroundColor: "#f9fafb", marginBottom: 10, padding: 10, borderRadius: 12 }}
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

            {/* PWYW Banner - shows after some helpful exchanges */}
            {state.showPWYW && <PWYWBanner onDismiss={dismissPWYW} />}

            <div ref={messagesEndRef} />
          </Box>
        </ScrollArea>

        <Box p="xs" style={{ width: "100%", marginTop: "10px" }}>
          <Paper
            radius="lg"
            p="sm"
            shadow="sm"
            withBorder
            style={{ width: '100%', background: '#f8fafc', borderRadius: 16 }}
          >
            {/* Two-line input: first line is textarea, second line is icons (now below, not absolutely positioned) */}
            <Flex direction="column" gap={8} style={{ width: '100%' }}>
              <Box style={{ position: 'relative', width: '100%' }}>
                <Flex align="flex-end" style={{ background: '#fff', border: '1px solid #e3e8f0', borderRadius: 10, padding: 4, marginTop: 10 }}>
                  {/* Textarea with paperclip inside bottom left and send inside bottom right */}
                  <Box style={{ position: 'relative', flex: 1, width: '100%'}}>
                    {state.isLoading ? (
                      <Textarea
                        style={{ width: '100%' }}
                        value={""}
                        placeholder={"Assistant is typing..."}
                        disabled
                        styles={{
                          input: {
                            border: 'none',
                            boxShadow: 'none',
                            background: 'transparent',
                            resize: 'none',
                            verticalAlign: 'top',
                          },
                          root: {
                            flex: 1,
                          },
                        }}
                      />
                    ) : (
                      <>
                        <Textarea
                          placeholder={"Type your message..."}
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
                          minRows={3}
                          maxRows={3}
                          autosize={false}
                          disabled={state.isProcessingFile}
                          styles={{
                            input: {
                              width: '100%',
                              border: 'none',
                              boxShadow: 'none',
                              background: 'transparent',
                              paddingBottom: 40,
                              resize: 'none',
                              verticalAlign: 'top',
                            },
                            root: {
                              flex: 1,
                            },
                          }}
                        />
                        {/* Paperclip inside bottom left */}
                        <Box style={{ position: 'absolute', left: 8, bottom: 8, zIndex: 2 }}>
                          <Tooltip label="Upload PDF" position="top">
                            <FileButton
                              onChange={handleFileUpload}
                              accept="application/pdf"
                              disabled={state.isProcessingFile}
                            >
                              {(props) => (
                                <ActionIcon
                                  {...props}
                                  size="md"
                                  loading={state.isProcessingFile}
                                  disabled={state.isProcessingFile}
                                  aria-label="Upload PDF"
                                  style={{
                                    ...THEME.buttonSharedStyles,
                                    borderRadius: THEME.borderRadius.buttonDefault,
                                  }}
                                >
                                  <IconPaperclip size={18} />
                                </ActionIcon>
                              )}
                            </FileButton>
                          </Tooltip>
                        </Box>
                        {/* Send button inside bottom right */}
                        <Box style={{ position: 'absolute', right: 8, bottom: 8, zIndex: 2 }}>
                          <Tooltip label="Send message" position="top">
                            <ActionIcon
                              onClick={handleSendMessage}
                              size="md"
                              disabled={!state.input.trim() || state.isProcessingFile}
                              aria-label="Send message"
                              style={{
                                ...THEME.buttonSharedStyles,
                                borderRadius: THEME.borderRadius.buttonDefault,
                              }}
                            >
                              <IconSend size={18} />
                            </ActionIcon>
                          </Tooltip>
                        </Box>

                      </>
                    )}
                  </Box>
                </Flex>
              </Box>
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

    // Get default procedure and condition from data attributes (from microsite)
    const defaultProcedure = chatRoot.dataset.defaultProcedure || undefined;
    const defaultCondition = chatRoot.dataset.defaultCondition || undefined;
    const medicare = chatRoot.dataset.medicare || undefined;
    const micrositeSlug = chatRoot.dataset.micrositeSlug || undefined;
    if (defaultProcedure) {
      console.log("Default procedure from microsite:", defaultProcedure);
    }
    if (defaultCondition) {
      console.log("Default condition from microsite:", defaultCondition);
    }
    if (medicare) {
      console.log("Medicare flag from microsite:", medicare);
    }
    if (micrositeSlug) {
      console.log("Microsite slug from microsite:", micrositeSlug);
    }

    const root = createRoot(chatRoot);
    root.render(
      <MantineProvider>
        <ChatInterface defaultProcedure={defaultProcedure} defaultCondition={defaultCondition} medicare={medicare} micrositeSlug={micrositeSlug} />
      </MantineProvider>,
    );
  } else {
    console.error("Chat interface root element not found");
  }
});

console.log("Chat interface script loaded");
export default ChatInterface;
