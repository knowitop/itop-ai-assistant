import {
  Alert,
  Badge,
  Button,
  Checkbox,
  Divider,
  Group,
  JsonInput,
  Loader,
  Modal,
  NumberInput,
  PasswordInput,
  SegmentedControl,
  Select,
  Stack,
  Switch,
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

// GET /api/setup/llm-providers — what each kind of endpoint needs. The form
// is rendered from this instead of hardcoding the provider list here.
export interface LlmProvider {
  id: string;
  label: string;
  base_url_mode: 'required' | 'optional' | 'unused';
  base_url_placeholder: string | null;
  api_key_mode: 'required' | 'optional' | 'unused';
  // null = depends on the server behind the URL, so ask the user
  supports_forced_tool_choice: boolean | null;
  notes: string;
}

export async function loadLlmProviders(): Promise<LlmProvider[]> {
  const data = await apiGet<{ providers: LlmProvider[] }>('/setup/llm-providers');
  return data.providers;
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
          <Tabs.Tab value="mapping">{t('connections.tab_mapping')}</Tabs.Tab>
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
        <Tabs.Panel value="mapping" pt="md">
          <MappingForm />
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
    <Stack maw={720}>
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
    <Stack maw={720}>
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

  const [providers, setProviders] = useState<LlmProvider[]>([]);
  const [provider, setProvider] = useState('openai_compatible');
  const [baseUrl, setBaseUrl] = useState('');
  const [model, setModel] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [thinkTags, setThinkTags] = useState<string[]>([]);
  const [params, setParams] = useState('{}');
  const [forceToolChoice, setForceToolChoice] = useState(false);
  const [secrets, setSecrets] = useState<Record<string, boolean>>({});

  const current = providers.find((p) => p.id === provider);

  const load = async () => {
    const [list, data] = await Promise.all([loadLlmProviders(), apiGet<SectionData>('/setup/llm')]);
    setProviders(list);
    setProvider(String(data.values.provider ?? 'openai_compatible'));
    setBaseUrl(String(data.values.base_url ?? ''));
    setModel(String(data.values.model ?? ''));
    setApiKey('');
    setThinkTags((data.values.think_tags as string[]) ?? []);
    setParams(JSON.stringify(data.values.params ?? {}, null, 2));
    setForceToolChoice(data.values.supports_forced_tool_choice === true);
    setSecrets(data.secrets);
    setLoaded(true);
  };

  useEffect(() => {
    load().catch((e: Error) => setError(e.message));
  }, []);

  // Throws on malformed JSON so save/test surface it instead of silently
  // dropping what the user typed
  const body = () => {
    const b: Record<string, unknown> = {
      provider,
      base_url: baseUrl,
      model: model || null,
      think_tags: thinkTags,
      params: JSON.parse(params || '{}'),
      // Only asked where the provider has no answer of its own; null there
      // means "use the provider's"
      supports_forced_tool_choice:
        current && current.supports_forced_tool_choice === null ? forceToolChoice : null,
    };
    if (apiKey) b.api_key = apiKey;
    return b;
  };

  const test = async () => {
    setBusy(true);
    setError(null);
    setSuccess(null);
    try {
      const result = await apiSend<{
        ok: boolean;
        response?: string;
        error?: string;
        tool_calling?: boolean;
        tool_error?: string;
        forced_tool_choice_ok?: boolean;
      }>('POST', '/setup/test-llm', body());
      if (!result.ok) setError(result.error ?? t('common.error_llm_failed'));
      else if (result.forced_tool_choice_ok === false)
        setError(t('connections.llm_tool_choice_rejected', { error: result.tool_error }));
      else if (!result.tool_calling)
        setError(t('connections.llm_no_tool_calling', { error: result.tool_error ?? '' }));
      else setSuccess(t('common.llm_test_ok', { response: result.response }));
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
    <Stack maw={720}>
      <StatusAlert error={error} success={success} />
      <Select
        label={t('common.field_provider')}
        description={current?.notes || t('connections.llm_provider_desc')}
        data={providers.map((p) => ({ value: p.id, label: p.label }))}
        value={provider}
        onChange={(value) => setProvider(value ?? 'openai_compatible')}
        allowDeselect={false}
      />
      {current?.base_url_mode !== 'unused' && (
        <TextInput
          label={t('common.field_base_url')}
          description={t('connections.llm_base_url_desc')}
          placeholder={current?.base_url_placeholder ?? ''}
          value={baseUrl}
          onChange={(e) => setBaseUrl(e.currentTarget.value)}
        />
      )}
      <TextInput
        label={t('common.field_model')}
        description={t('connections.llm_model_desc')}
        value={model}
        onChange={(e) => setModel(e.currentTarget.value)}
      />
      {current?.api_key_mode !== 'unused' && (
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
      )}
      <TagsInput
        label={t('common.field_think_tags')}
        description={t('connections.llm_think_tags_desc')}
        value={thinkTags}
        onChange={setThinkTags}
      />
      <JsonInput
        label={t('common.field_llm_params')}
        description={t('connections.llm_params_desc')}
        placeholder='{"temperature": 0.2}'
        validationError={t('connections.llm_params_invalid')}
        formatOnBlur
        autosize
        minRows={2}
        value={params}
        onChange={setParams}
      />
      {/* Only where the provider itself cannot answer — see llm_providers */}
      {current?.supports_forced_tool_choice === null && (
        <Switch
          label={t('connections.llm_tool_choice_label')}
          description={t('connections.llm_tool_choice_desc')}
          checked={forceToolChoice}
          onChange={(e) => setForceToolChoice(e.currentTarget.checked)}
        />
      )}
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

// GET /api/vector/status, cut down to what a fingerprint change costs: which
// families hold an index that a different model or dimension would invalidate.
interface IndexedFamilies {
  enabled: boolean;
  index: { family: string; enabled: boolean; active_version: number | null }[] | null;
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
  // The saved fingerprint, kept apart from the edited fields: what the
  // rebuild warning compares against.
  const [saved, setSaved] = useState<{ model: string; dimension: number | null }>({
    model: '',
    dimension: null,
  });
  const [indexed, setIndexed] = useState<string[]>([]);

  const load = async () => {
    const data = await apiGet<SectionData>('/setup/embeddings');
    setBaseUrl(String(data.values.base_url ?? ''));
    setModel(String(data.values.model ?? ''));
    setApiKey('');
    setDimension((data.values.dimension as number) ?? '');
    setBatchSize((data.values.batch_size as number) ?? '');
    setTimeout_((data.values.timeout as number) ?? '');
    setSecrets(data.secrets);
    setSaved({
      model: String(data.values.model ?? ''),
      dimension: (data.values.dimension as number) ?? null,
    });
    // Nothing indexed, indexing off, or a store that cannot answer — the
    // warning stays silent rather than guessing: changing the model then
    // costs nothing to undo.
    const vector = await apiGet<IndexedFamilies>('/vector/status').catch(() => null);
    setIndexed(
      vector?.enabled
        ? (vector.index ?? [])
            .filter((f) => f.enabled && f.active_version !== null)
            .map((f) => f.family)
        : [],
    );
    setLoaded(true);
  };

  // A changed model or dimension is a changed index fingerprint: the sweep
  // cannot mix vectors of two models, so it fills a replacement collection
  // from scratch and searches over the family are refused until it is done.
  const rebuildFamilies =
    model !== saved.model || (dimension !== '' && Number(dimension) !== saved.dimension)
      ? indexed
      : [];

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
    if (
      rebuildFamilies.length > 0 &&
      !window.confirm(
        t('connections.embeddings_rebuild_confirm', { families: rebuildFamilies.join(', ') }),
      )
    )
      return;
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
    <Stack maw={720}>
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
      {rebuildFamilies.length > 0 && (
        <Alert color="orange" title={t('connections.embeddings_rebuild_title')}>
          {t('connections.embeddings_rebuild_warning', { families: rebuildFamilies.join(', ') })}
        </Alert>
      )}
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
    <Stack maw={720}>
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

// One semantic field of one family, as GET /setup/mappings/fields describes
// it. The families and their fields come from the backend declarations, never
// from a list kept here: a field added to a schema must render without
// touching this file (ADR-025).
interface MappingField {
  name: string;
  description: string;
  default: string | null;
  kind: string;
  multi: boolean;
  roles: string[];
  // True when this deployment declared the field rather than the code.
  declared: boolean;
}

// GET /setup/mappings/vocabulary — what a field declared here may be, and the
// rules joining the two halves of that. Kept out of TypeScript for the same
// reason the fields are: a role added to the domain has to reach the form
// without an edit here, and a rule copied into it would drift from the one
// the server enforces.
interface Vocabulary {
  kinds: { name: string; declarable: boolean }[];
  roles: { name: string; requires_kind: string; singular: boolean }[];
}

// A field this deployment added, as the section stores it. No attribute code:
// that lives in `fields` beside every other field's, which is the whole of
// "a field an administrator declares is a field like any other" (ADR-034).
interface DeclaredSpec {
  kind: string;
  multi: boolean;
  roles: string[];
  description: string;
}

// What the form holds for one family: the attribute code per semantic field
// (null = no such attribute in this datamodel), the fields this deployment
// declared, and the per-class overrides, still edited as raw JSON.
interface FamilyMapping {
  fields: Record<string, string | null>;
  declared: Record<string, DeclaredSpec>;
  overrides: string;
}

interface StoredMapping {
  fields?: Record<string, string | null>;
  class_overrides?: Record<string, Record<string, string | null>>;
  declared?: Record<string, DeclaredSpec>;
}

// The field the modal is editing. `original` is the name it is stored under,
// null while it is new — a rename is a delete and an add, so the entry it
// replaces has to be found again on apply. `source` is its attribute code,
// asked here so that adding a field is one step rather than "add it, then
// find your own row in the table", and required: a field of one's own has no
// default to fall back on, so one naming no attribute reads nothing at all.
// Unmapping it later is still possible from its row, where it is a deliberate
// act rather than an unfinished declaration.
interface FieldDraft {
  family: string;
  original: string | null;
  name: string;
  spec: DeclaredSpec;
  source: string;
}

// A class override may only name a field of the family, so one left behind on
// a removed field makes the whole section refuse to save. Text that is not
// valid JSON is returned untouched: the save reports that on its own.
function withoutField(overrides: string, name: string): string {
  try {
    const parsed = JSON.parse(overrides) as Record<string, Record<string, unknown>>;
    const stripped = Object.entries(parsed).map(([klass, fields]) => [
      klass,
      Object.fromEntries(Object.entries(fields).filter(([field]) => field !== name)),
    ]);
    return JSON.stringify(Object.fromEntries(stripped), null, 2);
  } catch {
    return overrides;
  }
}

// The rows of one family: what the backend declares, with what this form has
// staged on top. A field added here has no declaration of its own until the
// section is saved and the fields are read again, so until then the form is
// the one that knows about it.
function rowsOf(fields: MappingField[], mapping: FamilyMapping | undefined): MappingField[] {
  const local = mapping?.declared ?? {};
  const rows = fields.map((field) => (field.name in local ? { ...field, ...local[field.name] } : field));
  const known = new Set(fields.map((field) => field.name));
  for (const [name, spec] of Object.entries(local)) {
    if (!known.has(name)) rows.push({ name, default: null, declared: true, ...spec });
  }
  return rows;
}

// Semantic field → iTop attribute code, one table per object family. One form
// over one section: a family is a declaration on the backend, so adding one
// adds a table here and nothing else. Field names are identifiers on both
// sides — our semantics on the left, an iTop attribute code on the right — so
// they are shown as they are, and only the declaration's own description is
// translated text.
function MappingForm() {
  const { t } = useTranslation();
  const [families, setFamilies] = useState<Record<string, MappingField[]> | null>(null);
  const [vocab, setVocab] = useState<Vocabulary | null>(null);
  const [mappings, setMappings] = useState<Record<string, FamilyMapping>>({});
  const [draft, setDraft] = useState<FieldDraft | null>(null);
  const [draftError, setDraftError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = async () => {
    const [fields, vocabulary, data] = await Promise.all([
      apiGet<Record<string, MappingField[]>>('/setup/mappings/fields'),
      apiGet<Vocabulary>('/setup/mappings/vocabulary'),
      apiGet<SectionData>('/setup/mappings'),
    ]);
    if (Object.keys(fields).length === 0) throw new Error('No object families are declared');
    const stored = (data.values.families ?? {}) as Record<string, StoredMapping>;
    const next: Record<string, FamilyMapping> = {};
    for (const [family, list] of Object.entries(fields)) {
      const saved = stored[family]?.fields ?? {};
      const row: Record<string, string | null> = {};
      // A field the section says nothing about is mapped as the declaration
      // has it — seeding from `null` would show every unedited field as absent.
      for (const field of list) {
        row[field.name] = field.name in saved ? saved[field.name] : field.default;
      }
      next[family] = {
        fields: row,
        declared: stored[family]?.declared ?? {},
        overrides: JSON.stringify(stored[family]?.class_overrides ?? {}, null, 2),
      };
    }
    setFamilies(fields);
    setVocab(vocabulary);
    setMappings(next);
  };

  useEffect(() => {
    load().catch((e: Error) => setError(e.message));
  }, []);

  const setField = (family: string, name: string, value: string | null) => {
    setSuccess(null);
    setMappings((current) => ({
      ...current,
      [family]: { ...current[family], fields: { ...current[family].fields, [name]: value } },
    }));
  };

  const setOverrides = (family: string, value: string) => {
    setSuccess(null);
    setMappings((current) => ({ ...current, [family]: { ...current[family], overrides: value } }));
  };

  const addField = (family: string) => {
    setDraftError(null);
    setDraft({
      family,
      original: null,
      name: '',
      spec: {
        kind: vocab?.kinds.find((kind) => kind.declarable)?.name ?? '',
        multi: false,
        roles: [],
        description: '',
      },
      source: '',
    });
  };

  const editField = (family: string, field: MappingField) => {
    const code = mappings[family]?.fields[field.name];
    setDraftError(null);
    setDraft({
      family,
      original: field.name,
      name: field.name,
      spec: { kind: field.kind, multi: field.multi, roles: field.roles, description: field.description },
      source: code ?? '',
    });
  };

  const removeField = (family: string, name: string) => {
    if (!window.confirm(t('connections.mapping_remove_confirm', { name }))) return;
    setSuccess(null);
    setMappings((current) => {
      const mapping = current[family];
      const declared = { ...mapping.declared };
      const fields = { ...mapping.fields };
      delete declared[name];
      delete fields[name];
      return { ...current, [family]: { declared, fields, overrides: withoutField(mapping.overrides, name) } };
    });
  };

  // What the draft may say it is, given the rest of the family. A kind that
  // cannot be declared is not offered, a role that kind cannot carry is
  // disabled with its reason, and so is a role the family already has a field
  // for — the same three rules the server enforces, shown before the 422.
  const kinds = (vocab?.kinds ?? []).filter((kind) => kind.declarable);
  const roles = (vocab?.roles ?? []).filter((role) => kinds.some((kind) => kind.name === role.requires_kind));
  const roleBlocker = (role: Vocabulary['roles'][number]): string | null => {
    if (!draft) return null;
    if (role.requires_kind !== draft.spec.kind) {
      return t('connections.mapping_role_needs_kind', { kind: role.requires_kind });
    }
    if (!role.singular) return null;
    const carrier = rowsOf(families?.[draft.family] ?? [], mappings[draft.family]).find(
      (row) => row.name !== draft.original && row.roles.includes(role.name),
    );
    return carrier ? t('connections.mapping_role_taken', { field: carrier.name }) : null;
  };

  const setKind = (kind: string) => {
    if (!draft) return;
    // A role states what the value is, so it cannot survive a change of what
    // the value is read as — the ones that no longer fit go with it.
    const fits = roles.filter((role) => role.requires_kind === kind).map((role) => role.name);
    const kept = draft.spec.roles.filter((role) => fits.includes(role));
    setDraft({ ...draft, spec: { ...draft.spec, kind, roles: kept } });
  };

  const applyDraft = () => {
    if (!draft) return;
    const name = draft.name.trim();
    if (!name) {
      setDraftError(t('connections.mapping_name_required'));
      return;
    }
    const rows = rowsOf(families?.[draft.family] ?? [], mappings[draft.family]);
    if (rows.some((row) => row.name === name && row.name !== draft.original)) {
      setDraftError(t('connections.mapping_name_taken'));
      return;
    }
    const source = draft.source.trim();
    if (!source) {
      setDraftError(t('connections.mapping_attribute_required'));
      return;
    }
    setSuccess(null);
    setMappings((current) => {
      const mapping = current[draft.family];
      const declared = { ...mapping.declared };
      const fields = { ...mapping.fields };
      let overrides = mapping.overrides;
      if (draft.original && draft.original !== name) {
        delete declared[draft.original];
        delete fields[draft.original];
        overrides = withoutField(overrides, draft.original);
      }
      declared[name] = { ...draft.spec, description: draft.spec.description.trim() };
      fields[name] = source;
      return { ...current, [draft.family]: { declared, fields, overrides } };
    });
    setDraft(null);
    setDraftError(null);
  };

  const save = async () => {
    // `families` goes as a whole object: the setup API merges a PATCH body
    // over the current config at the top level only, so a partial body would
    // reset every family it omits.
    const payload: Record<string, unknown> = {};
    for (const [family, mapping] of Object.entries(mappings)) {
      const mapped: Record<string, string | null> = {};
      for (const [name, value] of Object.entries(mapping.fields)) {
        // An empty input is no attribute code either, so it means the same as
        // the switch — never an empty string, which no datamodel could match.
        mapped[name] = value === null || value.trim() === '' ? null : value.trim();
      }
      let overrides: unknown;
      try {
        overrides = JSON.parse(mapping.overrides);
      } catch {
        setError(`${family}: ${t('common.invalid_json')}`);
        setSuccess(null);
        return;
      }
      payload[family] = { fields: mapped, class_overrides: overrides, declared: mapping.declared };
    }
    setBusy(true);
    setError(null);
    setSuccess(null);
    try {
      await apiSend<SectionData>('PATCH', '/setup/mappings', { families: payload });
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
      if (!(await resetSection('mappings', t('connections.reset_confirm', { section: 'mappings' })))) return;
      await load();
      setSuccess(t('common.section_reset'));
    } catch (e) {
      setError((e as Error).message);
    }
  };

  if (!families || !vocab) return error ? <Alert color="red">{error}</Alert> : <Loader />;

  const multiLabel = t('connections.mapping_multi_short');

  return (
    <Stack maw={780}>
      <Text c="dimmed" size="sm">
        {t('connections.mapping_desc')}
      </Text>
      <StatusAlert error={error} success={success} />
      {Object.entries(families).map(([family, fields]) => (
        <Stack key={family} gap="xs">
          <Title order={4}>
            <code>{family}</code>
          </Title>
          <Table verticalSpacing="xs">
            <Table.Thead>
              <Table.Tr>
                <Table.Th>{t('connections.mapping_field')}</Table.Th>
                <Table.Th>{t('connections.mapping_attribute')}</Table.Th>
                <Table.Th>{t('connections.mapping_absent')}</Table.Th>
                <Table.Th />
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {rowsOf(fields, mappings[family]).map((field) => (
                <Table.Tr key={field.name}>
                  <Table.Td>
                    <code>{field.name}</code>
                    {field.declared && (
                      <Text span size="xs" c="dimmed">
                        {' '}
                        ({t('connections.mapping_declared_here')})
                      </Text>
                    )}
                    {field.description && (
                      <Text size="xs" c="dimmed">
                        {field.description}
                      </Text>
                    )}
                    {field.declared && (
                      <Text size="xs" c="dimmed">
                        {[field.kind, ...(field.multi ? [multiLabel] : []), ...field.roles].join(' · ')}
                      </Text>
                    )}
                  </Table.Td>
                  <Table.Td>
                    <TextInput
                      value={mappings[family]?.fields[field.name] ?? ''}
                      disabled={mappings[family]?.fields[field.name] === null}
                      placeholder={field.default ?? ''}
                      aria-label={`${family}.${field.name}`}
                      onChange={(e) => setField(family, field.name, e.currentTarget.value)}
                    />
                  </Table.Td>
                  <Table.Td>
                    <Switch
                      checked={mappings[family]?.fields[field.name] === null}
                      aria-label={`${family}.${field.name}: ${t('connections.mapping_absent')}`}
                      // Turning it off seeds the value the placeholder was
                      // showing: an empty input means the same as the switch,
                      // so leaving it empty would undo the switch on save.
                      onChange={(e) =>
                        setField(family, field.name, e.currentTarget.checked ? null : (field.default ?? ''))
                      }
                    />
                  </Table.Td>
                  <Table.Td>
                    {field.declared && (
                      <Group gap="xs" wrap="nowrap">
                        <Button size="compact-xs" variant="subtle" onClick={() => editField(family, field)}>
                          {t('common.btn_edit')}
                        </Button>
                        <Button
                          size="compact-xs"
                          variant="subtle"
                          color="red"
                          onClick={() => removeField(family, field.name)}
                        >
                          {t('common.btn_remove')}
                        </Button>
                      </Group>
                    )}
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
          <Group>
            <Button size="compact-sm" variant="light" onClick={() => addField(family)}>
              {t('connections.mapping_add_field')}
            </Button>
          </Group>
          <Text c="dimmed" size="sm">
            {t('connections.mapping_added_desc')}
          </Text>
          <Title order={5} mt="xs">
            {t('connections.class_overrides')}
          </Title>
          <Text c="dimmed" size="sm">
            <Trans i18nKey="connections.class_overrides_desc" components={{ code: <code /> }} />
          </Text>
          <JsonInput
            value={mappings[family]?.overrides ?? '{}'}
            aria-label={`${family}: ${t('connections.class_overrides')}`}
            onChange={(value) => setOverrides(family, value)}
            autosize
            minRows={3}
            formatOnBlur
            validationError={t('common.invalid_json')}
          />
          <Divider my="sm" />
        </Stack>
      ))}
      <Group>
        <Button onClick={save} loading={busy}>
          {t('common.btn_save')}
        </Button>
        <Button variant="subtle" color="red" onClick={reset}>
          {t('common.btn_reset_defaults')}
        </Button>
      </Group>
      <Modal
        opened={draft !== null}
        onClose={() => {
          setDraft(null);
          setDraftError(null);
        }}
        title={
          draft?.original
            ? t('connections.mapping_edit_title', { name: draft.original })
            : t('connections.mapping_new_title', { family: draft?.family })
        }
      >
        {draft && (
          <Stack>
            {draftError && <Alert color="red">{draftError}</Alert>}
            <TextInput
              label={t('connections.mapping_name')}
              description={t('connections.mapping_name_desc')}
              value={draft.name}
              onChange={(e) => setDraft({ ...draft, name: e.currentTarget.value })}
            />
            <Select
              label={t('connections.mapping_kind')}
              data={kinds.map((kind) => ({
                value: kind.name,
                label: t(`connections.mapping_kind_${kind.name}`, { defaultValue: kind.name }),
              }))}
              value={draft.spec.kind}
              allowDeselect={false}
              onChange={(value) => value && setKind(value)}
            />
            <Switch
              label={t('connections.mapping_multi')}
              description={<Trans i18nKey="connections.mapping_multi_desc" components={{ code: <code /> }} />}
              checked={draft.spec.multi}
              onChange={(e) => setDraft({ ...draft, spec: { ...draft.spec, multi: e.currentTarget.checked } })}
            />
            <Checkbox.Group
              label={t('connections.mapping_roles')}
              description={t('connections.mapping_roles_desc')}
              value={draft.spec.roles}
              onChange={(value) => setDraft({ ...draft, spec: { ...draft.spec, roles: value } })}
            >
              <Stack gap={4} mt="xs">
                {roles.map((role) => {
                  const blocker = roleBlocker(role);
                  return (
                    <Checkbox
                      key={role.name}
                      value={role.name}
                      disabled={blocker !== null}
                      label={
                        <>
                          <code>{role.name}</code>{' '}
                          <Text span size="xs" c="dimmed">
                            {blocker ?? t(`connections.mapping_role_${role.name}`, { defaultValue: '' })}
                          </Text>
                        </>
                      }
                    />
                  );
                })}
              </Stack>
            </Checkbox.Group>
            <TextInput
              label={t('connections.mapping_description')}
              description={t('connections.mapping_description_desc')}
              value={draft.spec.description}
              onChange={(e) => setDraft({ ...draft, spec: { ...draft.spec, description: e.currentTarget.value } })}
            />
            <TextInput
              label={t('connections.mapping_attribute')}
              description={t('connections.mapping_attribute_desc')}
              value={draft.source}
              onChange={(e) => setDraft({ ...draft, source: e.currentTarget.value })}
            />
            <Group justify="flex-end">
              <Button
                variant="subtle"
                onClick={() => {
                  setDraft(null);
                  setDraftError(null);
                }}
              >
                {t('common.btn_cancel')}
              </Button>
              <Button onClick={applyDraft}>{t('common.btn_apply')}</Button>
            </Group>
          </Stack>
        )}
      </Modal>
    </Stack>
  );
}
