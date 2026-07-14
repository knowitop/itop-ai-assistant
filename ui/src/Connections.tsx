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
import { Trans, useTranslation } from 'react-i18next';

import { apiGet, apiSend, setToken } from './api';

// GET /api/setup/{section} returns non-secret values plus is-set flags for
// secrets; secret values never leave the server.
interface SectionData {
  values: Record<string, unknown>;
  secrets: Record<string, boolean>;
}

async function resetSection(section: string, confirmMsg: string): Promise<boolean> {
  if (!window.confirm(confirmMsg)) return false;
  await apiSend('DELETE', `/setup/${section}`);
  return true;
}

export default function Connections() {
  const { t } = useTranslation();
  return (
    <Stack>
      <Title order={2}>{t('connections.title')}</Title>
      <Tabs defaultValue="itop" keepMounted={false}>
        <Tabs.List>
          <Tabs.Tab value="itop">{t('connections.tab_itop')}</Tabs.Tab>
          <Tabs.Tab value="llm">{t('connections.tab_llm')}</Tabs.Tab>
          <Tabs.Tab value="embeddings">{t('connections.tab_embeddings')}</Tabs.Tab>
          <Tabs.Tab value="security">{t('connections.tab_security')}</Tabs.Tab>
          <Tabs.Tab value="ticket_mapping">{t('connections.tab_ticket_mapping')}</Tabs.Tab>
        </Tabs.List>
        <Tabs.Panel value="itop" pt="md">
          <ItopForm />
          <Divider my="lg" maw={560} />
          <ItopWebhooksForm />
        </Tabs.Panel>
        <Tabs.Panel value="llm" pt="md">
          <LlmForm />
        </Tabs.Panel>
        <Tabs.Panel value="embeddings" pt="md">
          <EmbeddingsForm />
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

function ItopForm() {
  const { t } = useTranslation();
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
      if (result.ok)
        setSuccess(t('common.conn_test_ok', { account: result.ai_person }));
      else setError(result.error ?? t('common.error_conn_failed'));
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
      setSuccess(t('common.saved'));
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
      if (!(await resetSection('itop', t('connections.reset_confirm', { section: 'itop' })))) return;
      await load();
      setSuccess(t('common.section_reset'));
    } catch (e) {
      setError((e as Error).message);
    }
  };

  if (!loaded) return error ? <Alert color="red">{error}</Alert> : <Loader />;

  return (
    <Stack maw={560}>
      <StatusAlert error={error} success={success} />
      <TextInput
        label={t('common.field_rest_api_url')}
        placeholder="http://itop.example.com/webservices/rest.php"
        value={url}
        onChange={(e) => setUrl(e.currentTarget.value)}
      />
      <Group grow>
        <TextInput
          label={t('common.field_api_version')}
          value={apiVersion}
          onChange={(e) => setApiVersion(e.currentTarget.value)}
        />
        <NumberInput
          label={t('common.field_timeout_seconds')}
          min={1}
          value={timeout_}
          onChange={setTimeout_}
        />
      </Group>
      <SegmentedControl
        value={auth}
        onChange={(value) => setAuth(value as 'basic' | 'token')}
        data={[
          { label: t('common.auth_user_password'), value: 'basic' },
          { label: t('common.auth_token'), value: 'token' },
        ]}
      />
      {auth === 'basic' ? (
        <Group grow align="start">
          <TextInput
            label={t('common.field_user')}
            value={user}
            onChange={(e) => setUser(e.currentTarget.value)}
          />
          <PasswordInput
            label={t('common.field_password')}
            placeholder={secrets.pwd ? t('common.secret_is_set') : t('common.secret_not_set')}
            value={pwd}
            onChange={(e) => setPwd(e.currentTarget.value)}
          />
        </Group>
      ) : (
        <PasswordInput
          label={t('common.field_token')}
          placeholder={secrets.token ? t('common.secret_is_set') : t('common.secret_not_set')}
          value={token}
          onChange={(e) => setTokenValue(e.currentTarget.value)}
        />
      )}
      <Group>
        <Button onClick={save} loading={busy}>
          {t('common.btn_save')}
        </Button>
        <Button variant="default" onClick={test} loading={busy}>
          {t('common.btn_test_connection')}
        </Button>
        <Button variant="subtle" color="red" onClick={reset}>
          {t('common.btn_reset_defaults')}
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
  const { t } = useTranslation();
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
      else setError(result.error ?? t('common.error_provisioning_failed'));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Stack maw={560}>
      <Title order={4}>{t('connections.webhooks_title')}</Title>
      {error && (
        <Alert color="red" style={{ whiteSpace: 'pre-wrap' }}>
          {error}
        </Alert>
      )}
      <Text c="dimmed" size="sm">
        <Trans i18nKey="connections.webhooks_desc" components={{ b: <b /> }} />
      </Text>
      <TextInput
        label={t('common.field_backend_url')}
        description={t('common.field_backend_url_desc')}
        value={backendUrl}
        onChange={(e) => setBackendUrl(e.currentTarget.value)}
      />
      <SegmentedControl
        value={auth}
        onChange={(value) => setAuth(value as 'basic' | 'token')}
        data={[
          { label: t('common.auth_admin_user_password'), value: 'basic' },
          { label: t('common.auth_admin_token'), value: 'token' },
        ]}
      />
      {auth === 'basic' ? (
        <Group grow align="start">
          <TextInput
            label={t('common.field_admin_user')}
            value={user}
            onChange={(e) => setUser(e.currentTarget.value)}
          />
          <PasswordInput
            label={t('common.field_admin_password')}
            value={pwd}
            onChange={(e) => setPwd(e.currentTarget.value)}
          />
        </Group>
      ) : (
        <PasswordInput
          label={t('common.field_admin_token')}
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
          {t('common.btn_configure_itop')}
        </Button>
      </Group>
    </Stack>
  );
}

function LlmForm() {
  const { t } = useTranslation();
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
      if (result.ok) setSuccess(t('common.llm_test_ok', { response: result.response }));
      else setError(result.error ?? t('common.error_llm_failed'));
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
      setSuccess(t('common.saved'));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const clearApiKey = async () => {
    if (!window.confirm(t('connections.api_key_clear_confirm'))) return;
    setError(null);
    setSuccess(null);
    try {
      await apiSend<SectionData>('PATCH', '/setup/llm', { api_key: null });
      await load();
      setSuccess(t('connections.api_key_cleared'));
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const reset = async () => {
    setError(null);
    setSuccess(null);
    try {
      if (!(await resetSection('llm', t('connections.reset_confirm', { section: 'llm' })))) return;
      await load();
      setSuccess(t('common.section_reset'));
    } catch (e) {
      setError((e as Error).message);
    }
  };

  if (!loaded) return error ? <Alert color="red">{error}</Alert> : <Loader />;

  return (
    <Stack maw={560}>
      <StatusAlert error={error} success={success} />
      <TextInput
        label={t('common.field_base_url')}
        description={t('connections.llm_base_url_desc')}
        placeholder="http://localhost:1234/v1"
        value={baseUrl}
        onChange={(e) => setBaseUrl(e.currentTarget.value)}
      />
      <TextInput
        label={t('common.field_model')}
        description={t('connections.llm_model_desc')}
        value={model}
        onChange={(e) => setModel(e.currentTarget.value)}
      />
      <PasswordInput
        label={t('common.field_api_key')}
        placeholder={secrets.api_key ? t('common.secret_is_set') : t('common.secret_not_set')}
        description={secrets.api_key ? undefined : t('connections.llm_api_key_desc')}
        value={apiKey}
        onChange={(e) => setApiKey(e.currentTarget.value)}
        rightSectionWidth={70}
        rightSection={
          secrets.api_key ? (
            <Button size="compact-xs" variant="subtle" color="red" onClick={clearApiKey}>
              {t('common.btn_clear')}
            </Button>
          ) : null
        }
      />
      <TagsInput
        label={t('common.field_think_tags')}
        description={t('connections.llm_think_tags_desc')}
        value={thinkTags}
        onChange={setThinkTags}
      />
      <Group>
        <Button onClick={save} loading={busy}>
          {t('common.btn_save')}
        </Button>
        <Button variant="default" onClick={test} loading={busy}>
          {t('common.btn_test_llm')}
        </Button>
        <Button variant="subtle" color="red" onClick={reset}>
          {t('common.btn_reset_defaults')}
        </Button>
      </Group>
    </Stack>
  );
}

// Deliberate clone of LlmForm for the embeddings endpoint (same section
// shape: base_url/model/api_key plus numeric tuning fields).
function EmbeddingsForm() {
  const { t } = useTranslation();
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const [baseUrl, setBaseUrl] = useState('');
  const [model, setModel] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [dimension, setDimension] = useState<number | string>('');
  const [batchSize, setBatchSize] = useState<number | string>('');
  const [timeout_, setTimeout_] = useState<number | string>('');
  const [secrets, setSecrets] = useState<Record<string, boolean>>({});

  const load = async () => {
    const data = await apiGet<SectionData>('/setup/embeddings');
    setBaseUrl(String(data.values.base_url ?? ''));
    setModel(String(data.values.model ?? ''));
    setApiKey('');
    setDimension((data.values.dimension as number) ?? '');
    setBatchSize((data.values.batch_size as number) ?? '');
    setTimeout_((data.values.timeout as number) ?? '');
    setSecrets(data.secrets);
    setLoaded(true);
  };

  useEffect(() => {
    load().catch((e: Error) => setError(e.message));
  }, []);

  // Empty numeric fields are omitted: PATCH-merge keeps the stored value.
  const body = () => {
    const b: Record<string, unknown> = { base_url: baseUrl, model: model || null };
    if (apiKey) b.api_key = apiKey;
    if (dimension !== '') b.dimension = Number(dimension);
    if (batchSize !== '') b.batch_size = Number(batchSize);
    if (timeout_ !== '') b.timeout = Number(timeout_);
    return b;
  };

  const test = async () => {
    setBusy(true);
    setError(null);
    setSuccess(null);
    try {
      const result = await apiSend<{
        ok: boolean;
        model?: string;
        dimension?: number;
        dimension_match?: boolean;
        error?: string;
      }>('POST', '/setup/test-embeddings', body());
      if (result.ok && result.dimension_match)
        setSuccess(
          t('connections.embeddings_test_ok', { model: result.model, dimension: result.dimension }),
        );
      else if (result.ok)
        // A mismatched dimension is a config error, not a success: the index
        // would reject these vectors at write time.
        setError(
          t('connections.embeddings_dimension_mismatch', {
            actual: result.dimension,
            expected: dimension,
          }),
        );
      else setError(result.error ?? t('common.error_embeddings_failed'));
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
      await apiSend<SectionData>('PATCH', '/setup/embeddings', body());
      await load();
      setSuccess(t('common.saved'));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const clearApiKey = async () => {
    if (!window.confirm(t('connections.api_key_clear_confirm'))) return;
    setError(null);
    setSuccess(null);
    try {
      await apiSend<SectionData>('PATCH', '/setup/embeddings', { api_key: null });
      await load();
      setSuccess(t('connections.api_key_cleared'));
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const reset = async () => {
    setError(null);
    setSuccess(null);
    try {
      if (
        !(await resetSection('embeddings', t('connections.reset_confirm', { section: 'embeddings' })))
      )
        return;
      await load();
      setSuccess(t('common.section_reset'));
    } catch (e) {
      setError((e as Error).message);
    }
  };

  if (!loaded) return error ? <Alert color="red">{error}</Alert> : <Loader />;

  return (
    <Stack maw={560}>
      <StatusAlert error={error} success={success} />
      <TextInput
        label={t('common.field_base_url')}
        description={t('connections.embeddings_base_url_desc')}
        placeholder="http://localhost:1234/v1"
        value={baseUrl}
        onChange={(e) => setBaseUrl(e.currentTarget.value)}
      />
      <TextInput
        label={t('common.field_model')}
        description={t('connections.embeddings_model_desc')}
        placeholder="bge-m3"
        value={model}
        onChange={(e) => setModel(e.currentTarget.value)}
      />
      <PasswordInput
        label={t('common.field_api_key')}
        placeholder={secrets.api_key ? t('common.secret_is_set') : t('common.secret_not_set')}
        description={secrets.api_key ? undefined : t('connections.llm_api_key_desc')}
        value={apiKey}
        onChange={(e) => setApiKey(e.currentTarget.value)}
        rightSectionWidth={70}
        rightSection={
          secrets.api_key ? (
            <Button size="compact-xs" variant="subtle" color="red" onClick={clearApiKey}>
              {t('common.btn_clear')}
            </Button>
          ) : null
        }
      />
      <Group grow>
        <NumberInput
          label={t('common.field_dimension')}
          min={1}
          max={4000}
          value={dimension}
          onChange={setDimension}
        />
        <NumberInput
          label={t('common.field_batch_size')}
          min={1}
          value={batchSize}
          onChange={setBatchSize}
        />
        <NumberInput
          label={t('common.field_timeout_seconds')}
          min={1}
          value={timeout_}
          onChange={setTimeout_}
        />
      </Group>
      <Group>
        <Button onClick={save} loading={busy}>
          {t('common.btn_save')}
        </Button>
        <Button variant="default" onClick={test} loading={busy}>
          {t('common.btn_test_embeddings')}
        </Button>
        <Button variant="subtle" color="red" onClick={reset}>
          {t('common.btn_reset_defaults')}
        </Button>
      </Group>
    </Stack>
  );
}

function SecurityForm() {
  const { t } = useTranslation();
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
      setSuccess(t('common.saved'));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const clear = async (field: 'webhook_token' | 'admin_token') => {
    const warning =
      field === 'admin_token'
        ? t('connections.clear_admin_token_confirm')
        : t('connections.clear_webhook_token_confirm');
    if (!window.confirm(warning)) return;
    setError(null);
    setSuccess(null);
    try {
      await apiSend<SectionData>('PATCH', '/setup/security', { [field]: null });
      await load();
      setSuccess(t(`connections.${field}_cleared`));
    } catch (e) {
      setError((e as Error).message);
    }
  };

  if (!loaded) return error ? <Alert color="red">{error}</Alert> : <Loader />;

  return (
    <Stack maw={560}>
      <StatusAlert error={error} success={success} />
      <Text c="dimmed" size="sm">
        {t('connections.security_desc')}
      </Text>
      <TokenField
        label={t('common.field_webhook_token')}
        description={t('connections.security_webhook_token_desc')}
        isSet={secrets.webhook_token}
        value={webhookToken}
        onChange={setWebhookToken}
        onClear={() => clear('webhook_token')}
      />
      <TokenField
        label={t('common.field_admin_token')}
        description={t('connections.security_admin_token_desc')}
        isSet={secrets.admin_token}
        value={adminToken}
        onChange={setAdminToken}
        onClear={() => clear('admin_token')}
      />
      <Group>
        <Button onClick={save} loading={busy} disabled={!webhookToken && !adminToken}>
          {t('common.btn_save')}
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
  const { t } = useTranslation();
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
        placeholder={props.isSet ? t('common.secret_is_set') : t('common.secret_not_set')}
        value={props.value}
        onChange={(e) => props.onChange(e.currentTarget.value)}
      />
      <Group gap="xs">
        <Button size="compact-xs" variant="default" onClick={generate}>
          {t('common.btn_generate')}
        </Button>
        {props.value && navigator.clipboard && (
          <Button
            size="compact-xs"
            variant="default"
            onClick={() => navigator.clipboard.writeText(props.value)}
          >
            {t('common.btn_copy')}
          </Button>
        )}
        {props.isSet && (
          <Button size="compact-xs" variant="subtle" color="red" onClick={props.onClear}>
            {t('common.btn_clear')}
          </Button>
        )}
      </Group>
    </Stack>
  );
}

function TicketMappingForm() {
  const { t } = useTranslation();
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
      setError(t('common.invalid_json'));
      setSuccess(null);
      return;
    }
    setBusy(true);
    setError(null);
    setSuccess(null);
    try {
      await apiSend<SectionData>('PATCH', '/setup/ticket_mapping', parsed);
      await load();
      setSuccess(t('common.saved'));
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
      if (
        !(await resetSection(
          'ticket_mapping',
          t('connections.reset_confirm', { section: 'ticket_mapping' }),
        ))
      )
        return;
      await load();
      setSuccess(t('common.section_reset'));
    } catch (e) {
      setError((e as Error).message);
    }
  };

  if (!loaded) return error ? <Alert color="red">{error}</Alert> : <Loader />;

  return (
    <Stack maw={720}>
      <StatusAlert error={error} success={success} />
      <Text c="dimmed" size="sm">
        <Trans i18nKey="connections.ticket_mapping_desc" components={{ code: <code /> }} />
      </Text>
      <JsonInput
        value={text}
        onChange={setText}
        autosize
        minRows={12}
        formatOnBlur
        validationError={t('common.invalid_json')}
      />
      <Group>
        <Button onClick={save} loading={busy}>
          {t('common.btn_save')}
        </Button>
        <Button variant="subtle" color="red" onClick={reset}>
          {t('common.btn_reset_defaults')}
        </Button>
      </Group>
    </Stack>
  );
}
