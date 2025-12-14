import React, { useState, useCallback } from "react";
import { createRoot } from "react-dom/client";
import {
  Box,
  Container,
  Button,
  Paper,
  Text,
  MantineProvider,
  Title,
  Card,
  Group,
  Stack,
  Loader,
  Alert,
  ScrollArea,
} from "@mantine/core";

// Types for Chooser API responses
interface ChooserCandidate {
  id: number;
  candidate_index: number;
  kind: string;
  model_name: string;
  content: string;
  metadata: Record<string, unknown> | null;
}

interface ChooserTaskContext {
  procedure?: string;
  diagnosis?: string;
  denial_text_preview?: string;
  insurance_company?: string;
  prompt?: string;
  history?: Array<{ role: string; content: string }>;
}

interface ChooserTask {
  task_id: number;
  task_type: string;
  task_context: ChooserTaskContext;
  candidates: ChooserCandidate[];
}

interface ChooserState {
  mode: "selection" | "loading" | "voting" | "confirmation" | "error";
  task: ChooserTask | null;
  selectedCandidate: ChooserCandidate | null;
  error: string | null;
  taskType: "appeal" | "chat" | null;
}

const THEME = {
  colors: {
    background: "#f4f6fb",
    buttonBackground: "#a5c422",
    buttonText: "#ffffff",
    selectedBorder: "#a5c422",
  },
  borderRadius: {
    small: 3,
    medium: 12,
    large: 24,
  },
} as const;

// Get CSRF token from cookies
const getCSRFToken = (): string => {
  const name = "csrftoken";
  let cookieValue = "";
  if (document.cookie && document.cookie !== "") {
    const cookies = document.cookie.split(";");
    for (let i = 0; i < cookies.length; i++) {
      const cookie = cookies[i].trim();
      if (cookie.substring(0, name.length + 1) === name + "=") {
	cookieValue = decodeURIComponent(cookie.substring(name.length + 1));
	break;
      }
    }
  }
  return cookieValue;
};

// Format context for display
const formatContext = (context: ChooserTaskContext, taskType: string): React.ReactNode => {
  if (taskType === "chat") {
    // For chat tasks, show the conversation history if present
    const parts: React.ReactNode[] = [];

    if (context.history && context.history.length > 0) {
      context.history.forEach((msg, idx) => {
	const isUser = msg.role === "user";
	parts.push(
	  <Box
	    key={`history-${idx}`}
	    mb="sm"
	    p="sm"
	    style={{
	      backgroundColor: isUser ? "#f0f4f8" : "#e8f5e9",
	      borderRadius: "8px",
	      borderLeft: `3px solid ${isUser ? "#64b5f6" : "#81c784"}`,
	    }}
	  >
	    <Text size="xs" fw={600} c={isUser ? "blue" : "green"} mb={4}>
	      {isUser ? "👤 User" : "🤖 Assistant"}
	    </Text>
	    <Text size="sm" style={{ whiteSpace: "pre-wrap" }}>
	      {msg.content}
	    </Text>
	  </Box>
	);
      });
    }

    // Add the current prompt (the question we're generating responses for)
    if (context.prompt) {
      parts.push(
	<Box
	  key="current-prompt"
	  p="sm"
	  style={{
	    backgroundColor: "#fff3e0",
	    borderRadius: "8px",
	    borderLeft: "3px solid #ffb74d",
	  }}
	>
	  <Text size="xs" fw={600} c="orange" mb={4}>
	    👤 User (respond to this)
	  </Text>
	  <Text size="sm" style={{ whiteSpace: "pre-wrap" }}>
	    {context.prompt}
	  </Text>
	</Box>
      );
    }

    return parts.length > 0 ? <Stack gap="xs">{parts}</Stack> : "No context available";
  }

  // For appeal tasks, show the denial details
  const parts: string[] = [];
  if (context.procedure) parts.push(`Procedure: ${context.procedure}`);
  if (context.diagnosis) parts.push(`Diagnosis: ${context.diagnosis}`);
  if (context.insurance_company) parts.push(`Insurance: ${context.insurance_company}`);
  if (context.denial_text_preview) parts.push(`Denial: ${context.denial_text_preview}`);

  return parts.join("\n") || "No context available";
};

// Candidate Card Component
const CandidateCard: React.FC<{
  candidate: ChooserCandidate;
  isSelected: boolean;
  onSelect: () => void;
  label: string;
}> = ({ candidate, isSelected, onSelect, label }) => {
  return (
    <Card
      shadow={isSelected ? "md" : "sm"}
      padding="lg"
      radius="md"
      withBorder
      onClick={onSelect}
      style={{
	cursor: "pointer",
	border: isSelected ? `3px solid ${THEME.colors.selectedBorder}` : "1px solid #e0e0e0",
	backgroundColor: isSelected ? "#f9fff0" : "#ffffff",
	transition: "all 0.2s ease",
      }}
    >
      <Group justify="space-between" mb="sm">
	<Text fw={700} size="lg">
	  Option {label}
	</Text>
	{isSelected && (
	  <Text size="sm" c="green" fw={500}>
	    ✓ Selected
	  </Text>
	)}
      </Group>
      <ScrollArea h={300} offsetScrollbars>
	<Text size="sm" style={{ whiteSpace: "pre-wrap" }}>
	  {candidate.content}
	</Text>
      </ScrollArea>
    </Card>
  );
};

// Main Chooser Component
const ChooserInterface: React.FC = () => {
  const [state, setState] = useState<ChooserState>({
    mode: "selection",
    task: null,
    selectedCandidate: null,
    error: null,
    taskType: null,
  });
  const [isSkipping, setIsSkipping] = useState(false);

  const fetchNextTask = useCallback(async (taskType: "appeal" | "chat") => {
    setState((prev) => ({ ...prev, mode: "loading", error: null, taskType }));

    try {
      const response = await fetch(`/ziggy/rest/chooser/next/${taskType}/`, {
	method: "GET",
	headers: {
	  "Content-Type": "application/json",
	},
	credentials: "include",
      });

      if (!response.ok) {
	if (response.status === 404) {
	  throw new Error("No tasks available right now. Please try again later.");
	}
	throw new Error(`Failed to fetch task: ${response.statusText}`);
      }

      const task: ChooserTask = await response.json();
      setState((prev) => ({
	...prev,
	mode: "voting",
	task,
	selectedCandidate: null,
      }));
    } catch (error) {
      setState((prev) => ({
	...prev,
	mode: "error",
	error: error instanceof Error ? error.message : "An error occurred",
      }));
    }
  }, []);

  const submitVote = useCallback(async () => {
    if (!state.task || !state.selectedCandidate) return;

    setState((prev) => ({ ...prev, mode: "loading" }));

    try {
      const response = await fetch("/ziggy/rest/chooser/vote/", {
	method: "POST",
	headers: {
	  "Content-Type": "application/json",
	  "X-CSRFToken": getCSRFToken(),
	},
	credentials: "include",
	body: JSON.stringify({
	  task_id: state.task.task_id,
	  chosen_candidate_id: state.selectedCandidate.id,
	  presented_candidate_ids: state.task.candidates.map((c) => c.id),
	}),
      });

      if (!response.ok) {
	const errorData = await response.json();
	throw new Error(errorData.error || "Failed to submit vote");
      }

      setState((prev) => ({
	...prev,
	mode: "confirmation",
      }));
    } catch (error) {
      setState((prev) => ({
	...prev,
	mode: "error",
	error: error instanceof Error ? error.message : "Failed to submit vote",
      }));
    }
  }, [state.task, state.selectedCandidate]);

  const handleSelectCandidate = useCallback((candidate: ChooserCandidate) => {
    setState((prev) => ({ ...prev, selectedCandidate: candidate }));
  }, []);

  const skipTask = useCallback(async () => {
    if (!state.task) return;

    setIsSkipping(true);

    try {
      const response = await fetch("/ziggy/rest/chooser/skip/", {
	method: "POST",
	headers: {
	  "Content-Type": "application/json",
	  "X-CSRFToken": getCSRFToken(),
	},
	credentials: "include",
	body: JSON.stringify({
	  task_id: state.task.task_id,
	}),
      });

      if (!response.ok) {
	const errorData = await response.json();
	throw new Error(errorData.error || "Failed to skip task");
      }

      // Fetch the next task
      if (state.taskType) {
	fetchNextTask(state.taskType);
      }
    } catch (error) {
      setState((prev) => ({
	...prev,
	mode: "error",
	error: error instanceof Error ? error.message : "Failed to skip task",
      }));
    } finally {
      setIsSkipping(false);
    }
  }, [state.task, state.taskType, fetchNextTask]);

  const resetToSelection = useCallback(() => {
    setState({
      mode: "selection",
      task: null,
      selectedCandidate: null,
      error: null,
      taskType: null,
    });
  }, []);

  const doAnother = useCallback(() => {
    if (state.taskType) {
      fetchNextTask(state.taskType);
    } else {
      resetToSelection();
    }
  }, [state.taskType, fetchNextTask, resetToSelection]);

  // Render mode selection
  if (state.mode === "selection") {
    return (
      <Container size="md" py="xl">
	<Stack gap="xl" align="center">
	  <Title order={1} ta="center">
	    Help Us Improve Our AI Responses
	  </Title>
	  <Text size="lg" ta="center" c="dimmed">
	    Help us train our AI to select the best responses by choosing your favorite
	    from a set of options.
	  </Text>
	  <Paper shadow="sm" p="md" radius="md" withBorder style={{ backgroundColor: "#fff3cd", borderColor: "#ffc107" }}>
	    <Text size="sm" ta="center" fw={500}>
	      ⚠️ Important: All scenarios shown are synthetic/anonymized for training purposes only.
	      No real patient data or actual insurance cases are used in this interface.
	    </Text>
	  </Paper>
	  <Group gap="lg" mt="xl">
	    <Button
	      size="xl"
	      radius="md"
	      onClick={() => fetchNextTask("appeal")}
	      style={{
		background: THEME.colors.buttonBackground,
		color: THEME.colors.buttonText,
	      }}
	    >
	      Score Appeal Letters
	    </Button>
	    <Button
	      size="xl"
	      radius="md"
	      onClick={() => fetchNextTask("chat")}
	      style={{
		background: THEME.colors.buttonBackground,
		color: THEME.colors.buttonText,
	      }}
	    >
	      Score Chat Responses
	    </Button>
	  </Group>
	  <Text size="sm" c="dimmed" ta="center" mt="xl">
	    Thanks for helping us choose better responses and fight bad insurance decisions 💪
	  </Text>
	</Stack>
      </Container>
    );
  }

  // Render loading state
  if (state.mode === "loading") {
    return (
      <Container size="md" py="xl">
	<Stack gap="xl" align="center">
	  <Loader size="xl" />
	  <Text size="lg">Loading...</Text>
	</Stack>
      </Container>
    );
  }

  // Render error state
  if (state.mode === "error") {
    return (
      <Container size="md" py="xl">
	<Stack gap="xl" align="center">
	  <Alert color="red" title="Error" radius="md">
	    {state.error}
	  </Alert>
	  <Button
	    size="lg"
	    radius="md"
	    onClick={resetToSelection}
	    style={{
	      background: THEME.colors.buttonBackground,
	      color: THEME.colors.buttonText,
	    }}
	  >
	    Go Back
	  </Button>
	</Stack>
      </Container>
    );
  }

  // Render confirmation state
  if (state.mode === "confirmation") {
    return (
      <Container size="md" py="xl">
	<Stack gap="xl" align="center">
	  <Title order={2} ta="center" c="green">
	    ✓ Vote Recorded!
	  </Title>
	  <Text size="lg" ta="center">
	    Thank you for helping us improve our responses!
	  </Text>
	  <Group gap="lg" mt="xl">
	    <Button
	      size="lg"
	      radius="md"
	      onClick={doAnother}
	      style={{
		background: THEME.colors.buttonBackground,
		color: THEME.colors.buttonText,
	      }}
	    >
	      Do Another One
	    </Button>
	    <Button
	      size="lg"
	      radius="md"
	      variant="outline"
	      onClick={resetToSelection}
	    >
	      Change Mode
	    </Button>
	  </Group>
	  <Text size="sm" c="dimmed" ta="center" mt="xl">
	    Thanks for helping us choose better responses and fight bad insurance decisions 💪
	  </Text>
	</Stack>
      </Container>
    );
  }

  // Render voting state
  if (state.mode === "voting" && state.task) {
    const labels = ["A", "B", "C", "D"];
    const hasEnoughCandidates = state.task.candidates.length >= 2;

    // If only one candidate, show a message and skip options
    if (!hasEnoughCandidates) {
      return (
	<Container size="md" py="xl">
	  <Stack gap="xl" align="center">
	    <Title order={2} ta="center">
	      Not Enough Options
	    </Title>
	    <Alert color="yellow" title="Single Option Available" radius="md">
	      This task only has one response option, so there's nothing to compare.
	      We'll skip this one and try to find another task for you.
	    </Alert>
	    <Group gap="lg" mt="xl">
	      <Button
		size="lg"
		radius="md"
		onClick={skipTask}
		loading={isSkipping}
		style={{
		  background: THEME.colors.buttonBackground,
		  color: THEME.colors.buttonText,
		}}
	      >
		Find Another Task
	      </Button>
	      <Button
		size="lg"
		radius="md"
		variant="outline"
		onClick={resetToSelection}
	      >
		Go Back
	      </Button>
	    </Group>
	  </Stack>
	</Container>
      );
    }

    return (
      <Container size="xl" py="xl">
	<Stack gap="xl">
	  <Title order={2} ta="center">
	    Choose the Best {state.taskType === "appeal" ? "Appeal Letter" : "Chat Response"}
	  </Title>

	  {/* Synthetic Data Warning */}
	  <Paper shadow="xs" p="xs" radius="md" withBorder style={{ backgroundColor: "#e7f3ff", borderColor: "#2196f3" }}>
	    <Text size="xs" ta="center" c="dimmed">
	      📋 Synthetic scenario for training purposes only
	    </Text>
	  </Paper>

	  {/* Context Section */}
	  <Paper shadow="sm" p="md" radius="md" withBorder>
	    <Text fw={600} mb="sm">
	      {state.task.task_type === "chat" ? "Conversation:" : "Context:"}
	    </Text>
	    {state.task.task_type === "chat" ? (
	      formatContext(state.task.task_context, state.task.task_type)
	    ) : (
	      <Text size="sm" style={{ whiteSpace: "pre-wrap" }}>
		{formatContext(state.task.task_context, state.task.task_type)}
	      </Text>
	    )}
	  </Paper>

	  {/* Candidates Grid */}
	  <Box>
	    <Group gap="lg" grow>
	      {state.task.candidates.map((candidate, index) => (
		<CandidateCard
		  key={candidate.id}
		  candidate={candidate}
		  isSelected={state.selectedCandidate?.id === candidate.id}
		  onSelect={() => handleSelectCandidate(candidate)}
		  label={labels[index] || String(index + 1)}
		/>
	      ))}
	    </Group>
	  </Box>

	  {/* Submit and Skip Buttons */}
	  <Group justify="center" mt="xl">
	    <Button
	      size="lg"
	      radius="md"
	      disabled={!state.selectedCandidate}
	      onClick={submitVote}
	      style={{
		background: state.selectedCandidate
		  ? THEME.colors.buttonBackground
		  : "#cccccc",
		color: THEME.colors.buttonText,
	      }}
	    >
	      Submit Vote
	    </Button>
	    <Button
	      size="lg"
	      radius="md"
	      variant="light"
	      onClick={skipTask}
	      loading={isSkipping}
	    >
	      Skip This One
	    </Button>
	    <Button
	      size="lg"
	      radius="md"
	      variant="outline"
	      onClick={resetToSelection}
	    >
	      Cancel
	    </Button>
	  </Group>

	  <Text size="sm" c="dimmed" ta="center" mt="lg">
	    Select the response you think is best, then click Submit Vote. Or skip if you're unsure.
	  </Text>
	</Stack>
      </Container>
    );
  }

  return null;
};

// Main App Component with MantineProvider
const App: React.FC = () => {
  return (
    <MantineProvider>
      <Box
	style={{
	  backgroundColor: THEME.colors.background,
	  minHeight: "80vh",
	  padding: "20px 0",
	}}
      >
	<ChooserInterface />
      </Box>
    </MantineProvider>
  );
};

// Mount the React app
const container = document.getElementById("chooser-root");
if (container) {
  const root = createRoot(container);
  root.render(<App />);
}

export default App;
