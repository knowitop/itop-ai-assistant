import {
  Alert,
  Badge,
  Button,
  Divider,
  Group,
  JsonInput,
  Loader,
  NumberInput,
  PasswordInput,
  SegmentedControl,
  Stack,
  Table,
  Tabs,
  TagsInput,
  Text,
  TextInput,
  Title,
} from '@mantine/core';
import { useEffect, useState } from 'react';

import { apiGet, apiSend, setToken } from './api';

// GET /api/setup/{section} returns non-secret values plus is-set flags for
// secrets; secret values never leave the server.
interface SectionData {
  values: Record<string, unknown>;
  secrets: Record<string, boolean>;
}

// One placeholder style for every secret field, here and in the wizard.
function secretPlaceholder(isSet: boolean): string {
  return isSet ? '•••• (already set — leave empty to keep)' : 'not set';
}

export default function Connections() {
  return (
    <Stack>
      <Title order={2}>Connections</Title>
      <Tabs defaultValue="itop" keepMounted={false}>
        <Tabs.List>
          <Tabs.Tab value="itop">iTop</Tabs.Tab>
          <Tabs.Tab value="llm">LLM</Tabs.Tab>
          <Tabs.Tab value="security">Security</Tabs.Tab>
          <Tabs.Tab value="ticket_mapping">Ticket mapping</Tabs.Tab>
        </Tabs.List>
        <Tabs.Panel value="itop" pt="md">
          <ItopForm />
          <Divider my="lg" maw={560} />
          <ItopWebhooksForm />
        </Tabs.Panel>
        <Tabs.Panel value="llm" pt="md">
          <LlmForm />
        </Tabs.Panel>
        <Tabs.Panel value="security" pt="md">
          <SecurityForm />
        </Tabs.Panel>
        <Tabs.Panel value="ticket_mapping" pt="md">
          <TicketMappingForm />
        </Tabs.Panel>
      </Tabs>
    </Stack>
  );
}

// Shared per-form status line: red for errors, green for confirmations.
function StatusAlert({ error, success }: { error: string | null; success: string | null }) {
  if (error) {
    return (
      <Alert color="red" style={{ whiteSpace: 'pre-wrap' }}>
        {error}
      </Alert>
    );
  }
  if (success) {
    return <Alert color="green">{success}</Alert>;
  }
  return null;
}

async function resetSection(section: string): Promise<boolean> {
  if (!window.confirm(`Reset the "${section}" section to env/yaml defaults?`)) return false;
  await apiSend('DELETE', `/setup/${section}`);
  return true;
}

function ItopForm() {
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const [url, setUrl] = useState('');
  const [apiVersion, setApiVersion] = useState('');
  const [timeout_, setTimeout_] = useState<number | string>('');
  const [auth, setAuth] = useState<'basic' | 'token'>('basic');
  const [user, setUser] = useState('');
  const [pwd, setPwd] = useState('');
  const [token, setTokenValue] = useState('');
  const [secrets, setSecrets] = useState<Record<string, boolean>>({});

  const load = async () => {
    const data = await apiGet<SectionData>('/setup/itop');
    setUrl(String(data.values.url ?? ''));
    setApiVersion(String(data.values.api_version ?? ''));
    setTimeout_((data.values.timeout as number) ?? '');
    setUser(String(data.values.user ?? ''));
    setPwd('');
    setTokenValue('');
    setSecrets(data.secrets);
    // Guess the configured auth method: user+pwd wins over token.
    setAuth(data.values.user ? 'basic' : data.secrets.token ? 'token' : 'basic');
    setLoaded(true);
  };

  useEffect(() => {
    load().catch((e: Error) => setError(e.message));
  }, []);

  // PATCH semantics: absent secret = keep stored, explicit null = clear.
  // Choosing an auth method clears the other method's credentials.
  const body = () => {
    const b: Record<string, unknown> = { url, api_version: apiVersion };
    if (timeout_ !== '') b.timeout = Number(timeout_);
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
    setSuccess(null);
    try {
      const result = await apiSend<{ ok: boolean; ai_person?: string; error?: string }>(
        'POST',
        '/setup/test-itop',
        body(),
      );
      if (result.ok) setSuccess(`Connection OK — AI service account: ${result.ai_person}`);
      else setError(result.error ?? 'Connection test failed');
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const save = async () => {
    setBusy(true);
    setError(null);
    setSuccess(null);
    try {
      await apiSend<SectionData>('PATCH', '/setup/itop', body());
      await load();
      setSuccess('Saved');
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const reset = async () => {
    setError(null);
    setSuccess(null);
    try {
      if (!(await resetSection('itop'))) return;
      await load();
      setSuccess('Section reset to defaults');
    } catch (e) {
      setError((e as Error).message);
    }
  };

  if (!loaded) return error ? <Alert color="red">{error}</Alert> : <Loader />;

  return (
    <Stack maw={560}>
      <StatusAlert error={error} success={success} />
      <TextInput label="REST API URL" value={url} onChange={(e) => setUrl(e.currentTarget.value)} />
      <Group grow>
        <TextInput
          label="API version"
          value={apiVersion}
          onChange={(e) => setApiVersion(e.currentTarget.value)}
        />
        <NumberInput label="Timeout (seconds)" min={1} value={timeout_} onChange={setTimeout_} />
      </Group>
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
        <Button onClick={save} loading={busy}>
          Save
        </Button>
        <Button variant="default" onClick={test} loading={busy}>
          Test connection
        </Button>
        <Button variant="subtle" color="red" onClick={reset}>
          Reset to defaults
        </Button>
      </Group>
    </Stack>
  );
}

// POST /api/setup/provision-itop report line (same shape in the wizard).
interface ProvisionItem {
  class: string;
  name: string;
  status: 'created' | 'exists' | 'skipped';
}

const PROVISION_STATUS_COLORS: Record<ProvisionItem['status'], string> = {
  created: 'green',
  exists: 'blue',
  skipped: 'yellow',
};

// Deliberate copy of the wizard's webhooks step (same duplication pattern as
// the connection forms) so webhooks can be (re)provisioned without the wizard.
function ItopWebhooksForm() {
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [report, setReport] = useState<ProvisionItem[] | null>(null);

  const [backendUrl, setBackendUrl] = useState(window.location.origin);
  const [auth, setAuth] = useState<'basic' | 'token'>('basic');
  const [user, setUser] = useState('');
  const [pwd, setPwd] = useState('');
  const [token, setTokenValue] = useState('');

  const configure = async () => {
    setBusy(true);
    setError(null);
    setReport(null);
    try {
      const body: Record<string, unknown> = { backend_url: backendUrl };
      if (auth === 'basic') {
        body.user = user;
        body.pwd = pwd;
      } else {
        body.token = token;
      }
      const result = await apiSend<{ ok: boolean; report?: ProvisionItem[]; error?: string }>(
        'POST',
        '/setup/provision-itop',
        body,
      );
      if (result.ok) setReport(result.report ?? []);
      else setError(result.error ?? 'Provisioning failed');
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Stack maw={560}>
      <Title order={4}>iTop webhooks</Title>
      {error && (
        <Alert color="red" style={{ whiteSpace: 'pre-wrap' }}>
          {error}
        </Alert>
      )}
      <Text c="dimmed" size="sm">
        Creates the triggers and webhooks in iTop that call this assistant (uses the saved webhook
        token). Requires an iTop <b>administrator</b> account — the credentials are used once and
        never stored. Existing objects are left untouched.
      </Text>
      <TextInput
        label="Backend URL"
        description="This assistant as reachable from the iTop server"
        value={backendUrl}
        onChange={(e) => setBackendUrl(e.currentTarget.value)}
      />
      <SegmentedControl
        value={auth}
        onChange={(value) => setAuth(value as 'basic' | 'token')}
        data={[
          { label: 'Admin user + password', value: 'basic' },
          { label: 'Admin token', value: 'token' },
        ]}
      />
      {auth === 'basic' ? (
        <Group grow align="start">
          <TextInput label="Admin user" value={user} onChange={(e) => setUser(e.currentTarget.value)} />
          <PasswordInput
            label="Admin password"
            value={pwd}
            onChange={(e) => setPwd(e.currentTarget.value)}
          />
        </Group>
      ) : (
        <PasswordInput
          label="Admin token"
          value={token}
          onChange={(e) => setTokenValue(e.currentTarget.value)}
        />
      )}
      {report && (
        <Table withTableBorder verticalSpacing={4}>
          <Table.Tbody>
            {report.map((item) => (
              <Table.Tr key={`${item.class}:${item.name}`}>
                <Table.Td width={90}>
                  <Badge size="sm" color={PROVISION_STATUS_COLORS[item.status] ?? 'gray'}>
                    {item.status}
                  </Badge>
                </Table.Td>
                <Table.Td>
                  <Text size="sm">
                    {item.class} — {item.name}
                  </Text>
                </Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      )}
      <Group>
        <Button
          onClick={configure}
          loading={busy}
          disabled={!backendUrl || (auth === 'basic' ? !user || !pwd : !token)}
        >
          Configure iTop
        </Button>
      </Group>
    </Stack>
  );
}

function LlmForm() {
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const [baseUrl, setBaseUrl] = useState('');
  const [model, setModel] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [thinkTags, setThinkTags] = useState<string[]>([]);
  const [secrets, setSecrets] = useState<Record<string, boolean>>({});

  const load = async () => {
    const data = await apiGet<SectionData>('/setup/llm');
    setBaseUrl(String(data.values.base_url ?? ''));
    setModel(String(data.values.model ?? ''));
    setApiKey('');
    setThinkTags((data.values.think_tags as string[]) ?? []);
    setSecrets(data.secrets);
    setLoaded(true);
  };

  useEffect(() => {
    load().catch((e: Error) => setError(e.message));
  }, []);

  const body = () => {
    const b: Record<string, unknown> = {
      base_url: baseUrl,
      model: model || null,
      think_tags: thinkTags,
    };
    if (apiKey) b.api_key = apiKey;
    return b;
  };

  const test = async () => {
    setBusy(true);
    setError(null);
    setSuccess(null);
    try {
      const result = await apiSend<{ ok: boolean; response?: string; error?: string }>(
        'POST',
        '/setup/test-llm',
        body(),
      );
      if (result.ok) setSuccess(`Model responded: ${result.response}`);
      else setError(result.error ?? 'LLM test failed');
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const save = async () => {
    setBusy(true);
    setError(null);
    setSuccess(null);
    try {
      await apiSend<SectionData>('PATCH', '/setup/llm', body());
      await load();
      setSuccess('Saved');
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const clearApiKey = async () => {
    if (!window.confirm('Clear the stored API key?')) return;
    setError(null);
    setSuccess(null);
    try {
      await apiSend<SectionData>('PATCH', '/setup/llm', { api_key: null });
      await load();
      setSuccess('API key cleared');
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const reset = async () => {
    setError(null);
    setSuccess(null);
    try {
      if (!(await resetSection('llm'))) return;
      await load();
      setSuccess('Section reset to defaults');
    } catch (e) {
      setError((e as Error).message);
    }
  };

  if (!loaded) return error ? <Alert color="red">{error}</Alert> : <Loader />;

  return (
    <Stack maw={560}>
      <StatusAlert error={error} success={success} />
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
        placeholder={secretPlaceholder(secrets.api_key)}
        description={secrets.api_key ? undefined : 'Omit for local LM Studio'}
        value={apiKey}
        onChange={(e) => setApiKey(e.currentTarget.value)}
        rightSectionWidth={70}
        rightSection={
          secrets.api_key ? (
            <Button size="compact-xs" variant="subtle" color="red" onClick={clearApiKey}>
              Clear
            </Button>
          ) : null
        }
      />
      <TagsInput
        label="Think tags"
        description="Tag names stripped as inline reasoning blocks"
        value={thinkTags}
        onChange={setThinkTags}
      />
      <Group>
        <Button onClick={save} loading={busy}>
          Save
        </Button>
        <Button variant="default" onClick={test} loading={busy}>
          Test LLM
        </Button>
        <Button variant="subtle" color="red" onClick={reset}>
          Reset to defaults
        </Button>
      </Group>
    </Stack>
  );
}

function SecurityForm() {
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const [webhookToken, setWebhookToken] = useState('');
  const [adminToken, setAdminToken] = useState('');
  const [secrets, setSecrets] = useState<Record<string, boolean>>({});

  const load = async () => {
    const data = await apiGet<SectionData>('/setup/security');
    setWebhookToken('');
    setAdminToken('');
    setSecrets(data.secrets);
    setLoaded(true);
  };

  useEffect(() => {
    load().catch((e: Error) => setError(e.message));
  }, []);

  const save = async () => {
    const b: Record<string, unknown> = {};
    if (webhookToken) b.webhook_token = webhookToken;
    if (adminToken) b.admin_token = adminToken;
    if (Object.keys(b).length === 0) return;
    setBusy(true);
    setError(null);
    setSuccess(null);
    try {
      await apiSend<SectionData>('PATCH', '/setup/security', b);
      // The API is locked by the new admin token from this moment on —
      // store it right away so the very next request still passes.
      if (adminToken) setToken(adminToken);
      await load();
      setSuccess('Saved');
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const clear = async (field: 'webhook_token' | 'admin_token') => {
    const warning =
      field === 'admin_token'
        ? 'Clear the admin token? The admin API becomes open to anyone who can reach it.'
        : 'Clear the webhook token? /webhook will accept unauthenticated requests.';
    if (!window.confirm(warning)) return;
    setError(null);
    setSuccess(null);
    try {
      await apiSend<SectionData>('PATCH', '/setup/security', { [field]: null });
      await load();
      setSuccess(`${field} cleared`);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  if (!loaded) return error ? <Alert color="red">{error}</Alert> : <Loader />;

  return (
    <Stack maw={560}>
      <StatusAlert error={error} success={success} />
      <Text c="dimmed" size="sm">
        Tokens are write-only: the current values are never shown. Use the generate buttons to
        create a strong random token, then copy it before saving.
      </Text>
      <TokenField
        label="Webhook token"
        description="Set the same value in the iTop Remote Application Connection (X-Auth-Token header)"
        isSet={secrets.webhook_token}
        value={webhookToken}
        onChange={setWebhookToken}
        onClear={() => clear('webhook_token')}
      />
      <TokenField
        label="Admin token"
        description="Bearer token for this UI and the /api endpoints; saved to this browser automatically"
        isSet={secrets.admin_token}
        value={adminToken}
        onChange={setAdminToken}
        onClear={() => clear('admin_token')}
      />
      <Group>
        <Button onClick={save} loading={busy} disabled={!webhookToken && !adminToken}>
          Save
        </Button>
      </Group>
    </Stack>
  );
}

function TokenField(props: {
  label: string;
  description: string;
  isSet: boolean;
  value: string;
  onChange: (value: string) => void;
  onClear: () => void;
}) {
  // crypto.getRandomValues works on plain http too, unlike crypto.randomUUID.
  const generate = () => {
    const bytes = crypto.getRandomValues(new Uint8Array(32));
    props.onChange(Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join(''));
  };
  return (
    <Stack gap={4}>
      <TextInput
        label={props.label}
        description={props.description}
        placeholder={secretPlaceholder(props.isSet)}
        value={props.value}
        onChange={(e) => props.onChange(e.currentTarget.value)}
      />
      <Group gap="xs">
        <Button size="compact-xs" variant="default" onClick={generate}>
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
        {props.isSet && (
          <Button size="compact-xs" variant="subtle" color="red" onClick={props.onClear}>
            Clear
          </Button>
        )}
      </Group>
    </Stack>
  );
}

function TicketMappingForm() {
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [text, setText] = useState('');

  const load = async () => {
    const data = await apiGet<SectionData>('/setup/ticket_mapping');
    setText(JSON.stringify(data.values, null, 2));
    setLoaded(true);
  };

  useEffect(() => {
    load().catch((e: Error) => setError(e.message));
  }, []);

  const save = async () => {
    let parsed: unknown;
    try {
      parsed = JSON.parse(text);
    } catch {
      setError('Invalid JSON');
      setSuccess(null);
      return;
    }
    setBusy(true);
    setError(null);
    setSuccess(null);
    try {
      await apiSend<SectionData>('PATCH', '/setup/ticket_mapping', parsed);
      await load();
      setSuccess('Saved');
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const reset = async () => {
    setError(null);
    setSuccess(null);
    try {
      if (!(await resetSection('ticket_mapping'))) return;
      await load();
      setSuccess('Section reset to defaults');
    } catch (e) {
      setError((e as Error).message);
    }
  };

  if (!loaded) return error ? <Alert color="red">{error}</Alert> : <Loader />;

  return (
    <Stack maw={720}>
      <StatusAlert error={error} success={success} />
      <Text c="dimmed" size="sm">
        Maps semantic ticket fields onto the iTop datamodel: <code>fields</code> (semantic name →
        attribute code, null = attribute absent), <code>class_overrides</code> (per-class
        differences), <code>active_statuses</code> (when the assistant may act).
      </Text>
      <JsonInput
        value={text}
        onChange={setText}
        autosize
        minRows={12}
        formatOnBlur
        validationError="Invalid JSON"
      />
      <Group>
        <Button onClick={save} loading={busy}>
          Save
        </Button>
        <Button variant="subtle" color="red" onClick={reset}>
          Reset to defaults
        </Button>
      </Group>
    </Stack>
  );
}
