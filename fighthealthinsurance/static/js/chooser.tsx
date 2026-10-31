import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
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
  // "pending": the server has no case for us yet but is writing one (it
  // answered 202), and we are waiting for it.
  mode: "selection" | "loading" | "pending" | "voting" | "confirmation" | "error";
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

const plural = (n: number, word: string): string => `${n} ${word}${n === 1 ? "" : "s"}`;

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
    return Array.isArray(data.windows) && data.windows.length > 0 ? data : "hidden";
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
      if (!boxRef.current || widgetRef.current !== null || !api || !api.render) {
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
    if (resetKey > 0 && widgetRef.current !== null && window.grecaptcha?.reset) {
      window.grecaptcha.reset(widgetRef.current);
      onTokenRef.current(null);
    }
  }, [resetKey]);

  return <div ref={boxRef} data-testid="arena-captcha" />;
};

// ---------------------------------------------------------------------------
// Referee aids: the reviewer's verdicts and the citation check

// How each verdict reads on the page. Colors are Mantine's names.
const VERDICTS: Record<string, { label: string; color: string; hint: string }> = {
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

const pubmedUrl = (pmid: string): string => `https://pubmed.ncbi.nlm.nih.gov/${pmid}/`;

// One PMID a draft cites, checked against the evidence on file.
interface Citation {
  pmid: string;
  // The study on file it names, if any. Not on file: the draft was told
  // to cite only the evidence listed, and the referee cannot check it here.
  item?: EvidenceItem;
}

const checkCitations = (content: string, evidence: EvidenceItem[]): Citation[] => {
  const citations: Citation[] = [];
  const re = new RegExp(PMID_PATTERN, "gi");
  let match: RegExpExecArray | null;
  while ((match = re.exec(content)) !== null) {
    const pmid = match[1] ?? match[2];
    if (!pmid || citations.some((c) => c.pmid === pmid)) continue;
    citations.push({ pmid, item: evidence.find((e) => String(e.pmid) === pmid) });
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
    return { label: "mixed", color: "yellow", hint: `On file. ${verdict.hint}`, suspicious: false };
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

const HighlightedDraft: React.FC<{ content: string; citations: Citation[] }> = ({
  content,
  citations,
}) => {
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
          The PMIDs each draft cites are checked against the evidence on file for you.
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
          <strong>Check the citations.</strong> Under each draft, the PMIDs it cites are
          matched to the evidence on file, and each study on file carries an automated
          reviewer&rsquo;s verdict on whether it supports the treatment. A PMID that is
          not on file cannot be checked here: open it, and if it is not real, or not
          what the draft claims, that is a foul. A draft that leans on a study that
          argues against the treatment is not helping the patient either.
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
        <Anchor href={item.url} target="_blank" rel="noopener noreferrer" size="sm">
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
              Reviewer: {percent(judgment.on_topic)} about this treatment and condition
              &middot; {percent(judgment.supports)} supports it &middot;{" "}
              {percent(judgment.undermines)} argues against it.
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
  const qaPairs = (context.qa_pairs ?? []).filter((p) => p.question && p.answer);
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
          An everyday case, kept simple on purpose. A letter that stays short and sticks
          to the facts is doing it right; one that pads it out with claims the facts do
          not support is not.
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

const CandidateCard: React.FC<{
  candidate: ChooserCandidate;
  label: string;
  isSelected: boolean;
  tieMode: boolean;
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
  tieMode,
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
      {checkFacts ? (
        <Box mb="sm">
          <CitationCheck citations={citations} hints={hints} hasEvidence={evidence.length > 0} />
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

// ---------------------------------------------------------------------------
// The arena

const ChooserInterface: React.FC<{ captchaSiteKey: string; captchaAlreadyPassed: boolean }> = ({
  captchaSiteKey,
  captchaAlreadyPassed,
}) => {
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
  const fetchNextTaskRef = useRef<((taskType: TaskType) => Promise<void>) | null>(null);

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
      setCaptchaNotice(message || "Please tick the \"I'm not a robot\" box, then try again.");
      setState((prev) => ({ ...prev, mode: "selection", task: null, taskType: null }));
    },
    [captchaSiteKey],
  );

  // The gate's answer, when a response is its 403; undefined otherwise.
  const captchaRefusal = async (response: Response): Promise<string | undefined> => {
    if (response.status !== 403) {
      return undefined;
    }
    try {
      const body = (await response.clone().json()) as { error?: string; message?: string };
      if (body.error === CAPTCHA_ERROR) {
        return body.message || "";
      }
    } catch {
      // Not the gate's JSON.
    }
    return undefined;
  };
  const [scoreboard, setScoreboard] = useState<Scoreboard>({ rounds: 0, fouls: 0 });
  const [lastRoundFouls, setLastRoundFouls] = useState(0);
  const [lastRoundTie, setLastRoundTie] = useState(false);
  const [rankings, setRankings] = useState<RankingsState>("pending");

  useEffect(() => {
    setScoreboard(readScoreboard());
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

  const fetchNextTask = useCallback(async (taskType: TaskType) => {
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
    const deadline = window.setTimeout(() => controller.abort(), NEXT_CASE_REQUEST_TIMEOUT_MS);
    try {
      const headers: Record<string, string> = { "Content-Type": "application/json" };
      if (captchaToken) {
        headers[CAPTCHA_HEADER] = captchaToken;
      }
      const response = await fetch(`/ziggy/rest/chooser/next/${taskType}/`, {
        method: "GET",
        headers,
        credentials: "include",
        signal: controller.signal,
      });
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
        setState((prev) => ({ ...prev, mode: "pending", error: null, taskType }));
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
  }, [captchaToken, backToCaptcha]);

  useEffect(() => {
    fetchNextTaskRef.current = fetchNextTask;
  }, [fetchNextTask]);

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
        const refusal = await captchaRefusal(response);
        if (refusal !== undefined) {
          backToCaptcha(refusal);
          return;
        }
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
  , backToCaptcha]);

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
      selectedCandidate: null,
      flaggedIds: [],
      tieMode: false,
      tiedIds: [],
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
          {captchaNeeded && (
            <Paper shadow="sm" p="md" radius="md" withBorder style={{ width: "100%", maxWidth: "760px" }}>
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
                <Text size="sm" c="red" mt="xs" data-testid="arena-captcha-notice">
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
              Please tick the &ldquo;I&rsquo;m not a robot&rdquo; box before stepping into the Arena.
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
            Every case here has been judged, so a few AI drafts are answering a fresh
            denial right now. This usually takes a few minutes, and this page will update
            by itself.
          </Text>
          <Button size="lg" radius="md" variant="outline" onClick={resetToSelection}>
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
          <Standings rankings={rankings} />
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
    const caseEvidence = (state.task.task_context.evidence ?? []).filter((e) => e.pmid);

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
                evidence={caseEvidence}
                checkFacts={isAppeal}
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
          <ChooserInterface captchaSiteKey={captchaSiteKey} captchaAlreadyPassed={captchaAlreadyPassed} />
        </Box>
      </ErrorBoundary>
    </MantineProvider>
  );
};

// Mount the React app
const container = document.getElementById("chooser-root");
const captchaSiteKey = container?.dataset.recaptchaSiteKey ?? "";
const captchaAlreadyPassed = container?.dataset.captchaPassed === "1";
if (container) {
  const root = createRoot(container);
  root.render(<App />);
}

export default App;
