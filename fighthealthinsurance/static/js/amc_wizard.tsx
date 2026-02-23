import React, { useState, useRef, useCallback } from "react";
import { createRoot } from "react-dom/client";
import {
  Anchor,
  Alert,
  Box,
  Button,
  Checkbox,
  Container,
  CopyButton,
  Divider,
  Group,
  Loader,
  MantineProvider,
  NativeSelect,
  Paper,
  SimpleGrid,
  Stack,
  Stepper,
  Text,
  Textarea,
  TextInput,
  Title,
  createTheme,
  List,
} from "@mantine/core";
import { getCSRFToken } from "./shared";

// =============================================================================
// Theme
// =============================================================================

const amcTheme = createTheme({
  fontFamily: "'Manrope', sans-serif",
  primaryColor: "blue",
  defaultRadius: 8,
  colors: {
    blue: [
      "#e3f2fd",
      "#bbdefb",
      "#90caf9",
      "#64b5f6",
      "#42a5f5",
      "#1976d2",
      "#1565c0",
      "#0d47a1",
      "#0d47a1",
      "#0d47a1",
    ],
  },
});

// =============================================================================
// Types
// =============================================================================

interface FormData {
  patientName: string;
  email: string;
  insuranceCompany: string;
  claimNumber: string;
  groupPolicyId: string;
  denialDate: string;
  denialReason: string;
  serviceDescription: string;
  justification: string;
  pii: boolean;
  tos: boolean;
  privacy: boolean;
  subscribe: boolean;
}

interface RootConfig {
  csrfToken: string;
  recaptchaSiteKey: string;
  wsHost: string;
  privacyUrl: string;
  tosUrl: string;
}

const DENIAL_REASONS = [
  "Not medically necessary",
  "Out of network",
  "Prior authorization required but not obtained",
  "Experimental or investigational",
  "Service not covered",
  "Other",
];

const initialFormData: FormData = {
  patientName: "",
  email: "",
  insuranceCompany: "",
  claimNumber: "",
  groupPolicyId: "",
  denialDate: "",
  denialReason: "",
  serviceDescription: "",
  justification: "",
  pii: false,
  tos: false,
  privacy: false,
  subscribe: false,
};

// =============================================================================
// Inline SVG Icons
// =============================================================================

function ShieldIcon({ size = 24, color = "white" }: { size?: number; color?: string }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill={color}>
      <path d="M12 1 3 5v6c0 5.55 3.84 10.74 9 12 5.16-1.26 9-6.45 9-12V5z" />
    </svg>
  );
}

function GavelIcon({ size = 28, color = "#1976d2" }: { size?: number; color?: string }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill={color}>
      <path d="M1 21h12v2H1zM5.24 8.07l2.83-2.83 14.14 14.14-2.83 2.83zM12.32 1l5.66 5.66-2.83 2.83-5.66-5.66zM3.83 9.48l5.66 5.66-2.83 2.83L1 12.31z" />
    </svg>
  );
}

function TrendingUpIcon({ size = 28, color = "#1976d2" }: { size?: number; color?: string }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill={color}>
      <path d="m16 6 2.29 2.29-4.88 4.88-4-4L2 16.59 3.41 18l6-6 4 4 6.3-6.29L22 12V6z" />
    </svg>
  );
}

function InfoIcon({ size = 28, color = "#1976d2" }: { size?: number; color?: string }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill={color}>
      <path d="M11 7h2v2h-2zm0 4h2v6h-2zm1-9C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2m0 18c-4.41 0-8-3.59-8-8s3.59-8 8-8 8 3.59 8 8-3.59 8-8 8" />
    </svg>
  );
}

function CheckCircleIcon({ size = 20, color = "#2e7d32" }: { size?: number; color?: string }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill={color}>
      <path d="M16.59 7.58 10 14.17l-3.59-3.58L5 12l5 5 8-8zM12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2m0 18c-4.42 0-8-3.58-8-8s3.58-8 8-8 8 3.58 8 8-3.58 8-8 8" />
    </svg>
  );
}

function ArrowForwardIcon() {
  return (
    <svg width={18} height={18} viewBox="0 0 24 24" fill="currentColor">
      <path d="m12 4-1.41 1.41L16.17 11H4v2h12.17l-5.58 5.59L12 20l8-8z" />
    </svg>
  );
}

function ArrowBackIcon() {
  return (
    <svg width={18} height={18} viewBox="0 0 24 24" fill="currentColor">
      <path d="M20 11H7.83l5.59-5.59L12 4l-8 8 8 8 1.41-1.41L7.83 13H20z" />
    </svg>
  );
}

// =============================================================================
// Helper
// =============================================================================

function buildDenialText(data: FormData): string {
  const parts = [
    `Insurance Company: ${data.insuranceCompany}`,
    data.claimNumber ? `Claim Number: ${data.claimNumber}` : null,
    data.groupPolicyId ? `Group/Policy ID: ${data.groupPolicyId}` : null,
    data.denialDate ? `Denial Date: ${data.denialDate}` : null,
    `Reason for Denial: ${data.denialReason}`,
    `Service/Treatment Denied: ${data.serviceDescription}`,
    "",
    `The insurance company has denied coverage for ${data.serviceDescription} stating the reason as: "${data.denialReason}".`,
    data.justification
      ? `\nAdditional context from the patient: ${data.justification}`
      : null,
  ];
  return parts.filter(Boolean).join("\n");
}

// =============================================================================
// AMC Header
// =============================================================================

function AMCHeader() {
  return (
    <Box
      component="header"
      style={{
        backgroundColor: "#1976d2",
        color: "white",
      }}
    >
      <Container size="lg" py="sm">
        <Group gap="sm" align="center">
          <ShieldIcon size={28} />
          <Text fw={600} size="lg" style={{ color: "white", lineHeight: 1 }}>
            Healthcare Appeal Helper
          </Text>
        </Group>
      </Container>
    </Box>
  );
}

// =============================================================================
// AMC Hero Section
// =============================================================================

function AMCHero() {
  return (
    <Box ta="center" py={48} px="md">
      <Container size="md">
        <Title
          order={1}
          style={{
            color: "#1976d2",
            fontSize: "2.4rem",
            fontWeight: 700,
            marginBottom: 8,
            lineHeight: 1.2,
          }}
        >
          Denied by your insurance?
        </Title>
        <Text
          size="xl"
          fw={500}
          mb="xs"
          style={{ fontSize: "1.3rem", lineHeight: 1.4 }}
        >
          You&apos;re not alone&mdash;and you can fight back.
        </Text>
        <Text size="lg" c="dimmed">
          Over 80% of prior authorization appeals are successful. Let&apos;s
          help you be one of them.
        </Text>
      </Container>
    </Box>
  );
}

// =============================================================================
// AMC Info Cards
// =============================================================================

const cardStyle = {
  border: "1px solid #e0e0e0",
  boxShadow: "0 2px 8px rgba(0,0,0,0.08)",
  borderRadius: 8,
  height: "100%",
};

function AMCInfoCards() {
  return (
    <Container size="lg" mb="xl" px="md">
      <SimpleGrid cols={{ base: 1, sm: 3 }} spacing="md">
        {/* Know Your Rights */}
        <Paper p="lg" style={cardStyle}>
          <Group gap="xs" mb="sm">
            <GavelIcon />
            <Title order={5} fw={600}>
              Know Your Rights
            </Title>
          </Group>
          <Stack gap={6}>
            {[
              "Every patient has the right to appeal a denied claim",
              "You can request an external review by an independent party",
              "Insurance companies must provide a written explanation for denials",
            ].map((text) => (
              <Group key={text} gap="xs" align="flex-start" wrap="nowrap">
                <Box mt={2}>
                  <CheckCircleIcon />
                </Box>
                <Text size="sm">{text}</Text>
              </Group>
            ))}
          </Stack>
        </Paper>

        {/* Appeal Success Rates */}
        <Paper p="lg" style={cardStyle}>
          <Group gap="xs" mb="sm">
            <TrendingUpIcon />
            <Title order={5} fw={600}>
              Appeal Success Rates
            </Title>
          </Group>
          <Stack gap="sm">
            <Box>
              <Text fw={700} size="xl" style={{ color: "#1976d2" }}>
                83%
              </Text>
              <Text size="sm">
                of prior authorization appeals are fully or partially successful
                according to Kaiser Family Foundation.
              </Text>
            </Box>
            <Box>
              <Text fw={700} size="xl" style={{ color: "#1976d2" }}>
                50-60%
              </Text>
              <Text size="sm">
                of claim denials can be overturned on the first appeal attempt.
              </Text>
            </Box>
            <Text size="xs" c="dimmed">
              Source: Kaiser Family Foundation
            </Text>
          </Stack>
        </Paper>

        {/* Need More Help? */}
        <Paper p="lg" style={cardStyle}>
          <Group gap="xs" mb="sm">
            <InfoIcon />
            <Title order={5} fw={600}>
              Need More Help?
            </Title>
          </Group>
          <Text size="sm" mb="sm">
            These resources offer additional guidance for navigating insurance
            appeals:
          </Text>
          <Stack gap="xs">
            <Anchor
              href="https://www.cms.gov/CCIIO/Resources/Files/external_appeals"
              target="_blank"
              size="sm"
            >
              External Appeals Information
            </Anchor>
            <Anchor
              href="https://www.healthcare.gov/appeal-insurance-company-decision/appeals/"
              target="_blank"
              size="sm"
            >
              Healthcare.gov Appeals
            </Anchor>
            <Anchor
              href="https://www.patientadvocate.org/"
              target="_blank"
              size="sm"
            >
              Patient Advocate Foundation
            </Anchor>
          </Stack>
        </Paper>
      </SimpleGrid>
    </Container>
  );
}

// =============================================================================
// AMC Footer
// =============================================================================

function AMCFooter() {
  return (
    <Box
      component="footer"
      style={{
        backgroundColor: "#1976d2",
        color: "white",
        marginTop: "auto",
      }}
      py="lg"
    >
      <Container size="lg">
        <SimpleGrid cols={{ base: 1, sm: 2 }} spacing="lg" mb="md">
          <Box>
            <Group gap="xs" mb="xs">
              <ShieldIcon size={20} />
              <Text fw={600} style={{ color: "white" }}>
                Healthcare Appeal Helper
              </Text>
            </Group>
            <Text size="sm" style={{ color: "rgba(255,255,255,0.8)" }}>
              Helping patients successfully appeal denied insurance claims.
            </Text>
          </Box>
          <Box>
            <Text fw={600} mb="xs" style={{ color: "white" }}>
              Resources
            </Text>
            <Stack gap={4}>
              <Anchor
                href="https://www.cms.gov/CCIIO/Resources/Files/external_appeals"
                target="_blank"
                size="sm"
                style={{ color: "rgba(255,255,255,0.8)" }}
              >
                External Appeals Information
              </Anchor>
              <Anchor
                href="https://www.healthcare.gov/appeal-insurance-company-decision/appeals/"
                target="_blank"
                size="sm"
                style={{ color: "rgba(255,255,255,0.8)" }}
              >
                Healthcare.gov Appeals
              </Anchor>
              <Anchor
                href="https://www.patientadvocate.org/"
                target="_blank"
                size="sm"
                style={{ color: "rgba(255,255,255,0.8)" }}
              >
                Patient Advocate Foundation
              </Anchor>
            </Stack>
          </Box>
        </SimpleGrid>

        <Divider style={{ borderColor: "rgba(255,255,255,0.2)" }} mb="md" />

        <Group justify="space-between" wrap="wrap">
          <Text size="sm" style={{ color: "rgba(255,255,255,0.8)" }}>
            AI-enhanced letter generation
          </Text>
          <Text size="sm" style={{ color: "rgba(255,255,255,0.8)" }}>
            Powered by{" "}
            <Anchor
              href="https://www.fighthealthinsurance.com"
              target="_blank"
              style={{ color: "white" }}
              size="sm"
            >
              Fight Health Insurance
            </Anchor>
          </Text>
        </Group>
      </Container>
    </Box>
  );
}

// =============================================================================
// Step 1: Patient Information
// =============================================================================

function Step1PatientInfo({
  formData,
  setFormData,
  errors,
}: {
  formData: FormData;
  setFormData: (data: FormData) => void;
  errors: Record<string, string>;
}) {
  const update = (field: keyof FormData, value: string | boolean) => {
    setFormData({ ...formData, [field]: value });
  };

  return (
    <Stack gap="lg">
      <Paper
        p="lg"
        style={{
          border: "1px solid #e0e0e0",
          boxShadow: "0 2px 8px rgba(0,0,0,0.08)",
          borderRadius: 8,
        }}
      >
        <Text fw={600} mb="sm">
          Patient Information
        </Text>
        <TextInput
          label="Patient's Full Name"
          required
          value={formData.patientName}
          onChange={(e) => update("patientName", e.target.value)}
          error={errors.patientName}
          mb="sm"
        />
        <TextInput
          label="Email"
          type="email"
          required
          value={formData.email}
          onChange={(e) => update("email", e.target.value)}
          error={errors.email}
          description="Required for sending your appeal and data deletion requests"
          mb="sm"
        />
      </Paper>

      <Paper
        p="lg"
        style={{
          border: "1px solid #e0e0e0",
          boxShadow: "0 2px 8px rgba(0,0,0,0.08)",
          borderRadius: 8,
        }}
      >
        <Text fw={600} mb="sm">
          Insurance Information
        </Text>
        <TextInput
          label="Insurance Company Name"
          required
          value={formData.insuranceCompany}
          onChange={(e) => update("insuranceCompany", e.target.value)}
          error={errors.insuranceCompany}
          mb="sm"
        />
        <Group grow mb="sm">
          <TextInput
            label="Claim Number"
            value={formData.claimNumber}
            onChange={(e) => update("claimNumber", e.target.value)}
          />
          <TextInput
            label="Group/Policy ID"
            value={formData.groupPolicyId}
            onChange={(e) => update("groupPolicyId", e.target.value)}
            description="From your insurance card"
          />
        </Group>
        <TextInput
          label="Date of Denial"
          type="date"
          required
          value={formData.denialDate}
          onChange={(e) => update("denialDate", e.target.value)}
          error={errors.denialDate}
        />
      </Paper>
    </Stack>
  );
}

// =============================================================================
// Step 2: Denial Details
// =============================================================================

function Step2DenialDetails({
  formData,
  setFormData,
  errors,
  config,
}: {
  formData: FormData;
  setFormData: (data: FormData) => void;
  errors: Record<string, string>;
  config: RootConfig;
}) {
  const update = (field: keyof FormData, value: string | boolean) => {
    setFormData({ ...formData, [field]: value });
  };

  return (
    <Stack gap="lg">
      <Paper
        p="lg"
        style={{
          border: "1px solid #e0e0e0",
          boxShadow: "0 2px 8px rgba(0,0,0,0.08)",
          borderRadius: 8,
        }}
      >
        <Text fw={600} mb="sm">
          Denial Details
        </Text>
        <NativeSelect
          label="Reason for Denial"
          required
          value={formData.denialReason}
          onChange={(e) => update("denialReason", e.target.value)}
          error={errors.denialReason}
          data={[
            { label: "-- Select a reason --", value: "" },
            ...DENIAL_REASONS.map((r) => ({ label: r, value: r })),
          ]}
          mb="sm"
        />
        <Textarea
          label="Description of Denied Service"
          required
          placeholder="Describe the service, treatment, or procedure that was denied..."
          minRows={4}
          value={formData.serviceDescription}
          onChange={(e) => update("serviceDescription", e.target.value)}
          error={errors.serviceDescription}
          mb="sm"
        />
        <Textarea
          label="Additional Medical Justification (Optional)"
          placeholder="Add any medical reasons why this service is necessary, or why the denial reason doesn't apply."
          minRows={3}
          value={formData.justification}
          onChange={(e) => update("justification", e.target.value)}
        />
      </Paper>

      <Paper
        p="lg"
        style={{
          border: "1px solid #e0e0e0",
          boxShadow: "0 2px 8px rgba(0,0,0,0.08)",
          borderRadius: 8,
        }}
      >
        <Text fw={600} mb="sm">
          Required Agreements
        </Text>
        <Stack gap="xs">
          <Checkbox
            label="I am aware that this is for personal use only."
            checked={formData.pii}
            onChange={(e) => update("pii", e.currentTarget.checked)}
            error={errors.pii}
          />
          <Checkbox
            label={
              <>
                I have read and understand the{" "}
                <Anchor href={config.privacyUrl} target="_blank">
                  privacy policy
                </Anchor>
                .
              </>
            }
            checked={formData.privacy}
            onChange={(e) => update("privacy", e.currentTarget.checked)}
            error={errors.privacy}
          />
          <Checkbox
            label={
              <>
                I agree to the{" "}
                <Anchor href={config.tosUrl} target="_blank">
                  terms of service
                </Anchor>
                .
              </>
            }
            checked={formData.tos}
            onChange={(e) => update("tos", e.currentTarget.checked)}
            error={errors.tos}
          />
          <Checkbox
            label="Subscribe to updates from Fight Health Insurance."
            checked={formData.subscribe}
            onChange={(e) => update("subscribe", e.currentTarget.checked)}
          />
        </Stack>
      </Paper>
    </Stack>
  );
}

// =============================================================================
// Step 3: Appeal Letter
// =============================================================================

function Step3AppealLetter({
  appealText,
  setAppealText,
  isGenerating,
  statusMessage,
  error,
  onStartOver,
}: {
  appealText: string;
  setAppealText: (text: string) => void;
  isGenerating: boolean;
  statusMessage: string;
  error: string | null;
  onStartOver: () => void;
}) {
  const downloadTxt = useCallback(() => {
    const blob = new Blob([appealText], { type: "text/plain" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "appeal-letter.txt";
    a.click();
    URL.revokeObjectURL(url);
  }, [appealText]);

  if (isGenerating) {
    return (
      <Stack align="center" gap="md" py="xl">
        <Loader size="lg" color="blue" />
        <Title order={3}>Generating Your Appeal Letter...</Title>
        <Text c="dimmed">{statusMessage || "Connecting to AI backend..."}</Text>
      </Stack>
    );
  }

  if (error) {
    return (
      <Stack gap="md">
        <Alert color="red" title="Error">
          {error}
        </Alert>
        <Button
          onClick={onStartOver}
          variant="outline"
          leftSection={<ArrowBackIcon />}
        >
          Go Back and Try Again
        </Button>
      </Stack>
    );
  }

  return (
    <Stack gap="lg">
      <Box>
        <Text size="lg" fw={600} mb={4}>
          Your Appeal Letter is Ready
        </Text>
        <Text size="sm" c="dimmed">
          Review the letter below, then copy or download it to send to your
          insurance company.
        </Text>
      </Box>

      <Paper
        p="lg"
        style={{
          border: "1px solid #e0e0e0",
          boxShadow: "0 2px 8px rgba(0,0,0,0.08)",
          borderRadius: 8,
        }}
      >
        <Textarea
          value={appealText}
          onChange={(e) => setAppealText(e.target.value)}
          minRows={20}
          autosize
          styles={{
            input: {
              fontFamily: "monospace",
              fontSize: "0.9rem",
              whiteSpace: "pre-wrap",
            },
          }}
        />
      </Paper>

      <Group justify="center" gap="md">
        <CopyButton value={appealText}>
          {({ copied, copy }) => (
            <Button
              variant="outline"
              onClick={copy}
              color={copied ? "teal" : "blue"}
            >
              {copied ? "Copied!" : "Copy to Clipboard"}
            </Button>
          )}
        </CopyButton>
        <Button variant="outline" onClick={downloadTxt}>
          Download as Text
        </Button>
        <Button variant="filled" onClick={onStartOver}>
          Start Over
        </Button>
      </Group>
    </Stack>
  );
}

// =============================================================================
// Main Wizard Component
// =============================================================================

function AMCWizard({ config }: { config: RootConfig }) {
  const [activeStep, setActiveStep] = useState(0);
  const [formData, setFormData] = useState<FormData>(initialFormData);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [appealText, setAppealText] = useState("");
  const [isGenerating, setIsGenerating] = useState(false);
  const [statusMessage, setStatusMessage] = useState("");
  const [generationError, setGenerationError] = useState<string | null>(null);
  const wsRef = useRef<WebSocket | null>(null);

  const validateStep1 = (): boolean => {
    const newErrors: Record<string, string> = {};
    if (!formData.patientName.trim())
      newErrors.patientName = "Patient name is required";
    if (!formData.email.trim()) newErrors.email = "Email is required";
    if (!formData.insuranceCompany.trim())
      newErrors.insuranceCompany = "Insurance company is required";
    if (!formData.denialDate) newErrors.denialDate = "Denial date is required";
    setErrors(newErrors);
    return Object.keys(newErrors).length === 0;
  };

  const validateStep2 = (): boolean => {
    const newErrors: Record<string, string> = {};
    if (!formData.denialReason)
      newErrors.denialReason = "Please select a denial reason";
    if (!formData.serviceDescription.trim())
      newErrors.serviceDescription = "Service description is required";
    if (!formData.pii) newErrors.pii = "Required";
    if (!formData.privacy) newErrors.privacy = "Required";
    if (!formData.tos) newErrors.tos = "Required";
    setErrors(newErrors);
    return Object.keys(newErrors).length === 0;
  };

  const handleNext = () => {
    if (activeStep === 0 && validateStep1()) {
      setActiveStep(1);
    } else if (activeStep === 1 && validateStep2()) {
      generateAppeal();
    }
  };

  const handleBack = () => {
    if (activeStep > 0) {
      setActiveStep(activeStep - 1);
      setErrors({});
    }
  };

  const handleStartOver = () => {
    setActiveStep(0);
    setFormData(initialFormData);
    setAppealText("");
    setIsGenerating(false);
    setStatusMessage("");
    setGenerationError(null);
    setErrors({});
    if (wsRef.current) {
      wsRef.current.close();
      wsRef.current = null;
    }
  };

  const generateAppeal = async () => {
    setActiveStep(2);
    setIsGenerating(true);
    setStatusMessage("Creating your denial record...");
    setGenerationError(null);

    try {
      // Step 1: Create denial via REST API
      const denialText = buildDenialText(formData);
      const response = await fetch("/ziggy/rest/amc-denials/", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRFToken": getCSRFToken(),
        },
        body: JSON.stringify({
          email: formData.email,
          denial_text: denialText,
          zip: "",
          pii: formData.pii,
          tos: formData.tos,
          privacy: formData.privacy,
          subscribe: formData.subscribe,
          insurance_company: formData.insuranceCompany,
          recaptcha_token: "",
        }),
      });

      if (!response.ok) {
        const errData = await response.json().catch(() => ({}));
        throw new Error(
          errData.error || `Server error: ${response.status}`
        );
      }

      const denialData = await response.json();
      const { denial_id, semi_sekret, email } = denialData;

      // Step 2: Connect to WebSocket for appeal streaming
      setStatusMessage("Generating your appeal letter...");
      const protocol =
        window.location.protocol === "https:" ? "wss:" : "ws:";
      const wsUrl = `${protocol}//${config.wsHost}/ws/streaming-appeals-backend/`;

      await new Promise<void>((resolve, reject) => {
        const ws = new WebSocket(wsUrl);
        wsRef.current = ws;
        let buffer = "";
        let appealFound = false;
        let timeoutHandle: ReturnType<typeof setTimeout>;

        const resetTimeout = () => {
          if (timeoutHandle) clearTimeout(timeoutHandle);
          timeoutHandle = setTimeout(() => {
            if (!appealFound) {
              ws.close();
              reject(
                new Error("Appeal generation timed out. Please try again.")
              );
            }
          }, 120000);
        };

        ws.onopen = () => {
          ws.send(
            JSON.stringify({
              denial_id: denial_id,
              email: formData.email || email,
              semi_sekret: semi_sekret,
            })
          );
          resetTimeout();
        };

        ws.onmessage = (event) => {
          resetTimeout();
          buffer += event.data;

          const lines = buffer.split("\n");
          buffer = lines.pop() || "";

          for (const line of lines) {
            const trimmed = line.trim();
            if (!trimmed) continue;

            try {
              const parsed = JSON.parse(trimmed);
              if (parsed.error) {
                setGenerationError(parsed.error);
                continue;
              }
              if (parsed.type === "status" && parsed.message) {
                setStatusMessage(parsed.message);
                continue;
              }
              if (parsed.appeal_text && !appealFound) {
                appealFound = true;
                setAppealText(parsed.appeal_text);
                setIsGenerating(false);
              } else if (
                typeof parsed === "string" &&
                parsed.length > 100 &&
                !appealFound
              ) {
                appealFound = true;
                setAppealText(parsed);
                setIsGenerating(false);
              }
            } catch {
              if (trimmed.length > 100 && !appealFound) {
                appealFound = true;
                setAppealText(trimmed);
                setIsGenerating(false);
              }
            }
          }
        };

        ws.onclose = () => {
          if (timeoutHandle) clearTimeout(timeoutHandle);
          if (appealFound) {
            resolve();
          } else if (buffer.trim().length > 100) {
            setAppealText(buffer.trim());
            setIsGenerating(false);
            resolve();
          } else {
            reject(
              new Error(
                "Connection closed before receiving an appeal. Please try again."
              )
            );
          }
        };

        ws.onerror = () => {
          if (timeoutHandle) clearTimeout(timeoutHandle);
          reject(
            new Error(
              "Failed to connect to appeal generation service. Please try again."
            )
          );
        };
      });
    } catch (err) {
      setIsGenerating(false);
      setGenerationError(
        err instanceof Error ? err.message : "An unexpected error occurred"
      );
    }
  };

  return (
    <Box style={{ display: "flex", flexDirection: "column", minHeight: "100vh" }}>
      <AMCHeader />

      <Box style={{ flex: 1 }}>
        {/* Hero and Info Cards only on initial step */}
        {activeStep === 0 && (
          <>
            <AMCHero />
            <AMCInfoCards />
          </>
        )}

        <Container size="md" py="xl" px="md">
          {/* Stepper */}
          <Stepper
            active={activeStep}
            mb="xl"
            color="blue"
            styles={{
              separator: { marginLeft: 4, marginRight: 4 },
            }}
          >
            <Stepper.Step label="Patient Information" />
            <Stepper.Step label="Denial Details" />
            <Stepper.Step label="Appeal Letter" />
          </Stepper>

          {/* Step Content */}
          {activeStep === 0 && (
            <Step1PatientInfo
              formData={formData}
              setFormData={setFormData}
              errors={errors}
            />
          )}
          {activeStep === 1 && (
            <Step2DenialDetails
              formData={formData}
              setFormData={setFormData}
              errors={errors}
              config={config}
            />
          )}
          {activeStep === 2 && (
            <Step3AppealLetter
              appealText={appealText}
              setAppealText={setAppealText}
              isGenerating={isGenerating}
              statusMessage={statusMessage}
              error={generationError}
              onStartOver={handleStartOver}
            />
          )}

          {/* Navigation Buttons */}
          {activeStep < 2 && (
            <Group justify="space-between" mt="xl">
              <Button
                variant="outline"
                onClick={handleBack}
                disabled={activeStep === 0}
                leftSection={<ArrowBackIcon />}
              >
                Back
              </Button>
              <Button
                onClick={handleNext}
                rightSection={activeStep === 0 ? <ArrowForwardIcon /> : undefined}
              >
                {activeStep === 1 ? "Generate Appeal Letter" : "Continue"}
              </Button>
            </Group>
          )}
        </Container>
      </Box>

      <AMCFooter />
    </Box>
  );
}

// =============================================================================
// Mount
// =============================================================================

document.addEventListener("DOMContentLoaded", () => {
  const root = document.getElementById("amc-wizard-root");
  if (root) {
    const config: RootConfig = {
      csrfToken: root.dataset.csrfToken || "",
      recaptchaSiteKey: root.dataset.recaptchaSiteKey || "",
      wsHost: root.dataset.wsHost || window.location.host,
      privacyUrl: root.dataset.privacyUrl || "/privacy_policy",
      tosUrl: root.dataset.tosUrl || "/tos",
    };

    createRoot(root).render(
      <MantineProvider theme={amcTheme}>
        <AMCWizard config={config} />
      </MantineProvider>
    );
  }
});

export default AMCWizard;
