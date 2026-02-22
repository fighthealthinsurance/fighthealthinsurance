import React, { useState, useRef, useCallback } from "react";
import { createRoot } from "react-dom/client";
import {
  Box,
  Button,
  Checkbox,
  Container,
  Group,
  MantineProvider,
  NativeSelect,
  Paper,
  Stepper,
  Text,
  Textarea,
  TextInput,
  Title,
  createTheme,
  Anchor,
  Alert,
  SegmentedControl,
  CopyButton,
  Loader,
  Stack,
} from "@mantine/core";
import { getCSRFToken } from "./shared";

// AMC Theme
const amcTheme = createTheme({
  fontFamily: "'Manrope', sans-serif",
  primaryColor: "blue",
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

// Types
interface FormData {
  authorType: "patient" | "provider";
  patientName: string;
  email: string;
  insuranceCompany: string;
  claimNumber: string;
  groupPolicyId: string;
  denialDate: string;
  providerName: string;
  providerTitle: string;
  providerOrganization: string;
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
  authorType: "patient",
  patientName: "",
  email: "",
  insuranceCompany: "",
  claimNumber: "",
  groupPolicyId: "",
  denialDate: "",
  providerName: "",
  providerTitle: "",
  providerOrganization: "",
  denialReason: "",
  serviceDescription: "",
  justification: "",
  pii: false,
  tos: false,
  privacy: false,
  subscribe: false,
};

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
      ? `\nAdditional context from the ${data.authorType}: ${data.justification}`
      : null,
    data.authorType === "provider"
      ? `\nThis appeal is being written by the healthcare provider: ${data.providerName}, ${data.providerTitle} at ${data.providerOrganization}.`
      : null,
  ];
  return parts.filter(Boolean).join("\n");
}

// Step 1: Patient Information
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
      <Text size="lg" fw={500}>
        Let&apos;s start with some basic information
      </Text>

      <Paper p="lg" withBorder>
        <Text fw={600} mb="sm">
          Who is writing this appeal?
        </Text>
        <SegmentedControl
          fullWidth
          value={formData.authorType}
          onChange={(val) => update("authorType", val)}
          data={[
            { label: "Patient", value: "patient" },
            { label: "Healthcare Provider", value: "provider" },
          ]}
          mb="xs"
        />
        <Text size="sm" c="dimmed">
          {formData.authorType === "patient"
            ? "The appeal will be written from the patient's perspective"
            : "The appeal will be written from the healthcare provider's perspective on behalf of the patient"}
        </Text>
      </Paper>

      <Paper p="lg" withBorder>
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

      {formData.authorType === "provider" && (
        <Paper p="lg" withBorder>
          <Text fw={600} mb="sm">
            Provider Information
          </Text>
          <TextInput
            label="Provider's Full Name"
            required
            value={formData.providerName}
            onChange={(e) => update("providerName", e.target.value)}
            error={errors.providerName}
            mb="sm"
          />
          <TextInput
            label="Provider's Title"
            required
            placeholder="e.g., MD, NP, PA, Physical Therapist"
            value={formData.providerTitle}
            onChange={(e) => update("providerTitle", e.target.value)}
            error={errors.providerTitle}
            mb="sm"
          />
          <TextInput
            label="Healthcare Organization/Practice"
            required
            value={formData.providerOrganization}
            onChange={(e) => update("providerOrganization", e.target.value)}
            error={errors.providerOrganization}
          />
        </Paper>
      )}

      <Paper p="lg" withBorder>
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

// Step 2: Denial Details
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
      <Text size="lg" fw={500}>
        Tell us about the denied service
      </Text>

      <Paper p="lg" withBorder>
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
          placeholder="Provide any additional information about why this service is medically necessary..."
          minRows={3}
          value={formData.justification}
          onChange={(e) => update("justification", e.target.value)}
        />
      </Paper>

      <Paper p="lg" withBorder>
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

// Step 3: Appeal Letter
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
  const downloadPdf = useCallback(() => {
    // Simple text-based PDF download using Blob
    const content = appealText;
    const blob = new Blob([content], { type: "text/plain" });
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
        <Loader size="lg" />
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
        <Button onClick={onStartOver} variant="outline">
          Start Over
        </Button>
      </Stack>
    );
  }

  return (
    <Stack gap="lg">
      <Text size="lg" fw={500}>
        Your Appeal Letter is Ready
      </Text>
      <Text size="sm" c="dimmed">
        Review the letter below, then copy or download it to send to your
        insurance company.
      </Text>

      <Paper p="lg" withBorder>
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

      <Group justify="center">
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
        <Button variant="outline" onClick={downloadPdf}>
          Download as Text
        </Button>
        <Button variant="filled" onClick={onStartOver}>
          Start Over
        </Button>
      </Group>
    </Stack>
  );
}

// Main Wizard Component
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
    if (formData.authorType === "provider") {
      if (!formData.providerName.trim())
        newErrors.providerName = "Provider name is required";
      if (!formData.providerTitle.trim())
        newErrors.providerTitle = "Provider title is required";
      if (!formData.providerOrganization.trim())
        newErrors.providerOrganization = "Organization is required";
    }
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
              reject(new Error("Appeal generation timed out. Please try again."));
            }
          }, 120000); // 2 minute timeout
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

          // Try to parse complete JSON lines
          const lines = buffer.split("\n");
          buffer = lines.pop() || ""; // Keep incomplete last line

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
              // Appeal content - take the first complete appeal
              if (parsed.appeal_text && !appealFound) {
                appealFound = true;
                setAppealText(parsed.appeal_text);
                setIsGenerating(false);
                // Don't close yet - let it finish naturally
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
              // Could be a raw text appeal
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
          // If we got an appeal, resolve successfully
          if (appealFound) {
            resolve();
          } else if (buffer.trim().length > 100) {
            // Check if there's remaining content in the buffer
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
    <Container size="md" py="xl">
      {/* Header */}
      <Box ta="center" mb="xl">
        <Title order={1} mb="xs">
          Healthcare Appeal Helper
        </Title>
        <Text c="dimmed" size="lg">
          Generate a professional appeal letter for your denied insurance claim
        </Text>
      </Box>

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
          >
            Back
          </Button>
          <Button onClick={handleNext}>
            {activeStep === 1 ? "Generate Appeal Letter" : "Continue"}
          </Button>
        </Group>
      )}

      {/* Footer */}
      <Box ta="center" mt="xl" pt="md" style={{ borderTop: "1px solid #e5e7eb" }}>
        <Text size="xs" c="dimmed">
          Powered by{" "}
          <Anchor href="/" size="xs">
            Fight Health Insurance
          </Anchor>
        </Text>
        <Text size="xs" c="dimmed" mt={4}>
          AI-enhanced letter generation
        </Text>
      </Box>
    </Container>
  );
}

// Mount the wizard
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
