import React, { useState, useEffect, useCallback } from "react";
import { createRoot } from "react-dom/client";
import {
  Alert,
  Badge,
  Box,
  Button,
  Card,
  Container,
  FileInput,
  Group,
  Loader,
  MantineProvider,
  Modal,
  Select,
  Stack,
  Tabs,
  Text,
  Textarea,
  TextInput,
  Title,
} from "@mantine/core";
import ErrorBoundary from "./ErrorBoundary";
import { THEME } from "./theme";
import { getCSRFToken } from "./shared";

// --- Types ---

interface AppealSummary {
  id: number;
  uuid: string;
  status: string;
  response_text: string | null;
  response_date: string | null;
  pending: boolean;
  professional_name: string | null;
  patient_name: string | null;
  insurance_company: string | null;
  denial_reason: string | null;
  creation_date: string | null;
  mod_date: string | null;
  chat_id: number | null;
}

interface AppealDetail extends AppealSummary {
  appeal_text: string | null;
  appeal_pdf_url: string | null;
}

interface DenialSummary {
  denial_id: number;
  uuid: string;
  insurance_company: string | null;
  procedure: string | null;
  diagnosis: string | null;
  denial_date: string | null;
  date: string | null;
  denial_type_names: string[];
  has_appeal: boolean;
}

interface CallLog {
  id: number;
  appeal: number | null;
  call_date: string;
  representative_name: string;
  reference_number: string;
  call_outcome: string;
  notes: string;
  follow_up_needed: boolean;
  follow_up_date: string | null;
  created_at: string;
  updated_at: string;
}

interface Evidence {
  id: number;
  appeal: number | null;
  title: string;
  description: string;
  filename: string;
  mime_type: string;
  created_at: string;
}

// --- API helpers ---

async function apiFetch<T>(url: string, options?: RequestInit): Promise<T> {
  const response = await fetch(url, {
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      "X-CSRFToken": getCSRFToken(),
    },
    ...options,
  });
  if (response.status === 403 || response.status === 401) {
    window.location.href = "/v0/auth/login";
    throw new Error("Not authenticated");
  }
  if (!response.ok) {
    throw new Error(`API error: ${response.status}`);
  }
  if (response.status === 204) return {} as T;
  return response.json();
}

// --- Status badge helper ---

function StatusBadge({ status }: { status: string }) {
  const colorMap: Record<string, string> = {
    sent: "green",
    "pending patient": "yellow",
    "pending professional": "blue",
    unknown: "gray",
  };
  return (
    <Badge color={colorMap[status] || "gray"} variant="light" size="sm">
      {status}
    </Badge>
  );
}

function formatDate(dateStr: string | null): string {
  if (!dateStr) return "N/A";
  return new Date(dateStr).toLocaleDateString();
}

function buildAppealOptions(appeals: AppealSummary[]) {
  return appeals.map((a) => ({
    value: String(a.id),
    label: `${a.insurance_company || "Appeal"} - ${formatDate(a.creation_date)}`,
  }));
}

// --- Appeals Tab ---

function AppealsTab() {
  const [appeals, setAppeals] = useState<AppealSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [expandedId, setExpandedId] = useState<number | null>(null);
  const [detail, setDetail] = useState<AppealDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  const fetchAppeals = useCallback(async () => {
    try {
      setLoading(true);
      const data = await apiFetch<AppealSummary[]>("/ziggy/rest/appeals/");
      setAppeals(data);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load appeals");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchAppeals();
  }, [fetchAppeals]);

  const handleExpand = useCallback(
    async (id: number) => {
      if (expandedId === id) {
        setExpandedId(null);
        setDetail(null);
        return;
      }
      setExpandedId(id);
      setDetailLoading(true);
      try {
        const data = await apiFetch<AppealDetail>(`/ziggy/rest/appeals/${id}/`);
        setDetail(data);
      } catch {
        setDetail(null);
      } finally {
        setDetailLoading(false);
      }
    },
    [expandedId],
  );

  if (loading) return <Loader size="lg" style={{ display: "block", margin: "2rem auto" }} />;
  if (error) return <Alert color="red" title="Error">{error}</Alert>;
  if (appeals.length === 0) {
    return (
      <Alert color="blue" title="No Appeals Yet">
        <Text size="sm">
          You don't have any appeals yet.{" "}
          <a href="/scan" style={{ color: THEME.colors.primary }}>
            Start a new appeal
          </a>{" "}
          or{" "}
          <a href="/chat" style={{ color: THEME.colors.primary }}>
            chat with us
          </a>{" "}
          for help.
        </Text>
      </Alert>
    );
  }

  return (
    <Stack gap="md">
      {appeals.map((appeal) => (
        <Card
          key={appeal.id}
          shadow="sm"
          padding="lg"
          radius={THEME.borderRadius.card}
          withBorder
          style={{ cursor: "pointer" }}
          onClick={() => handleExpand(appeal.id)}
        >
          <Group justify="space-between" mb="xs">
            <Group gap="sm">
              <Text fw={600} size="md">
                {appeal.insurance_company || "Unknown Insurer"}
              </Text>
              <StatusBadge status={appeal.status} />
            </Group>
            <Text size="sm" c="dimmed">
              {formatDate(appeal.creation_date)}
            </Text>
          </Group>
          {appeal.denial_reason && (
            <Text size="sm" c="dimmed" mb="xs">
              Denial reason: {appeal.denial_reason}
            </Text>
          )}
          {appeal.professional_name && (
            <Text size="sm" c="dimmed">
              Provider: {appeal.professional_name}
            </Text>
          )}
          {expandedId === appeal.id && (
            <Box mt="md" pt="md" style={{ borderTop: "1px solid #eee" }}>
              {detailLoading ? (
                <Loader size="sm" />
              ) : detail ? (
                <Stack gap="sm">
                  {detail.appeal_text && (
                    <Box>
                      <Text fw={500} size="sm" mb="xs">
                        Appeal Letter:
                      </Text>
                      <Box
                        p="sm"
                        style={{
                          background: "#f8f9fa",
                          borderRadius: 8,
                          maxHeight: 300,
                          overflow: "auto",
                          whiteSpace: "pre-wrap",
                          fontSize: "0.875rem",
                        }}
                      >
                        {detail.appeal_text}
                      </Box>
                    </Box>
                  )}
                  {detail.appeal_pdf_url && (
                    <Button
                      component="a"
                      href={detail.appeal_pdf_url}
                      target="_blank"
                      size="sm"
                      style={THEME.buttonSharedStyles}
                      onClick={(e: React.MouseEvent) => e.stopPropagation()}
                    >
                      Download PDF
                    </Button>
                  )}
                  {detail.response_text && (
                    <Box>
                      <Text fw={500} size="sm" mb="xs">
                        Insurance Response ({formatDate(detail.response_date)}):
                      </Text>
                      <Text size="sm">{detail.response_text}</Text>
                    </Box>
                  )}
                </Stack>
              ) : (
                <Text size="sm" c="dimmed">
                  Could not load details.
                </Text>
              )}
            </Box>
          )}
        </Card>
      ))}
    </Stack>
  );
}

// --- Denials Tab ---

function DenialsTab() {
  const [denials, setDenials] = useState<DenialSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    (async () => {
      try {
        setLoading(true);
        const data = await apiFetch<DenialSummary[]>("/ziggy/rest/denials/");
        setDenials(data);
      } catch (e) {
        setError(e instanceof Error ? e.message : "Failed to load denials");
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  if (loading) return <Loader size="lg" style={{ display: "block", margin: "2rem auto" }} />;
  if (error) return <Alert color="red" title="Error">{error}</Alert>;
  if (denials.length === 0) {
    return (
      <Alert color="blue" title="No Denials">
        <Text size="sm">No denial records found.</Text>
      </Alert>
    );
  }

  return (
    <Stack gap="md">
      {denials.map((denial) => (
        <Card
          key={denial.denial_id}
          shadow="sm"
          padding="lg"
          radius={THEME.borderRadius.card}
          withBorder
        >
          <Group justify="space-between" mb="xs">
            <Text fw={600} size="md">
              {denial.insurance_company || "Unknown Insurer"}
            </Text>
            <Group gap="xs">
              {denial.has_appeal ? (
                <Badge color="green" variant="light" size="sm">
                  Appeal Filed
                </Badge>
              ) : (
                <Badge color="orange" variant="light" size="sm">
                  No Appeal
                </Badge>
              )}
              <Text size="sm" c="dimmed">
                {formatDate(denial.denial_date || denial.date)}
              </Text>
            </Group>
          </Group>
          {denial.procedure && (
            <Text size="sm" mb="xs">
              Procedure: {denial.procedure}
            </Text>
          )}
          {denial.diagnosis && (
            <Text size="sm" mb="xs">
              Diagnosis: {denial.diagnosis}
            </Text>
          )}
          {denial.denial_type_names.length > 0 && (
            <Group gap="xs">
              {denial.denial_type_names.map((name) => (
                <Badge key={name} variant="outline" size="xs">
                  {name}
                </Badge>
              ))}
            </Group>
          )}
        </Card>
      ))}
    </Stack>
  );
}

// --- Call Logs Tab ---

interface CallLogFormData {
  call_date: string;
  representative_name: string;
  reference_number: string;
  call_outcome: string;
  notes: string;
  follow_up_needed: boolean;
  follow_up_date: string;
  appeal: string;
}

const emptyCallLogForm: CallLogFormData = {
  call_date: new Date().toISOString().slice(0, 16),
  representative_name: "",
  reference_number: "",
  call_outcome: "",
  notes: "",
  follow_up_needed: false,
  follow_up_date: "",
  appeal: "",
};

function CallLogsTab({ appeals }: { appeals: AppealSummary[] }) {
  const [logs, setLogs] = useState<CallLog[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [formData, setFormData] = useState<CallLogFormData>(emptyCallLogForm);
  const [submitting, setSubmitting] = useState(false);

  const fetchLogs = useCallback(async () => {
    try {
      setLoading(true);
      const data = await apiFetch<CallLog[]>("/ziggy/rest/call_logs/");
      setLogs(data);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load call logs");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchLogs();
  }, [fetchLogs]);

  const handleSubmit = useCallback(async () => {
    setSubmitting(true);
    try {
      const payload: Record<string, unknown> = {
        call_date: formData.call_date,
        representative_name: formData.representative_name,
        reference_number: formData.reference_number,
        call_outcome: formData.call_outcome,
        notes: formData.notes,
        follow_up_needed: formData.follow_up_needed,
        follow_up_date: formData.follow_up_date || null,
      };
      if (formData.appeal) {
        payload.appeal = parseInt(formData.appeal, 10);
      }
      await apiFetch<CallLog>("/ziggy/rest/call_logs/", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      setFormData(emptyCallLogForm);
      setShowForm(false);
      fetchLogs();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to save call log");
    } finally {
      setSubmitting(false);
    }
  }, [formData, fetchLogs]);

  const handleDelete = useCallback(
    async (id: number) => {
      if (!confirm("Delete this call log?")) return;
      try {
        await apiFetch(`/ziggy/rest/call_logs/${id}/`, { method: "DELETE" });
        fetchLogs();
      } catch (e) {
        setError(e instanceof Error ? e.message : "Failed to delete");
      }
    },
    [fetchLogs],
  );

  const appealOptions = buildAppealOptions(appeals);

  if (loading) return <Loader size="lg" style={{ display: "block", margin: "2rem auto" }} />;

  return (
    <Stack gap="md">
      {error && (
        <Alert color="red" title="Error" withCloseButton onClose={() => setError(null)}>
          {error}
        </Alert>
      )}
      <Group justify="space-between">
        <Text fw={500}>Your Insurance Call Logs</Text>
        <Button size="sm" style={THEME.buttonSharedStyles} onClick={() => setShowForm(true)}>
          Log a Call
        </Button>
      </Group>

      <Modal
        opened={showForm}
        onClose={() => setShowForm(false)}
        title="Log an Insurance Call"
        size="lg"
      >
        <Stack gap="sm">
          <TextInput
            label="Call Date & Time"
            type="datetime-local"
            value={formData.call_date}
            onChange={(e) => setFormData({ ...formData, call_date: e.target.value })}
            required
          />
          <TextInput
            label="Representative Name"
            placeholder="Who did you speak with?"
            value={formData.representative_name}
            onChange={(e) =>
              setFormData({ ...formData, representative_name: e.target.value })
            }
          />
          <TextInput
            label="Reference Number"
            placeholder="Call reference or confirmation number"
            value={formData.reference_number}
            onChange={(e) =>
              setFormData({ ...formData, reference_number: e.target.value })
            }
          />
          <Textarea
            label="Call Outcome"
            placeholder="What was the result of the call?"
            value={formData.call_outcome}
            onChange={(e) => setFormData({ ...formData, call_outcome: e.target.value })}
            minRows={2}
          />
          <Textarea
            label="Notes"
            placeholder="Additional notes..."
            value={formData.notes}
            onChange={(e) => setFormData({ ...formData, notes: e.target.value })}
            minRows={2}
          />
          {appealOptions.length > 0 && (
            <Select
              label="Related Appeal (optional)"
              placeholder="Link to an appeal"
              data={appealOptions}
              value={formData.appeal}
              onChange={(val) => setFormData({ ...formData, appeal: val || "" })}
              clearable
            />
          )}
          <Group justify="flex-end" mt="md">
            <Button variant="light" onClick={() => setShowForm(false)}>
              Cancel
            </Button>
            <Button
              style={THEME.buttonSharedStyles}
              onClick={handleSubmit}
              loading={submitting}
            >
              Save Call Log
            </Button>
          </Group>
        </Stack>
      </Modal>

      {logs.length === 0 ? (
        <Alert color="blue" title="No Call Logs">
          <Text size="sm">
            Keep track of your calls with insurance companies. Click "Log a Call" to get started.
          </Text>
        </Alert>
      ) : (
        logs.map((log) => (
          <Card key={log.id} shadow="sm" padding="lg" radius={THEME.borderRadius.card} withBorder>
            <Group justify="space-between" mb="xs">
              <Group gap="sm">
                <Text fw={600} size="md">
                  Call on {new Date(log.call_date).toLocaleString()}
                </Text>
                {log.follow_up_needed && (
                  <Badge color="orange" variant="light" size="sm">
                    Follow-up needed
                  </Badge>
                )}
              </Group>
              <Button
                size="xs"
                variant="subtle"
                color="red"
                onClick={() => handleDelete(log.id)}
              >
                Delete
              </Button>
            </Group>
            {log.representative_name && (
              <Text size="sm">Rep: {log.representative_name}</Text>
            )}
            {log.reference_number && (
              <Text size="sm" c="dimmed">
                Ref #: {log.reference_number}
              </Text>
            )}
            {log.call_outcome && (
              <Text size="sm" mt="xs">
                Outcome: {log.call_outcome}
              </Text>
            )}
            {log.notes && (
              <Text size="sm" c="dimmed" mt="xs">
                {log.notes}
              </Text>
            )}
            {log.follow_up_date && (
              <Text size="sm" c="dimmed" mt="xs">
                Follow up by: {formatDate(log.follow_up_date)}
              </Text>
            )}
          </Card>
        ))
      )}
    </Stack>
  );
}

// --- Evidence Tab ---

function EvidenceTab({ appeals }: { appeals: AppealSummary[] }) {
  const [evidence, setEvidence] = useState<Evidence[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [appealId, setAppealId] = useState<string>("");
  const [submitting, setSubmitting] = useState(false);

  const fetchEvidence = useCallback(async () => {
    try {
      setLoading(true);
      const data = await apiFetch<Evidence[]>("/ziggy/rest/patient_evidence/");
      setEvidence(data);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load evidence");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchEvidence();
  }, [fetchEvidence]);

  const handleSubmit = useCallback(async () => {
    if (!file) {
      setError("Please select a file");
      return;
    }
    setSubmitting(true);
    try {
      const formData = new FormData();
      formData.append("file", file);
      formData.append("title", title || file.name);
      formData.append("description", description);
      if (appealId) formData.append("appeal", appealId);

      const response = await fetch("/ziggy/rest/patient_evidence/", {
        method: "POST",
        credentials: "include",
        headers: {
          "X-CSRFToken": getCSRFToken(),
        },
        body: formData,
      });
      if (!response.ok) {
        const errData = await response.json();
        throw new Error(errData.error || `Upload failed: ${response.status}`);
      }
      setTitle("");
      setDescription("");
      setFile(null);
      setAppealId("");
      setShowForm(false);
      fetchEvidence();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to upload");
    } finally {
      setSubmitting(false);
    }
  }, [file, title, description, appealId, fetchEvidence]);

  const handleDelete = useCallback(
    async (id: number) => {
      if (!confirm("Delete this evidence document?")) return;
      try {
        await apiFetch(`/ziggy/rest/patient_evidence/${id}/`, { method: "DELETE" });
        fetchEvidence();
      } catch (e) {
        setError(e instanceof Error ? e.message : "Failed to delete");
      }
    },
    [fetchEvidence],
  );

  const appealOptions = buildAppealOptions(appeals);

  if (loading) return <Loader size="lg" style={{ display: "block", margin: "2rem auto" }} />;

  return (
    <Stack gap="md">
      {error && (
        <Alert color="red" title="Error" withCloseButton onClose={() => setError(null)}>
          {error}
        </Alert>
      )}
      <Group justify="space-between">
        <Text fw={500}>Your Evidence Documents</Text>
        <Button size="sm" style={THEME.buttonSharedStyles} onClick={() => setShowForm(true)}>
          Upload Evidence
        </Button>
      </Group>

      <Modal
        opened={showForm}
        onClose={() => setShowForm(false)}
        title="Upload Evidence Document"
        size="lg"
      >
        <Stack gap="sm">
          <TextInput
            label="Title"
            placeholder="Document title"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
          />
          <Textarea
            label="Description"
            placeholder="What is this document?"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            minRows={2}
          />
          <FileInput
            label="File"
            placeholder="Select a file (max 10MB)"
            value={file}
            onChange={setFile}
            accept=".pdf,.jpg,.jpeg,.png,.gif,.webp,.txt,.doc,.docx"
            required
          />
          {appealOptions.length > 0 && (
            <Select
              label="Related Appeal (optional)"
              placeholder="Link to an appeal"
              data={appealOptions}
              value={appealId}
              onChange={(val) => setAppealId(val || "")}
              clearable
            />
          )}
          <Group justify="flex-end" mt="md">
            <Button variant="light" onClick={() => setShowForm(false)}>
              Cancel
            </Button>
            <Button
              style={THEME.buttonSharedStyles}
              onClick={handleSubmit}
              loading={submitting}
            >
              Upload
            </Button>
          </Group>
        </Stack>
      </Modal>

      {evidence.length === 0 ? (
        <Alert color="blue" title="No Evidence">
          <Text size="sm">
            Upload supporting documents for your appeals. Click "Upload Evidence" to get started.
          </Text>
        </Alert>
      ) : (
        evidence.map((ev) => (
          <Card key={ev.id} shadow="sm" padding="lg" radius={THEME.borderRadius.card} withBorder>
            <Group justify="space-between" mb="xs">
              <Text fw={600} size="md">
                {ev.title}
              </Text>
              <Group gap="xs">
                <Badge variant="outline" size="xs">
                  {ev.mime_type}
                </Badge>
                <Button
                  size="xs"
                  variant="subtle"
                  color="red"
                  onClick={() => handleDelete(ev.id)}
                >
                  Delete
                </Button>
              </Group>
            </Group>
            {ev.description && (
              <Text size="sm" c="dimmed" mb="xs">
                {ev.description}
              </Text>
            )}
            <Text size="xs" c="dimmed">
              {ev.filename} - Uploaded {formatDate(ev.created_at)}
            </Text>
          </Card>
        ))
      )}
    </Stack>
  );
}

// --- Main Dashboard ---

function PatientDashboard() {
  const [appeals, setAppeals] = useState<AppealSummary[]>([]);

  useEffect(() => {
    (async () => {
      try {
        const data = await apiFetch<AppealSummary[]>("/ziggy/rest/appeals/");
        setAppeals(data);
      } catch {
        // Appeals will show their own error
      }
    })();
  }, []);

  return (
    <Container size="md" py="xl">
      <Title order={2} mb="lg" style={{ color: "#333" }}>
        My Appeals Dashboard
      </Title>
      <Tabs defaultValue="appeals">
        <Tabs.List mb="md">
          <Tabs.Tab value="appeals">Appeals</Tabs.Tab>
          <Tabs.Tab value="denials">Denials</Tabs.Tab>
          <Tabs.Tab value="calls">Call Logs</Tabs.Tab>
          <Tabs.Tab value="evidence">Evidence</Tabs.Tab>
        </Tabs.List>

        <Tabs.Panel value="appeals">
          <AppealsTab />
        </Tabs.Panel>
        <Tabs.Panel value="denials">
          <DenialsTab />
        </Tabs.Panel>
        <Tabs.Panel value="calls">
          <CallLogsTab appeals={appeals} />
        </Tabs.Panel>
        <Tabs.Panel value="evidence">
          <EvidenceTab appeals={appeals} />
        </Tabs.Panel>
      </Tabs>
    </Container>
  );
}

// --- App wrapper ---

const App: React.FC = () => {
  return (
    <MantineProvider>
      <ErrorBoundary componentName="PatientDashboard">
        <Box
          style={{
            background: THEME.colors.background,
            minHeight: "60vh",
            padding: "20px 0",
          }}
        >
          <PatientDashboard />
        </Box>
      </ErrorBoundary>
    </MantineProvider>
  );
};

// Mount the React app
const container = document.getElementById("patient-dashboard-root");
if (container) {
  const root = createRoot(container);
  root.render(<App />);
}
