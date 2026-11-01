import React, {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
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
  SegmentedControl,
  Select,
  SimpleGrid,
  Stack,
  Text,
  Textarea,
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
//
// The referee gets two aids, both computed from what is on the page and
// neither sent anywhere: each study on file carries the verdict of an
// automated reviewer of its abstract (does it support the treatment, argue
// against it, or miss the point), and each draft's PMIDs are matched to the
// evidence on file, so a citation that is not on file, or leans on a study
// that argues against the treatment, is pointed out before the vote.

// Types for Chooser API responses. Candidates carry no model name, row id
// or stored order on purpose: `id` is an opaque token the vote endpoint
// resolves; see ChooserCandidateSerializer.
interface ChooserCandidate {
  id: string;
  kind: string;
  content: string;
  // A chat draft's lookups, run before it answered (chooser_chat_tools).
  tool_calls?: ToolCall[];
}

// One lookup a chat draft made: the tool, what it asked, the status lines
// the tool reported and what came back for the model to read.
interface ToolCall {
  tool: string;
  call: string;
  found: string;
  notes?: string[];
  timed_out?: boolean;
  // On a synthesized draft: a lookup one of the drafts it merged made. The
  // synthesis read those drafts, not the lookups themselves.
  from_drafts?: boolean;
}

// What the automated reviewer made of one study for this treatment and
// condition (research_judging.Judgment.as_dict): a verdict in a word and
// the three probabilities behind it. Attached as the case is served; a
// study nobody has reviewed has none. The drafts never saw it.
interface EvidenceJudgment {
  verdict: string;
  on_topic: number;
  supports: number;
  undermines: number;
}

interface EvidenceItem {
  pmid?: string;
  title?: string;
  url?: string;
  summary?: string;
  judgment?: EvidenceJudgment;
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
  // "simple": an everyday case kept simple on purpose; "standard" otherwise.
  case_style?: string;
  practice_area?: string;
  prompt?: string;
  history?: Array<{ role: string; content: string }>;
}

interface ChooserTask {
  task_id: number;
  task_type: string;
  task_context: ChooserTaskContext;
  candidates: ChooserCandidate[];
  review_count?: number;
}

type TaskType = "appeal" | "chat";

interface ChooserState {
  // "pending": the server has no case for us yet but is writing one (it
  // answered 202), and we are waiting for it.
  mode:
    | "selection"
    | "loading"
    | "pending"
    | "voting"
    | "confirmation"
    | "error";
  task: ChooserTask | null;
  // One selection is a winner; several are equally good winners.
  selectedIds: string[];
  allBad: boolean;
  rationale: string;
  flaggedIds: string[];
  error: string | null;
  taskType: TaskType | null;
}

// The referee's record, kept in this browser only (never sent anywhere):
// a little momentum, nothing more.
interface Scoreboard {
  rounds: number;
  fouls: number;
}

// The "I'm not a robot" check at the start of the Arena, on the same terms
// as the site's other forms. The page carries the site key only while the
// captcha is enforced; the tick's token rides on the first next-task
// request in this header, and the server remembers a good one for the
// session (ChooserViewSet._captcha_gate), so the box is ticked once.
const CAPTCHA_HEADER = "X-Recaptcha-Response";
const CAPTCHA_ERROR = "captcha_required";

interface Grecaptcha {
  render: (
    container: HTMLElement,
    params: {
      sitekey: string;
      callback: (token: string) => void;
      "expired-callback": () => void;
      "error-callback": () => void;
    },
  ) => number;
  reset: (widgetId?: number) => void;
}

declare global {
  interface Window {
    grecaptcha?: Partial<Grecaptcha>;
  }
}

const SCOREBOARD_KEY = "fhi-arena-scoreboard";
// Waiting for a case the server is writing: how long between polls when
// the server does not say, and how long to wait in all before giving up.
// A case is a round of model calls, so minutes, not seconds.
const PENDING_RETRY_DEFAULT_SECONDS = 15;
const PENDING_GIVE_UP_MS = 15 * 60 * 1000;
// How long one next-case request may take. The endpoint only reads the
// pool, so this is a hung connection, not a slow answer: without it a poll
// that never returned left the page waiting past the give-up above.
const NEXT_CASE_REQUEST_TIMEOUT_MS = 30 * 1000;
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

// The kind of case the referee asked for after their first round, kept in
// this browser only. "either" keeps the server's usual warm-up; the other
// two go to the next-task endpoint as its difficulty parameter.
type CasePreference = "straightforward" | "either" | "complex";

const PREFERENCE_KEY = "fhi-arena-case-preference";
const PREFERENCE_OPTIONS: { value: CasePreference; label: string }[] = [
  { value: "straightforward", label: "Straightforward" },
  { value: "either", label: "Either" },
  { value: "complex", label: "Complex" },
];

const isCasePreference = (value: unknown): value is CasePreference =>
  PREFERENCE_OPTIONS.some((option) => option.value === value);

const readPreference = (): CasePreference | null => {
  try {
    const raw = window.localStorage.getItem(PREFERENCE_KEY);
    if (isCasePreference(raw)) return raw;
  } catch {
    // Blocked storage: the referee is asked again on the next visit.
  }
  return null;
};

const writePreference = (preference: CasePreference): void => {
  try {
    window.localStorage.setItem(PREFERENCE_KEY, preference);
  } catch {
    // The choice still holds for this visit.
  }
};

const PRACTICE_AREA_OPTIONS = [
  { value: "primary-care", label: "Primary care / general medicine" },
  { value: "cardiology", label: "Cardiology / heart care" },
  { value: "oncology", label: "Oncology / cancer care" },
  { value: "orthopedics", label: "Orthopedics / musculoskeletal care" },
  { value: "neurology", label: "Neurology" },
  { value: "mental-health", label: "Mental health / psychiatry" },
  { value: "womens-health", label: "Women's health / OB-GYN" },
  { value: "pediatrics", label: "Pediatrics" },
  { value: "gastroenterology", label: "Gastroenterology / digestive health" },
  {
    value: "endocrinology",
    label: "Endocrinology / diabetes and hormone care",
  },
  { value: "pulmonology", label: "Pulmonology / respiratory care" },
  { value: "rheumatology", label: "Rheumatology / autoimmune care" },
  { value: "nephrology", label: "Nephrology / kidney care" },
  { value: "dermatology", label: "Dermatology / skin care" },
  { value: "physical-medicine", label: "Physical medicine and rehabilitation" },
  { value: "emergency-medicine", label: "Emergency medicine" },
];

const PRACTICE_AREA_KEY = "fhi-arena-practice-area";

const practiceAreaLabel = (slug?: string): string | null =>
  PRACTICE_AREA_OPTIONS.find((option) => option.value === slug)?.label ?? null;

const readPracticeArea = (): string => {
  try {
    const stored = window.localStorage.getItem(PRACTICE_AREA_KEY) ?? "";
    return practiceAreaLabel(stored) ? stored : "";
  } catch {
    return "";
  }
};

const writePracticeArea = (practiceArea: string): void => {
  try {
    if (practiceArea) {
      window.localStorage.setItem(PRACTICE_AREA_KEY, practiceArea);
    } else {
      window.localStorage.removeItem(PRACTICE_AREA_KEY);
    }
  } catch {
    // The choice still holds for this visit.
  }
};

// The next-task URL, asking for the judge's difficulty and practice area.
const nextTaskUrl = (
  taskType: TaskType,
  preference: CasePreference | null,
  practiceArea: string,
): string => {
  const base = `/ziggy/rest/chooser/next/${taskType}/`;
  const params = new URLSearchParams();
  if (preference && preference !== "either")
    params.set("difficulty", preference);
  if (practiceArea) params.set("practice_area", practiceArea);
  const query = params.toString();
  return query ? `${base}?${query}` : base;
};

const plural = (n: number, word: string): string =>
  `${n} ${word}${n === 1 ? "" : "s"}`;

const REVIEW_MILESTONES = [1, 3, 5, 10, 25, 50, 100];

const encouragementFor = (rounds: number): string => {
  const milestones: Record<number, string> = {
    1: "First review in the books. Your judgment is already helping us separate useful drafts from merely confident ones.",
    3: "Three cases reviewed. You are building a careful referee record.",
    5: "Five reviews complete. That is a meaningful set of comparisons for improving future answers.",
    10: "Double digits. Thanks for giving these drafts such close attention.",
    25: "Twenty-five reviews. Your consistency is making the rankings more trustworthy.",
    50: "Fifty reviews. Exceptional referee work, and a real help to the people who come after you.",
    100: "One hundred reviews. You have given the Arena an extraordinary body of careful judgment.",
  };
  if (milestones[rounds]) return milestones[rounds];
  const next = REVIEW_MILESTONES.find((milestone) => milestone > rounds);
  return next
    ? `${plural(next - rounds, "review")} to your next milestone. Every careful call helps.`
    : "Another careful review. Thank you for keeping the Arena honest.";
};

// The public scoreboard (public_rankings.py, `?format=json`): synthetic
// votes only, already aggregated, so showing it here gives away nothing
// about the drafts in play (those are served blind, in random order).
interface RankingRow {
  rank: number;
  model: string;
  won: number;
  shown: number;
  win_rate: number | null;
  flag_rate: number | null;
  warming_up: boolean;
}

interface RankingTable {
  kind: string;
  label: string;
  rows: RankingRow[];
  leader: RankingRow | null;
}

interface Rankings {
  min_shown: number;
  totals: { votes: number; tasks_judged: number; tasks_ready: number };
  windows: Array<{ slug: string; label: string; tables: RankingTable[] }>;
}

const STANDINGS_ROWS = 3;

// "pending" until the first answer, so the link never flashes before a
// 404; "hidden" when the rankings page answers 404 (it is behind
// ARENA_RANKINGS_PAGE_ENABLED, and nothing here should link to it then)
// and when the fetch merely failed: without an answer the page may be
// off, and the menu and footer link it whenever it is on, so the Arena
// shows nothing rather than a link that may be a 404.
type RankingsState = Rankings | "hidden" | "pending";

const fetchRankings = async (): Promise<RankingsState> => {
  try {
    const response = await fetch(`${RANKINGS_URL}?format=json`, {
      headers: { Accept: "application/json" },
    });
    if (!response.ok) {
      return "hidden";
    }
    const data = (await response.json()) as Rankings;
    return Array.isArray(data.windows) && data.windows.length > 0
      ? data
      : "hidden";
  } catch {
    // The standings are a nicety; the Arena works without them.
    return "hidden";
  }
};

const ratePercent = (rate: number | null): string =>
  rate === null ? "\u2014" : `${Math.round(rate)}%`;

// Who is winning, in a glance: each table's all-time leader and its top few
// rows, with the link to the full rankings. Without data it is nothing.
const Standings: React.FC<{ rankings: RankingsState }> = ({ rankings }) => {
  if (rankings === "hidden" || rankings === "pending") {
    return null;
  }
  const allTime = rankings.windows[0];
  return (
    <Paper
      shadow="sm"
      p="md"
      radius="md"
      withBorder
      style={{ width: "100%", maxWidth: "760px" }}
      data-testid="arena-standings"
    >
      <Group justify="space-between" align="baseline" mb="xs">
        <Text fw={700}>Standings</Text>
        <Text size="xs" c="dimmed">
          {plural(rankings.totals.votes, "vote")} so far &middot;{" "}
          {allTime.label.toLowerCase()}
        </Text>
      </Group>
      <SimpleGrid
        cols={{ base: 1, sm: allTime.tables.length > 1 ? 2 : 1 }}
        spacing="md"
      >
        {allTime.tables.map((table) => {
          const ranked = table.rows
            .filter((r) => !r.warming_up)
            .slice(0, STANDINGS_ROWS);
          return (
            <Box key={table.kind}>
              <Text size="sm" fw={600}>
                {table.label}
              </Text>
              {table.leader ? (
                <>
                  <Text size="sm">
                    <span aria-hidden="true">🏆</span> Leading:{" "}
                    <strong>{table.leader.model}</strong> with a{" "}
                    {ratePercent(table.leader.win_rate)} win rate over{" "}
                    {plural(table.leader.shown, "vote")}.
                  </Text>
                  <List size="sm" spacing={2} mt={4} type="ordered">
                    {ranked.map((row) => (
                      <List.Item key={row.model}>
                        {row.model}: {ratePercent(row.win_rate)} win rate
                        {row.flag_rate !== null && row.flag_rate > 0
                          ? `, caught making things up ${ratePercent(row.flag_rate)}`
                          : ""}
                      </List.Item>
                    ))}
                  </List>
                </>
              ) : (
                <Text size="sm" c="dimmed">
                  No leader yet: a model is ranked once it has been judged{" "}
                  {plural(rankings.min_shown, "time")}. Your votes decide it.
                </Text>
              )}
            </Box>
          );
        })}
      </SimpleGrid>
      <Group justify="center" mt="md">
        <Button
          component="a"
          href={RANKINGS_URL}
          variant="outline"
          radius="md"
          size="sm"
        >
          See the full rankings
        </Button>
      </Group>
    </Paper>
  );
};

// The reCAPTCHA checkbox. The script loads on its own (chooser.html); this
// waits for it, draws the widget once, and reports the tick's token, or
// null when it expires. A change of `resetKey` clears the box, for a tick
// the server would not take.
const CaptchaBox: React.FC<{
  siteKey: string;
  resetKey: number;
  onToken: (token: string | null) => void;
}> = ({ siteKey, resetKey, onToken }) => {
  const boxRef = React.useRef<HTMLDivElement>(null);
  const widgetRef = React.useRef<number | null>(null);
  const onTokenRef = React.useRef(onToken);
  onTokenRef.current = onToken;

  useEffect(() => {
    let cancelled = false;
    const tryRender = (): boolean => {
      const api = window.grecaptcha;
      if (
        !boxRef.current ||
        widgetRef.current !== null ||
        !api ||
        !api.render
      ) {
        return false;
      }
      widgetRef.current = api.render(boxRef.current, {
        sitekey: siteKey,
        callback: (token: string) => onTokenRef.current(token),
        "expired-callback": () => onTokenRef.current(null),
        "error-callback": () => onTokenRef.current(null),
      });
      return true;
    };
    if (tryRender()) {
      return undefined;
    }
    const timer = window.setInterval(() => {
      if (cancelled || tryRender()) {
        window.clearInterval(timer);
      }
    }, 200);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [siteKey]);

  useEffect(() => {
    if (
      resetKey > 0 &&
      widgetRef.current !== null &&
      window.grecaptcha?.reset
    ) {
      window.grecaptcha.reset(widgetRef.current);
      onTokenRef.current(null);
    }
  }, [resetKey]);

  return <div ref={boxRef} data-testid="arena-captcha" />;
};

// ---------------------------------------------------------------------------
// Referee aids: the reviewer's verdicts and the citation check

// How each verdict reads on the page. Colors are Mantine's names.
const VERDICTS: Record<string, { label: string; color: string; hint: string }> =
  {
    supports: {
      label: "Supports the treatment",
      color: "green",
      hint: "The abstract reports the treatment as effective, beneficial or recommended for this condition.",
    },
    mixed: {
      label: "Mixed",
      color: "yellow",
      hint: "The abstract argues both ways. A draft may cite it, but should say what it actually found.",
    },
    against: {
      label: "Argues against",
      color: "red",
      hint: "The abstract reports the treatment as ineffective, harmful or unsupported. A draft that cites it as support is misreading it; that is a foul.",
    },
    off_topic: {
      label: "Off topic",
      color: "gray",
      hint: "Not about this treatment for this condition. A draft that cites it as evidence for the case is stretching.",
    },
    unclear: {
      label: "Unclear",
      color: "gray",
      hint: "The reviewer could not tell whether it helps the case.",
    },
  };

type VerdictLook = (typeof VERDICTS)[string];

const verdictOf = (item?: EvidenceItem): VerdictLook | null =>
  item?.judgment ? (VERDICTS[item.judgment.verdict] ?? null) : null;

const percent = (p: number): string => `${Math.round(p * 100)}%`;

// A PMID as a draft writes one ("PMID 12345678", "PMID: 12345678",
// "PubMed ID 12345678") or a PubMed link. Two capture groups; one is set.
const PMID_PATTERN =
  "\\b(?:PMID|PubMed\\s+ID)\\s*[:#]?\\s*(\\d{4,9})\\b|pubmed\\.ncbi\\.nlm\\.nih\\.gov\\/(\\d{4,9})";

const pubmedUrl = (pmid: string): string =>
  `https://pubmed.ncbi.nlm.nih.gov/${pmid}/`;

// One PMID a draft cites, checked against the evidence on file.
interface Citation {
  pmid: string;
  // The study on file it names, if any. Not on file: the draft was told
  // to cite only the evidence listed, and the referee cannot check it here.
  item?: EvidenceItem;
}

const checkCitations = (
  content: string,
  evidence: EvidenceItem[],
): Citation[] => {
  const citations: Citation[] = [];
  const re = new RegExp(PMID_PATTERN, "gi");
  let match: RegExpExecArray | null;
  while ((match = re.exec(content)) !== null) {
    const pmid = match[1] ?? match[2];
    if (!pmid || citations.some((c) => c.pmid === pmid)) continue;
    citations.push({
      pmid,
      item: evidence.find((e) => String(e.pmid) === pmid),
    });
  }
  return citations;
};

// How a cited PMID reads on the page: not on file first, then what the
// reviewer made of the study, then simply on file. `suspicious` marks the
// two the referee should look into before voting.
interface CitationLook {
  label: string;
  color: string;
  hint: string;
  suspicious: boolean;
}

const citationLook = (citation: Citation): CitationLook => {
  if (!citation.item) {
    return {
      label: "not on file",
      color: "red",
      hint: "This PMID is not in the evidence on file, and the drafts were told to cite only what is. Open it: if it does not exist, or does not say what the draft claims, that is a foul.",
      suspicious: true,
    };
  }
  const verdict = verdictOf(citation.item);
  const kind = citation.item.judgment?.verdict;
  if (verdict && (kind === "against" || kind === "off_topic")) {
    return {
      label: verdict.label.toLowerCase(),
      color: "orange",
      hint: `On file, but the reviewer read it as ${verdict.label.toLowerCase()}. ${verdict.hint} Check what the draft claims it found.`,
      suspicious: true,
    };
  }
  if (verdict && kind === "mixed") {
    return {
      label: "mixed",
      color: "yellow",
      hint: `On file. ${verdict.hint}`,
      suspicious: false,
    };
  }
  return {
    label: "on file",
    color: "green",
    hint: verdict
      ? `On file. ${verdict.hint}`
      : "On file. Check that the draft claims no more for it than its summary says.",
    suspicious: false,
  };
};

// Details the case never gives (no names, no dates, no member or claim
// numbers), so a draft that states one has made it up. A fill-in such as
// "[Date]" or "XXXX" is fine and is not matched.
const MONTHS =
  "(?:January|February|March|April|May|June|July|August|September|October|November|December)";
const DATE_RE = new RegExp(
  `\\b${MONTHS}\\s+\\d{1,2},?\\s+(?:19|20)\\d{2}\\b|\\b\\d{1,2}\\s+${MONTHS},?\\s+(?:19|20)\\d{2}\\b|\\b(?:0?[1-9]|1[0-2])/(?:0?[1-9]|[12]\\d|3[01])/(?:19|20)\\d{2}\\b`,
  "i",
);
const ID_RE =
  /\b(?:member|subscriber|claim|policy|group|reference|authorization|auth|case)\s*(?:id|number|no\.?|#)\s*[:#]?\s*([A-Za-z0-9][A-Za-z0-9-]{4,})\b/i;

const inventedDetails = (content: string): string[] => {
  const hints: string[] = [];
  const date = DATE_RE.exec(content);
  if (date) hints.push(`Gives a date: ${date[0]}`);
  const id = ID_RE.exec(content);
  if (id && !/^[xX#_-]+$/.test(id[1])) hints.push(`Gives a number: ${id[0]}`);
  return hints;
};

// The draft's text with each PMID marked by how its check came out, so the
// referee sees the citations where they are made.
const MARK_COLORS: Record<string, string> = {
  red: "#ffe3e3",
  orange: "#ffe8cc",
  yellow: "#fff3bf",
  green: "#e9fac8",
};

const HighlightedDraft: React.FC<{
  content: string;
  citations: Citation[];
}> = ({ content, citations }) => {
  if (citations.length === 0) return <>{content}</>;
  const parts: React.ReactNode[] = [];
  const re = new RegExp(PMID_PATTERN, "gi");
  let last = 0;
  let match: RegExpExecArray | null;
  while ((match = re.exec(content)) !== null) {
    const pmid = match[1] ?? match[2];
    const citation = citations.find((c) => c.pmid === pmid);
    if (!citation) continue;
    const look = citationLook(citation);
    parts.push(content.slice(last, match.index));
    parts.push(
      <mark
        key={`${match.index}-${pmid}`}
        title={look.hint}
        style={{
          backgroundColor: MARK_COLORS[look.color] ?? MARK_COLORS.green,
          textDecoration: look.suspicious ? "underline wavy" : undefined,
          borderRadius: "3px",
          padding: "0 2px",
        }}
      >
        {match[0]}
      </mark>,
    );
    last = match.index + match[0].length;
  }
  parts.push(content.slice(last));
  return <>{parts}</>;
};

// The chips under a draft's title: each PMID it cites, linked to PubMed so
// the referee can open it, and any detail the case never gave. Kept out of
// the card's click and key handling, like the flag control.
const CitationCheck: React.FC<{
  citations: Citation[];
  hints: string[];
  hasEvidence: boolean;
}> = ({ citations, hints, hasEvidence }) => {
  if (citations.length === 0 && hints.length === 0) {
    return hasEvidence ? (
      <Text size="xs" c="dimmed">
        Cites no PMID.
      </Text>
    ) : null;
  }
  return (
    <div
      onClick={(event) => event.stopPropagation()}
      onKeyDown={(event) => event.stopPropagation()}
    >
      <Group gap={6}>
        {citations.map((citation) => {
          const look = citationLook(citation);
          return (
            <Badge
              key={citation.pmid}
              component="a"
              href={citation.item?.url || pubmedUrl(citation.pmid)}
              target="_blank"
              rel="noopener noreferrer"
              title={look.hint}
              aria-label={`PMID ${citation.pmid}, ${look.label}; opens PubMed in a new tab`}
              color={look.color}
              variant={look.suspicious ? "filled" : "light"}
              size="sm"
              style={{ cursor: "pointer", textTransform: "none" }}
            >
              PMID {citation.pmid} · {look.label}
            </Badge>
          );
        })}
        {hints.map((hint) => (
          <Badge
            key={hint}
            color="orange"
            variant="light"
            size="sm"
            title="The case gives no names, dates or numbers, so a draft that states one has made it up (a fill-in like [Date] is fine)."
            style={{ textTransform: "none" }}
          >
            {hint}
          </Badge>
        ))}
      </Group>
    </div>
  );
};

// ---------------------------------------------------------------------------
// Pieces

const HouseRules: React.FC<{ compact?: boolean; isAppeal?: boolean }> = ({
  compact = false,
  isAppeal = true,
}) => {
  if (compact) {
    return (
      <Paper
        shadow="xs"
        p="sm"
        radius="md"
        withBorder
        style={{ backgroundColor: "#fffbea", borderColor: "#f5c518" }}
      >
        <Text size="sm" fw={700}>
          Referee checklist: truthful first, then responsive, useful, and clear.
        </Text>
        <List size="sm" spacing={4} mt={4}>
          <List.Item>
            <strong>Would you use it?</strong>{" "}
            {isAppeal
              ? "Does it answer this denial with case-specific reasons and a clear request?"
              : "Does it answer the person's question with a safe, useful next step?"}{" "}
            Longer is not automatically better.
          </List.Item>
          <List.Item>
            <strong>Check facts before style.</strong>{" "}
            {isAppeal
              ? "Use the citation chips to spot claims worth checking."
              : "Open the lookup results to check what the reply claims."}{" "}
            Flag <strong>Makes things up</strong> for fabricated facts, sources,
            or case details—not for weak writing.
          </List.Item>
          <List.Item>
            <strong>Choose the honest outcome.</strong> Select every equally
            good draft; reject all when none is usable; skip when you cannot
            judge. Do not force a pick or use <strong>all bad</strong> just
            because none is perfect.
          </List.Item>
        </List>
      </Paper>
    );
  }
  return (
    <Paper
      shadow="sm"
      p="md"
      radius="md"
      withBorder
      style={{ width: "100%", maxWidth: "760px" }}
    >
      <Text fw={700} mb="xs">
        How to judge a round
      </Text>
      <Text size="sm" mb="sm">
        <strong>Truthful first, then responsive, useful, and clear.</strong>{" "}
        Judge what is on the page, not how long or confident a draft sounds.
      </Text>
      <List spacing="xs" size="sm">
        <List.Item>
          <strong>Start with &ldquo;would I use this?&rdquo;</strong> For an
          appeal, does it answer the insurer&rsquo;s actual reason with
          case-specific reasons and a clear request? For a chat reply, does it
          answer the person&rsquo;s question and offer a safe, useful next step?
          Specific beats long and generic.
        </List.Item>
        <List.Item>
          <strong>Making things up is a foul.</strong> Compare the draft with
          the case facts, the patient&rsquo;s answers, and the evidence. Flag an
          invented study, law, test result, date, or history, and never select
          that draft. A weak, vague, or unhelpful draft is not automatically a
          foul; use the outcome buttons instead.
        </List.Item>
        <List.Item>
          <strong>Use the referee aids, then verify what matters.</strong>{" "}
          Citation chips and automated verdicts are hints, not rulings: open a
          study when a surprising or decisive claim depends on it. For chat
          replies, open <em>What it found</em> and check whether the answer
          matches the lookup.
        </List.Item>
        <List.Item>
          <strong>Equally good? Select them all.</strong> One selection picks a
          winner; multiple selections tie for best and split the vote. Select
          only drafts you would genuinely be happy to use.
        </List.Item>
        <List.Item>
          <strong>Rejecting all is different from skipping.</strong> Reject all
          when you are confident none is usable, even if none invented facts.
          Skip when you cannot judge the case—not simply because the drafts are
          imperfect. A sentence about why is always welcome, especially for a
          tie or no-winner review.
        </List.Item>
      </List>
    </Paper>
  );
};

const CaseRow: React.FC<{ label: string; value?: string }> = ({
  label,
  value,
}) => {
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

// The reviewer's verdict on one study, as a badge; nothing for a study
// nobody has reviewed.
const VerdictBadge: React.FC<{ item: EvidenceItem }> = ({ item }) => {
  const verdict = verdictOf(item);
  if (!verdict) return null;
  return (
    <Badge
      color={verdict.color}
      variant="light"
      size="sm"
      title={verdict.hint}
      style={{ textTransform: "none", verticalAlign: "middle" }}
    >
      {verdict.label}
    </Badge>
  );
};

// One article on file: its title (linked), its PMID, the reviewer's verdict
// when there is one, and the summary the drafts were given, clamped to a
// few lines until opened; opened, the reviewer's numbers too.
const EvidenceRow: React.FC<{ item: EvidenceItem }> = ({ item }) => {
  const [open, setOpen] = useState(false);
  const judgment = item.judgment;
  return (
    <List.Item>
      {item.url ? (
        <Anchor
          href={item.url}
          target="_blank"
          rel="noopener noreferrer"
          size="sm"
        >
          {item.title}
        </Anchor>
      ) : (
        item.title
      )}
      {item.pmid ? (
        <Text span size="xs" c="dimmed">
          {" "}
          (PMID {item.pmid}){" "}
        </Text>
      ) : null}
      <VerdictBadge item={item} />
      {item.summary ? (
        <Box>
          <Text size="xs" c="dimmed" lineClamp={open ? undefined : 3}>
            {item.summary}
          </Text>
          {open && judgment ? (
            <Text size="xs" c="dimmed" mt={2}>
              Reviewer: {percent(judgment.on_topic)} about this treatment and
              condition &middot; {percent(judgment.supports)} supports it
              &middot; {percent(judgment.undermines)} argues against it.
            </Text>
          ) : null}
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
  const qaPairs = (context.qa_pairs ?? []).filter(
    (p) => p.question && p.answer,
  );
  const evidence = (context.evidence ?? []).filter((e) => e.title);
  const reviewed = evidence.some((e) => verdictOf(e) !== null);
  return (
    <Stack gap="md">
      <Stack gap={6}>
        <CaseRow label="Treatment" value={context.procedure} />
        <CaseRow label="Diagnosis" value={context.diagnosis} />
        <CaseRow label="Insurer" value={context.insurance_company} />
        <CaseRow label="Their reason" value={context.denial_text_preview} />
      </Stack>
      {context.case_style === "simple" && (
        <Text size="sm" c="dimmed">
          An everyday case, kept simple on purpose. A letter that stays short
          and sticks to the facts is doing it right; one that pads it out with
          claims the facts do not support is not.
        </Text>
      )}
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
            Only what is above. Any detail about the patient beyond this is made
            up.
          </Text>
        )}
      </Box>
      {qaPairs.length > 0 && (
        <Box>
          <Text size="sm" fw={600} mb={4}>
            What the patient told us
          </Text>
          <Text size="xs" c="dimmed" mb={6}>
            The questions the app asked this (made-up) patient, and their
            answers. The drafts had these too, and may rely on them like the
            facts above.
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
            Nothing on file for this case. A draft that cites a study anyway is
            making it up.
          </Text>
        )}
        <Text size="xs" c="dimmed" mt={4}>
          A draft may cite only what is listed here, and claim no more for a
          study than it found. Anything else is a foul.
          {reviewed
            ? " Each verdict is an automated reviewer's reading of the abstract: whether the study is about this treatment for this condition, and whether it supports it. The drafts saw the summaries, not the verdicts. A hint, not a ruling: open the study when it matters."
            : ""}
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

// What a chat draft looked up before answering, so the voter can check the
// reply against what the lookup actually found (and see when it found
// nothing). The found text stays folded until asked for: it is long.
const ToolCallList: React.FC<{ calls: ToolCall[] }> = ({ calls }) => {
  const [open, setOpen] = useState<Record<number, boolean>>({});
  const fromDrafts = calls.some((call) => call.from_drafts);
  return (
    <Stack gap={6}>
      {calls.map((call, idx) => {
        const isOpen = !!open[idx];
        const empty = !call.found;
        return (
          <Box
            key={idx}
            p="xs"
            style={{
              backgroundColor: "#f3f0ff",
              borderRadius: "6px",
              borderLeft: "3px solid #9775fa",
            }}
          >
            <Group
              justify="space-between"
              gap="xs"
              wrap="nowrap"
              align="flex-start"
            >
              <Text size="xs" style={{ wordBreak: "break-word" }}>
                🔍 <b>{call.tool}</b>: {call.call}
                {call.timed_out ? (
                  <Text span size="xs" c="red">
                    {" "}
                    (timed out)
                  </Text>
                ) : empty ? (
                  <Text span size="xs" c="dimmed">
                    {" "}
                    (found nothing)
                  </Text>
                ) : null}
              </Text>
              {!empty || (call.notes?.length ?? 0) > 0 ? (
                <Button
                  size="compact-xs"
                  variant="subtle"
                  color="violet"
                  onClick={(event) => {
                    event.stopPropagation();
                    setOpen((prev) => ({ ...prev, [idx]: !prev[idx] }));
                  }}
                >
                  {isOpen ? "Hide" : "What it found"}
                </Button>
              ) : null}
            </Group>
            {isOpen ? (
              <Box mt={4}>
                {(call.notes ?? []).map((line, i) => (
                  <Text key={i} size="xs" c="dimmed">
                    {line}
                  </Text>
                ))}
                {!empty ? (
                  <ScrollArea.Autosize mah={240} offsetScrollbars>
                    <Text size="xs" mt={4} style={{ whiteSpace: "pre-wrap" }}>
                      {call.found}
                    </Text>
                  </ScrollArea.Autosize>
                ) : null}
              </Box>
            ) : null}
          </Box>
        );
      })}
      <Text size="xs" c="dimmed">
        {fromDrafts
          ? "The drafts this one combines looked this up; it read their answers, not the lookups. Does it claim no more than they found?"
          : "The draft looked this up before answering. Did it use what it found, and say no more than that?"}
      </Text>
    </Stack>
  );
};

const CandidateCard: React.FC<{
  candidate: ChooserCandidate;
  label: string;
  isSelected: boolean;
  showTieLabel: boolean;
  isFlagged: boolean;
  // The case's evidence, for the citation check; an appeal draft is also
  // checked for details the case never gave. A chat reply may rightly name
  // a date, so it gets neither.
  evidence: EvidenceItem[];
  checkFacts: boolean;
  onSelect: () => void;
  onToggleFlag: () => void;
}> = ({
  candidate,
  label,
  isSelected,
  showTieLabel,
  isFlagged,
  evidence,
  checkFacts,
  onSelect,
  onToggleFlag,
}) => {
  const [expanded, setExpanded] = useState(false);
  const citations = useMemo(
    () => (checkFacts ? checkCitations(candidate.content, evidence) : []),
    [candidate.content, evidence, checkFacts],
  );
  const hints = useMemo(
    () => (checkFacts ? inventedDetails(candidate.content) : []),
    [candidate.content, checkFacts],
  );
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
      role="checkbox"
      tabIndex={0}
      aria-checked={isSelected}
      aria-label={`Select draft ${label} as a top choice`}
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
        backgroundColor: isSelected
          ? "#f9fff0"
          : isFlagged
            ? "#fff5f5"
            : "#ffffff",
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
              {showTieLabel ? "Tied for best" : "Your pick"}
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
      {checkFacts ? (
        <Box mb="sm">
          <CitationCheck
            citations={citations}
            hints={hints}
            hasEvidence={evidence.length > 0}
          />
        </Box>
      ) : null}
      {candidate.tool_calls && candidate.tool_calls.length > 0 ? (
        // Its own clicks and keys (the fold buttons, selecting text to
        // read) are not a pick of the draft: Enter or Space on a fold
        // button would otherwise reach the card's key handler, which picks
        // the draft and swallows the button's own activation.
        <Box
          mb="sm"
          onClick={(event) => event.stopPropagation()}
          onKeyDown={(event) => event.stopPropagation()}
        >
          <ToolCallList calls={candidate.tool_calls} />
        </Box>
      ) : null}
      <ScrollArea h={expanded ? undefined : 320} offsetScrollbars>
        <Text size="sm" style={{ whiteSpace: "pre-wrap" }}>
          <HighlightedDraft content={candidate.content} citations={citations} />
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

// Asked once, after the referee's first round: each answer also starts the
// next round.
const PreferenceQuestion: React.FC<{
  onChoose: (preference: CasePreference) => void;
  buttonStyle: React.CSSProperties;
}> = ({ onChoose, buttonStyle }) => (
  <Paper
    shadow="sm"
    p="lg"
    radius="md"
    withBorder
    style={{ width: "100%", maxWidth: "640px" }}
  >
    <Stack gap="sm" align="center">
      <Title order={3} ta="center">
        What kind of case next?
      </Title>
      <Text size="sm" ta="center" c="dimmed">
        Straightforward cases are quicker reads, with shorter drafts and fewer
        studies to check. Complex ones take longer, with more to read and more
        citations to check. You can change this after any round.
      </Text>
      <Group justify="center" gap="sm" mt="xs">
        <Button
          radius="md"
          onClick={() => onChoose("straightforward")}
          style={buttonStyle}
        >
          Keep them straightforward
        </Button>
        <Button
          radius="md"
          onClick={() => onChoose("complex")}
          style={buttonStyle}
        >
          Bring on complex ones
        </Button>
        <Button radius="md" variant="subtle" onClick={() => onChoose("either")}>
          Either is fine
        </Button>
      </Group>
    </Stack>
  </Paper>
);

// Once answered, the choice can be changed between rounds.
const PreferencePicker: React.FC<{
  value: CasePreference;
  onChange: (preference: CasePreference) => void;
}> = ({ value, onChange }) => (
  <Stack gap={4} align="center">
    <Text size="sm" c="dimmed" id="arena-case-preference-label">
      Next cases
    </Text>
    <SegmentedControl
      radius="md"
      value={value}
      onChange={(next) => {
        if (isCasePreference(next)) onChange(next);
      }}
      data={PREFERENCE_OPTIONS}
      aria-labelledby="arena-case-preference-label"
    />
  </Stack>
);

// ---------------------------------------------------------------------------
// The arena

const ChooserInterface: React.FC<{
  captchaSiteKey: string;
  captchaAlreadyPassed: boolean;
  initialReviewCount: number | null;
}> = ({ captchaSiteKey, captchaAlreadyPassed, initialReviewCount }) => {
  const [state, setState] = useState<ChooserState>({
    mode: "selection",
    task: null,
    selectedIds: [],
    allBad: false,
    rationale: "",
    flaggedIds: [],
    error: null,
    taskType: null,
  });
  const [isSkipping, setIsSkipping] = useState(false);
  // The poll for a case being written (see fetchNextTask): one timer, and
  // when the wait started, so it can be given up. Cleared when the referee
  // leaves the Arena or the page unmounts.
  const pollTimer = useRef<number | null>(null);
  const pendingSince = useRef<number | null>(null);
  // Each next-task request takes a number, and leaving the Arena (or
  // unmounting) takes one too: an answer to an older request (a double
  // press, a skip racing a reload, a fetch from before the referee left) is
  // dropped, so it neither seats a stale case nor flips the page back to
  // waiting and starts the poll again.
  const requestSeq = useRef(0);
  // The poll asks through the latest fetchNextTask, not the one that set
  // it: that one still holds the captcha token as it was before it was spent.
  const fetchNextTaskRef = useRef<
    ((taskType: TaskType) => Promise<void>) | null
  >(null);

  const clearPoll = useCallback(() => {
    requestSeq.current += 1;
    if (pollTimer.current !== null) {
      window.clearTimeout(pollTimer.current);
      pollTimer.current = null;
    }
    pendingSince.current = null;
  }, []);

  useEffect(() => clearPoll, [clearPoll]);
  const [hasAcceptedTos, setHasAcceptedTos] = useState(false);
  // The captcha: the tick's token until it is spent, "passed" once the
  // server has taken one for this session, and a notice when it would not.
  const [captchaToken, setCaptchaToken] = useState<string | null>(null);
  const [captchaPassed, setCaptchaPassed] = useState(captchaAlreadyPassed);
  const [captchaResets, setCaptchaResets] = useState(0);
  const [captchaNotice, setCaptchaNotice] = useState<string | null>(null);
  const captchaNeeded = Boolean(captchaSiteKey) && !captchaPassed;
  const captchaReady = !captchaNeeded || captchaToken !== null;
  const canStart = hasAcceptedTos && captchaReady;

  // The gate refused a request: the tick was missing, spent or refused, or
  // the session lost its mark. Back to the landing with the box cleared and
  // the server's reason. A page that loaded while the captcha was off has
  // no site key to draw a box with, so it reloads to get one.
  const backToCaptcha = useCallback(
    (message: string | undefined) => {
      if (!captchaSiteKey) {
        window.location.reload();
        return;
      }
      setCaptchaPassed(false);
      setCaptchaToken(null);
      setCaptchaResets((n) => n + 1);
      setCaptchaNotice(
        message || 'Please tick the "I\'m not a robot" box, then try again.',
      );
      setState((prev) => ({
        ...prev,
        mode: "selection",
        task: null,
        taskType: null,
      }));
    },
    [captchaSiteKey],
  );

  // The gate's answer, when a response is its 403; undefined otherwise.
  const captchaRefusal = async (
    response: Response,
  ): Promise<string | undefined> => {
    if (response.status !== 403) {
      return undefined;
    }
    try {
      const body = (await response.clone().json()) as {
        error?: string;
        message?: string;
      };
      if (body.error === CAPTCHA_ERROR) {
        return body.message || "";
      }
    } catch {
      // Not the gate's JSON.
    }
    return undefined;
  };
  const [scoreboard, setScoreboard] = useState<Scoreboard>({
    rounds: 0,
    fouls: 0,
  });
  const [reviewCount, setReviewCount] = useState<number | null>(
    initialReviewCount,
  );
  const [lastRoundFouls, setLastRoundFouls] = useState(0);
  const [lastRoundTie, setLastRoundTie] = useState(false);
  const [lastRoundAllBad, setLastRoundAllBad] = useState(false);
  const [rankings, setRankings] = useState<RankingsState>("pending");
  // Null until the referee answers the question after their first round.
  // The ref is what fetches read, so a choice applies to the very next case.
  const [preference, setPreference] = useState<CasePreference | null>(null);
  const preferenceRef = useRef<CasePreference | null>(null);
  const [practiceArea, setPracticeArea] = useState("");
  const practiceAreaRef = useRef("");

  useEffect(() => {
    setScoreboard(readScoreboard());
    const stored = readPreference();
    preferenceRef.current = stored;
    setPreference(stored);
    const storedArea = readPracticeArea();
    practiceAreaRef.current = storedArea;
    setPracticeArea(storedArea);
    let cancelled = false;
    fetchRankings().then((data) => {
      if (!cancelled) {
        setRankings(data);
      }
    });
    return () => {
      cancelled = true;
    };
  }, []);

  const fetchNextTask = useCallback(
    async (taskType: TaskType) => {
      const seq = ++requestSeq.current;
      const stale = () => seq !== requestSeq.current;
      if (pollTimer.current !== null) {
        window.clearTimeout(pollTimer.current);
        pollTimer.current = null;
      }
      setState((prev) => ({
        ...prev,
        mode: pendingSince.current === null ? "loading" : "pending",
        error: null,
        taskType,
      }));

      // One deadline for the whole request, body included: a response whose
      // body stalled after its headers would otherwise hang the page too.
      const controller = new AbortController();
      const deadline = window.setTimeout(
        () => controller.abort(),
        NEXT_CASE_REQUEST_TIMEOUT_MS,
      );
      try {
        const headers: Record<string, string> = {
          "Content-Type": "application/json",
        };
        if (captchaToken) {
          headers[CAPTCHA_HEADER] = captchaToken;
        }
        const response = await fetch(
          nextTaskUrl(taskType, preferenceRef.current, practiceAreaRef.current),
          {
            method: "GET",
            headers,
            credentials: "include",
            signal: controller.signal,
          },
        );
        if (stale()) return;

        const refusal = await captchaRefusal(response);
        if (stale()) {
          return;
        }
        if (refusal !== undefined) {
          pendingSince.current = null;
          backToCaptcha(refusal);
          return;
        }
        if (captchaToken && (response.ok || response.status === 404)) {
          // A tick is single use, and these answers all come from past the
          // gate (a case being written is a 202, an empty arena a 404), so
          // the session now holds the mark. Any other failure may have come
          // before the gate and spent nothing; the next try sends the tick
          // again.
          setCaptchaToken(null);
          setCaptchaPassed(true);
          setCaptchaNotice(null);
        }

        if (response.status === 202) {
          // No case for us yet, but one is being written (the server set the
          // generation off, or another process holds it): wait for it, asking
          // again when the server says to, rather than sending the referee
          // away for "a few minutes" from a case that is one or two away.
          const started = pendingSince.current ?? Date.now();
          pendingSince.current = started;
          if (Date.now() - started > PENDING_GIVE_UP_MS) {
            pendingSince.current = null;
            throw new Error(
              "A new case is still being written. Please come back in a few minutes.",
            );
          }
          let retryAfter = PENDING_RETRY_DEFAULT_SECONDS;
          try {
            const body = (await response.json()) as { retry_after?: number };
            if (typeof body.retry_after === "number" && body.retry_after > 0) {
              retryAfter = body.retry_after;
            }
          } catch {
            // The body is a courtesy; the default interval is fine.
          }
          if (stale()) return;
          setState((prev) => ({
            ...prev,
            mode: "pending",
            error: null,
            taskType,
          }));
          pollTimer.current = window.setTimeout(() => {
            pollTimer.current = null;
            void fetchNextTaskRef.current?.(taskType);
          }, retryAfter * 1000);
          return;
        }
        pendingSince.current = null;

        if (!response.ok) {
          if (response.status === 404) {
            throw new Error(
              "The arena is empty right now: every case has been judged and no new one is on its way yet. Please try again later.",
            );
          }
          throw new Error(`Failed to fetch a case: ${response.statusText}`);
        }

        const task: ChooserTask = await response.json();
        if (stale()) return;
        if (task.review_count !== undefined) setReviewCount(task.review_count);
        setState((prev) => ({
          ...prev,
          mode: "voting",
          task,
          selectedIds: [],
          allBad: false,
          rationale: "",
          flaggedIds: [],
        }));
      } catch (error) {
        // Checked first: a timed-out request from before the referee left
        // must not reset the give-up clock of the wait they started since.
        if (stale()) return;
        const timedOut = controller.signal.aborted;
        if (timedOut) pendingSince.current = null;
        setState((prev) => ({
          ...prev,
          mode: "error",
          error: timedOut
            ? "The server did not answer in time. Please try again in a few minutes."
            : error instanceof Error
              ? error.message
              : "An error occurred",
        }));
      } finally {
        window.clearTimeout(deadline);
      }
    },
    [captchaToken, backToCaptcha],
  );

  useEffect(() => {
    fetchNextTaskRef.current = fetchNextTask;
  }, [fetchNextTask]);

  const submitVote = useCallback(async () => {
    if (!state.task) return;
    const selected = state.task.candidates
      .filter((candidate) => state.selectedIds.includes(candidate.id))
      .map((candidate) => candidate.id);
    const chosenId = state.allBad ? undefined : selected[0];
    if (!state.allBad && !chosenId) return;
    const tied = !state.allBad && selected.length > 1 ? selected : [];
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
          ...(chosenId ? { chosen_candidate_id: chosenId } : {}),
          presented_candidate_ids: state.task.candidates.map((c) => c.id),
          flagged_candidate_ids: state.flaggedIds,
          tied_candidate_ids: tied,
          all_bad: state.allBad,
          rationale: state.rationale,
        }),
      });

      if (!response.ok) {
        const refusal = await captchaRefusal(response);
        if (refusal !== undefined) {
          backToCaptcha(refusal);
          return;
        }
        const errorData = await response.json();
        throw new Error(errorData.error || "Failed to submit vote");
      }

      const result = (await response.json()) as { review_count?: number };
      if (result.review_count !== undefined)
        setReviewCount(result.review_count);
      const next = {
        rounds: scoreboard.rounds + 1,
        fouls: scoreboard.fouls + fouls,
      };
      writeScoreboard(next);
      setScoreboard(next);
      setLastRoundFouls(fouls);
      setLastRoundTie(tied.length > 0);
      setLastRoundAllBad(state.allBad);
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
    state.selectedIds,
    state.allBad,
    state.rationale,
    state.flaggedIds,
    scoreboard,
    backToCaptcha,
  ]);

  const handleSelectCandidate = useCallback((candidate: ChooserCandidate) => {
    // A flagged draft cannot win; clear its foul before selecting it.
    setState((prev) => {
      if (prev.flaggedIds.includes(candidate.id)) return prev;
      return {
        ...prev,
        allBad: false,
        selectedIds: prev.selectedIds.includes(candidate.id)
          ? prev.selectedIds.filter((id) => id !== candidate.id)
          : [...prev.selectedIds, candidate.id],
      };
    });
  }, []);

  const handleToggleAllBad = useCallback(() => {
    setState((prev) => ({ ...prev, allBad: !prev.allBad, selectedIds: [] }));
  }, []);

  const handleToggleFlag = useCallback((candidate: ChooserCandidate) => {
    setState((prev) => {
      const flagged = prev.flaggedIds.includes(candidate.id);
      return {
        ...prev,
        flaggedIds: flagged
          ? prev.flaggedIds.filter((id) => id !== candidate.id)
          : [...prev.flaggedIds, candidate.id],
        selectedIds: flagged
          ? prev.selectedIds
          : prev.selectedIds.filter((id) => id !== candidate.id),
      };
    });
  }, []);

  const skipTask = useCallback(async () => {
    if (!state.task) return;

    // Leaving the Arena while the skip is in flight must not bring the page
    // back to the next case when the skip completes.
    const seq = requestSeq.current;
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
        const refusal = await captchaRefusal(response);
        if (refusal !== undefined) {
          backToCaptcha(refusal);
          return;
        }
        const errorData = await response.json();
        throw new Error(errorData.error || "Failed to skip task");
      }

      if (state.taskType && seq === requestSeq.current) {
        fetchNextTask(state.taskType);
      }
    } catch (error) {
      if (seq !== requestSeq.current) return;
      setState((prev) => ({
        ...prev,
        mode: "error",
        error: error instanceof Error ? error.message : "Failed to skip task",
      }));
    } finally {
      setIsSkipping(false);
    }
  }, [state.task, state.taskType, fetchNextTask, backToCaptcha]);

  const resetToSelection = useCallback(() => {
    clearPoll();
    setState({
      mode: "selection",
      task: null,
      selectedIds: [],
      allBad: false,
      rationale: "",
      flaggedIds: [],
      error: null,
      taskType: null,
    });
  }, [clearPoll]);

  const doAnother = useCallback(() => {
    if (state.taskType) {
      fetchNextTask(state.taskType);
    } else {
      resetToSelection();
    }
  }, [state.taskType, fetchNextTask, resetToSelection]);

  const choosePreference = useCallback((next: CasePreference) => {
    preferenceRef.current = next;
    setPreference(next);
    writePreference(next);
  }, []);

  const choosePracticeArea = useCallback((next: string | null) => {
    const value = next ?? "";
    practiceAreaRef.current = value;
    setPracticeArea(value);
    writePracticeArea(value);
  }, []);

  // The answer to the question after the first round, and on to that round.
  const answerPreference = useCallback(
    (next: CasePreference) => {
      choosePreference(next);
      doAnother();
    },
    [choosePreference, doAnother],
  );

  const canVote = state.allBad || state.selectedIds.length > 0;

  // Keyboard: A to F toggle a top choice, Enter casts the review.
  useEffect(() => {
    // Not on the single-draft screen either: it offers no vote.
    if (
      state.mode !== "voting" ||
      !state.task ||
      state.task.candidates.length < 2
    )
      return;
    const task = state.task;
    const handler = (event: KeyboardEvent) => {
      // Ctrl+C to copy a passage must not also pick Draft C.
      if (event.ctrlKey || event.metaKey || event.altKey) return;
      const target = event.target as HTMLElement | null;
      if (
        target &&
        ["INPUT", "TEXTAREA", "SELECT", "A", "BUTTON"].includes(target.tagName)
      ) {
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
            Insurance said no. A few AI drafts said &ldquo;we&rsquo;ll see about
            that.&rdquo; You are the referee: read the case, read the drafts,
            pick the one that would actually win, and call out any that make
            things up.
          </Text>
          {reviewCount !== null && (
            <Badge size="lg" variant="light" color="blue">
              Your account: {plural(reviewCount, "review")}
            </Badge>
          )}
          {scoreboard.rounds > 0 && (
            <Text size="sm" c="dimmed">
              In this browser: {plural(scoreboard.rounds, "round")} judged
              &middot; {plural(scoreboard.fouls, "foul")} called
            </Text>
          )}
          {preference && (
            <PreferencePicker value={preference} onChange={choosePreference} />
          )}
          <Paper
            shadow="sm"
            p="md"
            radius="md"
            withBorder
            style={{ width: "100%", maxWidth: "760px" }}
          >
            <Select
              label="Which area of practice do you know best? (optional)"
              description="Choose the cases you can judge most confidently. General cases are always available."
              placeholder="No preference — show me general cases"
              data={PRACTICE_AREA_OPTIONS}
              value={practiceArea || null}
              onChange={choosePracticeArea}
              searchable
              clearable
              data-testid="arena-practice-area"
            />
            {practiceArea && (
              <Text size="xs" c="dimmed" mt="xs">
                General cases can start right away. We will prepare{" "}
                {practiceAreaLabel(practiceArea)} cases in the background and
                prioritize them when they are ready.
              </Text>
            )}
          </Paper>
          <Paper
            shadow="sm"
            p="md"
            radius="md"
            withBorder
            style={{
              backgroundColor: "#fff3cd",
              borderColor: "#ffc107",
              maxWidth: "760px",
            }}
          >
            <Text size="sm" ta="center" fw={500}>
              ⚠️ Every case here is synthetic/anonymized for training purposes
              only: an invented scenario about a real condition and treatment.
              No real patient data or actual insurance cases are used in this
              interface.
            </Text>
          </Paper>
          <HouseRules />
          <Paper
            shadow="sm"
            p="md"
            radius="md"
            withBorder
            style={{ width: "100%", maxWidth: "760px" }}
          >
            <Checkbox
              size="sm"
              aria-label="Agree to Terms of Service"
              checked={hasAcceptedTos}
              onChange={(event) =>
                setHasAcceptedTos(event.currentTarget.checked)
              }
              label={
                <Text size="sm">
                  I agree to the Terms of Service and understand the Arena
                  exists to improve AI response quality.
                </Text>
              }
            />
            <Text size="xs" mt="xs">
              <Anchor href="/tos" target="_blank" rel="noopener noreferrer">
                Read Terms of Service (opens in a new tab)
              </Anchor>
            </Text>
          </Paper>
          {captchaNeeded && (
            <Paper
              shadow="sm"
              p="md"
              radius="md"
              withBorder
              style={{ width: "100%", maxWidth: "760px" }}
            >
              <Text size="sm" mb="xs">
                One more thing: show us you are a person, not a script.
              </Text>
              <CaptchaBox
                siteKey={captchaSiteKey}
                resetKey={captchaResets}
                onToken={(token) => {
                  setCaptchaToken(token);
                  if (token) {
                    setCaptchaNotice(null);
                  }
                }}
              />
              {captchaNotice && (
                <Text
                  size="sm"
                  c="red"
                  mt="xs"
                  data-testid="arena-captcha-notice"
                >
                  {captchaNotice}
                </Text>
              )}
            </Paper>
          )}
          {!hasAcceptedTos && (
            <Text size="sm" c="orange" ta="center">
              Please accept the Terms of Service before stepping into the Arena.
            </Text>
          )}
          {hasAcceptedTos && !captchaReady && (
            <Text size="sm" c="orange" ta="center">
              Please tick the &ldquo;I&rsquo;m not a robot&rdquo; box before
              stepping into the Arena.
            </Text>
          )}
          <Group gap="lg" mt="md">
            <Button
              size="xl"
              radius="md"
              disabled={!canStart}
              onClick={() => fetchNextTask("appeal")}
              style={primaryButton(canStart)}
            >
              Judge Appeal Letters
            </Button>
            <Button
              size="xl"
              radius="md"
              disabled={!canStart}
              onClick={() => fetchNextTask("chat")}
              style={primaryButton(canStart)}
            >
              Judge Chat Replies
            </Button>
          </Group>
          <Standings rankings={rankings} />
          <Text size="sm" c="dimmed" ta="center">
            Thanks for helping us choose better responses and fight bad
            insurance decisions 💪
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

  // Render the wait for a case the server is writing
  if (state.mode === "pending") {
    return (
      <Container size="md" py="xl">
        <Stack gap="xl" align="center">
          <Loader size="xl" />
          <Title order={3} ta="center">
            Writing a new case
          </Title>
          <Text size="lg" ta="center" c="dimmed" style={{ maxWidth: "560px" }}>
            Every case here has been judged, so a few AI drafts are answering a
            fresh denial right now. This usually takes a few minutes, and this
            page will update by itself.
          </Text>
          <Button
            size="lg"
            radius="md"
            variant="outline"
            onClick={resetToSelection}
          >
            Leave the Arena
          </Button>
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
          <Button
            size="lg"
            radius="md"
            onClick={resetToSelection}
            style={primaryButton(true)}
          >
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
            Round {scoreboard.rounds}{" "}
            {lastRoundAllBad
              ? "found no usable drafts"
              : lastRoundTie
                ? "called a tie"
                : "done"}
            {lastRoundFouls > 0
              ? ` with ${plural(lastRoundFouls, "foul")} called`
              : ""}
            . Thank you for refereeing.
          </Text>
          <Text size="sm" c="dimmed" ta="center">
            In this browser: {plural(scoreboard.rounds, "round")} judged
            &middot; {plural(scoreboard.fouls, "foul")} called
            {reviewCount !== null
              ? ` · ${plural(reviewCount, "review")} across your account`
              : ""}
          </Text>
          <Alert
            color="lime"
            variant="light"
            radius="md"
            data-testid="arena-encouragement"
          >
            {encouragementFor(scoreboard.rounds)}
          </Alert>
          {preference ? (
            <>
              <PreferencePicker
                value={preference}
                onChange={choosePreference}
              />
              <Group gap="lg" mt="md">
                <Button
                  size="lg"
                  radius="md"
                  onClick={doAnother}
                  style={primaryButton(true)}
                >
                  Next round
                </Button>
                <Button
                  size="lg"
                  radius="md"
                  variant="outline"
                  onClick={resetToSelection}
                >
                  Switch arena
                </Button>
              </Group>
            </>
          ) : (
            <>
              <PreferenceQuestion
                onChoose={answerPreference}
                buttonStyle={primaryButton(true)}
              />
              <Button
                size="md"
                radius="md"
                variant="outline"
                onClick={resetToSelection}
              >
                Switch arena
              </Button>
            </>
          )}
          <Standings rankings={rankings} />
          <Text size="sm" c="dimmed" ta="center">
            Thanks for helping us choose better responses and fight bad
            insurance decisions 💪
          </Text>
        </Stack>
      </Container>
    );
  }

  // Render voting state
  if (state.mode === "voting" && state.task) {
    const hasEnoughCandidates = state.task.candidates.length >= 2;
    const isAppeal = state.task.task_type !== "chat";
    const caseEvidence = (state.task.task_context.evidence ?? []).filter(
      (e) => e.pmid,
    );

    // If only one candidate, show a message and skip options
    if (!hasEnoughCandidates) {
      return (
        <Container size="md" py="xl">
          <Stack gap="xl" align="center">
            <Title order={2} ta="center">
              Not enough contenders
            </Title>
            <Alert
              color="yellow"
              title="Only one draft in this case"
              radius="md"
            >
              There is nothing to compare here. We will skip this one and find
              you another case.
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
              <Button
                size="lg"
                radius="md"
                variant="outline"
                onClick={resetToSelection}
              >
                Back
              </Button>
            </Group>
          </Stack>
        </Container>
      );
    }

    const labelOf = (id: string) =>
      LABELS[state.task!.candidates.findIndex((c) => c.id === id)] ?? "?";
    const selectedLabels = state.task.candidates
      .filter((candidate) => state.selectedIds.includes(candidate.id))
      .map((candidate) => labelOf(candidate.id));
    let castLabel = "Select a draft to vote";
    if (state.allBad) {
      castLabel = "Submit: none are usable";
    } else if (selectedLabels.length > 1) {
      castLabel = `Cast tie: Drafts ${selectedLabels.join(" & ")}`;
    } else if (selectedLabels.length === 1) {
      castLabel = `Cast vote for Draft ${selectedLabels[0]}`;
    }

    return (
      <Container size="xl" py="xl">
        <Stack gap="lg">
          <Group justify="center" gap="sm">
            <Badge size="lg" variant="filled" color="lime">
              Round {scoreboard.rounds + 1}
            </Badge>
            {reviewCount !== null && (
              <Badge size="lg" variant="light" color="blue">
                {plural(reviewCount, "review")} on your account
              </Badge>
            )}
            {practiceAreaLabel(state.task.task_context.practice_area) && (
              <Badge size="lg" variant="light" color="teal">
                {practiceAreaLabel(state.task.task_context.practice_area)} case
              </Badge>
            )}
            {preference === "straightforward" || preference === "complex" ? (
              <Badge size="lg" variant="light" color="gray">
                {preference === "complex"
                  ? "Complex cases"
                  : "Straightforward cases"}
              </Badge>
            ) : null}
            <Title order={2} ta="center">
              {isAppeal
                ? "Which appeal letter wins?"
                : "Which chat reply wins?"}
            </Title>
          </Group>

          <Text size="xs" ta="center" c="dimmed">
            📋 Synthetic case for training purposes only. Drafts are shown
            blind, in random order.
          </Text>

          <HouseRules compact isAppeal={isAppeal} />

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

          <Text size="sm" ta="center" fw={500}>
            Select every draft you would be happy to send. One pick wins;
            multiple picks are equally good and split the vote.
          </Text>

          {/* The contenders */}
          <SimpleGrid cols={{ base: 1, md: 2 }} spacing="lg">
            {state.task.candidates.map((candidate, index) => (
              <CandidateCard
                key={candidate.id}
                candidate={candidate}
                label={LABELS[index] || String(index + 1)}
                isSelected={state.selectedIds.includes(candidate.id)}
                showTieLabel={selectedLabels.length > 1}
                isFlagged={state.flaggedIds.includes(candidate.id)}
                evidence={caseEvidence}
                checkFacts={isAppeal}
                onSelect={() => handleSelectCandidate(candidate)}
                onToggleFlag={() => handleToggleFlag(candidate)}
              />
            ))}
          </SimpleGrid>

          <Paper shadow="xs" p="md" radius="md" withBorder>
            <Textarea
              label="Why? (optional)"
              description="One sentence helps us understand your call. Please do not include personal information."
              placeholder={
                state.allBad
                  ? "For example: Every draft invents details that are not in the case."
                  : "For example: Draft B is specific, persuasive, and sticks to the evidence."
              }
              value={state.rationale}
              maxLength={500}
              autosize
              minRows={2}
              maxRows={4}
              onChange={(event) => {
                const rationale = event.currentTarget.value;
                setState((prev) => ({
                  ...prev,
                  rationale,
                }));
              }}
            />
            <Text size="xs" c="dimmed" ta="right" mt={4}>
              {state.rationale.length}/500
            </Text>
            {state.allBad && (
              <Alert color="orange" title="No winner selected" mt="sm">
                This review will record that you would not use any of these
                drafts. That is different from skipping: every draft gets no
                winning credit.
              </Alert>
            )}
          </Paper>

          {/* Cast, reject all, skip, leave */}
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
              variant={state.allBad ? "filled" : "light"}
              color="red"
              aria-pressed={state.allBad}
              onClick={handleToggleAllBad}
            >
              These are all bad — I wouldn&apos;t use any of them
            </Button>
            <Button
              size="lg"
              radius="md"
              variant="light"
              onClick={skipTask}
              loading={isSkipping}
            >
              Skip this round
            </Button>
            <Button
              size="lg"
              radius="md"
              variant="outline"
              onClick={resetToSelection}
            >
              Leave the Arena
            </Button>
          </Group>

          <Text size="sm" c="dimmed" ta="center">
            {state.allBad
              ? "Choose a draft instead if one is usable, or submit the no-winner review. You can still flag fabrication."
              : selectedLabels.length > 1
                ? "The selected drafts are tied for best, and each gets an equal share of your vote. Click one again to remove it."
                : "Click drafts (or press their letters) to toggle your top choices. Flag any that make things up, or skip if you are unsure."}
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
          <ChooserInterface
            captchaSiteKey={captchaSiteKey}
            captchaAlreadyPassed={captchaAlreadyPassed}
            initialReviewCount={initialReviewCount}
          />
        </Box>
      </ErrorBoundary>
    </MantineProvider>
  );
};

// Mount the React app
const container = document.getElementById("chooser-root");
const captchaSiteKey = container?.dataset.recaptchaSiteKey ?? "";
const captchaAlreadyPassed = container?.dataset.captchaPassed === "1";
const initialReviewCount =
  container?.dataset.reviewCount === undefined
    ? null
    : Number(container.dataset.reviewCount);
if (container) {
  const root = createRoot(container);
  root.render(<App />);
}

export default App;
