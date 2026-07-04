import {
  Alert,
  Badge,
  Button,
  Code,
  Group,
  List,
  Loader,
  PasswordInput,
  SegmentedControl,
  Stack,
  Stepper,
  Table,
  Text,
  TextInput,
  Title,
} from '@mantine/core';
import { ReactNode, useEffect, useState } from 'react';
import { Trans, useTranslation } from 'react-i18next';
import { Link } from 'react-router-dom';

import { apiGet, apiSend, fetchSetupStatus, SetupStatus, setToken } from './api';

// GET /api/setup/{section} shape (same as in Connections): non-secret values
// plus is-set flags for secrets.
interface SectionData {
  values: Record<string, unknown>;
  secrets: Record<string, boolean>;
}

// crypto.getRandomValues works on plain http too, unlike crypto.randomUUID.
function generateToken(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(32));
  return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
}

// Step icons: inline SVG outlines from Tabler Icons (MIT) — the dependency
// budget has no room for an icon package because of four pictograms.
function StepIcon({ children }: { children: ReactNode }) {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      width={16}
      height={16}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      {children}
    </svg>
  );
}

const SECURITY_ICON = (
  // lock
  <StepIcon>
    <rect x="5" y="11" width="14" height="10" rx="2" />
    <circle cx="12" cy="16" r="1" />
    <path d="M8 11v-4a4 4 0 0 1 8 0v4" />
  </StepIcon>
);

const ITOP_ICON = (
  // plug
  <StepIcon>
    <path d="M9.785 6l8.215 8.215l-2.054 2.054a5.81 5.81 0 1 1 -8.215 -8.215l2.054 -2.054z" />
    <path d="M4 20l3.5 -3.5" />
    <path d="M15 4l-3.5 3.5" />
    <path d="M20 9l-3.5 3.5" />
  </StepIcon>
);

const WEBHOOKS_ICON = (
  // webhook
  <StepIcon>
    <path d="M4.876 13.61a4 4 0 1 0 6.124 3.39h6" />
    <path d="M15.066 20.502a4 4 0 1 0 1.934 -7.502c-.706 0 -1.424 .179 -2 .5l-3 -5.5" />
    <path d="M16 8a4 4 0 1 0 -8 0c0 1.506 .77 2.818 2 3.5l-3 5.5" />
  </StepIcon>
);

const LLM_ICON = (
  // sparkles
  <StepIcon>
    <path d="M16 18a2 2 0 0 1 2 2a2 2 0 0 1 2 -2a2 2 0 0 1 -2 -2a2 2 0 0 1 -2 2zm0 -12a2 2 0 0 1 2 2a2 2 0 0 1 2 -2a2 2 0 0 1 -2 -2a2 2 0 0 1 -2 2zm-7 12a6 6 0 0 1 6 -6a6 6 0 0 1 -6 -6a6 6 0 0 1 -6 6a6 6 0 0 1 6 6z" />
  </StepIcon>
);

export default function SetupWizard() {
  const { t } = useTranslation();
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
        <Title order={2}>{t('setup.title')}</Title>
        {status.configured ? (
          <Alert color="green">{t('setup.configured')}</Alert>
        ) : (
          <Alert color="orange">
            <Text fw={500}>{t('setup.incomplete')}</Text>
            <List size="sm" mt={4}>
              {status.missing.map((item) => (
                <List.Item key={item}>{item}</List.Item>
              ))}
            </List>
          </Alert>
        )}
        <Group>
          <Button onClick={() => setWizardActive(true)}>{t('setup.btn_run_wizard')}</Button>
          <Button variant="default" component={Link} to="/connections">
            {t('setup.btn_edit_direct')}
          </Button>
        </Group>
      </Stack>
    );
  }

  const finish = async () => {
    setStep(4);
    // Refresh so the final screen reflects what the wizard actually saved.
    try {
      setStatus(await fetchSetupStatus());
    } catch (e) {
      setError((e as Error).message);
    }
  };

  // Security comes first: the webhooks step needs a saved webhook token.
  return (
    <Stack maw={640}>
      <Title order={2}>{t('setup.wizard_title')}</Title>
      <Stepper active={step} onStepClick={setStep} allowNextStepsSelect={false} size="xs">
        <Stepper.Step
          label={t('setup.step_security_label')}
          description={t('setup.step_security_desc')}
          icon={SECURITY_ICON}
        >
          <SecurityStep onDone={() => setStep(1)} />
        </Stepper.Step>
        <Stepper.Step
          label={t('setup.step_itop_label')}
          description={t('setup.step_itop_desc')}
          icon={ITOP_ICON}
        >
          <ItopStep onBack={() => setStep(0)} onDone={() => setStep(2)} />
        </Stepper.Step>
        <Stepper.Step
          label={t('setup.step_webhooks_label')}
          description={t('setup.step_webhooks_desc')}
          icon={WEBHOOKS_ICON}
        >
          <WebhooksStep onBack={() => setStep(1)} onDone={() => setStep(3)} />
        </Stepper.Step>
        <Stepper.Step
          label={t('setup.step_llm_label')}
          description={t('setup.step_llm_desc')}
          icon={LLM_ICON}
        >
          <LlmStep onBack={() => setStep(2)} onDone={finish} />
        </Stepper.Step>
        <Stepper.Completed>
          <FinalStep status={status} />
        </Stepper.Completed>
      </Stepper>
    </Stack>
  );
}

function LlmStep({ onBack, onDone }: { onBack: () => void; onDone: () => void }) {
  const { t } = useTranslation();
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
      if (result.ok) setTestResult(t('common.llm_test_ok', { response: result.response }));
      else setError(result.error ?? t('common.error_llm_failed'));
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
        label={t('common.field_base_url')}
        description={t('setup.llm_base_url_desc')}
        placeholder="http://localhost:1234/v1"
        value={baseUrl}
        onChange={(e) => setBaseUrl(e.currentTarget.value)}
      />
      <TextInput
        label={t('common.field_model')}
        description={t('setup.llm_model_desc')}
        value={model}
        onChange={(e) => setModel(e.currentTarget.value)}
      />
      <PasswordInput
        label={t('common.field_api_key')}
        placeholder={keySet ? t('common.secret_is_set') : t('common.secret_not_set')}
        description={keySet ? undefined : t('setup.llm_api_key_desc')}
        value={apiKey}
        onChange={(e) => setApiKey(e.currentTarget.value)}
      />
      <Group>
        <Button variant="subtle" onClick={onBack}>
          {t('common.btn_back')}
        </Button>
        <Button variant="default" onClick={test} loading={busy}>
          {t('common.btn_test_llm')}
        </Button>
        <Button onClick={saveAndNext} loading={busy} disabled={!model}>
          {t('common.btn_save_continue')}
        </Button>
      </Group>
    </Stack>
  );
}

function ItopStep({ onBack, onDone }: { onBack: () => void; onDone: () => void }) {
  const { t } = useTranslation();
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
      if (result.ok)
        setTestResult(t('common.conn_test_ok', { account: result.ai_person }));
      else setError(result.error ?? t('common.error_conn_failed'));
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
        label={t('common.field_rest_api_url')}
        description={t('setup.itop_url_desc')}
        placeholder="http://itop.example.com/webservices/rest.php"
        value={url}
        onChange={(e) => setUrl(e.currentTarget.value)}
      />
      <Text size="sm" c="dimmed">
        <Trans i18nKey="setup.itop_account_note" components={{ code: <Code /> }} />
      </Text>
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
        <Button variant="subtle" onClick={onBack}>
          {t('common.btn_back')}
        </Button>
        <Button variant="default" onClick={test} loading={busy}>
          {t('common.btn_test_connection')}
        </Button>
        <Button onClick={saveAndNext} loading={busy}>
          {t('common.btn_save_continue')}
        </Button>
      </Group>
    </Stack>
  );
}

function SecurityStep({ onDone }: { onDone: () => void }) {
  const { t } = useTranslation();
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
        <Trans i18nKey="setup.security_copy_alert" components={{ code: <Code /> }} />
      </Alert>
      <WizardTokenField
        label={t('common.field_webhook_token')}
        description={t('setup.security_webhook_token_desc')}
        isSet={secrets.webhook_token}
        value={webhookToken}
        onChange={setWebhookToken}
      />
      <WizardTokenField
        label={t('common.field_admin_token')}
        description={t('setup.security_admin_token_desc')}
        isSet={secrets.admin_token}
        value={adminToken}
        onChange={setAdminToken}
      />
      <Group>
        <Button onClick={save} loading={busy}>
          {webhookToken || adminToken ? t('common.btn_save_continue') : t('common.btn_continue')}
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
  const { t } = useTranslation();
  return (
    <Stack gap={4}>
      <TextInput
        label={props.label}
        description={props.description}
        placeholder={props.isSet ? t('common.secret_is_set') : undefined}
        value={props.value}
        onChange={(e) => props.onChange(e.currentTarget.value)}
        styles={{ input: { fontFamily: 'monospace', fontSize: 13 } }}
      />
      <Group gap="xs">
        <Button size="compact-xs" variant="default" onClick={() => props.onChange(generateToken())}>
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
      </Group>
    </Stack>
  );
}

// POST /api/setup/provision-itop report line (same shape in Connections).
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

function WebhooksStep({ onBack, onDone }: { onBack: () => void; onDone: () => void }) {
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
    <Stack pt="md">
      {error && (
        <Alert color="red" style={{ whiteSpace: 'pre-wrap' }}>
          {error}
        </Alert>
      )}
      <Text size="sm" c="dimmed">
        <Trans i18nKey="setup.webhooks_desc" components={{ b: <b /> }} />
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
      {report && <ProvisionReport report={report} />}
      <Group>
        <Button variant="subtle" onClick={onBack}>
          {t('common.btn_back')}
        </Button>
        {report ? (
          <Button onClick={onDone}>{t('common.btn_continue')}</Button>
        ) : (
          <>
            <Button
              onClick={configure}
              loading={busy}
              disabled={!backendUrl || (auth === 'basic' ? !user || !pwd : !token)}
            >
              {t('common.btn_configure_itop')}
            </Button>
            <Button variant="default" onClick={onDone}>
              {t('common.btn_skip')}
            </Button>
          </>
        )}
      </Group>
    </Stack>
  );
}

function ProvisionReport({ report }: { report: ProvisionItem[] }) {
  return (
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
  );
}

function FinalStep({ status }: { status: SetupStatus }) {
  const { t } = useTranslation();
  return (
    <Stack pt="md">
      {status.configured ? (
        <Alert color="green">
          <Text fw={500}>{t('setup.final_complete')}</Text>
          <Text size="sm" mt={4}>
            <Trans
              i18nKey="setup.final_complete_detail"
              values={{ origin: window.location.origin }}
              components={{ code: <Code /> }}
            />
          </Text>
        </Alert>
      ) : (
        <Alert color="orange">
          <Text fw={500}>{t('setup.final_missing')}</Text>
          <List size="sm" mt={4}>
            {status.missing.map((item) => (
              <List.Item key={item}>{item}</List.Item>
            ))}
          </List>
        </Alert>
      )}
      <Group>
        <Button component={Link} to="/runs">
          {t('setup.btn_open_runs')}
        </Button>
        <Button variant="default" component={Link} to="/connections">
          {t('setup.btn_open_connections')}
        </Button>
      </Group>
    </Stack>
  );
}
