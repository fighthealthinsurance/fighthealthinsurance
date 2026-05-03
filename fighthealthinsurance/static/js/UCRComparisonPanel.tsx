/**
 * UCRComparisonPanel
 *
 * Displays the UCR (Usual & Customary Rate) comparison for an out-of-network
 * denial. Fetches `denial.ucr_context` from /ziggy/rest/denials/{id}/ucr_context/
 * and surfaces the gap between what the insurer allowed and an independent
 * benchmark, with a button to edit billing info and re-trigger enrichment.
 *
 * See UCR-OON-Reimbursement-Plan.md §8.2.
 */

import React, { useState, useEffect, useCallback } from "react";
import {
  Box,
  Button,
  Collapse,
  Group,
  Loader,
  NumberInput,
  Paper,
  Stack,
  Text,
  TextInput,
  Title,
} from "@mantine/core";

type UCRRateRow = {
  percentile: number;
  amount_cents: number;
  source: string;
  effective_date: string;
  is_derived: boolean;
};

type UCRContext =
  | { status: "pending" }
  | {
      status: "ready";
      procedure_code: string;
      area_kind: string;
      area_code: string;
      billed_cents: number | null;
      allowed_cents: number | null;
      paid_cents: number | null;
      rates: UCRRateRow[];
      gap_p80_cents: number | null;
      gap_p80_pct: number | null;
      gap_p90_cents: number | null;
      gap_p90_pct: number | null;
      narrative: string;
      refreshed_at: string | null;
    };

type Props = {
  denialId: number;
  csrfToken: string;
  /** Override the API base for tests. Defaults to /ziggy/rest. */
  apiBase?: string;
};

const REST_BASE_DEFAULT = "/ziggy/rest";

function dollars(cents: number | null): string {
  if (cents === null || cents === undefined) return "—";
  return `$${(cents / 100).toFixed(2)}`;
}

async function fetchUCRContext(
  denialId: number,
  apiBase: string,
): Promise<UCRContext> {
  const response = await fetch(`${apiBase}/denials/${denialId}/ucr_context/`, {
    credentials: "same-origin",
  });
  if (!response.ok) {
    throw new Error(`UCR fetch failed: ${response.status}`);
  }
  return response.json();
}

async function postSetBillingInfo(
  denialId: number,
  body: {
    service_zip?: string;
    procedure_code?: string;
    procedure_modifier?: string;
    billed_amount_cents?: number | null;
    allowed_amount_cents?: number | null;
    paid_amount_cents?: number | null;
  },
  csrfToken: string,
  apiBase: string,
): Promise<void> {
  const response = await fetch(`${apiBase}/denials/set_billing_info/`, {
    method: "POST",
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      "X-CSRFToken": csrfToken,
    },
    body: JSON.stringify({ denial_id: denialId, ...body }),
  });
  if (!response.ok && response.status !== 202) {
    throw new Error(`set_billing_info failed: ${response.status}`);
  }
}

async function postRefresh(
  denialId: number,
  csrfToken: string,
  apiBase: string,
): Promise<void> {
  const response = await fetch(`${apiBase}/denials/refresh_ucr/`, {
    method: "POST",
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      "X-CSRFToken": csrfToken,
    },
    body: JSON.stringify({ denial_id: denialId }),
  });
  if (!response.ok && response.status !== 202) {
    throw new Error(`refresh_ucr failed: ${response.status}`);
  }
}

/** Convert a dollar string ("250.00") to cents (25000), or null when blank. */
function dollarsToCents(input: string): number | null {
  const trimmed = input.trim();
  if (!trimmed) return null;
  const parsed = Number(trimmed);
  if (!Number.isFinite(parsed) || parsed < 0) return null;
  return Math.round(parsed * 100);
}

function HeadlineGap({
  allowedCents,
  rates,
}: {
  allowedCents: number | null;
  rates?: UCRRateRow[];
}) {
  const safeRates = rates ?? [];
  const p80 = safeRates.find((r) => r.percentile === 80);
  if (!p80 || allowedCents === null) {
    return null;
  }
  return (
    <Title order={4}>
      Insurer allowed {dollars(allowedCents)} — independent benchmark suggests{" "}
      {dollars(p80.amount_cents)}.
    </Title>
  );
}

function RateTable({ rates }: { rates?: UCRRateRow[] }) {
  const safeRates = rates ?? [];
  if (safeRates.length === 0) {
    return <Text size="sm">No rate data available for this code/area.</Text>;
  }
  return (
    <Stack gap="xs">
      {safeRates.map((r) => (
        <Group justify="space-between" key={`${r.source}-${r.percentile}`}>
          <Text size="sm">
            p{r.percentile} ({r.source}
            {r.is_derived ? ", derived" : ""})
          </Text>
          <Text size="sm" fw={500}>
            {dollars(r.amount_cents)}
          </Text>
        </Group>
      ))}
    </Stack>
  );
}

function BillingForm({
  onSubmit,
  busy,
}: {
  onSubmit: (values: {
    service_zip: string;
    procedure_code: string;
    billed: number | null;
    allowed: number | null;
    paid: number | null;
  }) => void;
  busy: boolean;
}) {
  const [serviceZip, setServiceZip] = useState("");
  const [cpt, setCpt] = useState("");
  const [billed, setBilled] = useState<string | number>("");
  const [allowed, setAllowed] = useState<string | number>("");
  const [paid, setPaid] = useState<string | number>("");

  return (
    <Stack gap="sm">
      <Group grow>
        <TextInput
          label="Service ZIP"
          value={serviceZip}
          onChange={(e) => setServiceZip(e.currentTarget.value)}
          placeholder="94110"
          maxLength={5}
        />
        <TextInput
          label="CPT/HCPCS"
          value={cpt}
          onChange={(e) => setCpt(e.currentTarget.value.toUpperCase())}
          placeholder="99213"
          maxLength={10}
        />
      </Group>
      <Group grow>
        <NumberInput
          label="Billed ($)"
          value={billed}
          onChange={setBilled}
          min={0}
          step={0.01}
          decimalScale={2}
        />
        <NumberInput
          label="Allowed ($)"
          value={allowed}
          onChange={setAllowed}
          min={0}
          step={0.01}
          decimalScale={2}
        />
        <NumberInput
          label="Paid ($)"
          value={paid}
          onChange={setPaid}
          min={0}
          step={0.01}
          decimalScale={2}
        />
      </Group>
      <Button
        loading={busy}
        onClick={() =>
          onSubmit({
            service_zip: serviceZip.trim(),
            procedure_code: cpt.trim(),
            billed: dollarsToCents(String(billed)),
            allowed: dollarsToCents(String(allowed)),
            paid: dollarsToCents(String(paid)),
          })
        }
      >
        Save & re-check UCR
      </Button>
    </Stack>
  );
}

export function UCRComparisonPanel({
  denialId,
  csrfToken,
  apiBase = REST_BASE_DEFAULT,
}: Props): React.ReactElement {
  const [context, setContext] = useState<UCRContext | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [whyOpen, setWhyOpen] = useState(false);

  const refresh = useCallback(async () => {
    try {
      setError(null);
      const data = await fetchUCRContext(denialId, apiBase);
      setContext(data);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [denialId, apiBase]);

  // Single source of truth for the billing-form submit handler. Both the
  // pending-state and ready-state forms below use this so the POST shape
  // and post-submit behavior can't drift between them.
  const handleBillingSubmit = useCallback(
    async (values: {
      service_zip: string;
      procedure_code: string;
      billed: number | null;
      allowed: number | null;
      paid: number | null;
    }) => {
      setBusy(true);
      try {
        await postSetBillingInfo(
          denialId,
          {
            service_zip: values.service_zip || undefined,
            procedure_code: values.procedure_code || undefined,
            billed_amount_cents: values.billed,
            allowed_amount_cents: values.allowed,
            paid_amount_cents: values.paid,
          },
          csrfToken,
          apiBase,
        );
        setEditing(false);
        await refresh();
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setBusy(false);
      }
    },
    [denialId, csrfToken, apiBase, refresh],
  );

  useEffect(() => {
    refresh();
  }, [refresh]);

  if (error) {
    return (
      <Paper p="md" withBorder>
        <Text c="red">Couldn't load UCR comparison: {error}</Text>
      </Paper>
    );
  }
  if (context === null) {
    return (
      <Paper p="md" withBorder>
        <Group>
          <Loader size="sm" />
          <Text size="sm">Loading UCR comparison…</Text>
        </Group>
      </Paper>
    );
  }
  if (context.status === "pending") {
    return (
      <Paper p="md" withBorder>
        <Stack gap="xs">
          <Text size="sm">
            UCR comparison is pending — billing info hasn't been entered yet.
          </Text>
          <Button variant="light" onClick={() => setEditing(true)}>
            Add billing details
          </Button>
          <Collapse in={editing}>
            <BillingForm busy={busy} onSubmit={handleBillingSubmit} />
          </Collapse>
        </Stack>
      </Paper>
    );
  }

  return (
    <Paper p="md" withBorder>
      <Stack gap="md">
        <HeadlineGap
          allowedCents={context.allowed_cents}
          rates={context.rates}
        />

        {context.gap_p80_cents !== null && context.gap_p80_pct !== null && (
          <Text>
            That's a gap of {dollars(context.gap_p80_cents)} (
            {context.gap_p80_pct.toFixed(0)}%) below the 80th-percentile
            benchmark.
          </Text>
        )}

        <RateTable rates={context.rates} />

        <Box>
          <Button
            variant="subtle"
            size="xs"
            onClick={() => setWhyOpen((v) => !v)}
          >
            {whyOpen ? "Hide" : "Why this matters"}
          </Button>
          <Collapse in={whyOpen}>
            <Text size="sm" c="dimmed">
              When you see an out-of-network provider, your insurer pays based
              on a "usual and customary" rate. Plans often pick a low UCR,
              leaving you with a balance bill. The numbers above come from a
              public, defensible benchmark for the same procedure code in your
              ZIP3 area; the appeal letter cites them directly to argue for a
              higher allowable.
            </Text>
          </Collapse>
        </Box>

        <Group>
          <Button
            variant="default"
            onClick={async () => {
              setBusy(true);
              try {
                await postRefresh(denialId, csrfToken, apiBase);
                await refresh();
              } catch (e) {
                setError(e instanceof Error ? e.message : String(e));
              } finally {
                setBusy(false);
              }
            }}
            loading={busy}
          >
            Re-run UCR check
          </Button>
          <Button variant="subtle" onClick={() => setEditing((v) => !v)}>
            {editing ? "Cancel edit" : "Edit billing info"}
          </Button>
        </Group>

        <Collapse in={editing}>
          <BillingForm busy={busy} onSubmit={handleBillingSubmit} />
        </Collapse>

        {context.refreshed_at && (
          <Text size="xs" c="dimmed">
            Last refreshed: {new Date(context.refreshed_at).toLocaleString()}
          </Text>
        )}
      </Stack>
    </Paper>
  );
}

export default UCRComparisonPanel;
