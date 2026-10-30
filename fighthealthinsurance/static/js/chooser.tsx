import React, { useCallback, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  Alert,
  Anchor,
  Badge,
  Box,
  Button,
  Card,
  Checkbox,
  Container,
  Group,
  List,
  Loader,
  MantineProvider,
  Paper,
  ScrollArea,
  SimpleGrid,
  Stack,
  Text,
  Title,
} from "@mantine/core";
import ErrorBoundary from "./ErrorBoundary";
import { THEME } from "./theme";
import { getCSRFToken } from "./shared";

// The Appeal Arena: several AI drafts answer one synthetic denial, and the
// visitor is the referee. Two things make a vote worth collecting here:
// the drafts are blind (the API sends no model names), and a draft that
// makes things up can be flagged, so a letter cannot win on invented facts.
// Drafts that are equally good can be called a tie, which splits the vote.

// Types for Chooser API responses. Candidates carry no model name, row id
// or stored order on purpose: `id` is an opaque token the vote endpoint
// resolves; see ChooserCandidateSerializer.
interface ChooserCandidate {
  id: string;
  kind: string;
  content: string;
}

interface EvidenceItem {
  pmid?: string;
  title?: string;
  url?: string;
  summary?: string;
}

// The patient's answers to the questions the app asked them; the drafts
// were given these as facts about the patient.
interface QAPair {
  question: string;
  answer: string;
}

interface ChooserTaskContext {
  procedure?: string;
  diagnosis?: string;
  denial_text_preview?: string;
  insurance_company?: string;
  facts?: string[];
  evidence?: EvidenceItem[];
  qa_pairs?: QAPair[];
  seeded?: boolean;
  prompt?: string;
  history?: Array<{ role: string; content: string }>;
}

interface ChooserTask {
  task_id: number;
  task_type: string;
  task_context: ChooserTaskContext;
  candidates: ChooserCandidate[];
}

type TaskType = "appeal" | "chat";

interface ChooserState {
  mode: "selection" | "loading" | "voting" | "confirmation" | "error";
  task: ChooserTask | null;
  selectedCandidate: ChooserCandidate | null;
  flaggedIds: string[];
  // Tie mode: the referee marks two or more drafts as equally best instead
  // of picking one.
  tieMode: boolean;
  tiedIds: string[];
  error: string | null;
  taskType: TaskType | null;
}

// The referee's record, kept in this browser only (never sent anywhere):
// a little momentum, nothing more.
interface Scoreboard {
  rounds: number;
  fouls: number;
}

const SCOREBOARD_KEY = "fhi-arena-scoreboard";
const RANKINGS_URL = "/chooser/rankings/";
const LABELS = ["A", "B", "C", "D", "E", "F"];

const readScoreboard = (): Scoreboard => {
  try {
    const raw = window.localStorage.getItem(SCOREBOARD_KEY);
    if (raw) {
      const parsed = JSON.parse(raw) as Partial<Scoreboard>;
      return {
        rounds: Number(parsed.rounds) || 0,
        fouls: Number(parsed.fouls) || 0,
      };
    }
  } catch {
    // Private windows and blocked storage: start from zero every time.
  }
  return { rounds: 0, fouls: 0 };
};

const writeScoreboard = (board: Scoreboard): void => {
  try {
    window.localStorage.setItem(SCOREBOARD_KEY, JSON.stringify(board));
  } catch {
    // Not worth an error: the record is a nicety.
  }
};

const plural = (n: number, word: string): string => `${n} ${word}${n === 1 ? "" : "s"}`;

// ---------------------------------------------------------------------------
// Pieces

const HouseRules: React.FC<{ compact?: boolean }> = ({ compact = false }) => {
  if (compact) {
    return (
      <Paper
        shadow="xs"
        p="sm"
        radius="md"
        withBorder
        style={{ backgroundColor: "#fffbea", borderColor: "#f5c518" }}
      >
        <Text size="sm">
          <strong>House rules:</strong> pick the draft you would actually send. A draft
          that makes something up (a study, a law, a test result, a date, a history
          that is not in the case) is committing a foul: flag it, and never pick it.
          Too close to call? Call a tie. Not sure? Skip the round.
        </Text>
      </Paper>
    );
  }
  return (
    <Paper shadow="sm" p="md" radius="md" withBorder style={{ width: "100%", maxWidth: "760px" }}>
      <Text fw={700} mb="xs">
        House rules
      </Text>
      <List spacing="xs" size="sm">
        <List.Item>
          <strong>Pick the draft you would send.</strong> Specific, persuasive, and
          right about the case and the rules.
        </List.Item>
        <List.Item>
          <strong>Making things up is a foul.</strong> Each case lists what is known
          about the patient, what the patient answered when the app asked, and the
          evidence on file. A draft that invents a study,
          a law, a test result, a date or a history that is not there should never
          win, however good it sounds. Flag it with 🚩 and pick another.
        </List.Item>
        <List.Item>
          <strong>Too close to call? Call a tie.</strong> If two or more drafts are
          equally good, mark them all; the vote is split between them.
        </List.Item>
        <List.Item>
          <strong>Not sure? Skip it.</strong> A skipped round beats a coin flip.
        </List.Item>
      </List>
    </Paper>
  );
};

const CaseRow: React.FC<{ label: string; value?: string }> = ({ label, value }) => {
  if (!value) return null;
  return (
    <Group gap="xs" align="flex-start" wrap="nowrap">
      <Text size="sm" c="dimmed" style={{ minWidth: "7.5rem" }}>
        {label}
      </Text>
      <Text size="sm" style={{ whiteSpace: "pre-wrap" }}>
        {value}
      </Text>
    </Group>
  );
};

// One article on file: its title (linked), its PMID, and the summary the
// drafts were given, clamped to a few lines until opened.
const EvidenceRow: React.FC<{ item: EvidenceItem }> = ({ item }) => {
  const [open, setOpen] = useState(false);
  return (
    <List.Item>
      {item.url ? (
        <Anchor href={item.url} target="_blank" rel="noopener noreferrer" size="sm">
          {item.title}
        </Anchor>
      ) : (
        item.title
      )}
      {item.pmid ? (
        <Text span size="xs" c="dimmed">
          {" "}
          (PMID {item.pmid})
        </Text>
      ) : null}
      {item.summary ? (
        <Box>
          <Text size="xs" c="dimmed" lineClamp={open ? undefined : 3}>
            {item.summary}
          </Text>
          <Button
            size="compact-xs"
            variant="subtle"
            color="gray"
            px={0}
            onClick={() => setOpen((v) => !v)}
          >
            {open ? "Less" : "What it found"}
          </Button>
        </Box>
      ) : null}
    </List.Item>
  );
};

// The denial the drafts answer: the pair, the insurer's reason, what is known
// about the patient, what they told the app, and the evidence a draft may cite.
const AppealCase: React.FC<{ context: ChooserTaskContext }> = ({ context }) => {
  const facts = context.facts ?? [];
  const qaPairs = (context.qa_pairs ?? []).filter((p) => p.question && p.answer);
  const evidence = (context.evidence ?? []).filter((e) => e.title);
  return (
    <Stack gap="md">
      <Stack gap={6}>
        <CaseRow label="Treatment" value={context.procedure} />
        <CaseRow label="Diagnosis" value={context.diagnosis} />
        <CaseRow label="Insurer" value={context.insurance_company} />
        <CaseRow label="Their reason" value={context.denial_text_preview} />
      </Stack>
      <Box>
        <Text size="sm" fw={600} mb={4}>
          What is known about the patient
        </Text>
        {facts.length > 0 ? (
          <List size="sm" spacing={4}>
            {facts.map((fact, idx) => (
              <List.Item key={`fact-${idx}`}>{fact}</List.Item>
            ))}
          </List>
        ) : (
          <Text size="sm" c="dimmed">
            Only what is above. Any detail about the patient beyond this is made up.
          </Text>
        )}
      </Box>
      {qaPairs.length > 0 && (
        <Box>
          <Text size="sm" fw={600} mb={4}>
            What the patient told us
          </Text>
          <Text size="xs" c="dimmed" mb={6}>
            The questions the app asked this (made-up) patient, and their answers. The
            drafts had these too, and may rely on them like the facts above.
          </Text>
          <Stack gap={6}>
            {qaPairs.map((pair, idx) => (
              <Box key={`qa-${idx}`}>
                <Text size="sm" fw={500}>
                  Q: {pair.question}
                </Text>
                <Text size="sm" style={{ whiteSpace: "pre-wrap" }}>
                  A: {pair.answer}
                </Text>
              </Box>
            ))}
          </Stack>
        </Box>
      )}
      <Box>
        <Text size="sm" fw={600} mb={4}>
          Evidence on file
        </Text>
        {evidence.length > 0 ? (
          <List size="sm" spacing={4}>
            {evidence.map((item, idx) => (
              <EvidenceRow key={`evidence-${item.pmid ?? idx}`} item={item} />
            ))}
          </List>
        ) : (
          <Text size="sm" c="dimmed">
            Nothing on file for this case. A draft that cites a study anyway is making
            it up.
          </Text>
        )}
        <Text size="xs" c="dimmed" mt={4}>
          A draft may cite only what is listed here, and claim no more for a study than
          it found. Anything else is a foul.
        </Text>
      </Box>
    </Stack>
  );
};

const ChatCase: React.FC<{ context: ChooserTaskContext }> = ({ context }) => {
  const parts: React.ReactNode[] = [];
  (context.history ?? []).forEach((msg, idx) => {
    const isUser = msg.role === "user";
    parts.push(
      <Box
        key={`history-${idx}`}
        p="sm"
        style={{
          backgroundColor: isUser ? "#f0f4f8" : "#e8f5e9",
          borderRadius: "8px",
          borderLeft: `3px solid ${isUser ? "#64b5f6" : "#81c784"}`,
        }}
      >
        <Text size="xs" fw={600} c={isUser ? "blue" : "green"} mb={4}>
          {isUser ? "👤 Person" : "🤖 Assistant"}
        </Text>
        <Text size="sm" style={{ whiteSpace: "pre-wrap" }}>
          {msg.content}
        </Text>
      </Box>,
    );
  });
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
          👤 Person (the drafts answer this)
        </Text>
        <Text size="sm" style={{ whiteSpace: "pre-wrap" }}>
          {context.prompt}
        </Text>
      </Box>,
    );
  }
  return parts.length > 0 ? (
    <Stack gap="xs">{parts}</Stack>
  ) : (
    <Text size="sm" c="dimmed">
      No conversation available.
    </Text>
  );
};

const CandidateCard: React.FC<{
  candidate: ChooserCandidate;
  label: string;
  isSelected: boolean;
  tieMode: boolean;
  isFlagged: boolean;
  onSelect: () => void;
  onToggleFlag: () => void;
}> = ({ candidate, label, isSelected, tieMode, isFlagged, onSelect, onToggleFlag }) => {
  const [expanded, setExpanded] = useState(false);
  const border = isSelected
    ? `3px solid ${THEME.colors.primary}`
    : isFlagged
      ? "2px solid #e03131"
      : "1px solid #e0e0e0";
  return (
    <Card
      shadow={isSelected ? "md" : "sm"}
      padding="lg"
      radius="md"
      withBorder
      role="button"
      tabIndex={0}
      aria-pressed={isSelected}
      aria-label={tieMode ? `Mark draft ${label} as tied for best` : `Pick draft ${label}`}
      onClick={() => {
        // Highlighting a passage to re-read it ends in a click on the card;
        // that is not a pick.
        if (window.getSelection()?.toString()) return;
        onSelect();
      }}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") {
          // Picks the draft; must not also reach the page-level Enter
          // handler, which casts the vote.
          event.preventDefault();
          event.stopPropagation();
          onSelect();
        }
      }}
      style={{
        cursor: "pointer",
        border,
        backgroundColor: isSelected ? "#f9fff0" : isFlagged ? "#fff5f5" : "#ffffff",
        opacity: isFlagged ? 0.85 : 1,
        transition: "all 0.2s ease",
      }}
    >
      <Group justify="space-between" mb="sm" wrap="nowrap">
        <Group gap="xs">
          <Text fw={700} size="lg">
            Draft {label}
          </Text>
          {isSelected && (
            <Badge color="green" variant="light">
              {tieMode ? "Tied for best" : "Your pick"}
            </Badge>
          )}
          {isFlagged && (
            <Badge color="red" variant="light">
              🚩 Foul
            </Badge>
          )}
        </Group>
        {/* The whole flag control, label included, stays out of the card's
            click and key handling: Mantine renders the label beside the
            input, so a click on its text would otherwise bubble to the
            card and pick the draft it was meant to flag. */}
        <div
          onClick={(event) => event.stopPropagation()}
          onKeyDown={(event) => event.stopPropagation()}
        >
          <Checkbox
            size="sm"
            color="red"
            checked={isFlagged}
            onChange={() => onToggleFlag()}
            aria-label={`Flag draft ${label} as making things up`}
            label={<Text size="xs">Makes things up</Text>}
          />
        </div>
      </Group>
      <ScrollArea h={expanded ? undefined : 320} offsetScrollbars>
        <Text size="sm" style={{ whiteSpace: "pre-wrap" }}>
          {candidate.content}
        </Text>
      </ScrollArea>
      <Group justify="flex-end" mt="xs">
        <Button
          size="compact-xs"
          variant="subtle"
          color="gray"
          onClick={(event) => {
            event.stopPropagation();
            setExpanded((v) => !v);
          }}
        >
          {expanded ? "Collapse" : "Read it all"}
        </Button>
      </Group>
    </Card>
  );
};

// ---------------------------------------------------------------------------
// The arena

const ChooserInterface: React.FC = () => {
  const [state, setState] = useState<ChooserState>({
    mode: "selection",
    task: null,
    selectedCandidate: null,
    flaggedIds: [],
    tieMode: false,
    tiedIds: [],
    error: null,
    taskType: null,
  });
  const [isSkipping, setIsSkipping] = useState(false);
  const [hasAcceptedTos, setHasAcceptedTos] = useState(false);
  const [scoreboard, setScoreboard] = useState<Scoreboard>({ rounds: 0, fouls: 0 });
  const [lastRoundFouls, setLastRoundFouls] = useState(0);
  const [lastRoundTie, setLastRoundTie] = useState(false);

  useEffect(() => {
    setScoreboard(readScoreboard());
  }, []);

  const fetchNextTask = useCallback(async (taskType: TaskType) => {
    setState((prev) => ({ ...prev, mode: "loading", error: null, taskType }));

    try {
      const response = await fetch(`/ziggy/rest/chooser/next/${taskType}/`, {
        method: "GET",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
      });

      if (!response.ok) {
        if (response.status === 404) {
          throw new Error(
            "The arena is empty right now: every case has been judged and new ones are being written. Please try again in a few minutes.",
          );
        }
        throw new Error(`Failed to fetch a case: ${response.statusText}`);
      }

      const task: ChooserTask = await response.json();
      setState((prev) => ({
        ...prev,
        mode: "voting",
        task,
        selectedCandidate: null,
        flaggedIds: [],
        tieMode: false,
        tiedIds: [],
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
    if (!state.task) return;
    // A tie goes out in display order, its first draft standing as the pick.
    const tied = state.tieMode
      ? state.task.candidates.filter((c) => state.tiedIds.includes(c.id)).map((c) => c.id)
      : [];
    const chosenId = state.tieMode ? tied[0] : state.selectedCandidate?.id;
    if (!chosenId || (state.tieMode && tied.length < 2)) return;
    const fouls = state.flaggedIds.length;

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
          chosen_candidate_id: chosenId,
          presented_candidate_ids: state.task.candidates.map((c) => c.id),
          flagged_candidate_ids: state.flaggedIds,
          tied_candidate_ids: tied,
        }),
      });

      if (!response.ok) {
        const errorData = await response.json();
        throw new Error(errorData.error || "Failed to submit vote");
      }

      const next = { rounds: scoreboard.rounds + 1, fouls: scoreboard.fouls + fouls };
      writeScoreboard(next);
      setScoreboard(next);
      setLastRoundFouls(fouls);
      setLastRoundTie(tied.length > 0);
      setState((prev) => ({ ...prev, mode: "confirmation" }));
    } catch (error) {
      setState((prev) => ({
        ...prev,
        mode: "error",
        error: error instanceof Error ? error.message : "Failed to submit vote",
      }));
    }
  }, [
    state.task,
    state.selectedCandidate,
    state.flaggedIds,
    state.tieMode,
    state.tiedIds,
    scoreboard,
  ]);

  const handleSelectCandidate = useCallback((candidate: ChooserCandidate) => {
    // A flagged draft cannot be picked (or tied): the voter clears the flag
    // first. Silently dropping the flag on a click (a stray one, say, after
    // highlighting text) would turn a foul into the winner.
    setState((prev) => {
      if (prev.flaggedIds.includes(candidate.id)) return prev;
      if (prev.tieMode) {
        // In a tie, a click adds the draft to the tie or takes it out.
        return {
          ...prev,
          tiedIds: prev.tiedIds.includes(candidate.id)
            ? prev.tiedIds.filter((id) => id !== candidate.id)
            : [...prev.tiedIds, candidate.id],
        };
      }
      return { ...prev, selectedCandidate: candidate };
    });
  }, []);

  const handleToggleTieMode = useCallback(() => {
    setState((prev) => {
      if (!prev.tieMode) {
        // The current pick starts the tie.
        return {
          ...prev,
          tieMode: true,
          tiedIds: prev.selectedCandidate ? [prev.selectedCandidate.id] : [],
        };
      }
      // Back to one pick: the first tied draft on the page, if any.
      const first = prev.task?.candidates.find((c) => prev.tiedIds.includes(c.id)) ?? null;
      return {
        ...prev,
        tieMode: false,
        tiedIds: [],
        selectedCandidate: first ?? prev.selectedCandidate,
      };
    });
  }, []);

  const handleToggleFlag = useCallback((candidate: ChooserCandidate) => {
    setState((prev) => {
      const flagged = prev.flaggedIds.includes(candidate.id);
      return {
        ...prev,
        flaggedIds: flagged
          ? prev.flaggedIds.filter((id) => id !== candidate.id)
          : [...prev.flaggedIds, candidate.id],
        // Flagging the current pick un-picks it, and takes it out of a tie.
        selectedCandidate:
          !flagged && prev.selectedCandidate?.id === candidate.id
            ? null
            : prev.selectedCandidate,
        tiedIds: flagged ? prev.tiedIds : prev.tiedIds.filter((id) => id !== candidate.id),
      };
    });
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
        body: JSON.stringify({ task_id: state.task.task_id }),
      });

      if (!response.ok) {
        const errorData = await response.json();
        throw new Error(errorData.error || "Failed to skip task");
      }

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
      flaggedIds: [],
      tieMode: false,
      tiedIds: [],
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

  // The vote can be cast: one pick, or a tie of two or more.
  const canVote = state.tieMode ? state.tiedIds.length >= 2 : Boolean(state.selectedCandidate);

  // Keyboard: A to F pick a draft (or, in a tie, add or remove it), Enter
  // casts the vote.
  useEffect(() => {
    // Not on the single-draft screen either: it offers no vote.
    if (state.mode !== "voting" || !state.task || state.task.candidates.length < 2) return;
    const task = state.task;
    const handler = (event: KeyboardEvent) => {
      // Ctrl+C to copy a passage must not also pick Draft C.
      if (event.ctrlKey || event.metaKey || event.altKey) return;
      const target = event.target as HTMLElement | null;
      if (target && ["INPUT", "TEXTAREA", "SELECT", "A", "BUTTON"].includes(target.tagName)) {
        return;
      }
      const index = LABELS.indexOf(event.key.toUpperCase());
      if (index >= 0 && index < task.candidates.length) {
        handleSelectCandidate(task.candidates[index]);
      } else if (event.key === "Enter" && canVote) {
        submitVote();
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [state.mode, state.task, canVote, handleSelectCandidate, submitVote]);

  const primaryButton = (enabled: boolean) => ({
    background: enabled ? THEME.buttonSharedStyles.background : "#cccccc",
    color: enabled ? THEME.buttonSharedStyles.color : "#666666",
  });

  // Render mode selection
  if (state.mode === "selection") {
    return (
      <Container size="md" py="xl">
        <Stack gap="xl" align="center">
          <Badge size="lg" variant="light" color="lime">
            Help us pick better letters
          </Badge>
          <Title order={1} ta="center">
            Appeal Arena
          </Title>
          <Text size="lg" ta="center" c="dimmed" style={{ maxWidth: "640px" }}>
            Insurance said no. A few AI drafts said &ldquo;we&rsquo;ll see about that.&rdquo;
            You are the referee: read the case, read the drafts, pick the one that would
            actually win, and call out any that make things up.
          </Text>
          {scoreboard.rounds > 0 && (
            <Text size="sm" c="dimmed">
              Your record: {plural(scoreboard.rounds, "round")} judged &middot;{" "}
              {plural(scoreboard.fouls, "foul")} called
            </Text>
          )}
          <Paper
            shadow="sm"
            p="md"
            radius="md"
            withBorder
            style={{ backgroundColor: "#fff3cd", borderColor: "#ffc107", maxWidth: "760px" }}
          >
            <Text size="sm" ta="center" fw={500}>
              ⚠️ Every case here is synthetic/anonymized for training purposes only: an
              invented scenario about a real condition and treatment. No real patient
              data or actual insurance cases are used in this interface.
            </Text>
          </Paper>
          <HouseRules />
          <Paper shadow="sm" p="md" radius="md" withBorder style={{ width: "100%", maxWidth: "760px" }}>
            <Checkbox
              size="sm"
              aria-label="Agree to Terms of Service"
              checked={hasAcceptedTos}
              onChange={(event) => setHasAcceptedTos(event.currentTarget.checked)}
              label={
                <Text size="sm">
                  I agree to the Terms of Service and understand the Arena exists to
                  improve AI response quality.
                </Text>
              }
            />
            <Text size="xs" mt="xs">
              <Anchor href="/tos" target="_blank" rel="noopener noreferrer">
                Read Terms of Service (opens in a new tab)
              </Anchor>
            </Text>
          </Paper>
          {!hasAcceptedTos && (
            <Text size="sm" c="orange" ta="center">
              Please accept the Terms of Service before stepping into the Arena.
            </Text>
          )}
          <Group gap="lg" mt="md">
            <Button
              size="xl"
              radius="md"
              disabled={!hasAcceptedTos}
              onClick={() => fetchNextTask("appeal")}
              style={primaryButton(hasAcceptedTos)}
            >
              Judge Appeal Letters
            </Button>
            <Button
              size="xl"
              radius="md"
              disabled={!hasAcceptedTos}
              onClick={() => fetchNextTask("chat")}
              style={primaryButton(hasAcceptedTos)}
            >
              Judge Chat Replies
            </Button>
          </Group>
          <Text size="sm" c="dimmed" ta="center">
            Curious who is winning? <Anchor href={RANKINGS_URL}>See the rankings</Anchor>.
          </Text>
          <Text size="sm" c="dimmed" ta="center">
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
          <Text size="lg">Calling the next case...</Text>
        </Stack>
      </Container>
    );
  }

  // Render error state
  if (state.mode === "error") {
    return (
      <Container size="md" py="xl">
        <Stack gap="xl" align="center">
          <Alert color="red" title="Time out" radius="md">
            {state.error}
          </Alert>
          <Button size="lg" radius="md" onClick={resetToSelection} style={primaryButton(true)}>
            Back to the Arena
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
            ✓ Vote counted!
          </Title>
          <Text size="lg" ta="center">
            Round {scoreboard.rounds} {lastRoundTie ? "called a tie" : "done"}
            {lastRoundFouls > 0 ? ` with ${plural(lastRoundFouls, "foul")} called` : ""}.
            Thank you for refereeing.
          </Text>
          <Text size="sm" c="dimmed" ta="center">
            Your record: {plural(scoreboard.rounds, "round")} judged &middot;{" "}
            {plural(scoreboard.fouls, "foul")} called
          </Text>
          <Group gap="lg" mt="md">
            <Button size="lg" radius="md" onClick={doAnother} style={primaryButton(true)}>
              Next round
            </Button>
            <Button size="lg" radius="md" variant="outline" onClick={resetToSelection}>
              Switch arena
            </Button>
          </Group>
          <Text size="sm" c="dimmed" ta="center">
            <Anchor href={RANKINGS_URL}>See how the models rank</Anchor>
          </Text>
          <Text size="sm" c="dimmed" ta="center">
            Thanks for helping us choose better responses and fight bad insurance decisions 💪
          </Text>
        </Stack>
      </Container>
    );
  }

  // Render voting state
  if (state.mode === "voting" && state.task) {
    const hasEnoughCandidates = state.task.candidates.length >= 2;
    const isAppeal = state.task.task_type !== "chat";

    // If only one candidate, show a message and skip options
    if (!hasEnoughCandidates) {
      return (
        <Container size="md" py="xl">
          <Stack gap="xl" align="center">
            <Title order={2} ta="center">
              Not enough contenders
            </Title>
            <Alert color="yellow" title="Only one draft in this case" radius="md">
              There is nothing to compare here. We will skip this one and find you another
              case.
            </Alert>
            <Group gap="lg" mt="xl">
              <Button
                size="lg"
                radius="md"
                onClick={skipTask}
                loading={isSkipping}
                style={primaryButton(true)}
              >
                Find another case
              </Button>
              <Button size="lg" radius="md" variant="outline" onClick={resetToSelection}>
                Back
              </Button>
            </Group>
          </Stack>
        </Container>
      );
    }

    const labelOf = (id: string) =>
      LABELS[state.task!.candidates.findIndex((c) => c.id === id)] ?? "?";
    const selectedLabel = state.selectedCandidate ? labelOf(state.selectedCandidate.id) : null;
    const tiedLabels = state.task.candidates
      .filter((c) => state.tiedIds.includes(c.id))
      .map((c) => labelOf(c.id));
    let castLabel: string;
    if (state.tieMode) {
      castLabel =
        tiedLabels.length >= 2
          ? `Cast a tie: Drafts ${tiedLabels.join(" & ")}`
          : "Mark two or more drafts as tied";
    } else {
      castLabel = selectedLabel ? `Cast vote for Draft ${selectedLabel}` : "Pick a draft to vote";
    }

    return (
      <Container size="xl" py="xl">
        <Stack gap="lg">
          <Group justify="center" gap="sm">
            <Badge size="lg" variant="filled" color="lime">
              Round {scoreboard.rounds + 1}
            </Badge>
            <Title order={2} ta="center">
              {isAppeal ? "Which appeal letter wins?" : "Which chat reply wins?"}
            </Title>
          </Group>

          <Text size="xs" ta="center" c="dimmed">
            📋 Synthetic case for training purposes only. Drafts are shown blind, in random order.
          </Text>

          <HouseRules compact />

          {/* The case */}
          <Paper shadow="sm" p="md" radius="md" withBorder>
            <Text fw={700} mb="sm">
              {isAppeal ? "The case" : "The conversation"}
            </Text>
            {isAppeal ? (
              <AppealCase context={state.task.task_context} />
            ) : (
              <ChatCase context={state.task.task_context} />
            )}
          </Paper>

          {/* The contenders */}
          <SimpleGrid cols={{ base: 1, md: 2 }} spacing="lg">
            {state.task.candidates.map((candidate, index) => (
              <CandidateCard
                key={candidate.id}
                candidate={candidate}
                label={LABELS[index] || String(index + 1)}
                isSelected={
                  state.tieMode
                    ? state.tiedIds.includes(candidate.id)
                    : state.selectedCandidate?.id === candidate.id
                }
                tieMode={state.tieMode}
                isFlagged={state.flaggedIds.includes(candidate.id)}
                onSelect={() => handleSelectCandidate(candidate)}
                onToggleFlag={() => handleToggleFlag(candidate)}
              />
            ))}
          </SimpleGrid>

          {/* Cast, skip, leave */}
          <Group justify="center" mt="md">
            <Button
              size="lg"
              radius="md"
              disabled={!canVote}
              onClick={submitVote}
              style={primaryButton(canVote)}
            >
              {castLabel}
            </Button>
            <Button
              size="lg"
              radius="md"
              variant={state.tieMode ? "filled" : "light"}
              color="grape"
              aria-pressed={state.tieMode}
              onClick={handleToggleTieMode}
            >
              {state.tieMode ? "No tie: pick one" : "It's a tie"}
            </Button>
            <Button size="lg" radius="md" variant="light" onClick={skipTask} loading={isSkipping}>
              Skip this round
            </Button>
            <Button size="lg" radius="md" variant="outline" onClick={resetToSelection}>
              Leave the Arena
            </Button>
          </Group>

          <Text size="sm" c="dimmed" ta="center">
            {state.tieMode
              ? "Click each draft that is tied for best (or press its letters), then cast the tie. The vote is split between them. A flagged draft cannot be part of a tie."
              : "Click a draft (or press its letter) to pick it, flag any that make things up, then cast your vote. A flagged draft cannot be picked until you clear the flag. Two or more equally good? Call it a tie. Skip if you are unsure."}
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
      <ErrorBoundary componentName="Chooser">
        <Box
          style={{
            backgroundColor: THEME.colors.background,
            minHeight: "80vh",
            padding: "20px 0",
          }}
        >
          <ChooserInterface />
        </Box>
      </ErrorBoundary>
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
