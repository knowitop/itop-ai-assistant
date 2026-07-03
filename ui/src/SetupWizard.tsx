import {
  Alert,
  Button,
  Code,
  Group,
  List,
  Loader,
  PasswordInput,
  SegmentedControl,
  Stack,
  Stepper,
  Text,
  TextInput,
  Title,
} from '@mantine/core';
import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';

import { apiGet, apiSend, fetchSetupStatus, SetupStatus, setToken } from './api';

// GET /api/setup/{section} shape (same as in Connections): non-secret values
// plus is-set flags for secrets.
interface SectionData {
  values: Record<string, unknown>;
  secrets: Record<string, boolean>;
}

// One placeholder style for every secret field, here and in Connections.
function secretPlaceholder(isSet: boolean): string {
  return isSet ? '•••• (already set — leave empty to keep)' : 'not set';
}

// crypto.getRandomValues works on plain http too, unlike crypto.randomUUID.
function generateToken(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(32));
  return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
}

export default function SetupWizard() {
  const [status, setStatus] = useState<SetupStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [wizardActive, setWizardActive] = useState(false);
  const [step, setStep] = useState(0);

  useEffect(() => {
    fetchSetupStatus()
      .then((s) => {
        setStatus(s);
        // The wizard opens by itself on an unconfigured instance.
        setWizardActive(!s.configured);
      })
      .catch((e: Error) => setError(e.message));
  }, []);

  if (error) return <Alert color="red">{error}</Alert>;
  if (!status) return <Loader />;

  if (!wizardActive) {
    return (
      <Stack maw={640}>
        <Title order={2}>Setup</Title>
        {status.configured ? (
          <Alert color="green">
            The assistant is configured — <Code>/webhook</Code> is active.
          </Alert>
        ) : (
          <Alert color="orange">
            <Text fw={500}>Setup is incomplete. Missing:</Text>
            <List size="sm" mt={4}>
              {status.missing.map((item) => (
                <List.Item key={item}>{item}</List.Item>
              ))}
            </List>
          </Alert>
        )}
        <Group>
          <Button onClick={() => setWizardActive(true)}>Run setup wizard</Button>
          <Button variant="default" component={Link} to="/connections">
            Edit connections directly
          </Button>
        </Group>
      </Stack>
    );
  }

  const finish = async () => {
    setStep(3);
    // Refresh so the final screen reflects what the wizard actually saved.
    try {
      setStatus(await fetchSetupStatus());
    } catch (e) {
      setError((e as Error).message);
    }
  };

  return (
    <Stack maw={640}>
      <Title order={2}>Setup wizard</Title>
      <Stepper active={step} onStepClick={setStep} allowNextStepsSelect={false} size="sm">
        <Stepper.Step label="LLM" description="Model endpoint">
          <LlmStep onDone={() => setStep(1)} />
        </Stepper.Step>
        <Stepper.Step label="iTop" description="REST API access">
          <ItopStep onBack={() => setStep(0)} onDone={() => setStep(2)} />
        </Stepper.Step>
        <Stepper.Step label="Security" description="Access tokens">
          <SecurityStep onBack={() => setStep(1)} onDone={finish} />
        </Stepper.Step>
        <Stepper.Completed>
          <FinalStep status={status} />
        </Stepper.Completed>
      </Stepper>
    </Stack>
  );
}

function LlmStep({ onDone }: { onDone: () => void }) {
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [testResult, setTestResult] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const [baseUrl, setBaseUrl] = useState('');
  const [model, setModel] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [keySet, setKeySet] = useState(false);

  useEffect(() => {
    apiGet<SectionData>('/setup/llm')
      .then((data) => {
        setBaseUrl(String(data.values.base_url ?? ''));
        setModel(String(data.values.model ?? ''));
        setKeySet(data.secrets.api_key);
        setLoaded(true);
      })
      .catch((e: Error) => setError(e.message));
  }, []);

  const body = () => {
    const b: Record<string, unknown> = { base_url: baseUrl, model: model || null };
    if (apiKey) b.api_key = apiKey;
    return b;
  };

  const test = async () => {
    setBusy(true);
    setError(null);
    setTestResult(null);
    try {
      const result = await apiSend<{ ok: boolean; response?: string; error?: string }>(
        'POST',
        '/setup/test-llm',
        body(),
      );
      if (result.ok) setTestResult(`Model responded: ${result.response}`);
      else setError(result.error ?? 'LLM test failed');
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const saveAndNext = async () => {
    setBusy(true);
    setError(null);
    try {
      await apiSend('PATCH', '/setup/llm', body());
      onDone();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  if (!loaded) return error ? <Alert color="red">{error}</Alert> : <Loader />;

  return (
    <Stack pt="md">
      {error && (
        <Alert color="red" style={{ whiteSpace: 'pre-wrap' }}>
          {error}
        </Alert>
      )}
      {testResult && <Alert color="green">{testResult}</Alert>}
      <TextInput
        label="Base URL"
        description="OpenAI-compatible endpoint, e.g. http://localhost:1234/v1"
        value={baseUrl}
        onChange={(e) => setBaseUrl(e.currentTarget.value)}
      />
      <TextInput
        label="Model"
        description="Model name as exposed by the endpoint"
        value={model}
        onChange={(e) => setModel(e.currentTarget.value)}
      />
      <PasswordInput
        label="API key"
        placeholder={secretPlaceholder(keySet)}
        description={keySet ? undefined : 'Omit for local LM Studio'}
        value={apiKey}
        onChange={(e) => setApiKey(e.currentTarget.value)}
      />
      <Group>
        <Button variant="default" onClick={test} loading={busy}>
          Test LLM
        </Button>
        <Button onClick={saveAndNext} loading={busy} disabled={!model}>
          Save and continue
        </Button>
      </Group>
    </Stack>
  );
}

function ItopStep({ onBack, onDone }: { onBack: () => void; onDone: () => void }) {
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [testResult, setTestResult] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const [url, setUrl] = useState('');
  const [auth, setAuth] = useState<'basic' | 'token'>('basic');
  const [user, setUser] = useState('');
  const [pwd, setPwd] = useState('');
  const [token, setTokenValue] = useState('');
  const [secrets, setSecrets] = useState<Record<string, boolean>>({});

  useEffect(() => {
    apiGet<SectionData>('/setup/itop')
      .then((data) => {
        setUrl(String(data.values.url ?? ''));
        setUser(String(data.values.user ?? ''));
        setSecrets(data.secrets);
        setAuth(data.values.user ? 'basic' : data.secrets.token ? 'token' : 'basic');
        setLoaded(true);
      })
      .catch((e: Error) => setError(e.message));
  }, []);

  // Same semantics as the Connections form: the chosen auth method clears the
  // other method's credentials; empty secret fields keep the stored values.
  const body = () => {
    const b: Record<string, unknown> = { url };
    if (auth === 'basic') {
      b.user = user || null;
      b.token = null;
      if (pwd) b.pwd = pwd;
    } else {
      b.user = null;
      b.pwd = null;
      if (token) b.token = token;
    }
    return b;
  };

  const test = async () => {
    setBusy(true);
    setError(null);
    setTestResult(null);
    try {
      const result = await apiSend<{ ok: boolean; ai_person?: string; error?: string }>(
        'POST',
        '/setup/test-itop',
        body(),
      );
      if (result.ok) setTestResult(`Connection OK — AI service account: ${result.ai_person}`);
      else setError(result.error ?? 'Connection test failed');
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const saveAndNext = async () => {
    setBusy(true);
    setError(null);
    try {
      await apiSend('PATCH', '/setup/itop', body());
      onDone();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  if (!loaded) return error ? <Alert color="red">{error}</Alert> : <Loader />;

  return (
    <Stack pt="md">
      {error && (
        <Alert color="red" style={{ whiteSpace: 'pre-wrap' }}>
          {error}
        </Alert>
      )}
      {testResult && <Alert color="green">{testResult}</Alert>}
      <TextInput
        label="REST API URL"
        description="e.g. http://itop.example.com/webservices/rest.php"
        value={url}
        onChange={(e) => setUrl(e.currentTarget.value)}
      />
      <Text size="sm" c="dimmed">
        The account needs the <Code>REST Services User</Code> profile and is used as the AI
        service account — its comments must be distinguishable from engineers.
      </Text>
      <SegmentedControl
        value={auth}
        onChange={(value) => setAuth(value as 'basic' | 'token')}
        data={[
          { label: 'User + password', value: 'basic' },
          { label: 'Token', value: 'token' },
        ]}
      />
      {auth === 'basic' ? (
        <Group grow align="start">
          <TextInput label="User" value={user} onChange={(e) => setUser(e.currentTarget.value)} />
          <PasswordInput
            label="Password"
            placeholder={secretPlaceholder(secrets.pwd)}
            value={pwd}
            onChange={(e) => setPwd(e.currentTarget.value)}
          />
        </Group>
      ) : (
        <PasswordInput
          label="Token"
          placeholder={secretPlaceholder(secrets.token)}
          value={token}
          onChange={(e) => setTokenValue(e.currentTarget.value)}
        />
      )}
      <Group>
        <Button variant="subtle" onClick={onBack}>
          Back
        </Button>
        <Button variant="default" onClick={test} loading={busy}>
          Test connection
        </Button>
        <Button onClick={saveAndNext} loading={busy}>
          Save and continue
        </Button>
      </Group>
    </Stack>
  );
}

function SecurityStep({ onBack, onDone }: { onBack: () => void; onDone: () => void }) {
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const [webhookToken, setWebhookToken] = useState('');
  const [adminToken, setAdminToken] = useState('');
  const [secrets, setSecrets] = useState<Record<string, boolean>>({});

  useEffect(() => {
    apiGet<SectionData>('/setup/security')
      .then((data) => {
        setSecrets(data.secrets);
        // Pre-generate tokens that are not set yet; already-set ones are kept
        // unless the user generates a replacement.
        if (!data.secrets.webhook_token) setWebhookToken(generateToken());
        if (!data.secrets.admin_token) setAdminToken(generateToken());
        setLoaded(true);
      })
      .catch((e: Error) => setError(e.message));
  }, []);

  const save = async () => {
    const b: Record<string, unknown> = {};
    if (webhookToken) b.webhook_token = webhookToken;
    if (adminToken) b.admin_token = adminToken;
    setBusy(true);
    setError(null);
    try {
      if (Object.keys(b).length > 0) {
        await apiSend('PATCH', '/setup/security', b);
        // The API is locked by the new admin token from this moment on —
        // store it right away so the very next request still passes.
        if (adminToken) setToken(adminToken);
      }
      onDone();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  if (!loaded) return error ? <Alert color="red">{error}</Alert> : <Loader />;

  return (
    <Stack pt="md">
      {error && (
        <Alert color="red" style={{ whiteSpace: 'pre-wrap' }}>
          {error}
        </Alert>
      )}
      <Alert color="orange">
        Copy the tokens now — they are shown only this once. The admin token is saved to this
        browser automatically; the webhook token must be set in the iTop Remote Application
        Connection (<Code>X-Auth-Token</Code> header).
      </Alert>
      <WizardTokenField
        label="Webhook token"
        description="Shared secret for POST /webhook"
        isSet={secrets.webhook_token}
        value={webhookToken}
        onChange={setWebhookToken}
      />
      <WizardTokenField
        label="Admin token"
        description="Bearer token for this UI and the /api endpoints"
        isSet={secrets.admin_token}
        value={adminToken}
        onChange={setAdminToken}
      />
      <Group>
        <Button variant="subtle" onClick={onBack}>
          Back
        </Button>
        <Button onClick={save} loading={busy}>
          {webhookToken || adminToken ? 'Save and finish' : 'Finish'}
        </Button>
      </Group>
    </Stack>
  );
}

function WizardTokenField(props: {
  label: string;
  description: string;
  isSet: boolean;
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <Stack gap={4}>
      <TextInput
        label={props.label}
        description={props.description}
        placeholder={props.isSet ? secretPlaceholder(true) : undefined}
        value={props.value}
        onChange={(e) => props.onChange(e.currentTarget.value)}
        styles={{ input: { fontFamily: 'monospace', fontSize: 13 } }}
      />
      <Group gap="xs">
        <Button size="compact-xs" variant="default" onClick={() => props.onChange(generateToken())}>
          Generate
        </Button>
        {props.value && navigator.clipboard && (
          <Button
            size="compact-xs"
            variant="default"
            onClick={() => navigator.clipboard.writeText(props.value)}
          >
            Copy
          </Button>
        )}
      </Group>
    </Stack>
  );
}

function FinalStep({ status }: { status: SetupStatus }) {
  return (
    <Stack pt="md">
      {status.configured ? (
        <Alert color="green">
          <Text fw={500}>Setup complete — /webhook is active.</Text>
          <Text size="sm" mt={4}>
            Point the iTop Remote Application Connection at{' '}
            <Code>{window.location.origin}/webhook</Code> with the webhook token in the{' '}
            <Code>X-Auth-Token</Code> header, and make sure the trigger context excludes{' '}
            <Code>REST/JSON</Code> (see README).
          </Text>
        </Alert>
      ) : (
        <Alert color="orange">
          <Text fw={500}>Still missing:</Text>
          <List size="sm" mt={4}>
            {status.missing.map((item) => (
              <List.Item key={item}>{item}</List.Item>
            ))}
          </List>
        </Alert>
      )}
      <Group>
        <Button component={Link} to="/runs">
          Open runs
        </Button>
        <Button variant="default" component={Link} to="/connections">
          Open connections
        </Button>
      </Group>
    </Stack>
  );
}
