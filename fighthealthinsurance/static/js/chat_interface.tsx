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
  Switch,
} from "@mantine/core";

import { IconPaperclip, IconSend, IconUser, IconRefresh } from "./icons";
import { recognize } from "./scrub_ocr";
import { THEME } from "./theme";
import ErrorBoundary from "./ErrorBoundary";
import VoiceIntake from "./VoiceIntake";
import {
  getExternalModelsPreference,
  getUserInfo,
  saveExternalModelsPreference,
  saveUserInfo,
  scrubPersonalInfo,
  restorePersonalInfo,
  type UserInfo,
} from "./user_info_storage";

// Declare UET tracking function from base.html
declare global {
  interface Window {
    trackUETConversion?: (eventName: string, eventData?: Record<string, unknown>) => void;
  }
}

// Module-scoped flag to track if chat_start UET event has been sent this session
// This prevents duplicate conversions on WebSocket reconnects
let hasSentChatStartUET = false;

// PWYW Component for the chat interface
const PWYWBanner: React.FC<{ onDismiss: () => void }> = ({ onDismiss }) => {
  const handleSupport = () => {
    // Track donation initiation for UET (maximize this conversion)
    if (window.trackUETConversion) {
      window.trackUETConversion('donation_initiated', {
        event_category: 'donation',
        event_label: 'Chat PWYW Banner',
        event_action: 'begin_checkout'
      });
    }
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
// When true (set via localStorage from the console), each turn asks the
// server to echo back exactly what was sent to the backend model; frames
// arrive as debug_llm_input and are logged to the browser console. The
// server only honors it for DEBUG deployments and staff accounts.
// Guarded: storage access THROWS (rather than returning null) in an embedded
// iframe with third-party storage blocked, or under a block-all-cookies
// policy. This runs inside ws.onopen and the send helpers, so an unguarded
// throw here stopped the chat from ever sending its first frame.
const isChatDebugEnabled = (): boolean => {
  try {
    return localStorage.getItem("fhi_chat_debug") === "true";
  } catch {
    return false;
  }
};

// Server debug frames for one turn (only sent when debug is enabled AND the
// server allows it): the exact LLM input and the model-selection result.
interface ChatDebugInfo {
  input?: Record<string, unknown>;
  result?: Record<string, unknown>;
}

interface ChatMessage {
  role: "user" | "assistant";
  content: string;
  timestamp?: string;
  status?: "done" | "typing" | "error";
  uid?: string;
  // Optional side-by-side alternate answer (ephemeral: not persisted
  // server-side, so it only appears on live turns, not replays).
  alternate_content?: string;
  // Debug frames captured for the turn that produced this assistant
  // message (rendered as a collapsible panel under the bubble).
  debug_info?: ChatDebugInfo;
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
  statusMessage: string | null;
  requestStartTime: number | null;
  useExternalModels: boolean;
  showVoiceIntake: boolean;
}

// Shared styles that keep long/no-space content (URLs, claim IDs, OCR strings)
// from breaking the layout or causing horizontal page overflow.
const messageContentStyle: React.CSSProperties = {
  minWidth: 0,
  overflowWrap: "anywhere",
  wordBreak: "break-word",
};

// Above this length a message is collapsed behind a "Show more" toggle so a
// huge paste doesn't render as one giant block in the DOM.
const MESSAGE_COLLAPSE_THRESHOLD = 4000;
const MESSAGE_PREVIEW_CHARS = 2000;

// Renders a chat message body, collapsing very long content behind a toggle.
const ChatMessageContent: React.FC<{ content: string }> = ({ content }) => {
  const [expanded, setExpanded] = useState(false);
  const isLong = content.length > MESSAGE_COLLAPSE_THRESHOLD;

  if (!isLong) {
    return (
      <Box style={messageContentStyle}>
        <ReactMarkdown>{content}</ReactMarkdown>
      </Box>
    );
  }

  if (expanded) {
    return (
      <Box style={messageContentStyle}>
        <ReactMarkdown>{content}</ReactMarkdown>
        <Button variant="subtle" size="xs" px={4} mt="xs" onClick={() => setExpanded(false)}>
          Show less
        </Button>
      </Box>
    );
  }

  // Collapsed: show a plain-text, pre-wrapped preview (no markdown, no giant
  // DOM) with a toggle to expand the full content.
  return (
    <Box style={messageContentStyle}>
      <Box style={{ ...messageContentStyle, whiteSpace: "pre-wrap" }}>
        {content.slice(0, MESSAGE_PREVIEW_CHARS) + "…"}
      </Box>
      <Button variant="subtle" size="xs" px={4} mt="xs" onClick={() => setExpanded(true)}>
        Show more ({content.length.toLocaleString()} characters)
      </Button>
    </Box>
  );
};

// Typing animation component for loading state with elapsed time
// Collapsible side-by-side alternate answer (like ChatGPT's occasional
// "which response do you prefer?"). Collapsed by default; the preference
// buttons send lightweight feedback (either direction) so we learn which
// answers users like.
const AlternateAnswer: React.FC<{
  content: string;
  onPrefer: (preferred: "primary" | "alternate") => boolean;
}> = ({ content, onPrefer }) => {
  const [expanded, setExpanded] = useState(false);
  const [picked, setPicked] = useState<"primary" | "alternate" | null>(null);

  const pick = (choice: "primary" | "alternate") => {
    if (picked) return;
    if (onPrefer(choice)) {
      setPicked(choice);
    }
  };

  return (
    <Box mt="xs" style={{ borderTop: "1px dashed #d1d5db", paddingTop: 6 }}>
      <Button
        variant="subtle"
        size="compact-xs"
        onClick={() => setExpanded((prev) => !prev)}
      >
        {expanded ? "Hide alternate answer" : "🔀 See an alternate answer"}
      </Button>
      {expanded && (
        <Box
          mt="xs"
          style={{
            border: "1px solid #e5e7eb",
            borderRadius: 8,
            padding: "6px 10px",
            backgroundColor: "#ffffff",
          }}
        >
          <MantineText size="xs" c="dimmed" mb={4}>
            Alternate answer
          </MantineText>
          <Box style={messageContentStyle}>
            <ReactMarkdown>{content}</ReactMarkdown>
          </Box>
          {picked ? (
            <MantineText size="xs" c="dimmed" mt={4}>
              Thanks for the feedback!
            </MantineText>
          ) : (
            <MantineGroup gap="xs" mt={4}>
              <Button
                variant="light"
                size="compact-xs"
                onClick={() => pick("alternate")}
              >
                👍 I prefer this answer
              </Button>
              <Button
                variant="subtle"
                size="compact-xs"
                onClick={() => pick("primary")}
              >
                The original was better
              </Button>
            </MantineGroup>
          )}
        </Box>
      )}
    </Box>
  );
};

// One-line summary of a debug_llm_result frame for the panel header.
const summarizeDebugResult = (result: Record<string, unknown>): string => {
  const parts: string[] = [];
  if (typeof result.picked_model === "string") {
    parts.push(`picked ${result.picked_model}`);
  }
  if (typeof result.candidate_count === "number") {
    parts.push(`${result.candidate_count} candidates`);
  }
  if (typeof result.rejected_repeats === "number" && result.rejected_repeats > 0) {
    parts.push(`${result.rejected_repeats} repeats rejected`);
  }
  if (result.retry_used) {
    parts.push("retry used");
  }
  if (typeof result.elapsed_ms === "number") {
    parts.push(`${(result.elapsed_ms / 1000).toFixed(1)}s`);
  }
  return parts.join(" · ");
};

// Collapsible per-turn debug panel (only rendered when chat debug is on and
// the server sent debug frames): shows the model-selection summary up front
// and the raw LLM input/result JSON behind a toggle.
const DebugPanel: React.FC<{ debugInfo: ChatDebugInfo }> = ({ debugInfo }) => {
  const [expanded, setExpanded] = useState(false);
  const summary = debugInfo.result ? summarizeDebugResult(debugInfo.result) : "";

  return (
    <Box mt="xs" style={{ borderTop: "1px dashed #d1d5db", paddingTop: 6 }}>
      <Button
        variant="subtle"
        size="compact-xs"
        color="gray"
        onClick={() => setExpanded((prev) => !prev)}
      >
        {expanded ? "Hide debug" : `🔧 Debug${summary ? `: ${summary}` : ""}`}
      </Button>
      {expanded && (
        <Box
          mt="xs"
          style={{
            border: "1px solid #e5e7eb",
            borderRadius: 8,
            padding: "6px 10px",
            backgroundColor: "#f9fafb",
            fontFamily: "monospace",
            fontSize: 11,
            overflowX: "auto",
          }}
        >
          {debugInfo.result && (
            <>
              <MantineText size="xs" c="dimmed">
                Model selection
              </MantineText>
              <pre style={{ whiteSpace: "pre-wrap", margin: "4px 0" }}>
                {JSON.stringify(debugInfo.result, null, 2)}
              </pre>
            </>
          )}
          {debugInfo.input && (
            <>
              <MantineText size="xs" c="dimmed">
                LLM input
              </MantineText>
              <pre style={{ whiteSpace: "pre-wrap", margin: "4px 0" }}>
                {JSON.stringify(debugInfo.input, null, 2)}
              </pre>
            </>
          )}
        </Box>
      )}
    </Box>
  );
};

const TypingAnimation: React.FC<{ startTime?: number | null }> = ({ startTime }) => {
  const [dots, setDots] = useState(".");
  const [elapsed, setElapsed] = useState(0);

  useEffect(() => {
    const dotsInterval = setInterval(() => {
      setDots((prevDots) => {
        if (prevDots.length >= 3) return ".";
        return prevDots + ".";
      });
    }, 500);

    return () => clearInterval(dotsInterval);
  }, []);

  useEffect(() => {
    if (!startTime) return;

    const updateElapsed = () => {
      const now = Date.now();
      const elapsedSeconds = Math.floor((now - startTime) / 1000);
      setElapsed(elapsedSeconds);
    };

    // Update immediately
    updateElapsed();

    // Update every second
    const elapsedInterval = setInterval(updateElapsed, 1000);

    return () => clearInterval(elapsedInterval);
  }, [startTime]);

  const getStatusMessage = () => {
    if (!startTime) return null;
    
    if (elapsed < 45) {
      return `Most responses come in 45 seconds${elapsed > 0 ? ` (${elapsed}s elapsed)` : ""}`;
    } else if (elapsed < 60) {
      return `Still working on your response... Most responses complete within 60 seconds (${elapsed}s elapsed)`;
    } else if (elapsed < 360) {
      return `Still working on your response... Some can take up to 6 minutes (${elapsed}s elapsed). You can retry if needed.`;
    } else {
      return `This is taking longer than expected (${elapsed}s elapsed). Please try the retry button below.`;
    }
  };

  const statusMsg = getStatusMessage();

  return (
    <Box>
      <span style={{ marginLeft: 4 }}>Typing{dots}</span>
      {statusMsg && (
        <MantineText size="xs" c="dimmed" mt="xs" style={{ fontStyle: "italic" }}>
          {statusMsg}
        </MantineText>
      )}
    </Box>
  );
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

interface ChatInterfaceProps {
  enableVoiceIntake?: boolean;
  enableLocalSTT?: boolean;
  defaultProcedure?: string;
  defaultCondition?: string;
  medicare?: string;
  micrositeSlug?: string;
  initialMessage?: string;
}

const ChatInterface: React.FC<ChatInterfaceProps> = ({ defaultProcedure, defaultCondition, medicare, micrositeSlug, initialMessage, enableVoiceIntake, enableLocalSTT }) => {
  // State for our chat interface. Lazy initializer: the object literal (with
  // its localStorage reads) would otherwise be rebuilt on every render.
  const [state, setState] = useState<ChatState>(() => ({
    messages: [],
    isLoading: false,
    input: "",
    chatId: localStorage.getItem("fhi_chat_id"),
    error: null,
    isProcessingFile: false,
    showPWYW: false,
    messageCount: 0,
    statusMessage: null,
    requestStartTime: null,
    // Default-on: getExternalModelsPreference treats a missing key as true
    // (the raw === "true" read here used to silently disable external models
    // for anyone who never went through the consent form).
    useExternalModels: getExternalModelsPreference(),
    showVoiceIntake: Boolean(enableVoiceIntake),
  }));

  // Track when to show retry button (separate state to avoid re-render issues)
  const [showRetryButton, setShowRetryButton] = useState(false);

  // Track if we've sent the initial procedure message
  const hasSentInitialMessage = useRef(false);

  // Check if user has dismissed PWYW before
  const hasDismissedPWYW = localStorage.getItem("fhi_pwyw_dismissed") === "true";

  // Update retry button visibility based on elapsed time
  useEffect(() => {
    if (!state.requestStartTime || !state.isLoading) {
      setShowRetryButton(false);
      return;
    }

    const checkRetryButton = () => {
      const elapsed = Date.now() - state.requestStartTime!;
      setShowRetryButton(elapsed > 60000); // Show after 60 seconds
    };

    // Check immediately
    checkRetryButton();

    // Check every second
    const interval = setInterval(checkRetryButton, 1000);

    return () => clearInterval(interval);
  }, [state.requestStartTime, state.isLoading]);

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
  // Debug frames (LLM input / selection result) arrive before the assistant
  // content frame of the same turn; buffer them here so they attach to the
  // message that they describe.
  const pendingDebugRef = useRef<ChatDebugInfo | null>(null);

  // Initialize chat interface on load
  useEffect(() => {
    // Initialize session key immediately on mount (before WebSocket connects)
    getSessionKey();

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
    // Reconnect with exponential backoff instead of a fixed 3s loop, and
    // stop reconnecting entirely once the component unmounts (otherwise the
    // cleanup close() schedules a zombie socket 3s later).
    let unmounted = false;
    let reconnectAttempts = 0;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;

    const connectWebSocket = () => {
      if (unmounted) return;
      console.log("Connecting to WebSocket...");
      const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      const wsUrl = `${protocol}//${window.location.host}/ws/ongoing-chat/`;

      const ws = new WebSocket(wsUrl);
      ws.onopen = () => {
        console.log("WebSocket connected");
        wsRef.current = ws;
        reconnectAttempts = 0;

        // Track chat start conversion for UET (only once per session, not on reconnects)
        if (window.trackUETConversion && !hasSentChatStartUET) {
          window.trackUETConversion('chat_start', {
            event_category: 'engagement',
            event_label: 'Chat Started'
          });
          hasSentChatStartUET = true;
        }

        // Get user info for potential email data
        const userInfo = getUserInfo();
        const messageData = {
          session_key: getSessionKey(),
          email: userInfo?.email, // Send email if available
          is_patient: true, // Indicate this is a patient session
          microsite_slug: micrositeSlug || undefined, // Include microsite slug if available
          debug: isChatDebugEnabled() || undefined,
        };

        // If we have a chat ID, request the chat history we explicitily refresh from local storage
        // so reconnect does not capture the old state.
        let chatId = localStorage.getItem("fhi_chat_id");
        const useExternalModels = getExternalModelsPreference();

        // If we have an initial message (e.g., from explain denial page), start a NEW chat
        // even if there's an existing one - the user explicitly started a new denial explanation
        if (initialMessage && !hasSentInitialMessage.current) {
          console.log("Starting new chat for explain denial (clearing existing chat if any)");
          // Clear the old chat ID since we're starting fresh with a new denial
          localStorage.removeItem("fhi_chat_id");
          chatId = null;
        }

        if (chatId) {
          console.log("Replaying chat history for chat ID:", chatId);
          ws.send(
            JSON.stringify({
              ...messageData,
              chat_id: chatId,
              replay: true,
              use_external_models: useExternalModels,
            }),
          );
        } else {
          // If we don't have a chat ID no replay is needed
          console.log("Waiting for user input to start new chat");

          // If we have an initial message (e.g., from explain denial page), send it
          // Otherwise, if we have a default procedure from a microsite, send an initial message
          if (initialMessage && !hasSentInitialMessage.current) {
            hasSentInitialMessage.current = true;
            console.log("Sending initial message from explain denial page");

            // Small delay to ensure welcome message is displayed first
            setTimeout(() => {
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
                requestStartTime: Date.now(),
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
                  microsite_slug: micrositeSlug || undefined,
                  use_external_models: useExternalModels,
                  debug: isChatDebugEnabled() || undefined,
                }),
              );
            }, 500);
          } else if (defaultProcedure && !hasSentInitialMessage.current) {
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
                requestStartTime: Date.now(),
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
                  microsite_slug: micrositeSlug || undefined,
                  use_external_models: useExternalModels,
                  debug: isChatDebugEnabled() || undefined,
                }),
              );
            }, 500);
          }
        }
      };

      ws.onmessage = (event) => {
        let data;
        try {
          data = JSON.parse(event.data);
        } catch (parseError) {
          // A malformed/truncated frame must not throw out of onmessage --
          // that kills handling of every later frame on this socket.
          console.error("Ignoring unparseable WebSocket frame", parseError);
          return;
        }
        if (!data || typeof data !== "object") {
          return;
        }
        // Frames carry chat message content (PHI) -- log only the field names.
        console.debug("Received message frame, keys:", Object.keys(data ?? {}));

        // Debug echoes (only sent when the debug flag was requested AND the
        // server allows it): the exact LLM input for the turn and the
        // model-selection result. Logged to the console and buffered so the
        // next assistant message renders them as a collapsible panel.
        if (data.debug_llm_input) {
          console.log("FHI chat debug — LLM input:", data.debug_llm_input);
          pendingDebugRef.current = {
            ...(pendingDebugRef.current ?? {}),
            input: data.debug_llm_input,
          };
        }
        if (data.debug_llm_result) {
          console.log("FHI chat debug — model selection:", data.debug_llm_result);
          pendingDebugRef.current = {
            ...(pendingDebugRef.current ?? {}),
            result: data.debug_llm_result,
          };
        }

        // Get user info for restoring personal info
        const userInfo = getUserInfo();

        // Always store the chat ID when a frame carries one, BEFORE the
        // branch below: replay/error frames also carry chat_id, and the old
        // else-if chain meant a replay frame's messages short-circuited the
        // chat_id branch -- so after a server-side fork the new id was never
        // stored and every subsequent turn re-forked.
        if (data.chat_id) {
          if (data.chat_forked) {
            console.log("Server forked this chat; new chat ID:", data.chat_id);
          }
          localStorage.setItem("fhi_chat_id", data.chat_id);
          setState((prev) =>
            prev.chatId === data.chat_id
              ? prev
              : {
                  ...prev,
                  chatId: data.chat_id,
                },
          );
        }

        // Handle different types of messages from the server
        if (data.error) {
          // Terminal frame for this turn: drop any buffered debug frames so
          // they can't attach to a LATER assistant message and describe the
          // wrong turn.
          pendingDebugRef.current = null;
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
            // Keep requestStartTime so retry button remains visible with error
          }));
        } else if (data.messages) {
          // History replay: same reasoning as the error branch above.
          pendingDebugRef.current = null;
          // This is a history replay
          // Restore personal info for BOTH roles: user messages are stored
          // scrubbed ({{FIRST_NAME}} etc.), so without restoring them a
          // refresh showed placeholder tokens in the user's own bubbles.
          const processedMessages = data.messages.map((msg: ChatMessage) => {
            if (userInfo) {
              return {
                ...msg,
                content: restorePersonalInfo(msg.content, userInfo),
              };
            }
            return msg;
          });

          setState((prev) => {
            // Guard the wipe: an empty replay (e.g. against a just-forked
            // chat) must not erase messages the user can already see.
            if (
              !Array.isArray(processedMessages) ||
              (processedMessages.length === 0 && prev.messages.length > 0)
            ) {
              return prev;
            }
            return {
              ...prev,
              messages: processedMessages,
              // A replay only happens on (re)connect; any turn that was in
              // flight died with the old socket, so clear the loading state
              // instead of leaving the spinner stuck until the retry button
              // appears.
              isLoading: false,
              requestStartTime: null,
              statusMessage: null,
            };
          });
        }

        if (data.content && data.role) {
          // This is a new message - restore personal info if it's from the assistant
          const processedContent =
            data.role === "assistant" && userInfo
              ? restorePersonalInfo(data.content, userInfo)
              : data.content;

          // Optional side-by-side alternate answer, same restore treatment.
          let alternateContent: string | undefined = undefined;
          if (data.role === "assistant" && typeof data.alternate_content === "string") {
            alternateContent = userInfo
              ? restorePersonalInfo(data.alternate_content, userInfo)
              : data.alternate_content;
          }

          // Attach any buffered debug frames to the assistant message they
          // belong to (cleared either way so a later turn can't inherit a
          // stale panel).
          let debugInfo: ChatDebugInfo | undefined = undefined;
          if (data.role === "assistant" && pendingDebugRef.current) {
            debugInfo = pendingDebugRef.current;
          }
          pendingDebugRef.current = null;

          setState((prev) => ({
            ...prev,
            messages: [
              ...prev.messages,
              {
                role: data.role,
                content: processedContent,
                alternate_content: alternateContent,
                debug_info: debugInfo,
                timestamp: data.timestamp || new Date().toISOString(),
                status: "done",
              },
            ],
            isLoading: false,
            requestStartTime: null, // Clear the timer when we get a response
            statusMessage: null,
          }));
        } else if (data.status) {
          // This is a status update (typing, etc.)
          setState((prev) => ({
            ...prev,
            isLoading: true,
            statusMessage: data.status,
          }));
        }
      };

      ws.onclose = () => {
        console.log("WebSocket disconnected");
        wsRef.current = null;
        if (unmounted) return;
        // Exponential backoff: 3s, 6s, 12s, ... capped at 60s. A server
        // rejecting connections gets breathing room instead of a thundering
        // 3s retry loop from every open tab.
        const delay = Math.min(3000 * 2 ** reconnectAttempts, 60000);
        reconnectAttempts += 1;
        reconnectTimer = setTimeout(connectWebSocket, delay);
      };

      ws.onerror = (error) => {
        console.error("WebSocket error:", error);
      };
    };

    connectWebSocket();

    // Clean up WebSocket connection when component unmounts
    return () => {
      unmounted = true;
      if (reconnectTimer !== null) {
        clearTimeout(reconnectTimer);
      }
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
        const charCount = fileContent.length.toLocaleString();
        const userMessage: ChatMessage = {
          role: "user",
          content: `I've uploaded a document: **${file.name}** (${charCount} characters). The document is being analyzed.`,
          timestamp: new Date().toISOString(),
          status: "done",
        };

        setState((prev) => ({
          ...prev,
          messages: [...prev.messages, userMessage],
          isLoading: true,
          requestStartTime: Date.now(),
          statusMessage: `Analyzing document: ${file.name}...`,
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
            use_external_models: state.useExternalModels,
            debug: isChatDebugEnabled() || undefined,
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

  const sendChatMessage = (content: string, source?: "typed" | "voice_transcript") => {
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
      return false;
    }

    const userInfo = getUserInfo();
    const scrubbedContent = userInfo ? scrubPersonalInfo(content, userInfo) : content;

    const messageToSend = {
      chat_id: state.chatId,
      email: userInfo?.email,
      content: scrubbedContent,
      is_patient: true,
      session_key: getSessionKey(),
      use_external_models: state.useExternalModels,
      metadata: source ? { intake_source: source } : undefined,
      debug: isChatDebugEnabled() || undefined,
    };

    wsRef.current.send(JSON.stringify(messageToSend));
    return true;
  };

  // Lightweight feedback about a side-by-side alternate answer. Returns
  // whether the frame was actually sent.
  const sendAnswerFeedback = (preferred: "primary" | "alternate"): boolean => {
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
      return false;
    }
    wsRef.current.send(
      JSON.stringify({
        chat_id: state.chatId,
        session_key: getSessionKey(),
        answer_feedback: { preferred },
      }),
    );
    return true;
  };

  // Handle sending a new message
  const handleSendMessage = () => {
    if (!state.input.trim() || state.isLoading) return;

    const didSend = sendChatMessage(state.input, "typed");
    if (!didSend) {
      setState((prev) => ({
        ...prev,
        error: "Connection is still starting. Please try again in a moment.",
      }));
      return;
    }

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
      requestStartTime: Date.now(), // Track when the request started
      statusMessage: null,
    }));

  };

  // Handle retrying the last message
  const handleRetryLastMessage = () => {
    if (!wsRef.current) return;
    // Allow retry even while loading so long-running requests can be manually re-sent.

    // Find the last user message
    const lastUserMessage = [...state.messages]
      .reverse()
      .find((msg) => msg.role === "user");

    if (!lastUserMessage) return;

    const didSend = sendChatMessage(lastUserMessage.content, "typed");
    if (!didSend) {
      setState((prev) => ({
        ...prev,
        isLoading: false,
        error: "Connection is still starting. Please try retry again in a moment.",
      }));
      return;
    }

    setState((prev) => ({
      ...prev,
      isLoading: true,
      requestStartTime: Date.now(),
      statusMessage: null,
      error: null,
    }));
  };

  
  const submitVoiceTranscript = (transcript: string) => {
    if (!transcript.trim() || state.isLoading) return;
    const didSend = sendChatMessage(transcript, "voice_transcript");
    if (!didSend) {
      setState((prev) => ({
        ...prev,
        error: "Connection is still starting. Please submit your transcript again in a moment.",
      }));
      return;
    }

    const userMessage: ChatMessage = {
      role: "user",
      content: transcript,
      timestamp: new Date().toISOString(),
      status: "done",
    };
    setState((prev) => ({
      ...prev,
      messages: [...prev.messages, userMessage],
      input: "",
      isLoading: true,
      requestStartTime: Date.now(),
      statusMessage: null,
    }));
  };
// Handle toggling external models
  const handleToggleExternalModels = (checked: boolean) => {
    saveExternalModelsPreference(checked);
    setState((prev) => ({ ...prev, useExternalModels: checked }));
  };

  // Handle starting a new chat
  const startNewChat = (resetWebSocket: boolean = true) => {
    console.log("Starting new chat...");
    // Always clear the chat ID from localStorage when starting a new chat
    if (resetWebSocket) {
      console.log("Resetting chat ID in localStorage");
      localStorage.removeItem("fhi_chat_id");
    } else {
      console.log("Keeping existing chat ID in localStorage if present.");
    }

    let chatId = localStorage.getItem("fhi_chat_id");

    console.log("Resetting the chat state");
    // Reset the chat state but preserve useExternalModels setting
    const useExternalModels = getExternalModelsPreference();
    setState({
      messages: [],
      isLoading: false,
      input: "",
      chatId: chatId, // Reset chat ID
      error: null,
      isProcessingFile: false,
      showPWYW: false,
      messageCount: 0,
      statusMessage: null,
      requestStartTime: null,
      useExternalModels: useExternalModels,
      showVoiceIntake: Boolean(enableVoiceIntake),
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
          overflow: 'hidden', // keep long content inside the bubble
        }}
      >
        <Flex gap="xs" align="flex-start" style={{ minWidth: 0 }}>
          {!isUser && (
            <Image
              src="/static/images/better-logo.png"
              width={24}
              height={24}
              alt="FHI Logo"
              />
          )}
          <Box flex={1} style={{ minWidth: 0 }}>
            <MantineText fw={500} size="sm" c={isUser ? "blue" : "dark"} mb="xs">
              {isUser ? "You" : "FightHealthInsurance Assistant"}
            </MantineText>
            {message.status === "typing" ? (
              <TypingAnimation startTime={state.requestStartTime} />
            ) : (
              <>
                <ChatMessageContent content={message.content} />
                {!isUser && message.alternate_content && (
                  <AlternateAnswer
                    content={message.alternate_content}
                    onPrefer={(preferred) => sendAnswerFeedback(preferred)}
                  />
                )}
                {!isUser && message.debug_info && (
                  <DebugPanel debugInfo={message.debug_info} />
                )}
              </>
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
                  <TypingAnimation startTime={state.requestStartTime} />
                </Box>
                {/* Display server status messages if available */}
                {state.statusMessage && (
                  <Box mt="xs">
                    <MantineText size="xs" c="dimmed" style={{ fontStyle: "italic" }}>
                      {state.statusMessage}
                    </MantineText>
                  </Box>
                )}
                {/* Show retry button after 60 seconds */}
                {showRetryButton && (
                  <Box mt="sm">
                    <Button
                      size="xs"
                      onClick={handleRetryLastMessage}
                      disabled={!state.isLoading}
                      style={{
                        ...THEME.buttonSharedStyles,
                        borderRadius: THEME.borderRadius.buttonDefault,
                      }}
                      leftSection={<IconRefresh size={13} />}
                      aria-label="Retry sending message"
                    >
                      Retry
                    </Button>
                  </Box>
                )}
              </Paper>
            )}

            {/* Display error message with retry button */}
            {state.error && (
              <Paper
                shadow="xs"
                style={{ backgroundColor: "#fff5f5", marginBottom: 10, padding: 10, borderRadius: 12, border: "1px solid #feb2b2" }}
              >
                <Flex align="center" gap="xs">
                  <MantineText fw={500} size="sm" c="red">
                    ⚠️ Error
                  </MantineText>
                </Flex>
                <Box mt="xs">
                  <MantineText size="sm" c="red">
                    {state.error}
                  </MantineText>
                </Box>
                <Box mt="sm">
                  <Button
                    size="xs"
                    onClick={handleRetryLastMessage}
                    style={{
                      ...THEME.buttonSharedStyles,
                      borderRadius: THEME.borderRadius.buttonDefault,
                    }}
                    leftSection={<IconRefresh size={13} />}
                    aria-label="Retry sending message after error"
                  >
                    Retry
                  </Button>
                </Box>
              </Paper>
            )}

            {/* PWYW Banner - shows after some helpful exchanges */}
            {state.showPWYW && <PWYWBanner onDismiss={dismissPWYW} />}

            <div ref={messagesEndRef} />
          </Box>
        </ScrollArea>

        {state.showVoiceIntake && (
          <VoiceIntake
            enabledLocalSTT={Boolean(enableLocalSTT)}
            onSubmitTranscript={submitVoiceTranscript}
            onSwitchToTyping={() => setState((prev) => ({ ...prev, showVoiceIntake: false }))}
          />
        )}

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
                          maxRows={8}
                          autosize
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
                              // Auto-grow up to maxRows, then scroll internally.
                              // Note: do NOT set maxHeight/height here -- with
                              // `autosize`, react-textarea-autosize throws on
                              // style.maxHeight and crashes the component;
                              // maxRows already caps growth and scrolls.
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
    const initialMessage = chatRoot.dataset.initialMessage || undefined;
    const enableVoiceIntake = chatRoot.dataset.enableVoiceIntake === "true";
    const enableLocalSTT = chatRoot.dataset.enableLocalStt === "true";
    // Don't dump chatRoot.dataset: it can include data-initial-message, which
    // is built from the user's denial text. Non-PHI fields are logged below.
    console.log("Using microsite settings");
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
    if (initialMessage) {
      // The initial message is built from the user's denial text (PHI).
      console.log("Initial message provided, length:", initialMessage.length);
    }

    const root = createRoot(chatRoot);
    root.render(
      <MantineProvider>
        <ErrorBoundary componentName="ChatInterface">
          <ChatInterface
            defaultProcedure={defaultProcedure}
            defaultCondition={defaultCondition}
            medicare={medicare}
            micrositeSlug={micrositeSlug}
            initialMessage={initialMessage}
            enableVoiceIntake={enableVoiceIntake}
            enableLocalSTT={enableLocalSTT}
          />
        </ErrorBoundary>
      </MantineProvider>,
    );
  } else {
    console.error("Chat interface root element not found");
  }
});

console.log("Chat interface script loaded");
export default ChatInterface;
